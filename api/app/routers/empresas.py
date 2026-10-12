"""Sesión, empresas, usuarios, contadores asignados, configuración y catálogos."""
from uuid import UUID

import httpx
import psycopg
from fastapi import APIRouter, Depends, HTTPException
from psycopg import sql

from .. import crud
from ..auth import (
    Contexto,
    Usuario,
    admin_empresa,
    conexion,
    contexto_empresa,
    usuario_actual,
)
from ..config import settings
from ..db import Conexion
from ..esquemas import (
    AsignacionIn,
    CalendarioIn,
    CambioModuloIn,
    ConfiguracionIn,
    EmpresaIn,
    FeriadoIn,
    HorarioIn,
    ModuloCodigo,
    NuevaEmpresa,
    SucursalIn,
    UsuarioEmpresaIn,
    UsuarioEmpresaUpd,
)

r = APIRouter(tags=["empresas"])


@r.get("/yo", summary="Usuario actual, sus empresas y perfiles")
def yo(u: Usuario = Depends(usuario_actual), conn: Conexion = Depends(conexion)) -> dict:
    empresas = conn.execute("select * from public.mis_empresas()").fetchall()
    es_plataforma = conn.execute("select public.es_admin_plataforma() as ok").fetchone()["ok"]
    socio = conn.execute("select id, tipo, nombre, codigo_referido, estado from socios where usuario_id = %s",
                         (u.id,)).fetchone()
    empleos = conn.execute("select * from public.mis_empleos()").fetchall()
    return {"id": u.id, "correo": u.email, "empresas": empresas, "es_plataforma": es_plataforma,
            "socio": socio, "empleos": empleos}


@r.post("/empresas", status_code=201, summary="Registrar una empresa nueva (queda como administrador)")
def crear_empresa(datos: NuevaEmpresa, conn: Conexion = Depends(conexion)) -> dict:
    eid = conn.execute("select public.crear_empresa(%s, %s, %s, %s, %s) as id",
                       (datos.nombre, datos.cedula_juridica, datos.codigo_referido, datos.frecuencia,
                        list(dict.fromkeys(datos.modulos)))).fetchone()["id"]
    e = conn.execute("select * from empresas where id = %s", (eid,)).fetchone()
    return {**e, "modulos": conn.execute("select public.modulos_activos(%s) as m", (eid,)).fetchone()["m"]}


@r.get("/empresas/{empresa_id}")
def ver_empresa(ctx: Contexto = Depends(contexto_empresa)) -> dict:
    e = crud.obtener(ctx.conn, "empresas", ctx.empresa_id)
    modulos = ctx.conn.execute("select public.modulos_activos(%s) as m", (ctx.empresa_id,)).fetchone()["m"]
    return {**e, "mi_rol": ctx.rol, "modulos": modulos}


# ---------------------------------------------------------------- Módulos ---
@r.get("/modulos", summary="Módulos que se pueden contratar, con su precio de lista (sin IVA)")
def catalogo_modulos(conn: Conexion = Depends(conexion)) -> list[dict]:
    return conn.execute("select * from public.modulos where disponible order by orden").fetchall()


@r.get("/empresas/{empresa_id}/modulos", summary="Módulos de la empresa: activos y su historial")
def modulos_empresa(ctx: Contexto = Depends(contexto_empresa)) -> dict:
    activos = ctx.conn.execute("select public.modulos_activos(%s) as m", (ctx.empresa_id,)).fetchone()["m"]
    historial = ctx.conn.execute(
        """select m.modulo, o.nombre, m.desde, m.hasta from public.empresa_modulos m
             join public.modulos o on o.codigo = m.modulo
            where m.empresa_id = %s order by m.desde desc, o.orden""", (ctx.empresa_id,)).fetchall()
    return {"activos": activos, "historial": historial}


@r.put("/empresas/{empresa_id}/modulos/{modulo}", summary="Activar o desactivar un módulo (se cobra desde ese mes)")
def cambiar_modulo(modulo: ModuloCodigo, datos: CambioModuloIn, ctx: Contexto = Depends(admin_empresa)) -> dict:
    activos = ctx.conn.execute("select public.cambiar_modulo(%s, %s, %s) as m",
                               (ctx.empresa_id, modulo, datos.activo)).fetchone()["m"]
    return {"activos": activos}


