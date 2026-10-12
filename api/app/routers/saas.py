"""Fase 6 · SaaS: panel de la plataforma (dueña), socios, parámetros legales,
invitación y portal del empleado."""
from __future__ import annotations

import calendar
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

import httpx
import planilla_motor as M
import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import crud
from ..auth import (
    Contexto,
    Usuario,
    admin_empresa,
    admin_plataforma,
    conexion,
    usuario_actual,
)
from ..config import settings
from ..db import Conexion
from ..fechas import hoy_cr
from ..servicios import facturacion, pdf

r = APIRouter(prefix="/plataforma", tags=["plataforma"])
invitar = APIRouter(tags=["portal"])
portal = APIRouter(prefix="/portal", tags=["portal"])
routers = [r, invitar, portal]

ESTADOS_SERVICIO = Literal["prueba", "activo", "gracia", "solo_lectura", "suspendido", "baja"]
FUNCIONES = Literal["supervision", "soporte", "ventas", "implementacion"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =============================================================== Validación ==
CLAVES_DECIMAL = {"ccss_obrero", "ccss_obrero_pensionado_estado", "ccss_patronal", "bmc_mensual", "rop_obrero",
                  "rop_patronal", "prov_aguinaldo", "prov_vacaciones", "prov_cesantia_defecto", "credito_hijo",
                  "credito_conyuge", "max_pct_embargo"}
CLAVES_ENTERAS = {"dias_mes", "dias_incapacidad_patrono", "cesantia_max_anios", "edad_limite_bmc_reducida"}
CLAVES = CLAVES_DECIMAL | CLAVES_ENTERAS | {"ins_tasas", "ins_clase_defecto", "cesantia_tabla", "tramos_renta"}
# Rangos razonables: cualquier valor fuera de estos casi seguro es un error de digitación
# (p. ej. "10.83" en vez de "0.1083").
RANGOS = {
    "ccss_obrero": (Decimal("0.01"), Decimal("0.25")), "ccss_obrero_pensionado_estado": (Decimal("0"), Decimal("0.25")),
    "ccss_patronal": (Decimal("0.05"), Decimal("0.45")), "bmc_mensual": (Decimal("1000"), Decimal("5000000")),
    "rop_obrero": (Decimal("0"), Decimal("0.1")), "rop_patronal": (Decimal("0"), Decimal("0.1")),
    "prov_aguinaldo": (Decimal("0.05"), Decimal("0.1")), "prov_vacaciones": (Decimal("0.02"), Decimal("0.1")),
    "prov_cesantia_defecto": (Decimal("0"), Decimal("0.1")), "credito_hijo": (Decimal("0"), Decimal("100000")),
    "credito_conyuge": (Decimal("0"), Decimal("100000")), "max_pct_embargo": (Decimal("0"), Decimal("100")),
    "dias_mes": (28, 31), "dias_incapacidad_patrono": (0, 30), "cesantia_max_anios": (1, 30),
    "edad_limite_bmc_reducida": (18, 99),
}
SALARIOS_PRUEBA = ["250000", "700000", "1000000", "1500000", "2500000", "5000000"]


def _dec(v) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"valor no numérico: {v!r}") from exc
    if not d.is_finite():
        raise ValueError(f"valor no numérico: {v!r}")
    return d


def _vigente_json(conn: Conexion, fecha: date) -> dict:
    fila = conn.execute("select * from public.parametros_vigentes(%s)", (fecha,)).fetchone()
    return dict(fila["datos"]) if fila else {}


