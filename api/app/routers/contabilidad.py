"""Módulo de Contabilidad · Fases C1 a C3.

  C1 Núcleo: catálogo de cuentas (NIIF PYMES), diarios, asientos con partida doble,
     numeración por diario y año, borrador → contabilizado → revertido.
  C2 Integración: el cierre de una planilla entra solo al diario de Planillas y la
     reapertura lo revierte (servicios/contable.py).
  C3 Reportes y cierres: libro diario, mayor, balance de comprobación, estado de
     resultados, balance general (JSON, Excel y PDF); cierre y reapertura de meses;
     cierre anual con asiento de cierre a utilidades acumuladas.

Reglas: leer = cualquier rol de la empresa; registrar = puede operar; cerrar y reabrir
periodos = aprobador con MFA. Con el módulo apagado solo se consulta.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Literal, Optional
from uuid import UUID

import contabilidad_motor as CM
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql
from pydantic import Field, field_validator, model_validator

from ..auth import Contexto, aprobador, contexto_empresa, operador, requiere_modulo
from ..esquemas import Base
from ..servicios import contable
from . import reportes as R

r = APIRouter(prefix="/empresas/{empresa_id}/contabilidad", tags=["contabilidad"],
              dependencies=[Depends(requiere_modulo("contabilidad"))])

Tipo = Literal["activo", "pasivo", "patrimonio", "ingreso", "costo", "gasto"]
Naturaleza = Literal["deudora", "acreedora"]
Monto = Decimal


# =============================================================================
#                                   Esquemas
# =============================================================================
class CuentaIn(Base):
    codigo: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z._-]{0,29}$")
    nombre: str = Field(min_length=2, max_length=150)
    tipo: Tipo
    naturaleza: Optional[Naturaleza] = None
    padre_id: Optional[UUID] = None
    acepta_movimientos: bool = True
    descripcion: Optional[str] = Field(None, max_length=500)


class CuentaUpd(Base):
    codigo: Optional[str] = Field(None, pattern=r"^[0-9A-Za-z][0-9A-Za-z._-]{0,29}$")
    nombre: Optional[str] = Field(None, min_length=2, max_length=150)
    tipo: Optional[Tipo] = None
    naturaleza: Optional[Naturaleza] = None
    padre_id: Optional[UUID] = None
    acepta_movimientos: Optional[bool] = None
    activa: Optional[bool] = None
    descripcion: Optional[str] = Field(None, max_length=500)


class LineaIn(Base):
    cuenta_id: UUID
    debito: Monto = Field(Decimal(0), ge=0, max_digits=16, decimal_places=2)
    credito: Monto = Field(Decimal(0), ge=0, max_digits=16, decimal_places=2)
    descripcion: Optional[str] = Field(None, max_length=200)

    @model_validator(mode="after")
    def _un_lado(self):
        if (self.debito > 0) == (self.credito > 0):
            raise ValueError("Cada línea lleva débito o crédito (uno de los dos)")
        return self


class AsientoIn(Base):
    diario_id: Optional[UUID] = None
    fecha: date
    descripcion: str = Field(min_length=2, max_length=300)
    referencia: Optional[str] = Field(None, max_length=100)
    tipo: Literal["manual", "apertura"] = "manual"
    lineas: list[LineaIn] = Field(min_length=2, max_length=500)
    contabilizar: bool = False

    @field_validator("fecha")
    @classmethod
    def _rango(cls, v: date) -> date:
        if not date(2000, 1, 1) <= v <= date(2100, 12, 31):
            raise ValueError("Fecha fuera de rango")
        return v


class RevertirIn(Base):
    fecha: Optional[date] = None
    motivo: str = Field(min_length=3, max_length=200)


class MotivoIn(Base):
    motivo: str = Field(min_length=5, max_length=300)


# =============================================================================
#                                 Utilidades
# =============================================================================
def _dict_lineas(lineas: list[LineaIn]) -> list[dict]:
    return [l.model_dump() for l in lineas]


def _arbol(filas: list[dict]) -> list[dict]:
    """Ordena el catálogo como árbol (padre antes que hijos) y agrega `nivel`."""
    hijos: dict = defaultdict(list)
    ids = {f["id"] for f in filas}
    raices = []
    for f in filas:
        (hijos[f["padre_id"]] if f["padre_id"] in ids else raices).append(f)
    salida: list[dict] = []

    def visitar(f: dict, nivel: int) -> None:
        salida.append({**f, "nivel": nivel})
        for h in sorted(hijos.get(f["id"], []), key=lambda x: x["codigo"]):
            visitar(h, nivel + 1)

    for f in sorted(raices, key=lambda x: x["codigo"]):
        visitar(f, 0)
    return salida


def _rango(desde: date | None, hasta: date | None) -> tuple[date, date]:
    hoy = contable.hoy_cr()
    desde = desde or date(hoy.year, 1, 1)
    hasta = hasta or hoy
    if desde > hasta:
        raise HTTPException(422, "La fecha inicial es posterior a la final")
    return desde, hasta


def _empresa(ctx: Contexto) -> dict:
    return ctx.conn.execute("select nombre, cedula_juridica from empresas where id = %s", (ctx.empresa_id,)).fetchone()


def _sub(ctx: Contexto, texto: str) -> str:
    e = _empresa(ctx)
    return f"{e['nombre']}" + (f" · Céd. {e['cedula_juridica']}" if e["cedula_juridica"] else "") + f" · {texto}"


# =============================================================================
#                                    Resumen
# =============================================================================
@r.get("/resumen", summary="Tablero contable: situación, resultados del mes y del año, pendientes")
def resumen(ctx: Contexto = Depends(contexto_empresa)) -> dict:
    hoy = contable.hoy_cr()
    ctas = contable.a_motor(contable.cuentas(ctx.conn, ctx.empresa_id))
    bg = CM.balance_general(ctas, contable.totales(ctx.conn, ctx.empresa_id, hasta=hoy))
    er_mes = CM.estado_resultados(ctas, contable.totales(ctx.conn, ctx.empresa_id, hoy.replace(day=1), hoy, True))
    er_anio = CM.estado_resultados(ctas, contable.totales(ctx.conn, ctx.empresa_id, date(hoy.year, 1, 1), hoy, True))
    cuenta = ctx.conn.execute(
        """select count(*) filter (where estado = 'borrador')::int as borradores,
                  count(*) filter (where estado <> 'borrador' and fecha >= %s)::int as del_mes,
                  count(*) filter (where advertencia is not null and estado = 'borrador')::int as con_aviso
             from contabilidad.asientos where empresa_id = %s""", (hoy.replace(day=1), ctx.empresa_id)).fetchone()
    serie = ctx.conn.execute(
        """select extract(month from a.fecha)::int as mes,
                  coalesce(sum(l.credito - l.debito) filter (where c.tipo = 'ingreso'), 0) as ingresos,
                  coalesce(sum(l.debito - l.credito) filter (where c.tipo in ('costo','gasto')), 0) as egresos
             from contabilidad.asientos a join contabilidad.lineas l on l.asiento_id = a.id
             join contabilidad.cuentas c on c.id = l.cuenta_id
            where a.empresa_id = %s and a.estado in ('contabilizado','revertido') and a.origen <> 'cierre'
              and a.fecha between %s and %s
            group by 1 order by 1""", (ctx.empresa_id, date(hoy.year, 1, 1), hoy)).fetchall()
    por_mes = {f["mes"]: f for f in serie}
    ultimos = ctx.conn.execute(
        """select a.id, a.numero, a.fecha, a.descripcion, a.estado, a.origen, a.total_debito, a.advertencia,
                  d.codigo as diario_codigo
             from contabilidad.asientos a join contabilidad.diarios d on d.id = a.diario_id
            where a.empresa_id = %s order by a.creado_en desc limit 8""", (ctx.empresa_id,)).fetchall()
    cerrados = ctx.conn.execute(
        """select anio, mes from contabilidad.periodos where empresa_id = %s and estado = 'cerrado'
            order by anio desc, mes desc limit 1""", (ctx.empresa_id,)).fetchone()
    planilla = ctx.conn.execute("select public.modulo_activo(%s, 'planilla') as ok", (ctx.empresa_id,)).fetchone()["ok"]
    return contable.a_texto({
        "fecha": hoy, "activo": bg["activo"]["total"], "pasivo": bg["pasivo"]["total"],
        "patrimonio": bg["total_patrimonio"], "cuadra": bg["cuadra"],
        "efectivo": sum((f["monto"] for f in bg["activo"]["filas"]
                         if not f["es_grupo"] and f["codigo"][:4] in ("1101", "1102", "1103")), Decimal(0)),
        "mes": {"ingresos": er_mes["ingresos"]["total"], "egresos": er_mes["costos"]["total"] + er_mes["gastos"]["total"],
                "utilidad": er_mes["utilidad_neta"]},
        "anio": {"ingresos": er_anio["ingresos"]["total"], "egresos": er_anio["costos"]["total"] + er_anio["gastos"]["total"],
                 "utilidad": er_anio["utilidad_neta"]},
        "serie": [{"mes": m, "ingresos": por_mes.get(m, {}).get("ingresos", Decimal(0)),
                   "egresos": por_mes.get(m, {}).get("egresos", Decimal(0))} for m in range(1, hoy.month + 1)],
        "borradores": cuenta["borradores"], "con_aviso": cuenta["con_aviso"], "asientos_mes": cuenta["del_mes"],
        "ultimo_cerrado": cerrados, "ultimos": ultimos, "integracion_planilla": planilla,
        "cuentas": len(ctas),
    })


# =============================================================================
#                               Catálogo de cuentas
# =============================================================================
@r.get("/cuentas", summary="Catálogo de cuentas en árbol, con saldo actual")
def listar_cuentas(ctx: Contexto = Depends(contexto_empresa), buscar: str | None = Query(None, max_length=60),
                   solo_movimientos: bool = False, incluir_inactivas: bool = True) -> list[dict]:
    filas = contable.cuentas(ctx.conn, ctx.empresa_id)
    saldos = contable.totales(ctx.conn, ctx.empresa_id, hasta=contable.hoy_cr())
    usados = {str(f["cuenta_id"]) for f in ctx.conn.execute(
        "select distinct cuenta_id from contabilidad.lineas where empresa_id = %s", (ctx.empresa_id,)).fetchall()}
    comp = CM.balance_comprobacion(contable.a_motor(filas), {}, saldos, incluir_vacias=True)
    saldo = {f["id"]: f["saldo_final"] for f in comp["filas"]}
    salida = []
    for f in _arbol([dict(x) for x in filas]):
        if solo_movimientos and not f["acepta_movimientos"]:
            continue
        if not incluir_inactivas and not f["activa"]:
            continue
        if buscar and buscar.lower() not in (f["codigo"] + " " + f["nombre"]).lower():
            continue
        salida.append({**f, "saldo": saldo.get(str(f["id"]), Decimal(0)), "tiene_movimientos": str(f["id"]) in usados})
    return contable.a_texto(salida)


@r.post("/cuentas", status_code=201, summary="Agregar una cuenta (o subcuenta)")
def crear_cuenta(datos: CuentaIn, ctx: Contexto = Depends(operador)) -> dict:
    d = datos.model_dump()
    d["naturaleza"] = d["naturaleza"] or CM.naturaleza_de(d["tipo"])
    return ctx.conn.execute(
        """insert into contabilidad.cuentas (empresa_id, codigo, nombre, tipo, naturaleza, padre_id, acepta_movimientos, descripcion)
           values (%(e)s, %(codigo)s, %(nombre)s, %(tipo)s, %(naturaleza)s, %(padre_id)s, %(acepta_movimientos)s, %(descripcion)s)
           returning *""", {**d, "e": ctx.empresa_id}).fetchone()


CAMPOS_CUENTA = frozenset(CuentaUpd.model_fields)


@r.patch("/cuentas/{id}", summary="Editar una cuenta")
def editar_cuenta(id: UUID, datos: CuentaUpd, ctx: Contexto = Depends(operador)) -> dict:
    cambios = {k: v for k, v in datos.model_dump(exclude_unset=True).items()
               if k in CAMPOS_CUENTA and (v is not None or k in ("padre_id", "descripcion"))}
    if "tipo" in cambios and "naturaleza" not in cambios:
        cambios["naturaleza"] = CM.naturaleza_de(cambios["tipo"])
    if not cambios:
        raise HTTPException(422, "No hay cambios")
    sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in cambios)
    fila = ctx.conn.execute(sql.SQL("update contabilidad.cuentas set {} where id = %s and empresa_id = %s returning *")
                            .format(sets), [*cambios.values(), id, ctx.empresa_id]).fetchone()
    if not fila:
        raise HTTPException(404, "Cuenta no encontrada")
    return fila


@r.delete("/cuentas/{id}", status_code=204, summary="Borrar una cuenta sin movimientos ni subcuentas")
def borrar_cuenta(id: UUID, ctx: Contexto = Depends(operador)) -> None:
    if ctx.conn.execute("select 1 from contabilidad.lineas where cuenta_id = %s limit 1", (id,)).fetchone():
        raise HTTPException(409, "La cuenta tiene movimientos: puede desactivarla, pero no borrarla")
    if ctx.conn.execute("select 1 from contabilidad.cuentas where padre_id = %s limit 1", (id,)).fetchone():
        raise HTTPException(409, "La cuenta tiene subcuentas: bórrelas o muévalas primero")
    if not ctx.conn.execute("delete from contabilidad.cuentas where id = %s and empresa_id = %s", (id, ctx.empresa_id)).rowcount:
        raise HTTPException(404, "Cuenta no encontrada")


@r.get("/diarios", summary="Diarios de la empresa")
def diarios(ctx: Contexto = Depends(contexto_empresa)) -> list[dict]:
    return ctx.conn.execute("select * from contabilidad.diarios where empresa_id = %s order by codigo", (ctx.empresa_id,)).fetchall()


# =============================================================================
#                                    Asientos
# =============================================================================
@r.get("/asientos", summary="Asientos con filtros (más recientes primero)")
def listar_asientos(ctx: Contexto = Depends(contexto_empresa), desde: date | None = None, hasta: date | None = None,
                    estado: Literal["borrador", "contabilizado", "revertido"] | None = None,
                    diario_id: UUID | None = None, origen: str | None = Query(None, pattern=r"^[a-z]{3,15}$"),
                    buscar: str | None = Query(None, max_length=80), cuenta_id: UUID | None = None,
                    limite: int = Query(100, ge=1, le=500), desplazar: int = Query(0, ge=0)) -> dict:
    cond = sql.SQL("""a.empresa_id = %(e)s
          and (%(desde)s::date is null or a.fecha >= %(desde)s) and (%(hasta)s::date is null or a.fecha <= %(hasta)s)
          and (%(estado)s::text is null or a.estado = %(estado)s)
          and (%(diario)s::uuid is null or a.diario_id = %(diario)s)
          and (%(origen)s::text is null or a.origen = %(origen)s)
          and (%(buscar)s::text is null or a.descripcion ilike '%%' || %(buscar)s || '%%'
               or a.numero ilike '%%' || %(buscar)s || '%%' or a.referencia ilike '%%' || %(buscar)s || '%%')
          and (%(cuenta)s::uuid is null or exists (select 1 from contabilidad.lineas l
                                                     where l.asiento_id = a.id and l.cuenta_id = %(cuenta)s))""")
    p = {"e": ctx.empresa_id, "desde": desde, "hasta": hasta, "estado": estado, "diario": diario_id, "origen": origen,
         "buscar": buscar, "cuenta": cuenta_id, "lim": limite, "off": desplazar}
    total = ctx.conn.execute(sql.SQL("select count(*)::int n from contabilidad.asientos a where ") + cond, p).fetchone()["n"]
    filas = ctx.conn.execute(
        sql.SQL("""select a.id, a.numero, a.fecha, a.descripcion, a.referencia, a.origen, a.estado, a.total_debito,
                          a.total_credito, a.advertencia, a.revierte_a, a.revertido_por, a.creado_en,
                          d.codigo as diario_codigo, d.nombre as diario_nombre
                     from contabilidad.asientos a join contabilidad.diarios d on d.id = a.diario_id where """) + cond +
        sql.SQL(" order by a.fecha desc, a.creado_en desc limit %(lim)s offset %(off)s"), p).fetchall()
    return {"total": total, "asientos": filas}


@r.post("/asientos", status_code=201, summary="Registrar un asiento (borrador o contabilizado de una vez)")
def crear_asiento(datos: AsientoIn, ctx: Contexto = Depends(operador)) -> dict:
    diario = datos.diario_id or contable.diario_id(ctx.conn, ctx.empresa_id, "APE" if datos.tipo == "apertura" else "GEN")
    return contable.crear(ctx.conn, ctx.empresa_id, diario=diario, fecha=datos.fecha, descripcion=datos.descripcion,
                          referencia=datos.referencia, lineas=_dict_lineas(datos.lineas), origen=datos.tipo,
                          contabilizar_ya=datos.contabilizar)


@r.get("/asientos/{id}", summary="Detalle de un asiento con sus líneas")
def ver_asiento(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    return contable.detalle(ctx.conn, ctx.empresa_id, id)


@r.put("/asientos/{id}", summary="Reemplazar un borrador (cabecera y líneas)")
def editar_asiento(id: UUID, datos: AsientoIn, ctx: Contexto = Depends(operador)) -> dict:
    actual = contable.detalle(ctx.conn, ctx.empresa_id, id)
    diario = datos.diario_id or actual["diario_id"]
    a = contable.reemplazar(ctx.conn, ctx.empresa_id, id, {"diario_id": diario, "fecha": datos.fecha,
                                                           "descripcion": datos.descripcion, "referencia": datos.referencia},
                            _dict_lineas(datos.lineas))
    return contable.contabilizar(ctx.conn, ctx.empresa_id, id) if datos.contabilizar else a


@r.delete("/asientos/{id}", status_code=204, summary="Borrar un borrador")
def borrar_asiento(id: UUID, ctx: Contexto = Depends(operador)) -> None:
    a = contable.detalle(ctx.conn, ctx.empresa_id, id)
    if a["estado"] != "borrador":
        raise HTTPException(409, "Un asiento contabilizado no se borra: haga una reversión")
    ctx.conn.execute("delete from contabilidad.asientos where id = %s and empresa_id = %s", (id, ctx.empresa_id))


@r.post("/asientos/{id}/contabilizar", summary="Contabilizar un borrador (queda numerado e inmutable)")
def contabilizar(id: UUID, ctx: Contexto = Depends(operador)) -> dict:
    return contable.contabilizar(ctx.conn, ctx.empresa_id, id)


@r.post("/asientos/{id}/revertir", summary="Revertir un asiento contabilizado (crea el asiento contrario)")
def revertir(id: UUID, datos: RevertirIn, ctx: Contexto = Depends(operador)) -> dict:
    return contable.revertir(ctx.conn, ctx.empresa_id, id, datos.fecha, datos.motivo)


# =============================================================================
#                                  Periodos
# =============================================================================
MESES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Setiembre", "Octubre",
         "Noviembre", "Diciembre"]


@r.get("/periodos", summary="Los 12 meses del año con su estado y movimientos")
def periodos(anio: int = Query(..., ge=2000, le=2100), ctx: Contexto = Depends(contexto_empresa)) -> dict:
    estados = {f["mes"]: f for f in ctx.conn.execute(
        "select * from contabilidad.periodos where empresa_id = %s and anio = %s", (ctx.empresa_id, anio)).fetchall()}
    cuentas = {f["mes"]: f for f in ctx.conn.execute(
        """select extract(month from fecha)::int as mes, count(*) filter (where estado <> 'borrador')::int as asientos,
                  count(*) filter (where estado = 'borrador')::int as borradores
             from contabilidad.asientos where empresa_id = %s and extract(year from fecha) = %s group by 1""",
        (ctx.empresa_id, anio)).fetchall()}
    cierre = ctx.conn.execute("select * from contabilidad.cierres_anuales where empresa_id = %s and anio = %s",
                              (ctx.empresa_id, anio)).fetchone()
    meses = []
    for m in range(1, 13):
        e = estados.get(m)
        meses.append({"mes": m, "nombre": MESES[m], "estado": e["estado"] if e else "abierto",
                      "cerrado_en": e["cerrado_en"] if e else None, "motivo_reapertura": e["motivo_reapertura"] if e else None,
                      "asientos": cuentas.get(m, {}).get("asientos", 0), "borradores": cuentas.get(m, {}).get("borradores", 0)})
    return {"anio": anio, "meses": meses, "cierre_anual": cierre}


@r.post("/periodos/{anio}/{mes}/cerrar", summary="Cerrar un mes: ya nadie contabiliza con esa fecha")
def cerrar_mes(anio: int, mes: int, ctx: Contexto = Depends(aprobador)) -> dict:
    if not (2000 <= anio <= 2100 and 1 <= mes <= 12):
        raise HTTPException(422, "Mes inválido")
    pend = ctx.conn.execute("""select count(*)::int n from contabilidad.asientos where empresa_id = %s and estado = 'borrador'
                                 and extract(year from fecha) = %s and extract(month from fecha) = %s""",
                            (ctx.empresa_id, anio, mes)).fetchone()["n"]
    if pend:
        raise HTTPException(409, f"Hay {pend} asiento(s) en borrador en {MESES[mes]} {anio}: contabilícelos o bórrelos antes de cerrar")
    return ctx.conn.execute(
        """insert into contabilidad.periodos (empresa_id, anio, mes, estado, cerrado_por, cerrado_en)
           values (%s, %s, %s, 'cerrado', auth.uid(), now())
           on conflict (empresa_id, anio, mes) do update set estado = 'cerrado', cerrado_por = auth.uid(), cerrado_en = now()
           returning *""", (ctx.empresa_id, anio, mes)).fetchone()


@r.post("/periodos/{anio}/{mes}/reabrir", summary="Reabrir un mes cerrado (queda el motivo en la bitácora)")
def reabrir_mes(anio: int, mes: int, datos: MotivoIn, ctx: Contexto = Depends(aprobador)) -> dict:
    fila = ctx.conn.execute(
        """update contabilidad.periodos set estado = 'abierto', reabierto_por = auth.uid(), reabierto_en = now(),
                  motivo_reapertura = %s
            where empresa_id = %s and anio = %s and mes = %s and estado = 'cerrado' returning *""",
        (datos.motivo, ctx.empresa_id, anio, mes)).fetchone()
    if not fila:
        raise HTTPException(404, "Ese mes no está cerrado")
    return fila


@r.post("/cierre-anual/{anio}", summary="Cierre del ejercicio: resultados a utilidades acumuladas y año bloqueado")
def cierre_anual(anio: int, ctx: Contexto = Depends(aprobador)) -> dict:
    if not 2000 <= anio <= 2100:
        raise HTTPException(422, "Año inválido")
    return contable.a_texto(contable.cerrar_anio(ctx.conn, ctx.empresa_id, anio))


@r.post("/cierre-anual/{anio}/reabrir", summary="Deshacer el cierre anual (revierte el asiento de cierre)")
def reabrir_anio(anio: int, datos: MotivoIn, ctx: Contexto = Depends(aprobador)) -> dict:
    return contable.a_texto(contable.reabrir_anio(ctx.conn, ctx.empresa_id, anio, datos.motivo))


# =============================================================================
#                                    Reportes
# =============================================================================
def _diario(ctx: Contexto, desde: date, hasta: date) -> dict:
    asientos = ctx.conn.execute(
        """select a.id, a.numero, a.fecha, a.descripcion, a.referencia, a.estado, a.origen, d.codigo as diario_codigo
             from contabilidad.asientos a join contabilidad.diarios d on d.id = a.diario_id
            where a.empresa_id = %s and a.estado in ('contabilizado','revertido') and a.fecha between %s and %s
            order by a.fecha, a.numero""", (ctx.empresa_id, desde, hasta)).fetchall()
    lineas = defaultdict(list)
    for l in ctx.conn.execute(
            """select l.asiento_id, c.codigo, c.nombre, l.descripcion, l.debito, l.credito
                 from contabilidad.lineas l join contabilidad.asientos a on a.id = l.asiento_id
                 join contabilidad.cuentas c on c.id = l.cuenta_id
                where a.empresa_id = %s and a.estado in ('contabilizado','revertido') and a.fecha between %s and %s
                order by l.orden""", (ctx.empresa_id, desde, hasta)).fetchall():
        lineas[l["asiento_id"]].append(l)
    tot_d = tot_c = Decimal(0)
    salida = []
    for a in asientos:
        ls = lineas.get(a["id"], [])
        tot_d += sum((Decimal(x["debito"]) for x in ls), Decimal(0))
        tot_c += sum((Decimal(x["credito"]) for x in ls), Decimal(0))
        salida.append({**a, "lineas": ls})
    return {"desde": desde, "hasta": hasta, "asientos": salida, "totales": {"debitos": tot_d, "creditos": tot_c}}


def _mayor(ctx: Contexto, cuenta_id: UUID, desde: date, hasta: date) -> dict:
    cta = ctx.conn.execute("select * from contabilidad.cuentas where id = %s and empresa_id = %s",
                           (cuenta_id, ctx.empresa_id)).fetchone()
    if not cta:
        raise HTTPException(404, "Cuenta no encontrada")
    ids = [f["id"] for f in ctx.conn.execute(
        """with recursive arbol as (select id from contabilidad.cuentas where id = %s
                                    union all
                                    select c.id from contabilidad.cuentas c join arbol on c.padre_id = arbol.id)
           select id from arbol""", (cuenta_id,)).fetchall()]
    ini = ctx.conn.execute(
        """select coalesce(sum(l.debito), 0) d, coalesce(sum(l.credito), 0) c
             from contabilidad.lineas l join contabilidad.asientos a on a.id = l.asiento_id
            where a.empresa_id = %s and l.cuenta_id = any(%s) and a.estado in ('contabilizado','revertido') and a.fecha < %s""",
        (ctx.empresa_id, ids, desde)).fetchone()
    saldo = CM.saldo_natural(cta["naturaleza"], ini["d"], ini["c"])
    inicial = saldo
    movs = []
    for m in ctx.conn.execute(
            """select a.id as asiento_id, a.fecha, a.numero, a.descripcion as asiento, l.descripcion, c.codigo as cuenta_codigo,
                      l.debito, l.credito
                 from contabilidad.lineas l join contabilidad.asientos a on a.id = l.asiento_id
                 join contabilidad.cuentas c on c.id = l.cuenta_id
                where a.empresa_id = %s and l.cuenta_id = any(%s) and a.estado in ('contabilizado','revertido')
                  and a.fecha between %s and %s
                order by a.fecha, a.numero, l.orden""", (ctx.empresa_id, ids, desde, hasta)).fetchall():
        saldo += CM.saldo_natural(cta["naturaleza"], m["debito"], m["credito"])
        movs.append({**m, "saldo": saldo})
    return {"cuenta": cta, "desde": desde, "hasta": hasta, "saldo_inicial": inicial, "movimientos": movs,
            "debitos": sum((Decimal(m["debito"]) for m in movs), Decimal(0)),
            "creditos": sum((Decimal(m["credito"]) for m in movs), Decimal(0)), "saldo_final": saldo}


def _comprobacion(ctx: Contexto, desde: date, hasta: date, incluir_cierre: bool = True) -> dict:
    ctas = contable.a_motor(contable.cuentas(ctx.conn, ctx.empresa_id))
    ini = contable.totales(ctx.conn, ctx.empresa_id, antes_de=desde)
    mov = contable.totales(ctx.conn, ctx.empresa_id, desde, hasta, excluir_cierre=not incluir_cierre)
    return {"desde": desde, "hasta": hasta, **CM.balance_comprobacion(ctas, ini, mov)}


def _resultados(ctx: Contexto, desde: date, hasta: date) -> dict:
    ctas = contable.a_motor(contable.cuentas(ctx.conn, ctx.empresa_id))
    return {"desde": desde, "hasta": hasta,
            **CM.estado_resultados(ctas, contable.totales(ctx.conn, ctx.empresa_id, desde, hasta, excluir_cierre=True))}


def _balance(ctx: Contexto, fecha: date) -> dict:
    ctas = contable.a_motor(contable.cuentas(ctx.conn, ctx.empresa_id))
    return {"fecha": fecha, **CM.balance_general(ctas, contable.totales(ctx.conn, ctx.empresa_id, hasta=fecha))}


@r.get("/reportes/diario", summary="Libro diario")
def rep_diario(desde: date | None = None, hasta: date | None = None, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    return contable.a_texto(_diario(ctx, *_rango(desde, hasta)))


@r.get("/reportes/mayor", summary="Libro mayor de una cuenta (o grupo) con saldo corrido")
def rep_mayor(cuenta_id: UUID, desde: date | None = None, hasta: date | None = None,
              ctx: Contexto = Depends(contexto_empresa)) -> dict:
    return contable.a_texto(_mayor(ctx, cuenta_id, *_rango(desde, hasta)))


@r.get("/reportes/comprobacion", summary="Balance de comprobación")
def rep_comprobacion(desde: date | None = None, hasta: date | None = None, incluir_cierre: bool = True,
                     ctx: Contexto = Depends(contexto_empresa)) -> dict:
    return contable.a_texto(_comprobacion(ctx, *_rango(desde, hasta), incluir_cierre))


@r.get("/reportes/resultados", summary="Estado de resultados")
def rep_resultados(desde: date | None = None, hasta: date | None = None, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    return contable.a_texto(_resultados(ctx, *_rango(desde, hasta)))


@r.get("/reportes/balance", summary="Balance general (estado de situación financiera)")
def rep_balance(fecha: date | None = None, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    return contable.a_texto(_balance(ctx, fecha or contable.hoy_cr()))


def _sangria(f: dict) -> str:
    return "   " * f.get("nivel", 0) + f["nombre"]


@r.get("/reportes/{tipo}/exportar", summary="Exportar un reporte contable a Excel o PDF")
def exportar(tipo: Literal["diario", "mayor", "comprobacion", "resultados", "balance"],
             formato: Literal["xlsx", "pdf"] = "xlsx", desde: date | None = None, hasta: date | None = None,
             fecha: date | None = None, cuenta_id: UUID | None = None, ctx: Contexto = Depends(contexto_empresa)):
    C = R.C
    fmt = lambda d: d.strftime("%d/%m/%Y")  # noqa: E731
    if tipo == "balance":
        f = fecha or contable.hoy_cr()
        b = _balance(ctx, f)
        filas = []
        for titulo, sec in (("ACTIVO", b["activo"]), ("PASIVO", b["pasivo"]), ("PATRIMONIO", b["patrimonio"])):
            filas.append({"codigo": "", "cuenta": titulo, "monto": None})
            filas += [{"codigo": x["codigo"], "cuenta": _sangria(x), "monto": x["monto"]} for x in sec["filas"]]
            filas.append({"codigo": "", "cuenta": f"Total {titulo.lower()}", "monto": sec["total"]})
        filas.append({"codigo": "", "cuenta": "Utilidad (pérdida) del ejercicio", "monto": b["resultado_ejercicio"]})
        filas.append({"codigo": "", "cuenta": "Total pasivo y patrimonio", "monto": b["total_pasivo_patrimonio"]})
        rep = R.reporte("Balance general", _sub(ctx, f"Al {fmt(f)}"),
                        [C("codigo", "Código"), C("cuenta", "Cuenta"), C("monto", "Monto", "dinero")], filas, sumar=[])
        nombre = f"balance-general-{f}"
    elif tipo == "resultados":
        d, h = _rango(desde, hasta)
        e = _resultados(ctx, d, h)
        filas = []
        for titulo, sec in (("INGRESOS", e["ingresos"]), ("COSTOS", e["costos"]), ("GASTOS", e["gastos"])):
            filas.append({"codigo": "", "cuenta": titulo, "monto": None})
            filas += [{"codigo": x["codigo"], "cuenta": _sangria(x), "monto": x["monto"]} for x in sec["filas"]]
            filas.append({"codigo": "", "cuenta": f"Total {titulo.lower()}", "monto": sec["total"]})
            if titulo == "COSTOS":
                filas.append({"codigo": "", "cuenta": "Utilidad bruta", "monto": e["utilidad_bruta"]})
        filas.append({"codigo": "", "cuenta": "Utilidad (pérdida) neta", "monto": e["utilidad_neta"]})
        rep = R.reporte("Estado de resultados", _sub(ctx, f"Del {fmt(d)} al {fmt(h)}"),
                        [C("codigo", "Código"), C("cuenta", "Cuenta"), C("monto", "Monto", "dinero")], filas, sumar=[])
        nombre = f"estado-resultados-{d}-{h}"
    elif tipo == "comprobacion":
        d, h = _rango(desde, hasta)
        b = _comprobacion(ctx, d, h)
        rep = R.reporte("Balance de comprobación", _sub(ctx, f"Del {fmt(d)} al {fmt(h)}"),
                        [C("codigo", "Código"), C("cuenta", "Cuenta"), C("saldo_inicial", "Saldo inicial", "dinero"),
                         C("debitos", "Débitos", "dinero"), C("creditos", "Créditos", "dinero"),
                         C("saldo_deudor", "Saldo deudor", "dinero"), C("saldo_acreedor", "Saldo acreedor", "dinero")],
                        [{**x, "cuenta": _sangria(x)} for x in b["filas"]], sumar=[])
        rep["totales"] = {k: b["totales"][k] for k in ("debitos", "creditos", "saldo_deudor", "saldo_acreedor")}
        nombre = f"balance-comprobacion-{d}-{h}"
    elif tipo == "mayor":
        if not cuenta_id:
            raise HTTPException(422, "Indique la cuenta")
        d, h = _rango(desde, hasta)
        m = _mayor(ctx, cuenta_id, d, h)
        filas = [{"fecha": None, "numero": "", "detalle": "Saldo inicial", "debito": None, "credito": None,
                  "saldo": m["saldo_inicial"]}]
        filas += [{"fecha": x["fecha"], "numero": x["numero"], "detalle": x["descripcion"] or x["asiento"],
                   "debito": x["debito"] or None, "credito": x["credito"] or None, "saldo": x["saldo"]} for x in m["movimientos"]]
        rep = R.reporte(f"Libro mayor · {m['cuenta']['codigo']} {m['cuenta']['nombre']}",
                        _sub(ctx, f"Del {fmt(d)} al {fmt(h)}"),
                        [C("fecha", "Fecha", "fecha"), C("numero", "Asiento"), C("detalle", "Detalle"),
                         C("debito", "Débito", "dinero"), C("credito", "Crédito", "dinero"), C("saldo", "Saldo", "dinero")],
                        filas, sumar=[])
        rep["totales"] = {"debito": m["debitos"], "credito": m["creditos"], "saldo": m["saldo_final"]}
        nombre = f"mayor-{m['cuenta']['codigo']}-{d}-{h}"
    else:
        d, h = _rango(desde, hasta)
        di = _diario(ctx, d, h)
        filas = []
        for a in di["asientos"]:
            for i, l in enumerate(a["lineas"]):
                filas.append({"fecha": a["fecha"] if i == 0 else None, "numero": a["numero"] if i == 0 else "",
                              "detalle": a["descripcion"] if i == 0 else (l["descripcion"] or ""),
                              "cuenta": f"{l['codigo']} {l['nombre']}", "debito": l["debito"] or None,
                              "credito": l["credito"] or None})
        rep = R.reporte("Libro diario", _sub(ctx, f"Del {fmt(d)} al {fmt(h)}"),
                        [C("fecha", "Fecha", "fecha"), C("numero", "Asiento"), C("detalle", "Detalle"), C("cuenta", "Cuenta"),
                         C("debito", "Débito", "dinero"), C("credito", "Crédito", "dinero")], filas, sumar=[])
        rep["totales"] = {"debito": di["totales"]["debitos"], "credito": di["totales"]["creditos"]}
        nombre = f"libro-diario-{d}-{h}"
    return R.entregar(rep, formato, nombre)
