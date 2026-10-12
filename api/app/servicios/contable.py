"""Servicios del módulo de Contabilidad: asientos, reversiones, saldos e integración con Planilla.

Los montos y validaciones salen de `contabilidad_motor` (Python puro). La base vuelve a
validar al contabilizar (partida doble, periodo abierto, cuentas de detalle), así que
aunque algo se saltara la API, un asiento descuadrado nunca queda contabilizado.
"""
from __future__ import annotations

from datetime import date, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID

import contabilidad_motor as CM
from fastapi import HTTPException

from ..db import Conexion
from ..fechas import hoy_cr

CR = timezone(timedelta(hours=-6))
ESTADOS_CON_SALDO = ("contabilizado", "revertido")



def activo(conn: Conexion, empresa_id) -> bool:
    return bool(conn.execute("select public.modulo_activo(%s, 'contabilidad') as ok", (empresa_id,)).fetchone()["ok"])


# ---------------------------------------------------------------- Catálogo
def cuentas(conn: Conexion, empresa_id) -> list[dict]:
    return conn.execute("select * from contabilidad.cuentas where empresa_id = %s order by codigo", (empresa_id,)).fetchall()


def a_motor(filas: Iterable[dict]) -> list[CM.Cuenta]:
    return [CM.Cuenta(str(c["id"]), c["codigo"], c["nombre"], c["tipo"], c["naturaleza"],
                      str(c["padre_id"]) if c["padre_id"] else None, c["acepta_movimientos"], c["activa"]) for c in filas]


def totales(conn: Conexion, empresa_id, desde: date | None = None, hasta: date | None = None,
            excluir_cierre: bool = False, antes_de: date | None = None) -> dict[str, CM.Totales]:
    """Débitos y créditos por cuenta de los asientos con efecto (contabilizados o revertidos)."""
    filas = conn.execute(
        """select l.cuenta_id, sum(l.debito) as d, sum(l.credito) as c
             from contabilidad.lineas l join contabilidad.asientos a on a.id = l.asiento_id
            where a.empresa_id = %(e)s and a.estado = any(%(est)s)
              and (%(desde)s::date is null or a.fecha >= %(desde)s)
              and (%(hasta)s::date is null or a.fecha <= %(hasta)s)
              and (%(antes)s::date is null or a.fecha < %(antes)s)
              and (not %(sin_cierre)s or a.origen <> 'cierre')
            group by l.cuenta_id""",
        {"e": empresa_id, "est": list(ESTADOS_CON_SALDO), "desde": desde, "hasta": hasta, "antes": antes_de,
         "sin_cierre": excluir_cierre}).fetchall()
    return {str(f["cuenta_id"]): CM.Totales(Decimal(f["d"]), Decimal(f["c"])) for f in filas}


def diario_id(conn: Conexion, empresa_id, codigo: str) -> UUID:
    f = conn.execute("select id from contabilidad.diarios where empresa_id = %s and codigo = %s", (empresa_id, codigo)).fetchone()
    if not f:
        raise HTTPException(409, f"La empresa no tiene el diario {codigo}. Active de nuevo el módulo de Contabilidad.")
    return f["id"]


# ----------------------------------------------------------------- Asientos
def detalle(conn: Conexion, empresa_id, asiento_id) -> dict:
    a = conn.execute(
        """select a.*, d.codigo as diario_codigo, d.nombre as diario_nombre
             from contabilidad.asientos a join contabilidad.diarios d on d.id = a.diario_id
            where a.id = %s and a.empresa_id = %s""", (asiento_id, empresa_id)).fetchone()
    if not a:
        raise HTTPException(404, "Asiento no encontrado")
    a = dict(a)
    a["lineas"] = conn.execute(
        """select l.id, l.cuenta_id, c.codigo as cuenta_codigo, c.nombre as cuenta_nombre, l.descripcion, l.debito,
                  l.credito, l.orden
             from contabilidad.lineas l join contabilidad.cuentas c on c.id = l.cuenta_id
            where l.asiento_id = %s order by l.orden, l.id""", (asiento_id,)).fetchall()
    a["cuadra"] = sum((Decimal(l["debito"]) for l in a["lineas"]), Decimal(0)) == \
        sum((Decimal(l["credito"]) for l in a["lineas"]), Decimal(0))
    return a