def validar_parametros(vigente_desde: date, datos: dict, base: dict | None = None) -> dict:
    """Valida un juego de parámetros legales antes de guardarlo.

    `datos` puede ser completo o parcial: se combina sobre `base` (el juego vigente).
    1. Estructura y rangos de cada clave.
    2. Construye ParametrosLegales con planilla_motor.desde_json.
    3. Corre una batería de boletas de control con calcular_boleta (mensual y quincenal, con y sin
       hijos/cónyuge, pensionado y exento) y verifica invariantes: neto ≤ bruto, renta ≥ 0 y no
       decreciente con el salario, CCSS = base × tasa, costo patronal ≥ bruto.
    Devuelve {ok, errores[], advertencias[], casos[], datos (combinados, normalizados)}.
    Nota: la suite pytest del motor fija los montos de 2026 calculados a mano, así que no sirve para
    validar otro juego de parámetros; estas invariantes cumplen esa función."""
    errores: list[str] = []
    advertencias: list[str] = []
    if not isinstance(datos, dict) or not datos:
        return {"ok": False, "errores": ["Los parámetros deben ser un objeto JSON con al menos una clave"],
                "advertencias": [], "casos": [], "datos": None}
    desconocidas = sorted(set(datos) - CLAVES - {"vigente_desde"})
    if desconocidas:
        errores.append(f"Claves desconocidas: {', '.join(desconocidas)}")
    combinados = {**{k: v for k, v in (base or {}).items() if k in CLAVES},
                  **{k: v for k, v in datos.items() if k in CLAVES}}
    faltan = sorted({"ccss_obrero", "ccss_obrero_pensionado_estado", "ccss_patronal", "bmc_mensual", "ins_tasas",
                     "tramos_renta"} - set(combinados))
    if faltan:
        errores.append(f"Faltan claves obligatorias: {', '.join(faltan)}")

    for k in CLAVES_DECIMAL | CLAVES_ENTERAS:
        if k not in combinados:
            continue
        try:
            v = int(combinados[k]) if k in CLAVES_ENTERAS else _dec(combinados[k])
            if k in CLAVES_ENTERAS and str(combinados[k]).strip() != str(v):
                raise ValueError("debe ser entero")
        except (ValueError, TypeError) as exc:
            errores.append(f"{k}: {exc}")
            continue
        lo, hi = RANGOS.get(k, (None, None))
        if lo is not None and not (lo <= v <= hi):
            errores.append(f"{k} = {combinados[k]} está fuera del rango esperado [{lo}, {hi}]")

    ins = combinados.get("ins_tasas")
    if ins is not None:
        if not isinstance(ins, dict) or set(ins) != {"I", "II", "III", "IV", "V"}:
            errores.append("ins_tasas debe tener exactamente las clases I, II, III, IV y V")
        else:
            for c, t in ins.items():
                try:
                    if not (Decimal("0") < _dec(t) < Decimal("0.2")):
                        errores.append(f"ins_tasas.{c} = {t} fuera de rango (0, 0.2)")
                except ValueError as exc:
                    errores.append(f"ins_tasas.{c}: {exc}")
        if combinados.get("ins_clase_defecto", "II") not in ("I", "II", "III", "IV", "V"):
            errores.append("ins_clase_defecto debe ser I, II, III, IV o V")

    tramos = combinados.get("tramos_renta")
    if tramos is not None:
        try:
            if not isinstance(tramos, list) or not tramos:
                raise ValueError("debe ser una lista con al menos un tramo")
            prev_hasta, prev_tasa = Decimal("0"), Decimal("-1")
            for i, t in enumerate(tramos):
                desde, tasa = _dec(t["desde"]), _dec(t["tasa"])
                hasta = None if t.get("hasta") in (None, "") else _dec(t["hasta"])
                if desde != prev_hasta:
                    raise ValueError(f"el tramo {i + 1} debe empezar en {prev_hasta} (empieza en {desde})")
                if hasta is not None and hasta <= desde:
                    raise ValueError(f"el tramo {i + 1} tiene 'hasta' menor o igual a 'desde'")
                if hasta is None and i != len(tramos) - 1:
                    raise ValueError("solo el último tramo puede no tener tope")
                if not (Decimal("0") <= tasa <= Decimal("50")):
                    raise ValueError(f"la tasa del tramo {i + 1} ({tasa}) debe ser un porcentaje entre 0 y 50")
                if tasa < prev_tasa:
                    raise ValueError(f"la tasa del tramo {i + 1} es menor que la del tramo anterior")
                prev_hasta, prev_tasa = hasta, tasa
            if prev_hasta is not None:
                raise ValueError("el último tramo debe quedar sin tope ('hasta': null)")
        except (KeyError, TypeError) as exc:
            errores.append(f"tramos_renta: formato inválido ({exc})")
        except ValueError as exc:
            errores.append(f"tramos_renta: {exc}")

    ces = combinados.get("cesantia_tabla")
    if ces is not None:
        try:
            for a, d in ces.items():
                if not (1 <= int(a) <= 40 and Decimal("0") < _dec(d) <= Decimal("31")):
                    raise ValueError(f"año {a}: {d} días")
        except (ValueError, AttributeError, TypeError) as exc:
            errores.append(f"cesantia_tabla inválida: {exc}")

    if errores:
        return {"ok": False, "errores": errores, "advertencias": advertencias, "casos": [], "datos": None}

    try:
        p = M.desde_json(vigente_desde, combinados)
    except Exception as exc:  # noqa: BLE001 - cualquier error de construcción invalida el juego
        return {"ok": False, "errores": [f"El motor no pudo leer los parámetros: {exc}"], "advertencias": [],
                "casos": [], "datos": None}

    casos: list[dict] = []
    ini = vigente_desde.replace(day=1)
    fin_mes = ini.replace(day=calendar.monthrange(ini.year, ini.month)[1])
    variantes = [("mensual", M.Frecuencia.MENSUAL, ini, fin_mes, {}),
                 ("quincenal", M.Frecuencia.QUINCENAL, ini, ini.replace(day=15), {}),
                 ("mensual 2 hijos y cónyuge", M.Frecuencia.MENSUAL, ini, fin_mes, {"hijos": 2, "conyuge": True}),
                 ("mensual pensionado", M.Frecuencia.MENSUAL, ini, fin_mes, {"pensionado": "estado"}),
                 ("mensual exento", M.Frecuencia.MENSUAL, ini, fin_mes, {"exento": True})]
    for nombre, frec, desde, hasta, kw in variantes:
        renta_prev = Decimal("-1")
        for s in SALARIOS_PRUEBA:
            caso = f"{nombre} ₡{int(s):,}".replace(",", " ")
            try:
                res = M.calcular_boleta(M.EntradaBoleta(M.Empleado(Decimal(s), frec, fecha_ingreso=date(2020, 1, 1), **kw),
                                                        M.Periodo(desde, hasta)), p)
            except Exception as exc:  # noqa: BLE001
                errores.append(f"{caso}: el motor falló ({exc})")
                continue
            fallas = []
            if res.neto > res.bruto:
                fallas.append("neto mayor que el bruto")
            if res.neto < 0:
                fallas.append("neto negativo")
            if res.renta < 0:
                fallas.append("renta negativa")
            if res.renta < renta_prev:
                fallas.append("la renta baja al subir el salario")
            if res.costo_total_patrono < res.bruto:
                fallas.append("costo patronal menor que el bruto")
            if not kw.get("exento") and not kw.get("pensionado"):
                esperado = (res.base_ccss * p.ccss_obrero).quantize(Decimal("0.01"))
                if abs(res.ccss_obrero - esperado) > Decimal("1"):
                    fallas.append(f"CCSS obrero {res.ccss_obrero} ≠ base × tasa {esperado}")
            if kw.get("exento") and (res.ccss_obrero or res.renta):
                fallas.append("un empleado exento no debe pagar CCSS ni renta")
            renta_prev = res.renta
            casos.append({"caso": caso, "bruto": str(res.bruto), "ccss_obrero": str(res.ccss_obrero),
                          "renta": str(res.renta), "neto": str(res.neto), "costo_total": str(res.costo_total_patrono),
                          "ok": not fallas, "fallas": fallas})
            errores += [f"{caso}: {f}" for f in fallas]
    if base:
        cambios = sorted(k for k in combinados if combinados.get(k) != base.get(k))
        if cambios:
            advertencias.append(f"Cambia respecto al juego vigente: {', '.join(cambios)}")
        else:
            advertencias.append("Los parámetros son idénticos al juego vigente")
    return {"ok": not errores, "errores": errores, "advertencias": advertencias, "casos": casos,
            "datos": M.a_json(p) if not errores else None}


