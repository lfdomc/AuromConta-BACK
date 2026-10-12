"""Comisiones de socios (contadores y vendedores) y su liquidación mensual.

Reglas (negocio.configuracion 'comisiones'):
  * Vendedor que trajo al cliente: 100 % del subtotal sin IVA de las mensualidades 1, 2 y 3
    (`meses_vendedor`), solo cuando el cliente efectivamente paga. Nada después del mes 3.
  * Contador que trajo al cliente: `recurrente_contador_pct` (20 %) de cada mensualidad pagada (sin IVA)
    mientras siga asignado a la empresa (asignaciones_contador sin revocar) y activo.
  * Implementación (₡100 000 + IVA, la cobra A.U.R.O.M.): si el cliente vino por un contador, se le pagan
    `implementacion_contador` (₡50 000). En ventas de vendedor A.U.R.O.M. se queda todo.
  * Atribución: public.empresas.referido_por (código de referido al registrarse).
  * Idempotente por pago: clave única (socio_id, empresa_id, concepto, periodo, pago_id). Un mes pagado en
    varios abonos genera una comisión por abono (proporcional); recalcular no duplica.
  * Implementación pagada en abonos: proporcional a lo pagado, con tope de `implementacion_contador`.
  * Montos en colones: si el pago fue en dólares se convierte con el tipo de cambio del comprobante/pago.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException

from ..db import Conexion
from ..fechas import hoy_cr
from . import alertas

DOS = Decimal("0.01")
CATEGORIA_GASTO = "Comisiones de socios y vendedores"


def _q(v) -> Decimal:
    return Decimal(str(v)).quantize(DOS, ROUND_HALF_UP)


def parametros(conn) -> dict:
    fila = conn.execute("select valor from negocio.configuracion where clave = 'comisiones'").fetchone()
    v = (fila or {}).get("valor") or {}
    return {"implementacion_contador": Decimal(str(v.get("implementacion_contador", "50000"))),
            "recurrente_contador_pct": Decimal(str(v.get("recurrente_contador_pct", "20"))),
            "meses_vendedor": int(v.get("meses_vendedor", 3))}


def base_pago(conn, pago: dict) -> Decimal:
    """Monto del pago sin IVA, en colones."""
    tc = Decimal(pago.get("tipo_cambio") or 1)
    if pago.get("subtotal") is not None:
        return _q(Decimal(pago["subtotal"]) * tc)
    if pago.get("comprobante_id"):
        c = conn.execute("select subtotal, total, tipo_cambio from negocio.comprobantes where id = %s",
                         (pago["comprobante_id"],)).fetchone()
        if c:
            prop = Decimal(pago["monto"]) / Decimal(c["total"]) if Decimal(c["total"]) else Decimal(1)
            return _q(Decimal(c["subtotal"]) * min(prop, Decimal(1)) * Decimal(c["tipo_cambio"]))
    return _q(Decimal(pago["monto"]) / Decimal("1.13") * tc)


def _insertar(conn, socio_id, empresa_id, concepto, periodo, base, pct, monto, pago_id) -> dict | None:
    return conn.execute(
        """insert into public.comisiones (socio_id, empresa_id, concepto, periodo, base, porcentaje, monto, pago_id)
           values (%s,%s,%s,%s,%s,%s,%s,%s)
           on conflict (socio_id, empresa_id, concepto, periodo,
                        coalesce(pago_id, '00000000-0000-0000-0000-000000000000'::uuid)) do nothing returning *""",
        (socio_id, empresa_id, concepto, periodo, base, pct, _q(monto), pago_id)).fetchone()


def registrar_comisiones_por_pago(conn: Conexion, pago_id) -> list[dict]:
    pago = conn.execute("""select p.*, c.empresa_id from negocio.pagos p join negocio.clientes c on c.id = p.cliente_id
                            where p.id = %s""", (pago_id,)).fetchone()
    if not pago or not pago["empresa_id"] or Decimal(pago["monto"]) <= 0:
        return []
    emp = conn.execute("select referido_por from public.empresas where id = %s", (pago["empresa_id"],)).fetchone()
    if not emp or not emp["referido_por"]:
        return []
    socio = conn.execute("select * from public.socios where id = %s and estado = 'activo'", (emp["referido_por"],)).fetchone()
    if not socio:
        return []
    par = parametros(conn)
    base = base_pago(conn, pago)
    periodo = (pago["periodo"] or pago["fecha"]).replace(day=1)
    creadas: list[dict] = []

    if pago["concepto"] == "mensualidad":
        if socio["tipo"] == "vendedor":
            n = conn.execute("""select count(distinct periodo)::int n from negocio.pagos
                                 where cliente_id = %s and concepto = 'mensualidad' and monto > 0
                                   and periodo is not null and periodo <= %s""",
                             (pago["cliente_id"], periodo)).fetchone()["n"]
            if 1 <= n <= par["meses_vendedor"]:
                c = _insertar(conn, socio["id"], pago["empresa_id"], "mensualidad_vendedor", periodo, base,
                              Decimal(100), base, pago["id"])
                if c:
                    creadas.append(c)
        elif socio["tipo"] == "contador":
            asignado = conn.execute("""select 1 from public.asignaciones_contador
                                        where empresa_id = %s and socio_id = %s and revocado_en is null""",
                                    (pago["empresa_id"], socio["id"])).fetchone()
            if asignado:
                pct = par["recurrente_contador_pct"]
                c = _insertar(conn, socio["id"], pago["empresa_id"], "recurrente_contador", periodo, base, pct,
                              base * pct / 100, pago["id"])
                if c:
                    creadas.append(c)
    elif pago["concepto"] == "implementacion" and socio["tipo"] == "contador":
        cli = conn.execute("select implementacion from negocio.clientes where id = %s", (pago["cliente_id"],)).fetchone()
        precio = Decimal(cli["implementacion"]) if cli and cli["implementacion"] else Decimal(0)
        tope = par["implementacion_contador"]
        ya = conn.execute("""select coalesce(sum(monto), 0) as s from public.comisiones
                              where socio_id = %s and empresa_id = %s and concepto = 'implementacion' and estado <> 'anulada'
                                and pago_id is distinct from %s""",
                          (socio["id"], pago["empresa_id"], pago["id"])).fetchone()["s"]
        parte = tope * min(base / precio, Decimal(1)) if precio > 0 else tope
        monto = min(_q(parte), tope - Decimal(ya))
        if monto > 0:
            c = _insertar(conn, socio["id"], pago["empresa_id"], "implementacion", periodo, base, None, monto, pago["id"])
            if c:
                creadas.append(c)
    return creadas


def calcular_mes(conn, mes: date) -> list[dict]:
    """Recorre los pagos del mes y registra las comisiones que falten (idempotente)."""
    ini = mes.replace(day=1)
    fin = date(ini.year + (ini.month == 12), ini.month % 12 + 1, 1)
    creadas = []
    for p in conn.execute("select id from negocio.pagos where fecha >= %s and fecha < %s order by fecha, creado_en",
                          (ini, fin)).fetchall():
        creadas += registrar_comisiones_por_pago(conn, p["id"])
    return creadas


def generar_liquidaciones(conn, mes: date) -> list[dict]:
    """Agrupa por socio las comisiones pendientes hasta `mes` en su liquidación del mes."""
    mes = mes.replace(day=1)
    res = []
    socios = conn.execute("""select distinct socio_id from public.comisiones
                              where estado = 'pendiente' and liquidacion_id is null and periodo <= %s""", (mes,)).fetchall()
    for s in socios:
        liq = conn.execute("select * from public.liquidaciones_socio where socio_id = %s and mes = %s for update",
                           (s["socio_id"], mes)).fetchone()
        if liq and liq["pagado_en"]:
            continue  # ya pagada: lo nuevo entra en la del mes siguiente
        if not liq:
            liq = conn.execute("insert into public.liquidaciones_socio (socio_id, mes, total) values (%s, %s, 0) returning *",
                               (s["socio_id"], mes)).fetchone()
        conn.execute("""update public.comisiones set liquidacion_id = %s, estado = 'liquidada'
                         where socio_id = %s and estado = 'pendiente' and liquidacion_id is null and periodo <= %s""",
                     (liq["id"], s["socio_id"], mes))
        liq = conn.execute("""update public.liquidaciones_socio l
                                 set total = (select coalesce(sum(monto), 0) from public.comisiones where liquidacion_id = l.id)
                               where id = %s returning *""", (liq["id"],)).fetchone()
        res.append(liq)
    return res


def pagar_liquidacion(conn, liquidacion_id, referencia: str | None = None, fecha: date | None = None) -> dict:
    """Marca la liquidación pagada y registra el gasto (categoría de comisiones)."""
    liq = conn.execute("select * from public.liquidaciones_socio where id = %s for update", (liquidacion_id,)).fetchone()
    if not liq:
        raise HTTPException(404, "Liquidación no encontrada")
    if liq["pagado_en"]:
        raise HTTPException(409, "La liquidación ya estaba pagada")
    socio = conn.execute("select * from public.socios where id = %s", (liq["socio_id"],)).fetchone()
    cat = conn.execute("select id, deducible from negocio.categorias_gasto where nombre = %s", (CATEGORIA_GASTO,)).fetchone()
    fecha = fecha or hoy_cr()
    gasto = conn.execute(
        """insert into negocio.gastos (fecha, proveedor, identificacion_proveedor, categoria_id, descripcion,
                                       subtotal, iva, total, iva_acreditable, deducible)
           values (%s, %s, %s, %s, %s, %s, 0, %s, false, %s) returning *""",
        (fecha, socio["nombre"], socio.get("cedula"), cat["id"] if cat else None,
         f"Comisiones {socio['tipo']} {liq['mes']:%m/%Y}", liq["total"], liq["total"], cat["deducible"] if cat else True)).fetchone()
    conn.execute("insert into negocio.movimientos_caja (fecha, tipo, monto, nota) values (%s, 'comision', %s, %s)",
                 (fecha, liq["total"], f"Liquidación {socio['nombre']} {liq['mes']:%m/%Y}"))
    liq = conn.execute("""update public.liquidaciones_socio set pagado_en = %s, referencia_pago = %s, gasto_id = %s
                           where id = %s returning *""",
                       (datetime.combine(fecha, datetime.min.time()), referencia, gasto["id"], liquidacion_id)).fetchone()
    alertas.alertar(conn, "socio", f"Liquidación pagada a {socio['nombre']}: ₡{Decimal(liq['total']):,.2f}", "info",
                    telegram_ahora=False)
    return {**liq, "gasto": gasto}