def _errores(conn: Conexion, empresa_id, lineas: list[dict]) -> list[str]:
    por_id = {c.id: c for c in a_motor(cuentas(conn, empresa_id))}
    return CM.validar_asiento([CM.Linea(str(l["cuenta_id"]), Decimal(l["debito"] or 0), Decimal(l["credito"] or 0))
                               for l in lineas], por_id)


def _insertar_lineas(conn: Conexion, empresa_id, asiento_id, lineas: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """insert into contabilidad.lineas (empresa_id, asiento_id, cuenta_id, descripcion, debito, credito, orden)
               values (%s, %s, %s, %s, %s, %s, %s)""",
            [(empresa_id, asiento_id, l["cuenta_id"], (l.get("descripcion") or None), CM.r2(l.get("debito") or 0),
              CM.r2(l.get("credito") or 0), i) for i, l in enumerate(lineas)])


def crear(conn: Conexion, empresa_id, *, diario: UUID, fecha: date, descripcion: str, lineas: list[dict],
          referencia: str | None = None, origen: str = "manual", origen_id=None, revierte_a=None,
          advertencia: str | None = None, contabilizar_ya: bool = False) -> dict:
    if contabilizar_ya:
        errores = _errores(conn, empresa_id, lineas)
        if errores:
            raise HTTPException(422, "; ".join(errores))
    a = conn.execute(
        """insert into contabilidad.asientos (empresa_id, diario_id, fecha, descripcion, referencia, origen, origen_id,
                                              revierte_a, advertencia, total_debito, total_credito)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
        (empresa_id, diario, fecha, descripcion.strip(), referencia, origen, origen_id, revierte_a, advertencia,
         CM.r2(sum((Decimal(l.get("debito") or 0) for l in lineas), Decimal(0))),
         CM.r2(sum((Decimal(l.get("credito") or 0) for l in lineas), Decimal(0))))).fetchone()
    _insertar_lineas(conn, empresa_id, a["id"], lineas)
    if contabilizar_ya:
        conn.execute("update contabilidad.asientos set estado = 'contabilizado' where id = %s", (a["id"],))
    return detalle(conn, empresa_id, a["id"])


def reemplazar(conn: Conexion, empresa_id, asiento_id, cabecera: dict, lineas: list[dict]) -> dict:
    a = conn.execute("select estado from contabilidad.asientos where id = %s and empresa_id = %s for update",
                     (asiento_id, empresa_id)).fetchone()
    if not a:
        raise HTTPException(404, "Asiento no encontrado")
    if a["estado"] != "borrador":
        raise HTTPException(409, "Un asiento contabilizado no se modifica: haga una reversión")
    conn.execute(
        """update contabilidad.asientos set diario_id = %s, fecha = %s, descripcion = %s, referencia = %s,
                  total_debito = %s, total_credito = %s, advertencia = null where id = %s""",
        (cabecera["diario_id"], cabecera["fecha"], cabecera["descripcion"].strip(), cabecera.get("referencia"),
         CM.r2(sum((Decimal(l.get("debito") or 0) for l in lineas), Decimal(0))),
         CM.r2(sum((Decimal(l.get("credito") or 0) for l in lineas), Decimal(0))), asiento_id))
    conn.execute("delete from contabilidad.lineas where asiento_id = %s", (asiento_id,))
    _insertar_lineas(conn, empresa_id, asiento_id, lineas)
    return detalle(conn, empresa_id, asiento_id)


def contabilizar(conn: Conexion, empresa_id, asiento_id) -> dict:
    a = detalle(conn, empresa_id, asiento_id)
    if a["estado"] != "borrador":
        raise HTTPException(409, "El asiento ya está contabilizado")
    errores = _errores(conn, empresa_id, a["lineas"])
    if errores:
        raise HTTPException(422, "; ".join(errores))
    if not periodo_abierto(conn, empresa_id, a["fecha"]):
        raise HTTPException(409, f"El periodo {a['fecha'].month:02d}/{a['fecha'].year} está cerrado")
    conn.execute("update contabilidad.asientos set estado = 'contabilizado', advertencia = null where id = %s", (asiento_id,))
    if a["revierte_a"]:
        # Reversión que había quedado en borrador (periodo cerrado): ahora sí anula al original.
        conn.execute("""update contabilidad.asientos set estado = 'revertido', revertido_por = %s
                         where id = %s and estado = 'contabilizado'""", (asiento_id, a["revierte_a"]))
    return detalle(conn, empresa_id, asiento_id)


def periodo_abierto(conn: Conexion, empresa_id, fecha: date) -> bool:
    return bool(conn.execute("select contabilidad.periodo_abierto(%s, %s) as ok", (empresa_id, fecha)).fetchone()["ok"])


def revertir(conn: Conexion, empresa_id, asiento_id, fecha: date | None = None,
             motivo: str | None = None, exigir_abierto: bool = True, permitir_cierre: bool = False) -> dict:
    """Crea el asiento contrario y deja el original como revertido. Nada se borra."""
    a = detalle(conn, empresa_id, asiento_id)
    if a["estado"] != "contabilizado":
        raise HTTPException(409, "Solo un asiento contabilizado se puede revertir" if a["estado"] == "borrador"
                            else "El asiento ya fue revertido")
    if a["origen"] == "cierre" and not permitir_cierre:
        raise HTTPException(409, "El asiento de cierre anual se deshace reabriendo el año en Periodos y cierres")
    if conn.execute("select 1 from contabilidad.asientos where revierte_a = %s", (a["id"],)).fetchone():
        raise HTTPException(409, "Este asiento ya tiene una reversión en borrador: contabilícela")
    fecha = fecha or (a["fecha"] if periodo_abierto(conn, empresa_id, a["fecha"]) else hoy_cr())
    abierto = periodo_abierto(conn, empresa_id, fecha)
    if not abierto and exigir_abierto:
        raise HTTPException(409, f"El periodo {fecha.month:02d}/{fecha.year} está cerrado: elija otra fecha para la reversión")
    lineas = [{"cuenta_id": l.cuenta_id, "debito": l.debito, "credito": l.credito, "descripcion": l.descripcion}
              for l in CM.asiento_reverso(CM.Linea(str(x["cuenta_id"]), Decimal(x["debito"]), Decimal(x["credito"]),
                                                   x["descripcion"] or "") for x in a["lineas"])]
    texto = f"Reversión de {a['numero']}" + (f": {motivo.strip()}" if motivo and motivo.strip() else "")
    rev = crear(conn, empresa_id, diario=a["diario_id"], fecha=fecha, descripcion=texto[:300], lineas=lineas,
                referencia=a["numero"], origen="reversion", origen_id=a["origen_id"], revierte_a=a["id"],
                advertencia=None if abierto else "El periodo está cerrado: contabilice esta reversión al reabrirlo",
                contabilizar_ya=abierto)
    if abierto:
        conn.execute("update contabilidad.asientos set estado = 'revertido', revertido_por = %s where id = %s",
                     (rev["id"], a["id"]))
    return rev


# ------------------------------------------------------- Integración Planilla
CONCEPTOS_ACTIVO = {"prestamos_empleados", "subsidios_por_cobrar"}


def _tipo_concepto(concepto: str) -> str:
    if concepto.startswith("gasto_"):
        return "gasto"
    return "activo" if concepto in CONCEPTOS_ACTIVO else "pasivo"


def _cuenta_para(conn: Conexion, empresa_id, codigo: str, nombre: str, concepto: str,
                 creadas: list[str]) -> UUID:
    c = conn.execute("select id, acepta_movimientos, activa from contabilidad.cuentas where empresa_id = %s and codigo = %s",
                     (empresa_id, codigo)).fetchone()
    if c and c["acepta_movimientos"] and c["activa"]:
        return c["id"]
    if c:
        # Existe pero es de agrupación o está inactiva: se crea una subcuenta de detalle.
        codigo = f"{codigo}-01"
        sub = conn.execute("select id from contabilidad.cuentas where empresa_id = %s and codigo = %s",
                           (empresa_id, codigo)).fetchone()
        if sub:
            return sub["id"]
    tipo = _tipo_concepto(concepto)
    padre = conn.execute(
        """select id from contabilidad.cuentas
            where empresa_id = %s and tipo = %s and not acepta_movimientos and %s like codigo || '%%'
            order by length(codigo) desc limit 1""", (empresa_id, tipo, codigo)).fetchone()
    nueva = conn.execute(
        """insert into contabilidad.cuentas (empresa_id, codigo, nombre, tipo, naturaleza, padre_id, descripcion)
           values (%s, %s, %s, %s, %s, %s, 'Creada automáticamente desde Planilla') returning id""",
        (empresa_id, codigo, nombre[:150], tipo, CM.naturaleza_de(tipo), padre["id"] if padre else None)).fetchone()
    creadas.append(codigo)
    return nueva["id"]


def desde_planilla(conn: Conexion, planilla: dict, lineas_planilla: list[dict], descripcion: str | None = None,
                   limpiar: bool = True) -> dict | None:
    """Pasa el asiento de una planilla cerrada al diario de Planillas (PLA).

    `lineas_planilla`: [{concepto, codigo, nombre, descripcion, debito, credito}]. Las cuentas se
    buscan por código (Cuentas de planilla); si falta alguna se crea en el grupo que corresponde
    y queda la advertencia para que el contador la revise.
    """
    empresa_id = planilla["empresa_id"]
    if not activo(conn, empresa_id):
        return None
    # Con «un asiento por boleta» se llama una vez por boleta: solo la primera limpia los anteriores.
    for viejo in [] if not limpiar else conn.execute(
            """select id, estado from contabilidad.asientos where empresa_id = %s and origen_id = %s
                 and origen in ('planilla','aguinaldo') and estado in ('borrador','contabilizado')""",
            (empresa_id, planilla["id"])).fetchall():
        if viejo["estado"] == "borrador":
            conn.execute("delete from contabilidad.asientos where id = %s", (viejo["id"],))
        else:
            revertir(conn, empresa_id, viejo["id"], motivo="nuevo cierre de la planilla", exigir_abierto=False)
    creadas: list[str] = []
    lineas = []
    for l in lineas_planilla:
        if not (Decimal(l["debito"]) or Decimal(l["credito"])):
            continue
        lineas.append({"cuenta_id": _cuenta_para(conn, empresa_id, l["codigo"], l["nombre"], l["concepto"], creadas),
                       "debito": l["debito"], "credito": l["credito"], "descripcion": l["descripcion"]})
    if len(lineas) < 2:
        return None
    fecha = planilla["hasta"]
    abierto = periodo_abierto(conn, empresa_id, fecha)
    avisos = []
    if creadas:
        avisos.append("Se crearon cuentas que no existían en el catálogo: " + ", ".join(creadas) + ". Revise su ubicación.")
    if not abierto:
        avisos.append("El periodo contable está cerrado: el asiento quedó en borrador.")
    tipo = "aguinaldo" if planilla["tipo"] == "aguinaldo" else "planilla"
    return crear(conn, empresa_id, diario=diario_id(conn, empresa_id, "PLA"), fecha=fecha,
                 descripcion=descripcion or f"Planilla {planilla['tipo']} del {planilla['desde']:%d/%m/%Y} al {planilla['hasta']:%d/%m/%Y}",
                 lineas=lineas, referencia=f"Planilla {str(planilla['id'])[:8]}", origen=tipo, origen_id=planilla["id"],
                 advertencia=" ".join(avisos) or None, contabilizar_ya=abierto)


def revertir_planilla(conn: Conexion, empresa_id, planilla_id) -> list[dict]:
    """Al reabrir una planilla: borra sus borradores y revierte lo contabilizado."""
    hechos = []
    for a in conn.execute(
            """select id, estado from contabilidad.asientos where empresa_id = %s and origen_id = %s
                 and origen in ('planilla','aguinaldo') and estado in ('borrador','contabilizado')""",
            (empresa_id, planilla_id)).fetchall():
        if a["estado"] == "borrador":
            conn.execute("delete from contabilidad.asientos where id = %s", (a["id"],))
        else:
            hechos.append(revertir(conn, empresa_id, a["id"], motivo="reapertura de la planilla", exigir_abierto=False))
    return hechos


# ------------------------------------------------------------------ Cierres
def cerrar_anio(conn: Conexion, empresa_id, anio: int) -> dict:
    if conn.execute("select 1 from contabilidad.cierres_anuales where empresa_id = %s and anio = %s",
                    (empresa_id, anio)).fetchone():
        raise HTTPException(409, f"El año {anio} ya está cerrado")
    previo = conn.execute(
        """select min(extract(year from a.fecha))::int as anio from contabilidad.asientos a
            join contabilidad.lineas l on l.asiento_id = a.id join contabilidad.cuentas c on c.id = l.cuenta_id
           where a.empresa_id = %s and a.estado in ('contabilizado','revertido') and a.fecha < %s
             and c.tipo in ('ingreso','costo','gasto')
             and not exists (select 1 from contabilidad.cierres_anuales x where x.empresa_id = a.empresa_id
                               and x.anio = extract(year from a.fecha)::int)""",
        (empresa_id, date(anio, 1, 1))).fetchone()["anio"]
    if previo:
        raise HTTPException(409, f"Primero cierre el año {previo}: tiene resultados sin cerrar")
    if conn.execute("""select 1 from contabilidad.asientos where empresa_id = %s and estado = 'borrador'
                        and fecha between %s and %s limit 1""", (empresa_id, date(anio, 1, 1), date(anio, 12, 31))).fetchone():
        raise HTTPException(409, f"Hay asientos en borrador en {anio}: contabilícelos o bórrelos antes del cierre anual")
    destino = conn.execute("""select id from contabilidad.cuentas where empresa_id = %s and codigo = '3301'
                               and acepta_movimientos and activa""", (empresa_id,)).fetchone()
    if not destino:
        raise HTTPException(409, "Falta la cuenta 3301 Utilidades acumuladas (de detalle y activa) para el cierre")
    ctas = a_motor(cuentas(conn, empresa_id))
    saldos = totales(conn, empresa_id, date(anio, 1, 1), date(anio, 12, 31))
    lineas = CM.asiento_cierre(ctas, saldos, str(destino["id"]))
    if not lineas:
        raise HTTPException(409, f"El año {anio} no tiene ingresos, costos ni gastos que cerrar")
    if not periodo_abierto(conn, empresa_id, date(anio, 12, 31)):
        raise HTTPException(409, f"Diciembre {anio} está cerrado: reábralo para registrar el cierre anual")
    asiento = crear(conn, empresa_id, diario=diario_id(conn, empresa_id, "CIE"), fecha=date(anio, 12, 31),
                    descripcion=f"Cierre del ejercicio {anio}",
                    lineas=[{"cuenta_id": l.cuenta_id, "debito": l.debito, "credito": l.credito,
                             "descripcion": l.descripcion} for l in lineas],
                    origen="cierre", contabilizar_ya=True)
    for mes in range(1, 13):
        conn.execute(
            """insert into contabilidad.periodos (empresa_id, anio, mes, estado, cerrado_por, cerrado_en)
               values (%s, %s, %s, 'cerrado', auth.uid(), now())
               on conflict (empresa_id, anio, mes) do update set estado = 'cerrado', cerrado_por = auth.uid(), cerrado_en = now()""",
            (empresa_id, anio, mes))
    conn.execute("insert into contabilidad.cierres_anuales (empresa_id, anio, asiento_id) values (%s, %s, %s)",
                 (empresa_id, anio, asiento["id"]))
    return {"anio": anio, "asiento": asiento, "utilidad": -sum((Decimal(l.debito) - Decimal(l.credito)
                                                                for l in lineas if l.cuenta_id == str(destino["id"])),
                                                               Decimal(0))}


def reabrir_anio(conn: Conexion, empresa_id, anio: int, motivo: str) -> dict:
    c = conn.execute("select * from contabilidad.cierres_anuales where empresa_id = %s and anio = %s for update",
                     (empresa_id, anio)).fetchone()
    if not c:
        raise HTTPException(404, f"El año {anio} no tiene cierre anual")
    if conn.execute("select 1 from contabilidad.cierres_anuales where empresa_id = %s and anio > %s",
                    (empresa_id, anio)).fetchone():
        raise HTTPException(409, "Reabra primero los años posteriores")
    conn.execute("delete from contabilidad.cierres_anuales where empresa_id = %s and anio = %s", (empresa_id, anio))
    conn.execute("""update contabilidad.periodos set estado = 'abierto', reabierto_por = auth.uid(), reabierto_en = now(),
                           motivo_reapertura = %s where empresa_id = %s and anio = %s and mes = 12""",
                 (motivo, empresa_id, anio))
    rev = revertir(conn, empresa_id, c["asiento_id"], fecha=date(anio, 12, 31), motivo=f"reapertura del año: {motivo}",
                   permitir_cierre=True)
    return {"anio": anio, "reversion": rev}


def numeros(filas: Iterable[dict], claves: Iterable[str]) -> list[dict]:
    claves = list(claves)
    return [{k: (str(CM.r2(v)) if k in claves and v is not None else v) for k, v in f.items()} for f in filas]


def a_texto(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(CM.r2(v))
    if isinstance(v, dict):
        return {k: a_texto(x) for k, x in v.items()}
    if isinstance(v, list):
        return [a_texto(x) for x in v]
    return v