def insertar_parametros(conn: Conexion, vigente_desde: date, datos: dict, fuente: str | None,
                        usuario_id) -> dict:
    datos = {k: v for k, v in datos.items() if k != "vigente_desde"}
    try:
        with conn.transaction():
            return conn.execute("""insert into parametros_legales (vigente_desde, datos, fuente, aprobado_por)
                                   values (%s, %s, %s, %s) returning *""",
                                (vigente_desde, json.dumps(datos), fuente, usuario_id)).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, f"Ya existe un juego de parámetros vigente desde {vigente_desde:%d/%m/%Y}") from exc


# ================================================================ Plataforma ==
@r.get("/resumen", summary="Resumen de la plataforma: clientes, empleados, MRR y soporte")
def resumen(conn: Conexion = Depends(admin_plataforma)) -> dict:
    estados = conn.execute("select estado_servicio, count(*)::int as n from empresas group by 1").fetchall()
    empleados = conn.execute("select count(*)::int as n from empleados where estado = 'activo'").fetchone()["n"]
    # MRR en colones con todos los módulos activos (los clientes en dólares se ven en el tablero de negocio).
    mrr = Decimal(0)
    hoy = hoy_cr()
    for c in conn.execute("select * from negocio.clientes where estado in ('activo', 'gracia')").fetchall():
        p = facturacion.precio_empresa(conn, c, c["empresa_id"], hoy)
        if p["moneda"] == "CRC":
            mrr += p["subtotal"]
    modulos = {f["modulo"]: f["n"] for f in conn.execute(
        "select modulo, count(*)::int n from empresa_modulos where hasta is null group by 1").fetchall()}
    tickets = conn.execute("""select count(*) filter (where estado in ('abierto','en_proceso','escalado'))::int as abiertos,
                                     count(*) filter (where estado = 'escalado' or (nivel = 'plataforma'
                                                      and estado in ('abierto','en_proceso')))::int as plataforma,
                                     count(*) filter (where prioridad = 'urgente' and estado not in ('resuelto','cerrado'))::int as urgentes
                                from tickets""").fetchone()
    socios = conn.execute("select tipo, count(*)::int as n from socios where estado = 'activo' group by 1").fetchall()
    alertas = conn.execute("select count(*)::int as n from negocio.alertas where not atendida").fetchone()["n"]
    return {"clientes": {"total": sum(e["n"] for e in estados), "por_estado": {e["estado_servicio"]: e["n"] for e in estados}},
            "empleados_activos": empleados, "mrr": Decimal(mrr).quantize(Decimal("0.01")),
            "tickets": tickets, "socios": {s["tipo"]: s["n"] for s in socios}, "alertas_pendientes": alertas,
            "modulos": modulos}


