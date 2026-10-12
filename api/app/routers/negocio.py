"""Fase 7 · Administración interna de A.U.R.O.M. (solo la dueña) y cobros de la suscripción (Fase 8).

  /negocio/*                       solo `admin_plataforma` (RLS del esquema negocio lo refuerza).
  /empresas/{e}/suscripcion*       administrador de la empresa: solo su propia suscripción.
  /webhooks/tilopay[/retorno]      sin sesión; secreto propio + confirmación con la API de Tilopay.
  /cron/cobros, /cron/facturas     encabezado X-Cron-Secret.

`db.sistema()` (sin RLS) se usa únicamente en los webhooks y en las tareas programadas.
"""
from __future__ import annotations

import calendar
import hmac
import logging
import time
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Optional
from uuid import UUID

import psycopg
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import RedirectResponse, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from .. import config, crud, db
from ..auth import Contexto, admin_empresa, admin_plataforma
from ..fechas import hoy_cr
from ..servicios import alertas, cifrado, comisiones, facturacion, tilopay
from ..servicios.facturacion import _q

log = logging.getLogger("aurom.negocio")

r = APIRouter(prefix="/negocio", tags=["negocio"])
suscripcion = APIRouter(prefix="/empresas/{empresa_id}/suscripcion", tags=["suscripcion"])
webhooks = APIRouter(prefix="/webhooks", tags=["webhooks"])
cron = APIRouter(prefix="/cron", tags=["cron"])
routers = [r, suscripcion, webhooks, cron]

ESTADOS_COBRABLES = ("activo", "gracia", "solo_lectura", "suspendido")
CERO = Decimal(0)



def _mes(anio: int, mes: int) -> tuple[date, date]:
    ini = date(anio, mes, 1)
    return ini, date(anio + (mes == 12), mes % 12 + 1, 1)


def _primero(d: date) -> date:
    return d.replace(day=1)


