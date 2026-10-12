"""Operaciones genéricas sobre tablas de una empresa (RLS hace el resto)."""

import re
from datetime import date
from typing import Any, LiteralString, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg import sql
from pydantic import BaseModel, create_model

from .auth import Contexto, contexto_empresa, operador
from .db import Conexion

# Tablas que se pueden tocar con estas operaciones genéricas. Los nombres llegan siempre
# desde el código (nunca del cliente), pero igual se validan antes de pasar a sql.Identifier.
TABLAS = frozenset({
    "aguinaldos", "asientos", "asignaciones_contador", "boleta_lineas", "boletas", "bonos", "calendarios_pago",
    "documentos_empleado", "embargos", "empleados", "empresas", "feriados", "historial_salarial", "horas_extra",
    "incapacidades", "liquidaciones", "marcas_asistencia", "movimientos_empleado", "otras_deducciones",
    "pensiones_alimentarias", "permisos", "planillas", "prestamo_pagos", "prestamos", "rebajos_renta", "socios",
    "sucursales", "tickets", "tipos_horario", "usuarios_empresa", "vacaciones",
    "negocio.cambios_normativos", "negocio.clientes", "negocio.comprobantes", "negocio.gastos",
})

# Órdenes permitidos para los listados genéricos: fragmentos SQL escritos a mano.
ORDENES: dict[str, LiteralString] = {
    "creado_en desc": "creado_en desc",
    "desde desc": "desde desc",
    "entrada desc": "entrada desc",
    "fecha desc": "fecha desc",
    "nombre": "nombre",
}

# Columnas de fecha por las que pueden filtrar los listados genéricos (desde / hasta).
CAMPOS_FECHA = frozenset({"desde", "entrada", "fecha"})

# Las columnas salen de modelos Pydantic (extra="forbid"); además deben ser identificadores simples.
_COLUMNA = re.compile(r"[a-z_][a-z0-9_]*")


def _tabla(tabla: str) -> sql.Identifier:
    if tabla not in TABLAS:
        raise ValueError(f"Tabla no permitida: {tabla}")
    esquema, nombre = tabla.split(".") if "." in tabla else ("public", tabla)
    return sql.Identifier(esquema, nombre)


def _columnas(nombres) -> list[str]:
    cols = list(nombres)
    malas = [c for c in cols if not isinstance(c, str) or not _COLUMNA.fullmatch(c)]
    if malas:
        raise ValueError(f"Columnas no permitidas: {malas}")
    return cols


def insertar(conn: Conexion, tabla: str, datos: dict[str, Any]) -> dict:
    cols = _columnas(datos)
    q = sql.SQL("insert into {} ({}) values ({}) returning *").format(
        _tabla(tabla), sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        sql.SQL(", ").join(sql.Placeholder() for _ in cols))
    return conn.execute(q, [datos[c] for c in cols]).fetchone()


def actualizar(conn: Conexion, tabla: str, id_: Any, datos: dict[str, Any],
               empresa_id: UUID | None = None) -> dict | None:
    ident = _tabla(tabla)
    if not datos:
        return conn.execute(sql.SQL("select * from {} where id = %s").format(ident), (id_,)).fetchone()
    sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in _columnas(datos))
    cond = sql.SQL("id = %s") + (sql.SQL(" and empresa_id = %s") if empresa_id else sql.SQL(""))
    q = sql.SQL("update {} set {} where {} returning *").format(ident, sets, cond)
    params = list(datos.values()) + [id_] + ([empresa_id] if empresa_id else [])
    return conn.execute(q, params).fetchone()


def obtener(conn, tabla: str, id_: Any, empresa_id: UUID | None = None) -> dict:
    cond = sql.SQL("id = %s") + (sql.SQL(" and empresa_id = %s") if empresa_id else sql.SQL(""))
    fila = conn.execute(sql.SQL("select * from {} where {}").format(_tabla(tabla), cond),
                        [id_] + ([empresa_id] if empresa_id else [])).fetchone()
    if not fila:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No encontrado")
    return fila


def parcial(modelo: type[BaseModel]) -> type[BaseModel]:
    campos = {n: (Optional[f.annotation], None) for n, f in modelo.model_fields.items()}
    return create_model(f"{modelo.__name__}Parcial", __config__=modelo.model_config, **campos)