_EMPRESAS_SQL = """
    select e.id, e.nombre, e.cedula_juridica, e.correo, e.telefono, e.plan, e.estado_servicio, e.creado_en,
           e.referido_por, rs.nombre as referido_por_nombre,
           (select count(*)::int from empleados x where x.empresa_id = e.id and x.estado = 'activo') as empleados,
           (select count(*)::int from tickets t where t.empresa_id = e.id
             and t.estado in ('abierto','en_proceso','escalado')) as tickets_abiertos,
           (select max(p.hasta) from planillas p where p.empresa_id = e.id and p.estado = 'cerrada') as ultima_planilla,
           public.modulos_activos(e.id) as modulos,
           coalesce((select json_agg(json_build_object('socio_id', s.id, 'nombre', s.nombre, 'tipo', s.tipo,
                                                       'funciones', a.funciones, 'permiso', a.permiso,
                                                       'soporte_principal', a.soporte_principal))
                       from asignaciones_contador a join socios s on s.id = a.socio_id
                      where a.empresa_id = e.id and a.revocado_en is null), '[]'::json) as socios
      from empresas e left join socios rs on rs.id = e.referido_por"""


@r.get("/empresas", summary="Todas las empresas clientes")
def empresas(conn: Conexion = Depends(admin_plataforma), estado: str | None = None,
             buscar: str | None = None) -> list[dict]:
    q, p = sql.SQL(_EMPRESAS_SQL) + sql.SQL(" where true"), []
    if estado:
        q += sql.SQL(" and e.estado_servicio = %s"); p.append(estado)
    if buscar:
        q += sql.SQL(" and (e.nombre ilike %s or e.cedula_juridica ilike %s)"); p += [f"%{buscar}%"] * 2
    return conn.execute(q + sql.SQL(" order by e.nombre"), p).fetchall()


@r.get("/empresas/{id}")
def empresa(id: UUID, conn: Conexion = Depends(admin_plataforma)) -> dict:
    fila = conn.execute(sql.SQL(_EMPRESAS_SQL) + sql.SQL(" where e.id = %s"), (id,)).fetchone()
    if not fila:
        raise HTTPException(404, "Empresa no encontrada")
    fila["cliente"] = conn.execute("select * from negocio.clientes where empresa_id = %s", (id,)).fetchone()
    return fila