def _sig_mes(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =============================================================================
#                         Cobro de la suscripción (núcleo)
# =============================================================================
def config_cobro(conn) -> dict:
    v = facturacion._cfg(conn, "cobro", {}) or {}
    return {"dias_gracia": int(v.get("dias_gracia", 7)), "reintentos": [int(x) for x in v.get("reintentos", [1, 3, 5])],
            "dias_suspension": int(v.get("dias_suspension", 30)),
            "desde": date.fromisoformat(v["desde"]) if v.get("desde") else None}


def fecha_cobro(cliente: dict, periodo: date) -> date:
    return periodo.replace(day=min(int(cliente["dia_cobro"]), calendar.monthrange(periodo.year, periodo.month)[1]))


TOLERANCIA_PAGO = Decimal("1.00")   # diferencias de redondeo de céntimos


def monto_periodo(conn, cliente_id, periodo: date) -> Decimal:
    """Lo que se debe del mes: la factura del periodo (menos notas de crédito) o, si aún no hay factura,
    el precio calculado."""
    fac = conn.execute(
        """select coalesce(sum(case when tipo = '03' then -total else total end), 0) as t, count(*)::int as n
             from negocio.comprobantes where cliente_id = %s and periodo = %s and concepto = 'suscripcion'
              and estado not in ('anulado', 'rechazado', 'error')""", (cliente_id, periodo)).fetchone()
    if fac["n"]:
        return Decimal(fac["t"])
    return Decimal(facturacion.precio_cliente(conn, cliente_id, periodo)["total"])


def periodo_pagado(conn, cliente_id, periodo: date) -> bool:
    """El mes está pagado cuando los abonos cubren lo que se debe (antes bastaba cualquier abono > 0)."""
    pagado = conn.execute("""select coalesce(sum(monto), 0) as s from negocio.pagos where cliente_id = %s
                               and concepto = 'mensualidad' and periodo = %s and monto > 0""",
                          (cliente_id, periodo)).fetchone()["s"]
    debe = monto_periodo(conn, cliente_id, periodo)
    if debe <= 0:
        return True     # ningún módulo activo ese mes: no hay nada que cobrar
    return Decimal(pagado) >= debe - TOLERANCIA_PAGO


def periodos_pendientes(conn, cliente: dict, hoy: date) -> list[date]:
    """Meses ya vencidos (fecha de cobro <= hoy) sin pago, del más antiguo al más reciente."""
    cfg = config_cobro(conn)
    p = _primero(max(cliente["alta"], cfg["desde"] or cliente["alta"]))
    res = []
    while p <= _primero(hoy):
        if fecha_cobro(cliente, p) <= hoy and not periodo_pagado(conn, cliente["id"], p):
            res.append(p)
        p = _sig_mes(p)
    return res


def estado_por_atraso(dias: int | None, cfg: dict) -> str:
    if dias is None or dias < 0:
        return "activo"
    if dias < cfg["dias_gracia"]:
        return "gracia"
    if dias < cfg["dias_suspension"]:
        return "solo_lectura"
    return "suspendido"


ETIQUETA_ESTADO = {"activo": "activo", "gracia": "en gracia (pago pendiente)", "solo_lectura": "en solo lectura",
                   "suspendido": "suspendido", "prueba": "en prueba", "baja": "dado de baja"}


def reevaluar_estado(conn, cliente_id, hoy: date | None = None) -> dict:
    """Pone el estado según la deuda: gracia -> solo_lectura -> suspendido; activo al ponerse al día."""
    hoy = hoy or hoy_cr()
    c = conn.execute("select * from negocio.clientes where id = %s for update", (cliente_id,)).fetchone()
    if not c or c["estado"] not in ESTADOS_COBRABLES:
        return {"cliente_id": cliente_id, "estado": c["estado"] if c else None, "cambio": False}
    cfg = config_cobro(conn)
    pend = periodos_pendientes(conn, c, hoy)
    dias = (hoy - fecha_cobro(c, pend[0])).days if pend else None
    nuevo = estado_por_atraso(dias, cfg)
    gracia_desde = fecha_cobro(c, pend[0]) if pend else None
    if nuevo != c["estado"] or gracia_desde != c["gracia_desde"]:
        conn.execute("update negocio.clientes set estado = %s, gracia_desde = %s where id = %s", (nuevo, gracia_desde, cliente_id))
    if nuevo != c["estado"]:
        sev = "info" if nuevo in ("activo", "gracia") else ("aviso" if nuevo == "solo_lectura" else "critica")
        texto = (f"{c['razon_social']} se puso al día: servicio reactivado." if nuevo == "activo"
                 else f"{c['razon_social']} pasó a {ETIQUETA_ESTADO[nuevo]} ({dias} días de atraso).")
        alertas.alertar(conn, "pago", texto, sev)
    return {"cliente_id": cliente_id, "estado": nuevo, "anterior": c["estado"], "cambio": nuevo != c["estado"],
            "dias_atraso": dias, "pendientes": pend}


def registrar_pago(conn, *, cliente_id, monto, medio: str, concepto: str = "mensualidad", periodo: date | None = None,
                   comprobante_id=None, subtotal=None, moneda: str = "CRC", tipo_cambio=1, comision=0,
                   retencion_iva=0, retencion_renta=0, referencia: str | None = None, cobro_id=None,
                   fecha: date | None = None, reevaluar: bool = True) -> dict:
    """Registra el pago, genera comisiones y reactiva el servicio si queda al día."""
    if comprobante_id:
        comp = conn.execute("select * from negocio.comprobantes where id = %s", (comprobante_id,)).fetchone()
        if not comp or comp["cliente_id"] != cliente_id:
            raise HTTPException(422, "El comprobante no es de ese cliente")
        periodo = periodo or comp["periodo"]
        concepto = comp["concepto"] if comp["concepto"] in ("mensualidad", "implementacion") else (
            "mensualidad" if comp["concepto"] == "suscripcion" else "otro")
        moneda, tipo_cambio = comp["moneda"], comp["tipo_cambio"]
        if subtotal is None:
            prop = Decimal(str(monto)) / Decimal(comp["total"]) if Decimal(comp["total"]) else Decimal(1)
            subtotal = _q(Decimal(comp["subtotal"]) * min(prop, Decimal(1)))
    if concepto == "mensualidad" and not periodo:
        raise HTTPException(422, "Indique el periodo (mes) que paga la mensualidad")
    if subtotal is None:
        subtotal = _q(Decimal(str(monto)) / Decimal("1.13"))
    try:
        with conn.transaction():
            pago = conn.execute(
                """insert into negocio.pagos (cliente_id, comprobante_id, fecha, monto, medio, comision, retencion_iva,
                                              retencion_renta, referencia, concepto, periodo, subtotal, moneda, tipo_cambio, cobro_id)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
                (cliente_id, comprobante_id, fecha or hoy_cr(), _q(monto), medio, _q(comision), _q(retencion_iva),
                 _q(retencion_renta), referencia, concepto, _primero(periodo) if periodo else None, _q(subtotal), moneda,
                 tipo_cambio, cobro_id)).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Ya existe un pago con esa referencia") from exc
    pago = dict(pago)
    pago["comisiones"] = comisiones.registrar_comisiones_por_pago(conn, pago["id"])
    if reevaluar:
        pago["estado_cliente"] = reevaluar_estado(conn, cliente_id)["estado"]
    return pago


def _metodo_activo(conn, cliente_id) -> dict | None:
    return conn.execute("select * from negocio.metodos_pago where cliente_id = %s and estado = 'activo' "
                        "order by creado_en desc limit 1", (cliente_id,)).fetchone()


def _tarifa_tarjeta(conn) -> Decimal:
    return Decimal(str(facturacion._cfg(conn, "tarifa_tarjeta_pct", "5")))


def aprobar_cobro(conn, cobro: dict, *, comision=None, retencion_iva=0, retencion_renta=0, referencia=None) -> dict:
    """Cobro aprobado: pago, factura (medio 02 tarjeta), envío a Hacienda, comisiones y reactivación."""
    if comision is None:
        comision = _q(Decimal(cobro["total"]) * _tarifa_tarjeta(conn) / 100)
    conn.execute("update negocio.cobros set resultado = 'aprobado', comision = %s where id = %s", (comision, cobro["id"]))
    try:
        comp = facturacion.crear_factura_suscripcion(conn, cobro["cliente_id"], cobro["periodo"], "02")
    except HTTPException as exc:
        comp = None
        alertas.alertar(conn, "factura", f"Cobro aprobado sin factura (cliente {cobro['cliente_id']}): {exc.detail}", "aviso")
    pago = registrar_pago(conn, cliente_id=cobro["cliente_id"], monto=cobro["total"], medio="tarjeta",
                          concepto="mensualidad", periodo=cobro["periodo"], comprobante_id=comp["id"] if comp else None,
                          subtotal=cobro["subtotal"], moneda=cobro["moneda"],
                          tipo_cambio=comp["tipo_cambio"] if comp else 1, comision=comision,
                          retencion_iva=retencion_iva, retencion_renta=retencion_renta,
                          referencia=referencia or cobro["orden_proveedor"], cobro_id=cobro["id"])
    conn.execute("update negocio.cobros set pago_id = %s, comprobante_id = %s where id = %s",
                 (pago["id"], comp["id"] if comp else None, cobro["id"]))
    cli = conn.execute("select razon_social from negocio.clientes where id = %s", (cobro["cliente_id"],)).fetchone()
    alertas.alertar(conn, "pago", f"Pago con tarjeta de {cli['razon_social']}: {cobro['moneda']} {Decimal(cobro['total']):,.2f} "
                                  f"({facturacion.nombre_mes(cobro['periodo'])}).", "info")
    if comp and comp["estado"] == "borrador":
        try:
            facturacion.emitir(conn, comp["id"])
        except HTTPException as exc:
            alertas.alertar(conn, "factura", f"No se emitió la factura de {cli['razon_social']}: {exc.detail}", "aviso")
    return pago


def cobrar_periodo(conn, cliente: dict, periodo: date, metodo: dict) -> dict:
    """Intenta cobrar con la tarjeta guardada la mensualidad de `periodo`."""
    precio = facturacion.precio_cliente(conn, cliente["id"], periodo)
    intento = conn.execute("select count(*)::int n from negocio.cobros where cliente_id = %s and periodo = %s",
                           (cliente["id"], periodo)).fetchone()["n"] + 1
    cobro = conn.execute(
        """insert into negocio.cobros (cliente_id, periodo, empleados_facturados, subtotal, iva, total, intento, moneda,
                                       descripcion, metodo_pago_id)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning *""",
        (cliente["id"], periodo, precio["empleados"], precio["subtotal"], precio["iva"], precio["total"], intento,
         precio["moneda"], f"Suscripción {facturacion.nombre_mes(periodo)}", metodo["id"])).fetchone()
    orden = f"AUR{cobro['id'].hex[:16].upper()}"
    conn.execute("update negocio.cobros set orden_proveedor = %s where id = %s", (orden, cobro["id"]))
    cobro = {**cobro, "orden_proveedor": orden}
    try:
        res = tilopay.cobrar_token(token_tarjeta=cifrado.descifrar(metodo["token"], str(metodo["cliente_id"])), correo=metodo["correo"] or cliente["correo_facturacion"],
                                   monto_total=precio["total"], moneda=precio["moneda"], orden=orden)
    except tilopay.ErrorTilopay as exc:
        conn.execute("update negocio.cobros set resultado = 'error', codigo_respuesta = %s where id = %s",
                     (str(exc)[:200], cobro["id"]))
        alertas.alertar(conn, "error", f"Cobro a {cliente['razon_social']} no se pudo intentar: {exc}", "aviso")
        return {**cobro, "resultado": "error", "detalle": str(exc)}
    if res["aprobado"]:
        conn.execute("update negocio.cobros set codigo_respuesta = %s where id = %s", (res["codigo"], cobro["id"]))
        pago = aprobar_cobro(conn, cobro, referencia=res["transaccion"] or orden)
        return {**cobro, "resultado": "aprobado", "pago_id": pago["id"]}
    conn.execute("update negocio.cobros set resultado = 'rechazado', codigo_respuesta = %s where id = %s",
                 (f"{res['codigo']} {res['descripcion'] or ''}"[:200], cobro["id"]))
    alertas.alertar(conn, "pago", f"Cobro RECHAZADO a {cliente['razon_social']} (intento {intento}, "
                                  f"{facturacion.nombre_mes(periodo)}): {res['descripcion'] or res['codigo']}", "aviso")
    return {**cobro, "resultado": "rechazado", "detalle": res["descripcion"]}


def procesar_cliente(conn, cliente: dict, hoy: date, *, forzar_intento: bool = False) -> dict:
    cfg = config_cobro(conn)
    agenda = [0] + cfg["reintentos"]
    metodo = _metodo_activo(conn, cliente["id"])
    intentos = []
    if metodo:
        for p in periodos_pendientes(conn, cliente, hoy):
            hechos = conn.execute("""select count(*)::int n, bool_or(creado_en::date = %s) hoy,
                                            bool_or(resultado = 'pendiente') pendiente
                                       from negocio.cobros where cliente_id = %s and periodo = %s""",
                                  (hoy, cliente["id"], p)).fetchone()
            if hechos["hoy"] or hechos["pendiente"]:
                continue
            dias = (hoy - fecha_cobro(cliente, p)).days
            toca = hechos["n"] < len(agenda) and dias >= agenda[hechos["n"]]
            if toca or forzar_intento:
                res = cobrar_periodo(conn, cliente, p, metodo)
                intentos.append({"periodo": p, "resultado": res["resultado"], "intento": res["intento"]})
                if res["resultado"] != "aprobado":
                    break
    est = reevaluar_estado(conn, cliente["id"], hoy)
    return {"cliente_id": cliente["id"], "razon_social": cliente["razon_social"], "intentos": intentos, **est}


def procesar_cobros(conn, hoy: date) -> dict:
    res = {"fecha": hoy, "clientes": [], "errores": []}
    for c in conn.execute("select * from negocio.clientes where estado = any(%s) order by razon_social",
                          (list(ESTADOS_COBRABLES),)).fetchall():
        try:
            with conn.transaction():
                res["clientes"].append(procesar_cliente(conn, c, hoy))
        except Exception as exc:  # noqa: BLE001 - un cliente con error no detiene a los demás
            log.exception("Cobro de %s falló", c["razon_social"])
            res["errores"].append(f"{c['razon_social']}: {getattr(exc, 'detail', exc)}")
    res["resumen"] = {e: sum(1 for x in res["clientes"] if x["estado"] == e) for e in ESTADOS_COBRABLES}
    res["aprobados"] = sum(1 for x in res["clientes"] for i in x["intentos"] if i["resultado"] == "aprobado")
    res["rechazados"] = sum(1 for x in res["clientes"] for i in x["intentos"] if i["resultado"] != "aprobado")
    return res


def procesar_facturas(conn, hoy: date) -> dict:
    """Borradores del mes (clientes sin tarjeta), emisión de pendientes y consulta de estados."""
    res: dict[str, Any] = {"fecha": hoy, "creadas": 0, "emitidas": 0, "consultadas": 0, "aceptadas": 0,
                           "rechazadas": 0, "errores": []}
    periodo = _primero(hoy)

    def paso(nombre, fn, *a):
        try:
            with conn.transaction():
                return fn(*a)
        except Exception as exc:  # noqa: BLE001
            res["errores"].append(f"{nombre}: {getattr(exc, 'detail', exc)}")
            return None

    for c in conn.execute("""select c.* from negocio.clientes c where c.estado in ('activo','gracia','solo_lectura')
                               and not exists (select 1 from negocio.metodos_pago m where m.cliente_id = c.id and m.estado = 'activo')""",
                          ).fetchall():
        if fecha_cobro(c, periodo) > hoy:
            continue
        ya = conn.execute("""select 1 from negocio.comprobantes where cliente_id = %s and periodo = %s and concepto = 'suscripcion'
                               and tipo in ('01','04') and estado <> 'anulado'""", (c["id"], periodo)).fetchone()
        if not ya and paso(c["razon_social"], facturacion.crear_factura_suscripcion, conn, c["id"], periodo, "04"):
            res["creadas"] += 1
    for comp in conn.execute("""select id, consecutivo from negocio.comprobantes
                                 where estado = 'borrador' or (estado in ('error','firmado') and intentos_envio < 5)
                                 order by creado_en""").fetchall():
        e = paso(f"emitir {comp['id']}", facturacion.emitir, conn, comp["id"])
        if e and e["estado"] == "enviado":
            res["emitidas"] += 1
    for comp in conn.execute("select id from negocio.comprobantes where estado = 'enviado' order by fecha").fetchall():
        e = paso(f"consultar {comp['id']}", facturacion.consultar, conn, comp["id"])
        if e:
            res["consultadas"] += 1
            res["aceptadas"] += e["estado"] == "aceptado"
            res["rechazadas"] += e["estado"] == "rechazado"
    if res["errores"]:
        alertas.alertar(conn, "factura", f"Facturación automática con {len(res['errores'])} problema(s): "
                                         + "; ".join(res["errores"][:3]), "aviso")
    return res


# =============================================================================
#                                  Cron
# =============================================================================
def cron_autorizado(x_cron_secret: str = Header(default="")) -> None:
    s = config.settings.cron_secret
    if not s or not hmac.compare_digest(s.encode(), (x_cron_secret or "").encode()):
        raise HTTPException(401, "Secreto de tarea programada inválido")


@cron.post("/cobros", dependencies=[Depends(cron_autorizado)], summary="Cobros con tarjeta y estados de servicio (diario)")
def cron_cobros(fecha: date | None = Query(None, description="Solo para pruebas o reprocesos")) -> dict:
    with db.sistema() as conn:
        return procesar_cobros(conn, fecha or hoy_cr())


@cron.post("/facturas", dependencies=[Depends(cron_autorizado)],
           summary="Genera, emite y consulta comprobantes en Hacienda (diario o cada hora)")
def cron_facturas(fecha: date | None = Query(None)) -> dict:
    with db.sistema() as conn:
        return procesar_facturas(conn, fecha or hoy_cr())


# =============================================================================
#                                Webhooks
# =============================================================================
def _evento(conn, tipo: str, contenido: dict, valida: bool, error: str | None = None) -> dict:
    import json as _json
    return conn.execute("""insert into negocio.eventos_webhook (origen, tipo, contenido, firma_valida, error)
                           values ('tilopay', %s, %s::jsonb, %s, %s) returning *""",
                        (tipo, _json.dumps(contenido, default=str), valida, error)).fetchone()


@webhooks.post("/tilopay", summary="Notificación de Tilopay (se confirma con la API antes de aplicar)")
async def webhook_tilopay(request: Request, secreto: str | None = Query(None),
                          x_webhook_secret: str | None = Header(default=None)) -> dict:
    try:
        cuerpo = await request.json()
    except Exception:  # noqa: BLE001
        cuerpo = dict(await request.form()) if request.headers.get("content-type", "").startswith(
            ("application/x-www-form-urlencoded", "multipart/")) else {}
    if not isinstance(cuerpo, dict):
        cuerpo = {"datos": cuerpo}
    valida = tilopay.secreto_webhook_valido(x_webhook_secret or secreto)
    cuerpo.pop("card", None)  # por si viniera un token: no se guarda en la bitácora
    with db.sistema() as conn:
        ev = _evento(conn, str(cuerpo.get("type") or cuerpo.get("event") or "pago"), cuerpo, valida)
        if not valida:
            conn.execute("update negocio.eventos_webhook set error = 'secreto inválido' where id = %s", (ev["id"],))
    if not valida:
        raise HTTPException(401, "Firma del webhook inválida")
    orden = str(cuerpo.get("orderNumber") or cuerpo.get("order") or cuerpo.get("order_id") or "")
    resultado = {"ok": True, "evento_id": ev["id"], "aplicado": False}
    with db.sistema() as conn:
        try:
            cobro = conn.execute("select * from negocio.cobros where orden_proveedor = %s for update", (orden,)).fetchone() if orden else None
            if cobro and cobro["resultado"] != "aprobado":
                conf = tilopay.consultar_orden(orden)
                if conf and conf["aprobado"]:
                    aprobar_cobro(conn, cobro, comision=conf["comision"] or None, retencion_iva=conf["retencion_iva"],
                                  retencion_renta=conf["retencion_renta"], referencia=conf["transaccion"] or orden)
                    resultado["aplicado"] = True
            conn.execute("update negocio.eventos_webhook set procesado_en = now() where id = %s", (ev["id"],))
        except Exception as exc:  # noqa: BLE001 - se responde 200 para que Tilopay no reintente en bucle
            log.exception("Webhook Tilopay")
            conn.rollback()
            conn.execute("update negocio.eventos_webhook set error = %s where id = %s", (str(exc)[:500], ev["id"]))
            resultado["error"] = "registrado"
    return resultado


@webhooks.get("/tilopay/retorno", include_in_schema=True,
              summary="Retorno del formulario de Tilopay al registrar la tarjeta (redirige al portal)")
def retorno_tilopay(request: Request, estado: str = Query(...)) -> RedirectResponse:
    params = dict(request.query_params)
    datos = tilopay.leer_estado(estado)
    if not datos:
        raise HTTPException(400, "Enlace de registro vencido o inválido")
    empresa_id, cliente_id = datos["e"], datos["c"]
    tarjeta = tilopay.datos_tarjeta(params)
    destino = f"{config.settings.app_url}/e/{empresa_id}/suscripcion"
    seguro = {k: v for k, v in params.items() if k not in tilopay.PARAMS_TOKEN}
    with db.sistema() as conn:
        aprobado = str(params.get("code", "1")) == tilopay.CODIGO_APROBADO
        if not tarjeta["token"] or not aprobado:
            _evento(conn, "tokenizacion", seguro, True, "sin token en el retorno" if aprobado else "registro no aprobado")
            return RedirectResponse(f"{destino}?tarjeta=error", status_code=303)
        cli = conn.execute("select * from negocio.clientes where id = %s and empresa_id = %s", (cliente_id, empresa_id)).fetchone()
        if not cli:
            raise HTTPException(404, "Cliente no encontrado")
        for viejo in conn.execute("select * from negocio.metodos_pago where cliente_id = %s and estado = 'activo'",
                                  (cliente_id,)).fetchall():
            conn.execute("update negocio.metodos_pago set estado = 'eliminado' where id = %s", (viejo["id"],))
            try:
                tilopay.eliminar_tarjeta(token_tarjeta=cifrado.descifrar(viejo["token"], str(cliente_id)), correo=viejo["correo"] or cli["correo_facturacion"])
            except tilopay.ErrorTilopay:
                pass
        conn.execute("""insert into negocio.metodos_pago (cliente_id, token, ultimos4, marca, vence, correo)
                        values (%s, %s, %s, %s, %s, %s)""",
                     (cliente_id, cifrado.cifrar(tarjeta["token"], str(cliente_id)), tarjeta["ultimos4"], tarjeta["marca"], tarjeta["vence"],
                      datos.get("m") or cli["correo_facturacion"]))
        _evento(conn, "tokenizacion", seguro, True)
        alertas.alertar(conn, "pago", f"{cli['razon_social']} registró una tarjeta terminada en {tarjeta['ultimos4'] or '????'}.",
                        "info", telegram_ahora=False)
        # Si debía meses, se intenta cobrar de inmediato con la tarjeta nueva.
        if cli["estado"] in ESTADOS_COBRABLES and periodos_pendientes(conn, cli, hoy_cr()):
            try:
                with conn.transaction():
                    procesar_cliente(conn, cli, hoy_cr(), forzar_intento=True)
            except Exception:  # noqa: BLE001
                log.exception("Cobro inmediato tras registrar tarjeta")
    return RedirectResponse(f"{destino}?tarjeta=ok", status_code=303)


# =============================================================================
#                    Suscripción vista por el cliente (Fase 8)
# =============================================================================
MENSAJE_ESTADO = {
    "prueba": "Su empresa está en periodo de prueba.",
    "activo": "Su suscripción está al día.",
    "gracia": "Tenemos un pago pendiente. Puede seguir trabajando normalmente durante el periodo de gracia.",
    "solo_lectura": "Por falta de pago la empresa quedó en solo lectura. Al pagar se reactiva automáticamente.",
    "suspendido": "El servicio está suspendido por falta de pago. Al pagar se reactiva automáticamente.",
    "baja": "La suscripción fue dada de baja.",
}


def _datos_suscripcion(conn, empresa_id) -> dict:
    emp = conn.execute("select id, nombre, plan, estado_servicio from public.empresas where id = %s", (empresa_id,)).fetchone()
    cli = conn.execute("select * from negocio.clientes where empresa_id = %s", (empresa_id,)).fetchone()
    hoy = hoy_cr()
    p = facturacion.precio_empresa(conn, cli, empresa_id, hoy)
    metodo = conn.execute("select * from negocio.metodo_pago_empresa(%s)", (empresa_id,)).fetchone()
    cobros = conn.execute("""select id, periodo, empleados_facturados, subtotal, iva, total, moneda, intento, resultado,
                                    creado_en from negocio.cobros where cliente_id = %s order by creado_en desc limit 24""",
                          (cli["id"],)).fetchall() if cli else []
    comps = conn.execute("""select id, tipo, clave, consecutivo, fecha, periodo, moneda, subtotal, impuesto, total, estado, concepto
                              from negocio.comprobantes where cliente_id = %s and estado in ('enviado','aceptado','anulado')
                             order by fecha desc limit 24""", (cli["id"],)).fetchall() if cli else []
    estado = emp["estado_servicio"]
    return {
        "empresa_id": emp["id"], "empresa": emp["nombre"], "plan": emp["plan"], "estado": estado,
        "mensaje": MENSAJE_ESTADO.get(estado, ""),
        "cliente": {k: cli[k] for k in ("razon_social", "identificacion", "correo_facturacion", "dia_cobro", "alta",
                                         "moneda", "precio_fundador_hasta", "gracia_desde")} if cli else None,
        "precio": {"periodo": _primero(hoy), "empleados": p["empleados"], "empleados_incluidos": p.get("empleados_incluidos"),
                   "bloques_adicionales": p["bloques_adicionales"], "subtotal": p["subtotal"], "iva": p["iva"],
                   "total": p["total"], "moneda": p["moneda"], "precio_fundador": p["precio_fundador"],
                   "modulos": p["modulos"]},
        "proximo_cobro": fecha_cobro(cli, _sig_mes(hoy)) if cli else None,
        "metodo_pago": dict(metodo) if metodo else None,
        "cobros": cobros, "comprobantes": comps,
    }


@suscripcion.get("", summary="Plan, precio, estado, método de pago, cobros y comprobantes de mi empresa")
def ver_suscripcion(ctx: Contexto = Depends(admin_empresa)) -> dict:
    return _datos_suscripcion(ctx.conn, ctx.empresa_id)


@suscripcion.post("/metodo-pago", summary="URL del formulario seguro de Tilopay para registrar la tarjeta")
def metodo_pago(ctx: Contexto = Depends(admin_empresa)) -> dict:
    if not tilopay.configurado():
        raise HTTPException(409, "El pago con tarjeta aún no está disponible. Escríbanos para pagar por SINPE o transferencia.")
    try:
        with ctx.conn.transaction():
            cid = ctx.conn.execute("select negocio.alta_cliente_empresa(%s, %s) id",
                                   (ctx.empresa_id, ctx.usuario.email)).fetchone()["id"]
    except psycopg.errors.RaiseException as exc:
        raise HTTPException(409, f"No se pudo crear la ficha de facturación: {exc.diag.message_primary}. "
                                 "Registre la cédula jurídica de la empresa.") from exc
    cli = ctx.conn.execute("select * from negocio.clientes where id = %s", (cid,)).fetchone()
    estado = tilopay.firmar_estado({"c": str(cid), "e": str(ctx.empresa_id), "m": cli["correo_facturacion"], "t": int(time.time())})
    redirect = f"{config.settings.api_url.rstrip('/')}/webhooks/tilopay/retorno?estado={estado}"
    nombre = (cli["razon_social"] or "Cliente").split(" ", 1)
    try:
        url = tilopay.url_tokenizacion(correo=cli["correo_facturacion"], nombre=nombre[0],
                                       apellido=nombre[1] if len(nombre) > 1 else "", redirect=redirect)
    except tilopay.ErrorTilopay as exc:
        raise HTTPException(502, f"Tilopay no respondió: {exc}") from exc
    return {"url": url}


def _comprobante_cliente(ctx: Contexto, id: UUID) -> dict:
    c = ctx.conn.execute("""select co.id from negocio.comprobantes co join negocio.clientes cl on cl.id = co.cliente_id
                             where co.id = %s and cl.empresa_id = %s and co.estado in ('enviado','aceptado','anulado')""",
                         (id, ctx.empresa_id)).fetchone()
    if not c:
        raise HTTPException(404, "Comprobante no encontrado")
    return c


@suscripcion.get("/comprobantes/{id}/pdf", response_class=Response, summary="PDF de un comprobante propio")
def pdf_cliente(id: UUID, ctx: Contexto = Depends(admin_empresa)) -> Response:
    _comprobante_cliente(ctx, id)
    return Response(facturacion.pdf_comprobante(ctx.conn, id), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="comprobante-{id}.pdf"'})


@suscripcion.get("/comprobantes/{id}/xml", response_class=Response, summary="XML firmado de un comprobante propio")
def xml_cliente(id: UUID, ctx: Contexto = Depends(admin_empresa)) -> Response:
    _comprobante_cliente(ctx, id)
    x = ctx.conn.execute("select clave, xml_firmado from negocio.comprobantes where id = %s", (id,)).fetchone()
    return Response(x["xml_firmado"] or "", media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{x["clave"]}.xml"'})


# =============================================================================
#                          /negocio (solo la dueña)
# =============================================================================
TipoId = Literal["01", "02", "03", "04"]


class PrecioModuloIn(_Base):
    """Precio propio de un módulo para un cliente (lo que no se indica sale del precio de lista)."""
    precio_mensual: Optional[Decimal] = Field(None, ge=0, max_digits=14, decimal_places=2)
    precio_mensual_usd: Optional[Decimal] = Field(None, ge=0, max_digits=14, decimal_places=2)
    incluidos: Optional[int] = Field(None, ge=0)
    tam_bloque: Optional[int] = Field(None, ge=1)
    precio_bloque: Optional[Decimal] = Field(None, ge=0, max_digits=14, decimal_places=2)
    precio_bloque_usd: Optional[Decimal] = Field(None, ge=0, max_digits=14, decimal_places=2)


class ClienteIn(_Base):
    empresa_id: Optional[UUID] = None
    razon_social: str = Field(min_length=3, max_length=100)
    tipo_identificacion: TipoId = "02"
    identificacion: str = Field(min_length=9, max_length=20)
    correo_facturacion: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=160)
    telefono: Optional[str] = None
    actividad_receptor: Optional[str] = Field(None, max_length=6)
    precio_mensual: Decimal = Decimal("55000")
    moneda: Literal["CRC", "USD"] = "CRC"
    precio_fundador_hasta: Optional[date] = None
    empleados_incluidos: int = Field(50, ge=0)
    precio_bloque_10: Decimal = Decimal("11000")
    implementacion: Decimal = Decimal("100000")
    dia_cobro: int = Field(1, ge=1, le=28)
    estado: Literal["prueba", "activo", "gracia", "solo_lectura", "suspendido", "baja"] = "activo"
    alta: Optional[date] = None
    notas: Optional[str] = None
    # Planilla usa los campos de arriba; los demás módulos, este (p. ej. {"contabilidad": {...}}).
    precios_modulos: Optional[dict[Literal["contabilidad"], PrecioModuloIn]] = None


ClienteUpd = crud.parcial(ClienteIn)


def _json_precios(d: dict) -> dict:
    if d.get("precios_modulos") is not None:
        d["precios_modulos"] = Jsonb({k: {c: str(v) if isinstance(v, Decimal) else v for c, v in
                                          (x if isinstance(x, dict) else x.model_dump()).items() if v is not None}
                                      for k, x in d["precios_modulos"].items()})
    elif "precios_modulos" in d:
        d["precios_modulos"] = Jsonb({})
    return d


def _alinear_inicio_planilla(conn, cliente: dict) -> None:
    """Si el cliente paga desde antes de que la empresa existiera en el sistema (clientes de Odoo),
    Planilla se cobra desde su fecha de alta (ver migración 0014)."""
    if cliente.get("empresa_id") and cliente.get("alta"):
        conn.execute("select public.alinear_inicio_planilla(%s, %s)", (cliente["empresa_id"], cliente["alta"]))


def _cliente_con_precio(conn, c: dict) -> dict:
    c = dict(c)
    try:
        p = facturacion.precio_cliente(conn, c["id"], hoy_cr())
        c["precio_actual"] = {k: p[k] for k in ("empleados", "bloques_adicionales", "subtotal", "iva", "total", "moneda",
                                                "precio_fundador", "modulos")}
    except HTTPException:
        c["precio_actual"] = None
    c["metodo_pago"] = conn.execute("select marca, ultimos4, vence from negocio.metodos_pago where cliente_id = %s "
                                    "and estado = 'activo'", (c["id"],)).fetchone()
    return c


@r.get("/clientes", summary="Clientes (empresas que pagan la suscripción)")
def listar_clientes(conn=Depends(admin_plataforma), estado: str | None = None, buscar: str | None = None) -> list[dict]:
    filas = conn.execute("""select * from negocio.clientes
                             where (%(e)s::text is null or estado = %(e)s)
                               and (%(b)s::text is null or razon_social ilike '%%' || %(b)s || '%%' or identificacion like %(b)s || '%%')
                             order by razon_social""", {"e": estado, "b": buscar}).fetchall()
    return [_cliente_con_precio(conn, c) for c in filas]


@r.post("/clientes", status_code=201)
def crear_cliente(datos: ClienteIn, conn=Depends(admin_plataforma)) -> dict:
    d = _json_precios(datos.model_dump(exclude_none=True))
    d["identificacion"] = facturacion._digitos(d["identificacion"])
    # Las empresas registradas ya tienen ficha automática (migración 0016): se completa con estos datos.
    existente = conn.execute("select id from negocio.clientes where empresa_id = %s", (d["empresa_id"],)).fetchone() \
        if d.get("empresa_id") else None
    if existente:
        c = crud.actualizar(conn, "negocio.clientes", existente["id"], {k: v for k, v in d.items() if k != "empresa_id"})
        _alinear_inicio_planilla(conn, c)
        return _cliente_con_precio(conn, c)
    try:
        with conn.transaction():
            c = crud.insertar(conn, "negocio.clientes", d)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Esa empresa ya tiene ficha de cliente") from exc
    _alinear_inicio_planilla(conn, c)
    return _cliente_con_precio(conn, c)


@r.get("/clientes/{id}")
def ver_cliente(id: UUID, conn=Depends(admin_plataforma)) -> dict:
    c = _cliente_con_precio(conn, crud.obtener(conn, "negocio.clientes", id))
    c["cobros"] = conn.execute("select * from negocio.cobros where cliente_id = %s order by creado_en desc limit 36", (id,)).fetchall()
    c["pagos"] = conn.execute("select * from negocio.pagos where cliente_id = %s order by fecha desc limit 36", (id,)).fetchall()
    c["comprobantes"] = [facturacion.resumen_comprobante(x) for x in conn.execute(
        "select * from negocio.comprobantes where cliente_id = %s order by fecha desc limit 36", (id,)).fetchall()]
    c["pendientes"] = periodos_pendientes(conn, c, hoy_cr()) if c["estado"] in ESTADOS_COBRABLES else []
    return c


@r.patch("/clientes/{id}")
def editar_cliente(id: UUID, datos: ClienteUpd, conn=Depends(admin_plataforma)) -> dict:  # type: ignore[valid-type]
    d = _json_precios(datos.model_dump(exclude_unset=True))
    if "identificacion" in d and d["identificacion"]:
        d["identificacion"] = facturacion._digitos(d["identificacion"])
    if d.get("estado") == "baja":
        d.setdefault("baja", hoy_cr())
    antes = conn.execute("select estado, alta from negocio.clientes where id = %s", (id,)).fetchone()
    if not antes:
        raise HTTPException(404, "Cliente no encontrado")
    # Al salir de «prueba» el cobro empieza este mes (si no se indicó otra fecha de alta).
    if (antes["estado"] == "prueba" and d.get("estado") in ESTADOS_COBRABLES and "alta" not in d
            and antes["alta"] < hoy_cr().replace(day=1)):
        d["alta"] = hoy_cr()
    c = crud.actualizar(conn, "negocio.clientes", id, d)
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    if "estado" in d and c.get("empresa_id"):
        # Misma regla en las dos pantallas: el estado del cliente es el estado del servicio de su empresa.
        conn.execute("update public.empresas set estado_servicio = %s where id = %s", (c["estado"], c["empresa_id"]))
    _alinear_inicio_planilla(conn, c)
    return _cliente_con_precio(conn, c)


@r.delete("/clientes/{id}", summary="Dar de baja (no borra: queda el historial fiscal)")
def baja_cliente(id: UUID, conn=Depends(admin_plataforma)) -> dict:
    c = conn.execute("update negocio.clientes set estado = 'baja', baja = %s where id = %s returning *", (hoy_cr(), id)).fetchone()
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    return c


@r.get("/clientes/{id}/precio", summary="Precio de un mes según colaboradores activos")
def precio(id: UUID, periodo: date | None = None, conn=Depends(admin_plataforma)) -> dict:
    return facturacion.precio_cliente(conn, id, periodo or hoy_cr())


@r.get("/cobros", summary="Intentos de cobro con tarjeta")
def listar_cobros(conn=Depends(admin_plataforma), cliente_id: UUID | None = None, resultado: str | None = None) -> list[dict]:
    return conn.execute("""select co.*, c.razon_social from negocio.cobros co join negocio.clientes c on c.id = co.cliente_id
                            where (%(c)s::uuid is null or co.cliente_id = %(c)s) and (%(r)s::text is null or co.resultado = %(r)s)
                            order by co.creado_en desc limit 500""", {"c": cliente_id, "r": resultado}).fetchall()


# ------------------------------------------------------------ Comprobantes --
@r.post("/facturas/generar", summary="Borradores de factura del mes según colaboradores activos")
def generar_facturas(periodo: date = Query(..., description="YYYY-MM-01"), incluir_tarjeta: bool = False,
                     conn=Depends(admin_plataforma)) -> dict:
    periodo = _primero(periodo)
    facturacion._cabys(conn, "cabys_suscripcion")
    creadas, existentes, errores = [], 0, []
    for c in conn.execute("select * from negocio.clientes where estado = any(%s) order by razon_social",
                          (list(ESTADOS_COBRABLES),)).fetchall():
        if not incluir_tarjeta and _metodo_activo(conn, c["id"]):
            continue
        ya = conn.execute("""select 1 from negocio.comprobantes where cliente_id = %s and periodo = %s and concepto = 'suscripcion'
                               and tipo in ('01','04') and estado <> 'anulado'""", (c["id"], periodo)).fetchone()
        if ya:
            existentes += 1
            continue
        try:
            with conn.transaction():
                comp = facturacion.crear_factura_suscripcion(conn, c["id"], periodo, "04")
            creadas.append({**facturacion.resumen_comprobante(comp), "razon_social": c["razon_social"],
                            "empleados": comp["empleados_facturados"]})
        except HTTPException as exc:
            errores.append({"cliente_id": c["id"], "razon_social": c["razon_social"], "mensaje": exc.detail})
    return {"periodo": periodo, "creadas": creadas, "existentes": existentes, "errores": errores}


@r.post("/facturas/implementacion/{cliente_id}", status_code=201, summary="Borrador de la factura de implementación")
def factura_implementacion(cliente_id: UUID, conn=Depends(admin_plataforma)) -> dict:
    return facturacion.crear_factura_implementacion(conn, cliente_id)


@r.get("/comprobantes")
def listar_comprobantes(conn=Depends(admin_plataforma), estado: str | None = None, desde: date | None = None,
                        hasta: date | None = None, cliente_id: UUID | None = None, tipo: str | None = None) -> list[dict]:
    filas = conn.execute(
        """select co.*, c.razon_social from negocio.comprobantes co left join negocio.clientes c on c.id = co.cliente_id
            where (%(e)s::text is null or co.estado = %(e)s)
              and (%(d)s::date is null or (co.fecha at time zone 'America/Costa_Rica')::date >= %(d)s)
              and (%(h)s::date is null or (co.fecha at time zone 'America/Costa_Rica')::date <= %(h)s)
              and (%(c)s::uuid is null or co.cliente_id = %(c)s) and (%(t)s::text is null or co.tipo = %(t)s)
            order by co.fecha desc limit 1000""", {"e": estado, "d": desde, "h": hasta, "c": cliente_id, "t": tipo}).fetchall()
    return [{**facturacion.resumen_comprobante(f), "razon_social": f["razon_social"]} for f in filas]


@r.get("/comprobantes/{id}")
def ver_comprobante(id: UUID, conn=Depends(admin_plataforma)) -> dict:
    c = facturacion.detalle_comprobante(conn, id)
    c.pop("xml_firmado", None)
    c["pagos"] = conn.execute("select * from negocio.pagos where comprobante_id = %s order by fecha", (id,)).fetchall()
    c["respuesta_hacienda"] = c.get("respuesta_hacienda")
    return c


@r.post("/comprobantes/{id}/emitir", summary="Clave, consecutivo, XML 4.4, firma, envío a Hacienda")
def emitir(id: UUID, conn=Depends(admin_plataforma)) -> dict:
    return facturacion.emitir(conn, id)


@r.post("/comprobantes/{id}/consultar", summary="Estado en Hacienda (y correo al cliente si quedó aceptado)")
def consultar(id: UUID, conn=Depends(admin_plataforma)) -> dict:
    return facturacion.consultar(conn, id)


@r.post("/comprobantes/{id}/reenviar", summary="Reenvía al cliente el PDF y el XML")
def reenviar(id: UUID, conn=Depends(admin_plataforma)) -> dict:
    try:
        return {"id_mensaje": facturacion.enviar_correo(conn, id)}
    except facturacion.correo.CorreoNoConfigurado as exc:
        raise HTTPException(409, str(exc)) from exc


@r.post("/comprobantes/{id}/anular-borrador", summary="Anula un borrador que no se va a emitir")
def anular_borrador(id: UUID, conn=Depends(admin_plataforma)) -> dict:
    c = conn.execute("update negocio.comprobantes set estado = 'anulado' where id = %s and estado = 'borrador' and clave is null "
                     "returning *", (id,)).fetchone()
    if not c:
        raise HTTPException(409, "Solo se anulan borradores sin clave; para lo emitido use nota de crédito")
    return facturacion.resumen_comprobante(c)


@r.get("/comprobantes/{id}/xml", response_class=Response)
def xml(id: UUID, respuesta: bool = False, conn=Depends(admin_plataforma)) -> Response:
    c = crud.obtener(conn, "negocio.comprobantes", id)
    contenido = c["xml_respuesta"] if respuesta else c["xml_firmado"]
    if not contenido:
        raise HTTPException(404, "El comprobante aún no tiene XML" if not respuesta else "Aún no hay respuesta de Hacienda")
    return Response(contenido, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{c["clave"]}{"_respuesta" if respuesta else ""}.xml"'})


@r.get("/comprobantes/{id}/pdf", response_class=Response)
def pdf_comprobante(id: UUID, conn=Depends(admin_plataforma)) -> Response:
    return Response(facturacion.pdf_comprobante(conn, id), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="comprobante-{id}.pdf"'})


class NotaCreditoIn(_Base):
    razon: str = Field(min_length=5, max_length=180)
    monto: Optional[Decimal] = Field(None, gt=0, description="Sin IVA. Vacío = anulación total")
    emitir: bool = False


@r.post("/comprobantes/{id}/nota-credito", status_code=201)
def nota_credito(id: UUID, datos: NotaCreditoIn, conn=Depends(admin_plataforma)) -> dict:
    nc = facturacion.crear_nota_credito(conn, id, datos.razon, datos.monto)
    if datos.emitir:
        return {**nc, **facturacion.emitir(conn, nc["id"])}
    return nc


# ------------------------------------------------------------------- Pagos --
class PagoIn(_Base):
    cliente_id: UUID
    comprobante_id: Optional[UUID] = None
    fecha: Optional[date] = None
    monto: Decimal = Field(gt=0, description="Total recibido (con IVA)")
    medio: Literal["sinpe", "transferencia", "tarjeta", "efectivo"]
    concepto: Literal["mensualidad", "implementacion", "otro"] = "mensualidad"
    periodo: Optional[date] = None
    subtotal: Optional[Decimal] = None
    referencia: Optional[str] = None
    comision: Decimal = CERO
    retencion_iva: Decimal = CERO
    retencion_renta: Decimal = CERO


@r.get("/pagos")
def listar_pagos(conn=Depends(admin_plataforma), cliente_id: UUID | None = None, desde: date | None = None,
                 hasta: date | None = None) -> list[dict]:
    return conn.execute("""select p.*, c.razon_social from negocio.pagos p join negocio.clientes c on c.id = p.cliente_id
                            where (%(c)s::uuid is null or p.cliente_id = %(c)s) and (%(d)s::date is null or p.fecha >= %(d)s)
                              and (%(h)s::date is null or p.fecha <= %(h)s)
                            order by p.fecha desc, p.creado_en desc limit 1000""",
                        {"c": cliente_id, "d": desde, "h": hasta}).fetchall()


@r.post("/pagos", status_code=201, summary="Registrar un pago (SINPE, transferencia...); genera comisiones y reactiva")
def crear_pago(datos: PagoIn, conn=Depends(admin_plataforma)) -> dict:
    cli = crud.obtener(conn, "negocio.clientes", datos.cliente_id)
    pago = registrar_pago(conn, **datos.model_dump(), moneda=cli["moneda"] if not datos.comprobante_id else "CRC")
    alertas.alertar(conn, "pago", f"Pago registrado de {cli['razon_social']}: {Decimal(pago['monto']):,.2f} ({datos.medio}).",
                    "info", telegram_ahora=False)
    return pago


# ------------------------------------------------------------------ Gastos --
class GastoIn(_Base):
    fecha: date
    proveedor: str = Field(min_length=2)
    identificacion_proveedor: Optional[str] = None
    categoria_id: Optional[UUID] = None
    descripcion: Optional[str] = None
    moneda: Literal["CRC", "USD"] = "CRC"
    tipo_cambio: Decimal = Decimal(1)
    subtotal: Decimal = Field(ge=0)
    iva: Decimal = Field(CERO, ge=0)
    total: Optional[Decimal] = None
    iva_acreditable: Optional[bool] = None
    deducible: Optional[bool] = None
    clave_comprobante: Optional[str] = Field(None, pattern=r"^\d{50}$")


GastoUpd = crud.parcial(GastoIn)


def _defaults_categoria(conn, d: dict) -> dict:
    if d.get("categoria_id"):
        cat = conn.execute("select * from negocio.categorias_gasto where id = %s", (d["categoria_id"],)).fetchone()
        if not cat:
            raise HTTPException(422, "Categoría de gasto no existe")
        if d.get("iva_acreditable") is None:
            d["iva_acreditable"] = cat["iva_acreditable"]
        if d.get("deducible") is None:
            d["deducible"] = cat["deducible"]
    return d


@r.get("/gastos")
def listar_gastos(conn=Depends(admin_plataforma), desde: date | None = None, hasta: date | None = None,
                  categoria_id: UUID | None = None) -> list[dict]:
    return conn.execute("""select g.id, g.fecha, g.proveedor, g.identificacion_proveedor, g.categoria_id, g.descripcion,
                                  g.moneda, g.tipo_cambio, g.subtotal, g.iva, g.total, g.iva_acreditable, g.deducible,
                                  g.clave_comprobante, g.mensaje_receptor, g.creado_en, cg.nombre categoria
                             from negocio.gastos g left join negocio.categorias_gasto cg on cg.id = g.categoria_id
                            where (%(d)s::date is null or g.fecha >= %(d)s) and (%(h)s::date is null or g.fecha <= %(h)s)
                              and (%(c)s::uuid is null or g.categoria_id = %(c)s)
                            order by g.fecha desc limit 2000""", {"d": desde, "h": hasta, "c": categoria_id}).fetchall()


@r.get("/gastos/proveedores", summary="Proveedores ya usados en gastos (para autocompletar)")
def proveedores_gastos(conn=Depends(admin_plataforma)) -> list[dict]:
    """Un proveedor por identificación (o por nombre si no tiene), con los datos de su gasto más reciente."""
    return conn.execute("""select distinct on (coalesce(nullif(identificacion_proveedor, ''), lower(proveedor)))
                                  proveedor, identificacion_proveedor, categoria_id, fecha as ultimo
                             from negocio.gastos
                            order by coalesce(nullif(identificacion_proveedor, ''), lower(proveedor)), fecha desc, creado_en desc
                            limit 1000""").fetchall()


@r.post("/gastos", status_code=201)
def crear_gasto(datos: GastoIn, conn=Depends(admin_plataforma)) -> dict:
    d = _defaults_categoria(conn, datos.model_dump(exclude_none=True))
    d.setdefault("total", d["subtotal"] + d.get("iva", CERO))
    try:
        with conn.transaction():
            return crud.insertar(conn, "negocio.gastos", d)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Ese comprobante ya está registrado como gasto") from exc


@r.patch("/gastos/{id}")
def editar_gasto(id: UUID, datos: GastoUpd, conn=Depends(admin_plataforma)) -> dict:  # type: ignore[valid-type]
    g = crud.actualizar(conn, "negocio.gastos", id, _defaults_categoria(conn, datos.model_dump(exclude_unset=True)))
    if not g:
        raise HTTPException(404, "Gasto no encontrado")
    return g


@r.post("/gastos/importar-xml", status_code=201, summary="Registrar un gasto desde el XML de una factura recibida (4.3 o 4.4)")
async def importar_xml(archivo: UploadFile = File(...), categoria_id: Optional[UUID] = Form(None),
                       mensaje_receptor: Optional[Literal["aceptado", "aceptado_parcial", "rechazado"]] = Form(None),
                       conn=Depends(admin_plataforma)) -> dict:
    contenido = await archivo.read()
    if len(contenido) > 2_000_000:
        raise HTTPException(422, "El XML es demasiado grande")
    x = facturacion.parsear_xml_recibido(contenido)
    if not categoria_id:
        cat = conn.execute("select id from negocio.categorias_gasto where nombre = 'Otros'").fetchone()
        categoria_id = cat["id"] if cat else None
    advertencias = []
    emisor = facturacion._cfg(conn, "emisor", {}) or {}
    mi_id = facturacion._digitos(emisor.get("identificacion"))
    if mi_id and x["receptor_identificacion"] and facturacion._digitos(x["receptor_identificacion"]) != mi_id:
        advertencias.append("El receptor del comprobante no coincide con la identificación de A.U.R.O.M.")
    if not x["firmado"]:
        advertencias.append("El XML no trae firma digital.")
    if x["tipo"] == "03":
        advertencias.append("Es una nota de crédito: se registra con montos negativos.")
    signo = Decimal(-1) if x["tipo"] == "03" else Decimal(1)
    d = _defaults_categoria(conn, {
        "fecha": x["fecha"], "proveedor": x["emisor_nombre"] or "Proveedor", "identificacion_proveedor": x["emisor_identificacion"],
        "categoria_id": categoria_id, "descripcion": x["detalle"] or None, "moneda": x["moneda"], "tipo_cambio": x["tipo_cambio"],
        "subtotal": x["subtotal"] * signo, "iva": x["iva"] * signo, "total": x["total"] * signo,
        "clave_comprobante": x["clave"] if len(x["clave"]) == 50 else None,
        "consecutivo_comprobante": x["consecutivo"] if len(x["consecutivo"]) == 20 else None,
        "comprobante_tipo": x["tipo"], "xml_recibido": contenido.decode("utf-8", "replace"),
        "mensaje_receptor": mensaje_receptor})
    if d.get("iva_acreditable") is None:
        d["iva_acreditable"] = True
    try:
        with conn.transaction():
            g = crud.insertar(conn, "negocio.gastos", d)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Ese comprobante ya está registrado como gasto") from exc
    g = dict(g)
    g.pop("xml_recibido", None)
    return {**g, "version": x["version"], "otros_impuestos": x["otros_impuestos"], "advertencias": advertencias,
            "nota_mensaje_receptor": "La decisión queda registrada; el MensajeReceptor no se envía a Hacienda desde aquí."
            if mensaje_receptor else None}


@r.get("/categorias-gasto")
def categorias(conn=Depends(admin_plataforma)) -> list[dict]:
    return conn.execute("select * from negocio.categorias_gasto order by nombre").fetchall()


# ================================================================ Reportes ===
def _ventas(conn, ini: date, fin: date) -> dict:
    """Ventas aceptadas por Hacienda en colones (facturas/tiquetes menos notas de crédito)."""
    f = conn.execute(
        """select coalesce(sum(case when tipo = '03' then -1 else 1 end * subtotal * tipo_cambio), 0) base,
                  coalesce(sum(case when tipo = '03' then -1 else 1 end * impuesto * tipo_cambio), 0) iva,
                  coalesce(sum(case when tipo = '03' then subtotal * tipo_cambio end), 0) nc_base,
                  coalesce(sum(case when tipo = '03' then impuesto * tipo_cambio end), 0) nc_iva,
                  count(*)::int n
             from negocio.comprobantes
            where clave is not null and estado in ('aceptado','anulado') and tipo in ('01','03','04')
              and (fecha at time zone 'America/Costa_Rica')::date >= %s and (fecha at time zone 'America/Costa_Rica')::date < %s""",
        (ini, fin)).fetchone()
    return {k: (_q(v) if k != "n" else v) for k, v in f.items()}


def _gastos(conn, ini: date, fin: date) -> dict:
    f = conn.execute(
        """select coalesce(sum(subtotal * tipo_cambio), 0) subtotal,
                  coalesce(sum(case when iva_acreditable then iva * tipo_cambio end), 0) iva_acreditable,
                  coalesce(sum(case when iva_acreditable then subtotal * tipo_cambio end), 0) base_acreditable,
                  coalesce(sum(case when not iva_acreditable then iva * tipo_cambio end), 0) iva_no_acreditable,
                  coalesce(sum(case when deducible then subtotal * tipo_cambio
                                     + case when not iva_acreditable then iva * tipo_cambio else 0 end end), 0) deducible
             from negocio.gastos where fecha >= %s and fecha < %s""", (ini, fin)).fetchone()
    return {k: _q(v) for k, v in f.items()}


def _pagos(conn, ini: date, fin: date) -> dict:
    f = conn.execute("""select coalesce(sum(monto * tipo_cambio), 0) cobrado, coalesce(sum(comision), 0) comision,
                               coalesce(sum(retencion_iva), 0) retencion_iva, coalesce(sum(retencion_renta), 0) retencion_renta
                          from negocio.pagos where fecha >= %s and fecha < %s""", (ini, fin)).fetchone()
    return {k: _q(v) for k, v in f.items()}


def resumen_iva(conn, anio: int, mes: int) -> dict:
    ini, fin = _mes(anio, mes)
    v, g, p = _ventas(conn, ini, fin), _gastos(conn, ini, fin), _pagos(conn, ini, fin)
    neto = v["iva"] - g["iva_acreditable"] - p["retencion_iva"]
    return {
        "anio": anio, "mes": mes, "periodo": ini,
        "ventas": {"gravadas_13": v["base"] + v["nc_base"], "notas_credito": v["nc_base"], "netas": v["base"],
                   "comprobantes": v["n"]},
        "debito_fiscal": v["iva"],
        "compras": {"con_credito": g["base_acreditable"], "total": g["subtotal"]},
        "credito_fiscal": g["iva_acreditable"],
        "iva_no_acreditable": g["iva_no_acreditable"],
        "retenciones_iva_tarjeta": p["retencion_iva"],
        "impuesto_por_pagar": max(neto, CERO),
        "saldo_a_favor": max(-neto, CERO),
        "nota": "Resumen para la declaración D-104 (IVA). Débito: comprobantes aceptados por Hacienda; crédito: gastos con IVA "
                "acreditable; retenciones de tarjeta como pago a cuenta. Revíselo con su contador antes de presentar.",
    }


def _validar_mes(anio: int, mes: int) -> None:
    if not (1 <= mes <= 12 and 2020 <= anio <= 2100):
        raise HTTPException(422, "Mes o año inválido")


@r.get("/cierres/{anio}/{mes}", summary="Resumen de IVA del mes (D-104); guardado si el mes está cerrado")
def ver_cierre_mes(anio: int, mes: int, conn=Depends(admin_plataforma)) -> dict:
    _validar_mes(anio, mes)
    per = conn.execute("select * from negocio.periodos where mes = %s", (date(anio, mes, 1),)).fetchone()
    if per and per["estado"] == "cerrado":
        return {**per["resumen"], "estado": "cerrado", "cerrado_en": per["cerrado_en"]}
    return {**resumen_iva(conn, anio, mes), "estado": "abierto"}


@r.post("/cierres/{anio}/{mes}", summary="Cerrar el mes: guarda el resumen de IVA")
def cerrar_mes(anio: int, mes: int, reabrir: bool = False, conn=Depends(admin_plataforma)) -> dict:
    _validar_mes(anio, mes)
    m = date(anio, mes, 1)
    per = conn.execute("select * from negocio.periodos where mes = %s for update", (m,)).fetchone()
    if per and per["estado"] == "cerrado" and not reabrir:
        raise HTTPException(409, "El mes ya está cerrado (use reabrir=true para recalcular)")
    res = resumen_iva(conn, anio, mes)
    import json as _json
    conn.execute("""insert into negocio.periodos (mes, estado, resumen, cerrado_en) values (%s, 'cerrado', %s::jsonb, now())
                    on conflict (mes) do update set estado = 'cerrado', resumen = excluded.resumen, cerrado_en = now()""",
                 (m, _json.dumps(res, default=str)))
    return {**res, "estado": "cerrado"}


def _impuesto_tramos(base: Decimal, tramos: list[dict]) -> Decimal:
    imp = CERO
    for t in tramos:
        desde = Decimal(str(t["desde"]))
        hasta = Decimal(str(t["hasta"])) if t.get("hasta") is not None else None
        if base <= desde:
            break
        tope = min(base, hasta) if hasta is not None else base
        imp += (tope - desde) * Decimal(str(t["tasa"])) / 100
    return _q(imp)


def renta_anual(conn, anio: int) -> dict:
    ini, fin = date(anio, 1, 1), date(anio + 1, 1, 1)
    v, g, p = _ventas(conn, ini, fin), _gastos(conn, ini, fin), _pagos(conn, ini, fin)
    por_cat = conn.execute(
        """select coalesce(cg.nombre, 'Sin categoría') categoria,
                  sum(g.subtotal * g.tipo_cambio + case when not g.iva_acreditable then g.iva * g.tipo_cambio else 0 end) monto
             from negocio.gastos g left join negocio.categorias_gasto cg on cg.id = g.categoria_id
            where g.fecha >= %s and g.fecha < %s and g.deducible group by 1 order by 2 desc""", (ini, fin)).fetchall()
    gastos = g["deducible"] + p["comision"]
    utilidad = v["base"] - gastos
    cfg = facturacion._cfg(conn, "renta", {}) or {}
    adv = []
    impuesto = None
    if cfg.get("tramos"):
        impuesto = _impuesto_tramos(max(utilidad, CERO), cfg["tramos"])
    else:
        adv.append("Los tramos del impuesto sobre la renta de persona física con actividad lucrativa no están configurados "
                   "(Configuración › renta.tramos): confírmelos con el contador para el periodo fiscal.")
    ccss = None
    if cfg.get("ccss_independiente_pct"):
        ccss = _q(max(utilidad, CERO) * Decimal(str(cfg["ccss_independiente_pct"])) / 100)
        adv.append("La CCSS de trabajador independiente se estima sobre la utilidad; la CCSS usa un ingreso de referencia "
                   "y su propia escala: confírmelo con el contador.")
    else:
        adv.append("Porcentaje de CCSS de trabajador independiente no configurado (Configuración › renta.ccss_independiente_pct): "
                   "confírmelo con el contador.")
    return {"anio": anio, "ingresos": v["base"], "notas_credito": v["nc_base"],
            "gastos_deducibles": g["deducible"], "comisiones_tarjeta": p["comision"], "gastos_por_categoria": por_cat,
            "total_gastos": gastos, "utilidad": utilidad, "base_imponible": max(utilidad, CERO),
            "impuesto_estimado": impuesto, "retenciones_renta_tarjeta": p["retencion_renta"],
            "impuesto_neto_estimado": max(impuesto - p["retencion_renta"], CERO) if impuesto is not None else None,
            "ccss_independiente_estimada": ccss, "advertencias": adv}


@r.get("/renta/{anio}", summary="Estado de resultados del año, base imponible y CCSS independiente")
def renta(anio: int, conn=Depends(admin_plataforma)) -> dict:
    _validar_mes(anio, 1)
    return renta_anual(conn, anio)


def config_reparto(conn) -> dict:
    v = facturacion._cfg(conn, "reparto", {}) or {}
    tramos = v.get("tramos") or [{"hasta": None, "pct_salario": "50"}]
    return {"tramos": tramos, "tope": Decimal(str(v.get("tope_salario_neto", "4000000"))),
            "pct_caja": Decimal(str(v.get("pct_caja_del_resto", "50")))}


def repartir(utilidad: Decimal, cfg: dict) -> dict:
    if utilidad <= 0:
        return {"pct_salario": CERO, "salario": CERO, "caja": CERO, "inversion": CERO}
    pct = Decimal(str(cfg["tramos"][-1]["pct_salario"]))
    for t in cfg["tramos"]:
        if t.get("hasta") is None or utilidad <= Decimal(str(t["hasta"])):
            pct = Decimal(str(t["pct_salario"]))
            break
    salario = min(_q(utilidad * pct / 100), cfg["tope"])
    resto = utilidad - salario
    caja = _q(resto * cfg["pct_caja"] / 100)
    return {"pct_salario": pct, "salario": salario, "caja": caja, "inversion": resto - caja}


def reparto_anual(conn, anio: int) -> dict:
    cfg = config_reparto(conn)
    hoy = hoy_cr()
    meses = []
    for m in range(1, 13):
        ini, fin = _mes(anio, m)
        if ini > hoy:
            break
        v, g, p = _ventas(conn, ini, fin), _gastos(conn, ini, fin), _pagos(conn, ini, fin)
        util = v["base"] - g["deducible"] - p["comision"]
        real = {x["tipo"]: _q(x["monto"]) for x in conn.execute(
            "select tipo, sum(monto) monto from negocio.movimientos_caja where fecha >= %s and fecha < %s group by 1",
            (ini, fin)).fetchall()}
        meses.append({"mes": ini, "ingresos": v["base"], "gastos": g["deducible"] + p["comision"], "utilidad": util,
                      **repartir(util, cfg), "registrado": real})
    tot = {k: sum((x[k] for x in meses), CERO) for k in ("ingresos", "gastos", "utilidad", "salario", "caja", "inversion")}
    return {"anio": anio, "parametros": {"tramos": cfg["tramos"], "tope_salario_neto": cfg["tope"],
                                         "pct_caja_del_resto": cfg["pct_caja"]},
            "meses": meses, "totales": tot,
            "nota": "El salario es neto, con tope mensual; el resto de la utilidad se divide entre caja e inversión. "
                    "Los tramos se ajustan en Configuración › reparto."}


@r.get("/reparto", summary="Reparto mensual de la utilidad: salario de la dueña, caja e inversión")
def reparto(anio: int | None = None, conn=Depends(admin_plataforma)) -> dict:
    return reparto_anual(conn, anio or hoy_cr().year)


@r.get("/tablero", summary="MRR, clientes, cobrado, por cobrar, gastos, utilidad, IVA y reparto")
def tablero(anio: int | None = None, conn=Depends(admin_plataforma)) -> dict:
    hoy = hoy_cr()
    anio = anio or hoy.year
    ini, fin = date(anio, 1, 1), date(anio + 1, 1, 1)
    por_estado = {f["estado"]: f["n"] for f in conn.execute(
        "select estado, count(*)::int n from negocio.clientes group by 1").fetchall()}
    tc = facturacion._cfg(conn, "tipo_cambio_usd", {}) or {}
    tc_usd = Decimal(str(tc["valor"])) if tc.get("valor") else None
    mrr, empleados, sin_tc = CERO, 0, 0
    for c in conn.execute("select id from negocio.clientes where estado in ('activo','gracia','solo_lectura')").fetchall():
        p = facturacion.precio_cliente(conn, c["id"], hoy)
        empleados += p["empleados"]
        if p["moneda"] == "USD":
            if tc_usd is None:
                sin_tc += 1
                continue
            mrr += p["subtotal"] * tc_usd
        else:
            mrr += p["subtotal"]
    v, g, p = _ventas(conn, ini, fin), _gastos(conn, ini, fin), _pagos(conn, ini, fin)
    por_cobrar = conn.execute(
        """select coalesce(sum(co.total * co.tipo_cambio - coalesce(pg.pagado, 0)), 0) monto, count(*)::int n
             from negocio.comprobantes co
             left join lateral (select sum(monto * tipo_cambio) pagado from negocio.pagos where comprobante_id = co.id) pg on true
            where co.tipo in ('01','04') and co.estado in ('enviado','aceptado')
              and co.total * co.tipo_cambio > coalesce(pg.pagado, 0) + 0.01""").fetchone()
    utilidad = v["base"] - g["deducible"] - p["comision"]
    iva_mes = resumen_iva(conn, hoy.year, hoy.month) if anio == hoy.year else None
    rep = reparto_anual(conn, anio)
    return {"anio": anio, "mrr": _q(mrr), "mrr_sin_tipo_cambio_usd": sin_tc, "clientes": por_estado,
            "empleados_facturables": empleados, "cobrado": p["cobrado"], "por_cobrar": _q(por_cobrar["monto"]),
            "comprobantes_por_cobrar": por_cobrar["n"], "ingresos": v["base"], "gastos": g["deducible"] + p["comision"],
            "utilidad": utilidad, "iva_mes_actual": iva_mes, "reparto": rep["totales"],
            "alertas_sin_atender": conn.execute("select count(*)::int n from negocio.alertas where not atendida").fetchone()["n"]}


# =========================================================== Configuración ===
CLAVES_CONFIG = {"emisor", "cabys_suscripcion", "cabys_implementacion", "comisiones", "tarifa_tarjeta_pct", "precios",
                 "cobro", "tipo_cambio_usd", "firma_politica", "proveedor_sistemas", "reparto", "renta"}


class ConfigIn(BaseModel):
    valor: Any


@r.get("/configuracion", summary="Toda la configuración del negocio")
def listar_config(conn=Depends(admin_plataforma)) -> list[dict]:
    return conn.execute("select * from negocio.configuracion where clave not like 'cron_%%' order by clave").fetchall()


@r.get("/configuracion/{clave}")
def ver_config(clave: str, conn=Depends(admin_plataforma)) -> dict:
    f = conn.execute("select * from negocio.configuracion where clave = %s", (clave,)).fetchone()
    if not f:
        raise HTTPException(404, "Clave de configuración no existe")
    return f


@r.put("/configuracion/{clave}", summary="Guardar un valor de configuración. Body: {\"valor\": <json>}")
def guardar_config(clave: str, datos: ConfigIn, conn=Depends(admin_plataforma)) -> dict:
    import re as _re
    if clave not in CLAVES_CONFIG:
        raise HTTPException(422, f"Clave no permitida. Opciones: {', '.join(sorted(CLAVES_CONFIG))}")
    v = datos.valor
    if clave.startswith("cabys_") and v is not None and not _re.fullmatch(r"\d{13}", str(v)):
        raise HTTPException(422, "El código CABYS debe tener 13 dígitos")
    if clave == "emisor":
        if not isinstance(v, dict):
            raise HTTPException(422, "El emisor debe ser un objeto")
        actual = facturacion._cfg(conn, "emisor", {}) or {}
        v = {**actual, **v}
    if clave == "tarifa_tarjeta_pct" and v is not None:
        try:
            if not (0 <= Decimal(str(v)) <= 20):
                raise ValueError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, "Porcentaje inválido") from exc
        v = str(v)
    facturacion._guardar_cfg(conn, clave, v)
    return ver_config(clave, conn)


# ============================================================== Comisiones ===
def _mes_param(mes: str | None) -> date:
    if not mes:
        return _primero(hoy_cr())
    try:
        return date.fromisoformat(mes + "-01" if len(mes) == 7 else mes).replace(day=1)
    except ValueError as exc:
        raise HTTPException(422, "Mes inválido (use AAAA-MM)") from exc


@r.post("/comisiones/calcular", summary="Registra las comisiones que falten por los pagos del mes")
def calcular_comisiones(mes: str | None = None, conn=Depends(admin_plataforma)) -> dict:
    m = _mes_param(mes)
    creadas = comisiones.calcular_mes(conn, m)
    return {"mes": m, "creadas": len(creadas), "comisiones": creadas}


@r.get("/comisiones")
def listar_comisiones(mes: str | None = None, socio_id: UUID | None = None, estado: str | None = None,
                      conn=Depends(admin_plataforma)) -> list[dict]:
    m = _mes_param(mes) if mes else None
    return conn.execute("""select c.*, s.nombre socio, s.tipo socio_tipo, e.nombre empresa
                             from public.comisiones c join public.socios s on s.id = c.socio_id
                             join public.empresas e on e.id = c.empresa_id
                            where (%(m)s::date is null or c.periodo = %(m)s) and (%(s)s::uuid is null or c.socio_id = %(s)s)
                              and (%(e)s::text is null or c.estado = %(e)s)
                            order by c.periodo desc, s.nombre""", {"m": m, "s": socio_id, "e": estado}).fetchall()


@r.post("/liquidaciones-socio/generar", summary="Agrupa las comisiones pendientes en la liquidación del mes")
def generar_liquidaciones(mes: str | None = None, conn=Depends(admin_plataforma)) -> list[dict]:
    return comisiones.generar_liquidaciones(conn, _mes_param(mes))


@r.get("/liquidaciones-socio")
def listar_liquidaciones(mes: str | None = None, conn=Depends(admin_plataforma)) -> list[dict]:
    m = _mes_param(mes) if mes else None
    return conn.execute("""select l.*, s.nombre socio, s.tipo socio_tipo, s.iban from public.liquidaciones_socio l
                             join public.socios s on s.id = l.socio_id
                            where (%(m)s::date is null or l.mes = %(m)s) order by l.mes desc, s.nombre""", {"m": m}).fetchall()


class PagoLiquidacionIn(_Base):
    referencia: Optional[str] = None
    fecha: Optional[date] = None


@r.post("/liquidaciones-socio/{id}/pagar", summary="Marca pagada y registra el gasto en 'Comisiones de socios y vendedores'")
def pagar_liquidacion(id: UUID, datos: PagoLiquidacionIn = Body(default_factory=PagoLiquidacionIn),
                      conn=Depends(admin_plataforma)) -> dict:
    return comisiones.pagar_liquidacion(conn, id, datos.referencia, datos.fecha)