@r.patch("/empresas/{empresa_id}")
def editar_empresa(datos: EmpresaIn, ctx: Contexto = Depends(admin_empresa)) -> dict:
    return crud.actualizar(ctx.conn, "empresas", ctx.empresa_id, datos.model_dump(exclude_unset=True))


@r.get("/empresas/{empresa_id}/configuracion")
def ver_configuracion(ctx: Contexto = Depends(contexto_empresa)) -> dict:
    fila = ctx.conn.execute("select * from configuracion_planilla where empresa_id = %s", (ctx.empresa_id,)).fetchone()
    if not fila:
        fila = ctx.conn.execute("insert into configuracion_planilla (empresa_id) values (%s) returning *",
                                (ctx.empresa_id,)).fetchone()
    return fila


# Columnas editables de configuracion_planilla: exactamente los campos del modelo de entrada.
CAMPOS_CONFIGURACION = frozenset(ConfiguracionIn.model_fields)


@r.patch("/empresas/{empresa_id}/configuracion")
def editar_configuracion(datos: ConfiguracionIn, ctx: Contexto = Depends(admin_empresa)) -> dict:
    ver_configuracion(ctx)
    cambios = datos.model_dump(exclude_unset=True)
    if not cambios:
        return ver_configuracion(ctx)
    cambios = {k: v for k, v in cambios.items() if k in CAMPOS_CONFIGURACION}
    sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in cambios)
    return ctx.conn.execute(sql.SQL("update configuracion_planilla set {} where empresa_id = %s returning *").format(sets),
                            [*cambios.values(), ctx.empresa_id]).fetchone()


# --------------------------------------------------------------- Usuarios ---
@r.get("/empresas/{empresa_id}/usuarios")
def usuarios(ctx: Contexto = Depends(admin_empresa)) -> list[dict]:
    return ctx.conn.execute("select * from public.usuarios_de_empresa(%s)", (ctx.empresa_id,)).fetchall()


@r.post("/empresas/{empresa_id}/usuarios", status_code=201,
        summary="Agregar usuario por correo (si no tiene cuenta se le envía invitación)")
def agregar_usuario(datos: UsuarioEmpresaIn, ctx: Contexto = Depends(admin_empresa)) -> dict:
    uid = ctx.conn.execute("select public.agregar_usuario_empresa(%s, %s, %s) as id",
                           (ctx.empresa_id, datos.correo, datos.rol)).fetchone()["id"]
    if uid:
        return {"estado": "agregado", "id": uid}
    if not (settings.supabase_url and settings.supabase_service_key):
        raise HTTPException(409, "El correo no tiene cuenta y las invitaciones no están configuradas")
    # Invitación por la API de administración de Supabase Auth (única acción con llave de servicio).
    resp = httpx.post(f"{settings.supabase_url}/auth/v1/invite",
                      headers={"apikey": settings.supabase_service_key,
                               "Authorization": f"Bearer {settings.supabase_service_key}"},
                      json={"email": datos.correo, "data": {"empresa_id": str(ctx.empresa_id), "rol": datos.rol},
                            "redirect_to": f"{settings.app_url}/auth/callback"}, timeout=20)
    if resp.status_code >= 300:
        raise HTTPException(502, f"No se pudo enviar la invitación: {resp.text[:200]}")
    nuevo = resp.json().get("id")
    ctx.conn.execute("insert into usuarios_empresa (empresa_id, usuario_id, rol) values (%s,%s,%s) "
                     "on conflict (empresa_id, usuario_id) do nothing", (ctx.empresa_id, nuevo, datos.rol))
    return {"estado": "invitado", "id": nuevo}


@r.patch("/empresas/{empresa_id}/usuarios/{id}")
def editar_usuario(id: UUID, datos: UsuarioEmpresaUpd, ctx: Contexto = Depends(admin_empresa)) -> dict:
    fila = crud.actualizar(ctx.conn, "usuarios_empresa", id, datos.model_dump(exclude_unset=True), ctx.empresa_id)
    if not fila:
        raise HTTPException(404, "No encontrado")
    return fila