class EmpresaPlataformaUpd(_Base):
    estado_servicio: ESTADOS_SERVICIO | None = None
    plan: str | None = Field(None, min_length=1, max_length=40)


@r.patch("/empresas/{id}", summary="Cambiar estado del servicio o plan")
def editar_empresa(id: UUID, datos: EmpresaPlataformaUpd, conn: Conexion = Depends(admin_plataforma)) -> dict:
    cambios = datos.model_dump(exclude_unset=True, exclude_none=True)
    fila = crud.actualizar(conn, "empresas", id, cambios)
    if not fila:
        raise HTTPException(404, "Empresa no encontrada")
    if "estado_servicio" in cambios:
        conn.execute("update negocio.clientes set estado = %s where empresa_id = %s", (cambios["estado_servicio"], id))
    return empresa(id, conn)


# -------------------------------------------------------------------- Socios --
class SocioIn(_Base):
    tipo: Literal["contador", "vendedor"]
    nombre: str = Field(min_length=2)
    correo: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    cedula: str | None = None
    telefono: str | None = None
    iban: str | None = None
    certificado: bool = False
    estado: Literal["pendiente", "activo", "suspendido", "baja"] = "activo"
    codigo_referido: str | None = Field(None, pattern=r"^[A-Za-z0-9]{4,16}$")
    invitar: bool = True


class SocioUpd(_Base):
    tipo: Literal["contador", "vendedor"] | None = None
    nombre: str | None = None
    correo: str | None = None
    cedula: str | None = None
    telefono: str | None = None
    iban: str | None = None
    certificado: bool | None = None
    estado: Literal["pendiente", "activo", "suspendido", "baja"] | None = None
    telegram_chat_id: str | None = None