def crud_router(ruta: str, tabla: str, modelo_in: type[BaseModel], *, modelo_upd: type[BaseModel] | None = None,
                filtros: tuple[str, ...] = ("empleado_id", "estado"), campo_fecha: str | None = None,
                orden: str = "creado_en desc", etiqueta: str = "",
                estado_pendiente: str | None = None) -> APIRouter:
    """Lista / crea / obtiene / edita / borra filas de `tabla` para una empresa.

    `estado_pendiente`: la tabla tiene flujo de aprobación. Quien no es aprobador
    crea siempre en ese estado, no puede cambiar el estado, y si edita algo ya
    aprobado vuelve a quedar pendiente. Lo ya pagado en una boleta no se edita ni
    se borra (evita pagarlo dos veces)."""
    APROBADOS = {"aprobada", "aprobado"}
    if orden not in ORDENES:
        raise ValueError(f"Orden no permitido: {orden}")
    if campo_fecha is not None and campo_fecha not in CAMPOS_FECHA:
        raise ValueError(f"Campo de fecha no permitido: {campo_fecha}")
    ident = _tabla(tabla)
    orden_sql = sql.SQL(ORDENES[orden])

    def _pagado(fila: dict) -> bool:
        return fila.get("boleta_id") is not None or fila.get("estado") in ("pagada", "pagado")
    r = APIRouter(prefix=f"/empresas/{{empresa_id}}/{ruta}", tags=[etiqueta or ruta])
    Upd = modelo_upd or parcial(modelo_in)

    @r.get("", summary=f"Listar {ruta}")
    def listar(ctx: Contexto = Depends(contexto_empresa), empleado_id: UUID | None = None, estado: str | None = None,
               desde: date | None = None, hasta: date | None = None,
               limite: int = Query(500, le=5000), desplazamiento: int = 0) -> list[dict]:
        conds: list[sql.Composable] = [sql.SQL("empresa_id = %s")]
        params: list[Any] = [ctx.empresa_id]
        if empleado_id and "empleado_id" in filtros:
            conds.append(sql.SQL("empleado_id = %s")); params.append(empleado_id)
        if estado and "estado" in filtros:
            conds.append(sql.SQL("estado = %s")); params.append(estado)
        if campo_fecha and desde:
            conds.append(sql.SQL("{} >= %s").format(sql.Identifier(campo_fecha))); params.append(desde)
        if campo_fecha and hasta:
            conds.append(sql.SQL("{} <= %s").format(sql.Identifier(campo_fecha))); params.append(hasta)
        q = sql.SQL("select * from {} where {} order by {} limit %s offset %s").format(
            ident, sql.SQL(" and ").join(conds), orden_sql)
        return ctx.conn.execute(q, params + [limite, desplazamiento]).fetchall()

    @r.post("", status_code=201, summary=f"Crear en {ruta}")
    def crear(datos: modelo_in, ctx: Contexto = Depends(operador)) -> dict:  # type: ignore[valid-type]
        d = datos.model_dump()
        if estado_pendiente and not ctx.puede_aprobar and d.get("estado") not in (None, estado_pendiente):
            d["estado"] = estado_pendiente
        return insertar(ctx.conn, tabla, {**d, "empresa_id": ctx.empresa_id})

    @r.get("/{id}", summary=f"Obtener de {ruta}")
    def ver(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> dict:
        return obtener(ctx.conn, tabla, id, ctx.empresa_id)

    @r.patch("/{id}", summary=f"Editar en {ruta}")
    def editar(id: UUID, datos: Upd, ctx: Contexto = Depends(operador)) -> dict:  # type: ignore[valid-type]
        cambios = datos.model_dump(exclude_unset=True)
        if estado_pendiente:
            actual = obtener(ctx.conn, tabla, id, ctx.empresa_id)
            if _pagado(actual):
                raise HTTPException(409, "Ya se pagó en una planilla: no se puede modificar")
            if not ctx.puede_aprobar:
                if "estado" in cambios and cambios["estado"] != actual.get("estado"):
                    raise HTTPException(403, "Solo un aprobador cambia el estado")
                if cambios and actual.get("estado") in APROBADOS:
                    cambios["estado"] = estado_pendiente
        fila = actualizar(ctx.conn, tabla, id, cambios, ctx.empresa_id)
        if not fila:
            raise HTTPException(404, "No encontrado o sin permiso")
        return fila

    @r.delete("/{id}", status_code=204, summary=f"Borrar de {ruta}")
    def borrar(id: UUID, ctx: Contexto = Depends(operador)) -> None:
        if estado_pendiente and _pagado(obtener(ctx.conn, tabla, id, ctx.empresa_id)):
            raise HTTPException(409, "Ya se pagó en una planilla: no se puede borrar")
        n = ctx.conn.execute(sql.SQL("delete from {} where id = %s and empresa_id = %s").format(ident),
                             (id, ctx.empresa_id)).rowcount
        if not n:
            raise HTTPException(404, "No encontrado o sin permiso")

    return r