# ------------------------------------------- Contadores / vendedores asignados
@r.get("/empresas/{empresa_id}/contadores", summary="Contadores y vendedores asignados a la empresa")
def contadores(ctx: Contexto = Depends(contexto_empresa), incluir_revocados: bool = False) -> list[dict]:
    revocados = sql.SQL("") if incluir_revocados else sql.SQL("and a.revocado_en is null")
    return ctx.conn.execute(sql.SQL("""
        select a.*, s.nombre, s.tipo, s.correo, s.telefono, s.codigo_referido
          from asignaciones_contador a join socios s on s.id = a.socio_id
         where a.empresa_id = %s {}
         order by a.soporte_principal desc, a.desde""").format(revocados), (ctx.empresa_id,)).fetchall()


@r.post("/empresas/{empresa_id}/contadores", status_code=201,
        summary="Asignar un contador o vendedor (supervisión, soporte, ventas)")
def asignar_contador(datos: AsignacionIn, ctx: Contexto = Depends(admin_empresa)) -> dict:
    socio_id = datos.socio_id
    if not socio_id:
        s = ctx.conn.execute("select * from public.socio_por_codigo(%s)", (datos.codigo_referido,)).fetchone()
        if not s:
            raise HTTPException(404, "No hay un socio activo con ese código")
        socio_id = s["id"]
    if datos.soporte_principal:
        ctx.conn.execute("update asignaciones_contador set soporte_principal = false "
                         "where empresa_id = %s and revocado_en is null", (ctx.empresa_id,))
    try:
        return crud.insertar(ctx.conn, "asignaciones_contador", {
            "empresa_id": ctx.empresa_id, "socio_id": socio_id, "funciones": list(datos.funciones),
            "permiso": datos.permiso, "soporte_principal": datos.soporte_principal})
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Ese socio ya está asignado a la empresa") from exc


@r.delete("/empresas/{empresa_id}/contadores/{id}", status_code=204, summary="Revocar acceso del contador")
def revocar_contador(id: UUID, u: Usuario = Depends(usuario_actual), ctx: Contexto = Depends(admin_empresa)) -> None:
    n = ctx.conn.execute("update asignaciones_contador set revocado_en = now(), revocado_por = %s "
                         "where id = %s and empresa_id = %s and revocado_en is null",
                         (u.id, id, ctx.empresa_id)).rowcount
    if not n:
        raise HTTPException(404, "Asignación no encontrada")


# --------------------------------------------------------------- Auditoría --
@r.get("/empresas/{empresa_id}/auditoria")
def auditoria(ctx: Contexto = Depends(admin_empresa), tabla: str | None = None, limite: int = 200) -> list[dict]:
    q = sql.SQL("select a.*, public.correo_usuario(a.usuario_id) as correo from auditoria a where empresa_id = %s")
    params: list = [ctx.empresa_id]
    if tabla:
        q += sql.SQL(" and tabla = %s"); params.append(tabla)
    return ctx.conn.execute(q + sql.SQL(" order by id desc limit %s"), params + [min(limite, 2000)]).fetchall()


@r.get("/empresas/{empresa_id}/feriados", tags=["catalogos"])
def feriados(ctx: Contexto = Depends(contexto_empresa), anio: int | None = None) -> list[dict]:
    q = sql.SQL("select * from feriados where (empresa_id is null or empresa_id = %s)")
    params: list = [ctx.empresa_id]
    if anio:
        q += sql.SQL(" and extract(year from fecha) = %s"); params.append(anio)
    return ctx.conn.execute(q + sql.SQL(" order by fecha"), params).fetchall()


@r.post("/empresas/{empresa_id}/feriados", status_code=201, tags=["catalogos"])
def crear_feriado(datos: FeriadoIn, ctx: Contexto = Depends(admin_empresa)) -> dict:
    return crud.insertar(ctx.conn, "feriados", {**datos.model_dump(), "empresa_id": ctx.empresa_id})


@r.get("/parametros-legales", tags=["catalogos"])
def parametros(conn: Conexion = Depends(conexion)) -> list[dict]:
    return conn.execute("select * from parametros_legales order by vigente_desde desc").fetchall()


catalogos = [
    crud.crud_router("sucursales", "sucursales", SucursalIn, filtros=(), orden="nombre", etiqueta="catalogos"),
    crud.crud_router("calendarios", "calendarios_pago", CalendarioIn, filtros=(), orden="nombre", etiqueta="catalogos"),
    crud.crud_router("horarios", "tipos_horario", HorarioIn, filtros=(), orden="nombre", etiqueta="catalogos"),
]