def _invitar_supabase(correo: str, datos: dict, destino: str) -> str | None:
    """Invitación por la API de administración de Supabase Auth. None si no está configurada."""
    if not (settings.supabase_url and settings.supabase_service_key):
        return None
    resp = httpx.post(f"{settings.supabase_url}/auth/v1/invite",
                      headers={"apikey": settings.supabase_service_key,
                               "Authorization": f"Bearer {settings.supabase_service_key}"},
                      json={"email": correo, "data": datos, "redirect_to": f"{settings.app_url}{destino}"}, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(502, f"No se pudo enviar la invitación: {resp.text[:200]}")
    return resp.json().get("id")


@r.get("/socios", summary="Contadores y vendedores")
def socios(conn: Conexion = Depends(admin_plataforma), tipo: str | None = None,
           estado: str | None = None) -> list[dict]:
    q = sql.SQL("""select s.*,
                  (select count(*)::int from asignaciones_contador a where a.socio_id = s.id and a.revocado_en is null) as clientes,
                  (select coalesce(sum(monto), 0) from comisiones c where c.socio_id = s.id and c.estado = 'pendiente') as comisiones_pendientes
             from socios s where true""")
    p: list = []
    if tipo:
        q += sql.SQL(" and s.tipo = %s"); p.append(tipo)
    if estado:
        q += sql.SQL(" and s.estado = %s"); p.append(estado)
    return conn.execute(q + sql.SQL(" order by s.nombre"), p).fetchall()


@r.post("/socios", status_code=201, summary="Registrar un socio (vincula su cuenta o lo invita)")
def crear_socio(datos: SocioIn, u: Usuario = Depends(usuario_actual),
                conn: Conexion = Depends(admin_plataforma)) -> dict:
    d = datos.model_dump(exclude={"invitar"}, exclude_none=True)
    if d.get("codigo_referido"):
        d["codigo_referido"] = d["codigo_referido"].upper()
    uid = conn.execute("select public.buscar_usuario_por_correo(%s) as id", (datos.correo,)).fetchone()["id"]
    invitado = False
    if not uid and datos.invitar:
        uid = _invitar_supabase(datos.correo, {"socio": True, "tipo": datos.tipo}, "/socios")
        invitado = uid is not None
    if uid:
        d["usuario_id"] = uid
    try:
        with conn.transaction():
            fila = crud.insertar(conn, "socios", d)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Ya existe un socio con esa cuenta o ese código de referido") from exc
    return {**fila, "invitado": invitado, "cuenta_vinculada": uid is not None,
            "enlace_registro": f"{settings.app_url}/registro?ref={fila['codigo_referido']}"}


@r.get("/socios/{id}")
def socio(id: UUID, conn: Conexion = Depends(admin_plataforma)) -> dict:
    s = crud.obtener(conn, "socios", id)
    s["asignaciones"] = conn.execute("""select a.*, e.nombre as empresa_nombre, e.estado_servicio
                                          from asignaciones_contador a join empresas e on e.id = a.empresa_id
                                         where a.socio_id = %s order by a.revocado_en nulls first, e.nombre""", (id,)).fetchall()
    s["comisiones"] = conn.execute("""select estado, coalesce(sum(monto), 0) as total, count(*)::int as n
                                        from comisiones where socio_id = %s group by estado""", (id,)).fetchall()
    s["enlace_registro"] = f"{settings.app_url}/registro?ref={s['codigo_referido']}"
    return s


@r.patch("/socios/{id}")
def editar_socio(id: UUID, datos: SocioUpd, conn: Conexion = Depends(admin_plataforma)) -> dict:
    cambios = datos.model_dump(exclude_unset=True)
    if "correo" in cambios and cambios["correo"]:
        uid = conn.execute("select public.buscar_usuario_por_correo(%s) as id", (cambios["correo"],)).fetchone()["id"]
        actual = crud.obtener(conn, "socios", id)
        if uid and not actual["usuario_id"]:
            cambios["usuario_id"] = uid
    try:
        with conn.transaction():
            fila = crud.actualizar(conn, "socios", id, cambios)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Esa cuenta ya pertenece a otro socio") from exc
    if not fila:
        raise HTTPException(404, "Socio no encontrado")
    return fila


class AsignarIn(_Base):
    empresa_id: UUID
    funciones: list[FUNCIONES] = Field(default_factory=lambda: ["supervision", "soporte"], min_length=1)
    permiso: Literal["lectura", "operacion"] = "lectura"
    soporte_principal: bool = True


@r.post("/socios/{id}/asignar", status_code=201, summary="Asignar el socio a una empresa cliente")
def asignar(id: UUID, datos: AsignarIn, u: Usuario = Depends(usuario_actual),
            conn: Conexion = Depends(admin_plataforma)) -> dict:
    s = crud.obtener(conn, "socios", id)
    if s["estado"] != "activo":
        raise HTTPException(409, "El socio no está activo")
    crud.obtener(conn, "empresas", datos.empresa_id)
    if datos.soporte_principal and "soporte" in datos.funciones:
        conn.execute("update asignaciones_contador set soporte_principal = false "
                     "where empresa_id = %s and revocado_en is null", (datos.empresa_id,))
    try:
        with conn.transaction():
            return crud.insertar(conn, "asignaciones_contador", {
                "empresa_id": datos.empresa_id, "socio_id": id, "funciones": list(dict.fromkeys(datos.funciones)),
                "permiso": datos.permiso, "soporte_principal": datos.soporte_principal and "soporte" in datos.funciones,
                "asignado_por": u.id})
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Ese socio ya está asignado a la empresa") from exc


# ------------------------------------------------------- Parámetros legales --
class ParametrosIn(_Base):
    vigente_desde: date
    datos: dict
    fuente: str = Field(min_length=3)


@r.post("/parametros", status_code=201, summary="Nuevo juego de parámetros legales (se valida con el motor)")
def nuevos_parametros(datos: ParametrosIn, u: Usuario = Depends(usuario_actual),
                      conn: Conexion = Depends(admin_plataforma)) -> dict:
    v = validar_parametros(datos.vigente_desde, datos.datos, _vigente_json(conn, datos.vigente_desde))
    if not v["ok"]:
        raise HTTPException(422, "Parámetros inválidos: " + "; ".join(v["errores"][:10]))
    fila = insertar_parametros(conn, datos.vigente_desde, v["datos"], datos.fuente, u.id)
    return {**fila, "validacion": v}


# ===================================================== Invitar al portal =====
class InvitarPortalIn(_Base):
    correo: str | None = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@invitar.post("/empresas/{empresa_id}/empleados/{id}/invitar-portal",
              summary="Dar acceso al portal del empleado (vincula su cuenta o le envía invitación)")
def invitar_portal(id: UUID, datos: InvitarPortalIn | None = None, ctx: Contexto = Depends(admin_empresa)) -> dict:
    emp = crud.obtener(ctx.conn, "empleados", id, ctx.empresa_id)
    if emp["estado"] != "activo":
        raise HTTPException(409, "Solo se invita al portal a empleados activos")
    correo = (datos.correo if datos and datos.correo else emp["correo"] or "").strip()
    if not correo:
        raise HTTPException(409, "El empleado no tiene correo: regístrelo en su ficha")
    uid = ctx.conn.execute("select public.buscar_usuario_por_correo(%s, %s) as id", (correo, ctx.empresa_id)).fetchone()["id"]
    estado = "vinculado"
    if not uid:
        if not (settings.supabase_url and settings.supabase_service_key):
            raise HTTPException(409, "El correo no tiene cuenta y las invitaciones no están configuradas")
        uid = _invitar_supabase(correo, {"empresa_id": str(ctx.empresa_id), "empleado_id": str(id), "rol": "empleado"},
                                "/portal")
        estado = "invitado"
    otro = ctx.conn.execute("select id from empleados where empresa_id = %s and usuario_id = %s and id <> %s",
                            (ctx.empresa_id, uid, id)).fetchone()
    if otro:
        raise HTTPException(409, "Esa cuenta ya está vinculada a otro empleado de la empresa")
    cambios = {"usuario_id": uid}
    if not emp["correo"]:
        cambios["correo"] = correo
    crud.actualizar(ctx.conn, "empleados", id, cambios, ctx.empresa_id)
    return {"estado": estado, "usuario_id": uid, "correo": correo, "enlace": f"{settings.app_url}/portal"}


# ============================================================ Portal ==========
CAMPOS_PERFIL = ("id", "empresa_id", "tipo_identificacion", "identificacion", "nombre", "apellidos", "correo", "telefono",
                 "fecha_nacimiento", "fecha_ingreso", "estado", "puesto", "frecuencia", "salario_mensual", "banco", "iban",
                 "saldo_vacaciones", "hijos", "conyuge")


def _mis_fichas(conn: Conexion, u: Usuario) -> list[dict]:
    """Fichas del usuario como empleado (por RLS: empleado_ve_su_ficha)."""
    return conn.execute("select * from empleados where usuario_id = %s order by estado, fecha_ingreso desc",
                        (u.id,)).fetchall()


def _ficha(conn: Conexion, u: Usuario, empleado_id: UUID | None) -> dict:
    fichas = _mis_fichas(conn, u)
    if empleado_id:
        fichas = [f for f in fichas if f["id"] == empleado_id]
    if not fichas:
        raise HTTPException(404, "No tiene acceso al portal del empleado")
    return fichas[0]


@portal.get("/perfil", summary="Ficha del empleado (y sus otros empleos en la plataforma)")
def perfil(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion),
           empleado_id: UUID | None = None) -> dict:
    f = _ficha(conn, u, empleado_id)
    empleos = conn.execute("select * from public.portal_empleos()").fetchall()
    nombres = {e["empleado_id"]: e["empresa_nombre"] for e in empleos}
    return {**{k: f.get(k) for k in CAMPOS_PERFIL}, "empresa_nombre": nombres.get(f["id"]),
            "empleos": [{"empleado_id": e["empleado_id"], "empresa_id": e["empresa_id"], "empresa": e["empresa_nombre"]}
                        for e in empleos]}


