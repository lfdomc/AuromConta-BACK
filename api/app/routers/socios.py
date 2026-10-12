"""Fase 8 · Portal de socios (contadores y vendedores): perfil, clientes asignados,
comisiones, liquidaciones, tickets de sus clientes y prospectos con atribución de 90 días."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..auth import Usuario, conexion, usuario_actual
from ..config import settings
from ..db import Conexion
from ..fechas import hoy_cr
from .autogestion import tickets_soporte

r = APIRouter(prefix="/socios", tags=["socios"])


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _socio(conn: Conexion, u: Usuario) -> dict:
    """Ficha del socio del usuario (RLS: el socio ve la propia)."""
    s = conn.execute("select * from socios where usuario_id = %s", (u.id,)).fetchone()
    if not s:
        raise HTTPException(403, "Su cuenta no está registrada como contador o vendedor")
    if s["estado"] != "activo":
        raise HTTPException(403, f"Su cuenta de socio está {s['estado']}")
    return s


def enlace_registro(codigo: str) -> str:
    return f"{settings.app_url}/registro?ref={codigo}"


def _mes(valor: str | None) -> date | None:
    if not valor:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", valor.strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise HTTPException(422, "El mes debe tener el formato AAAA-MM")
    return date(int(m.group(1)), int(m.group(2)), 1)


# ------------------------------------------------------------------ Perfil --
@r.get("/yo", summary="Perfil del socio, código de referido, enlace de registro y resumen de comisiones")
def yo(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion)) -> dict:
    s = _socio(conn, u)
    resumen = conn.execute("""
        select coalesce(sum(monto) filter (where estado = 'pendiente'), 0) as pendiente,
               coalesce(sum(monto) filter (where estado = 'liquidada'), 0) as liquidado,
               coalesce(sum(monto) filter (where estado <> 'anulada' and periodo = date_trunc('month', current_date)::date), 0) as mes_actual,
               coalesce(sum(monto) filter (where estado <> 'anulada' and periodo >= date_trunc('year', current_date)::date), 0) as anio_actual
          from comisiones where socio_id = %s""", (s["id"],)).fetchone()
    clientes = conn.execute("select count(distinct empresa_id)::int as n from asignaciones_contador "
                            "where socio_id = %s and revocado_en is null", (s["id"],)).fetchone()["n"]
    prospectos = conn.execute("select count(*)::int as n from prospectos where socio_id = %s and estado = 'abierto'",
                              (s["id"],)).fetchone()["n"]
    return {**s, "enlace_registro": enlace_registro(s["codigo_referido"]), "telegram_vinculado": bool(s["telegram_chat_id"]),
            "comisiones": resumen, "clientes": clientes, "prospectos_abiertos": prospectos}


class PerfilUpd(_Base):
    telefono: str | None = Field(None, max_length=30)
    iban: str | None = None
    telegram_chat_id: str | None = Field(None, max_length=40, description="Mejor use POST /telegram/vincular")
    nombre: str | None = Field(None, min_length=2)
    cedula: str | None = None

    @field_validator("iban")
    @classmethod
    def _iban(cls, v):
        if v is None or v == "":
            return None
        limpio = v.replace(" ", "").upper()
        if not re.fullmatch(r"CR\d{20}", limpio):
            raise ValueError("El IBAN costarricense tiene la forma CR + 20 dígitos")
        # Verificación ISO 7064 (mod 97)
        num = "".join(str(int(c, 36)) for c in limpio[4:] + limpio[:4])
        if int(num) % 97 != 1:
            raise ValueError("El IBAN no es válido (dígito verificador)")
        return limpio

    @field_validator("telegram_chat_id")
    @classmethod
    def _chat(cls, v):
        if v not in (None, "") and not re.fullmatch(r"-?\d{3,20}", v):
            raise ValueError("El chat de Telegram debe ser numérico")
        return v or None


# Columnas de socios que el propio socio puede editar: exactamente los campos de PerfilUpd.
CAMPOS_PERFIL = frozenset(PerfilUpd.model_fields)


@r.patch("/yo", summary="Actualizar teléfono, IBAN o Telegram")
def editar_yo(datos: PerfilUpd, u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion)) -> dict:
    s = _socio(conn, u)
    cambios = {k: v for k, v in datos.model_dump(exclude_unset=True).items() if k in CAMPOS_PERFIL}
    if cambios:
        sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in cambios)
        conn.execute(sql.SQL("update socios set {} where id = %s").format(sets), [*cambios.values(), s["id"]])
    return yo(u, conn)


# ---------------------------------------------------------------- Clientes --
def clientes_de_socio(conn: Conexion) -> list[dict]:
    return conn.execute("""
        select a.empresa_id, e.nombre, e.estado_servicio, e.plan, a.funciones, a.permiso, a.soporte_principal, a.desde,
               (select count(*)::int from empleados x where x.empresa_id = e.id and x.estado = 'activo') as empleados,
               (select count(*)::int from tickets t where t.empresa_id = e.id
                  and t.estado in ('abierto','en_proceso','escalado')) as tickets_abiertos,
               (select json_build_object('id', p.id, 'tipo', p.tipo, 'desde', p.desde, 'hasta', p.hasta, 'estado', p.estado)
                  from planillas p where p.empresa_id = e.id order by p.hasta desc, p.creado_en desc limit 1) as ultima_planilla
          from asignaciones_contador a join empresas e on e.id = a.empresa_id
         where a.socio_id = public.mi_socio_id() and a.revocado_en is null
         order by e.nombre""").fetchall()


@r.get("/clientes", summary="Empresas asignadas al socio")
def clientes(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion)) -> list[dict]:
    _socio(conn, u)
    return clientes_de_socio(conn)


# -------------------------------------------------------------- Comisiones --
@r.get("/comisiones", summary="Comisiones del socio (opcional: mes AAAA-MM)")
def comisiones(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion),
               mes: str | None = None, estado: Literal["pendiente", "liquidada", "anulada"] | None = None) -> list[dict]:
    s = _socio(conn, u)
    q = sql.SQL("""select c.*, e.nombre as empresa_nombre from comisiones c
             left join empresas e on e.id = c.empresa_id
            where c.socio_id = %s""")
    p: list = [s["id"]]
    periodo = _mes(mes)
    if periodo:
        q += sql.SQL(" and c.periodo = %s"); p.append(periodo)
    if estado:
        q += sql.SQL(" and c.estado = %s"); p.append(estado)
    return conn.execute(q + sql.SQL(" order by c.periodo desc, e.nombre"), p).fetchall()


@r.get("/liquidaciones", summary="Liquidaciones (pagos) de comisiones al socio")
def liquidaciones(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion)) -> list[dict]:
    s = _socio(conn, u)
    return conn.execute("""select l.*, (select count(*)::int from comisiones c where c.liquidacion_id = l.id) as comisiones
                             from liquidaciones_socio l where l.socio_id = %s order by l.mes desc""", (s["id"],)).fetchall()


@r.get("/tickets", summary="Tickets de los clientes asignados")
def tickets(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion),
            estado: str | None = Query(None, description="abiertos (defecto) | todos | un estado")) -> list[dict]:
    _socio(conn, u)
    return tickets_soporte(conn, solo_socio=True, estado=estado)


# -------------------------------------------------------------- Prospectos --
class ProspectoIn(_Base):
    empresa_nombre: str = Field(min_length=2, max_length=200)
    contacto: str | None = None
    correo: str | None = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    telefono: str | None = None


@r.get("/prospectos", summary="Prospectos del socio")
def prospectos(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion),
               estado: str | None = None) -> list[dict]:
    s = _socio(conn, u)
    q, p = sql.SQL("select *, (vence_atribucion - current_date) as dias_restantes from prospectos where socio_id = %s"), [s["id"]]
    if estado:
        q += sql.SQL(" and estado = %s"); p.append(estado)
    return conn.execute(q + sql.SQL(" order by registrado_en desc"), p).fetchall()


@r.post("/prospectos", status_code=201, summary="Registrar un prospecto (atribución por 90 días)")
def crear_prospecto(datos: ProspectoIn, u: Usuario = Depends(usuario_actual),
                    conn: Conexion = Depends(conexion)) -> dict:
    s = _socio(conn, u)
    nombre = " ".join(datos.empresa_nombre.split())
    ocupado = conn.execute("select public.prospecto_ocupado(%s) as vence", (nombre,)).fetchone()["vence"]
    if ocupado:
        raise HTTPException(409, f"Otro socio ya registró ese prospecto; su atribución vence el {ocupado:%d/%m/%Y}")
    propio = conn.execute("""select vence_atribucion from prospectos where socio_id = %s and estado = 'abierto'
                              and public.sin_tildes(trim(empresa_nombre)) = public.sin_tildes(%s)""", (s["id"], nombre)).fetchone()
    if propio:
        raise HTTPException(409, f"Ya tiene ese prospecto registrado (atribución hasta {propio['vence_atribucion']:%d/%m/%Y})")
    fila = conn.execute("""insert into prospectos (socio_id, empresa_nombre, contacto, correo, telefono)
                           values (%s,%s,%s,%s,%s) returning *""",
                        (s["id"], nombre, datos.contacto, datos.correo, datos.telefono)).fetchone()
    return {**fila, "dias_restantes": (fila["vence_atribucion"] - hoy_cr()).days,
            "enlace_registro": enlace_registro(s["codigo_referido"])}