def _planillas_portal(conn) -> dict:
    return {p["planilla_id"]: p for p in conn.execute("select * from public.portal_planillas()").fetchall()}


@portal.get("/boletas", summary="Boletas del empleado de planillas cerradas")
def mis_boletas(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion),
                empleado_id: UUID | None = None, anio: int | None = None) -> list[dict]:
    pls = _planillas_portal(conn)
    q = sql.SQL("""select b.id, b.planilla_id, b.empleado_id, b.empresa_id, b.salario_base, b.horas_extra, b.vacaciones, b.bonos,
                  b.bruto, b.ccss_obrero, b.renta, b.total_deducciones, b.neto
             from boletas b join empleados e on e.id = b.empleado_id
            where e.usuario_id = %s""")
    p: list = [u.id]
    if empleado_id:
        q += sql.SQL(" and b.empleado_id = %s"); p.append(empleado_id)
    salida = []
    for b in conn.execute(q, p).fetchall():
        pl = pls.get(b["planilla_id"])
        if not pl or (anio and pl["hasta"].year != anio):
            continue
        salida.append({**b, "empresa_nombre": pl["empresa_nombre"], "tipo": pl["tipo"], "desde": pl["desde"],
                       "hasta": pl["hasta"], "fecha_pago": pl["fecha_pago"]})
    return sorted(salida, key=lambda x: (x["hasta"], x["desde"]), reverse=True)


@portal.get("/boletas/{id}/pdf", response_class=Response, summary="PDF de una boleta propia")
def mi_boleta_pdf(id: UUID, u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion)) -> Response:
    # RLS (empleado_ve_sus_boletas) solo deja ver boletas propias de planillas cerradas.
    b = conn.execute("select b.* from boletas b join empleados e on e.id = b.empleado_id "
                     "where b.id = %s and e.usuario_id = %s", (id, u.id)).fetchone()
    pl = _planillas_portal(conn).get(b["planilla_id"]) if b else None
    if not b or not pl:
        raise HTTPException(404, "Boleta no encontrada")
    # Misma salida que planillas._pdf_boleta, pero con el encabezado que expone portal_planillas()
    # (el empleado no tiene acceso RLS a empresas ni planillas).
    empleado = conn.execute("select * from empleados where id = %s", (b["empleado_id"],)).fetchone()
    lineas = conn.execute("select * from boleta_lineas where boleta_id = %s order by orden", (id,)).fetchall()
    empresa = {"nombre": pl["empresa_nombre"], "cedula_juridica": pl["cedula_juridica"]}
    planilla = {"desde": pl["desde"], "hasta": pl["hasta"], "fecha_pago": pl["fecha_pago"]}
    contenido = pdf.boleta_pdf(empresa, planilla, empleado, b, lineas, pl["nota_boleta"])
    return Response(contenido, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="boleta-{pl["hasta"]}.pdf"'})


@portal.get("/vacaciones", summary="Saldo y solicitudes de vacaciones")
def mis_vacaciones(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion),
                   empleado_id: UUID | None = None) -> dict:
    f = _ficha(conn, u, empleado_id)
    movimientos = conn.execute("select * from vacaciones where empleado_id = %s order by creado_en desc", (f["id"],)).fetchall()
    acumulado = conn.execute("select fecha, dias, saldo_resultante from vacaciones_acumuladas where empleado_id = %s "
                             "order by fecha desc limit 24", (f["id"],)).fetchall()
    return {"empleado_id": f["id"], "saldo": f["saldo_vacaciones"], "solicitudes": movimientos, "acumulaciones": acumulado}


class SolicitudVacacionesIn(_Base):
    dias: Decimal = Field(gt=0, le=60)
    desde: date
    hasta: date
    empleado_id: UUID | None = None

    @field_validator("hasta")
    @classmethod
    def _orden(cls, v, info):
        if "desde" in info.data and v < info.data["desde"]:
            raise ValueError("La fecha final no puede ser anterior a la inicial")
        return v


@portal.post("/vacaciones", status_code=201, summary="Solicitar vacaciones (las aprueba la empresa)")
def solicitar_vacaciones(datos: SolicitudVacacionesIn, u: Usuario = Depends(usuario_actual),
                         conn: Conexion = Depends(conexion)) -> dict:
    f = _ficha(conn, u, datos.empleado_id)
    if f["estado"] != "activo":
        raise HTTPException(409, "Su ficha no está activa")
    if datos.dias > Decimal((datos.hasta - datos.desde).days + 1):
        raise HTTPException(422, "Los días solicitados superan el rango de fechas")
    if datos.dias > Decimal(f["saldo_vacaciones"]):
        raise HTTPException(409, f"Solicita {datos.dias} días y su saldo es {f['saldo_vacaciones']}")
    return crud.insertar(conn, "vacaciones", {"empresa_id": f["empresa_id"], "empleado_id": f["id"], "tipo": "disfrute",
                                              "desde": datos.desde, "hasta": datos.hasta, "dias": datos.dias,
                                              "estado": "solicitada"})
