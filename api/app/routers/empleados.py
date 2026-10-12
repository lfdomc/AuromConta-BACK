"""Empleados, historial salarial y aumento masivo."""
from decimal import Decimal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from planilla_motor.dinero import r2
from psycopg import sql

from .. import crud
from ..auth import Contexto, aprobador, contexto_empresa, operador, requiere_modulo
from ..esquemas import (
    AumentoMasivoIn,
    CambioSalarialIn,
    DecisionIn,
    DocumentoIn,
    EmpleadoIn,
    EmpleadoUpd,
    MovimientoIn,
)
from ..fechas import hoy_cr

r = APIRouter(prefix="/empresas/{empresa_id}/empleados", tags=["empleados"], dependencies=[Depends(requiere_modulo("planilla"))])


@r.get("")
def listar(ctx: Contexto = Depends(contexto_empresa), estado: str | None = "activo", buscar: str | None = None,
           sucursal_id: UUID | None = None) -> list[dict]:
    q = sql.SQL("""select e.*, s.nombre as sucursal from empleados e left join sucursales s on s.id = e.sucursal_id
           where e.empresa_id = %s""")
    p: list = [ctx.empresa_id]
    if estado:
        q += sql.SQL(" and e.estado = %s"); p.append(estado)
    if sucursal_id:
        q += sql.SQL(" and e.sucursal_id = %s"); p.append(sucursal_id)
    if buscar:
        q += sql.SQL(" and (e.nombre || ' ' || e.apellidos || ' ' || e.identificacion) ilike %s"); p.append(f"%{buscar}%")
    return ctx.conn.execute(q + sql.SQL(" order by e.apellidos, e.nombre"), p).fetchall()


@r.post("", status_code=201)
def crear(datos: EmpleadoIn, ctx: Contexto = Depends(operador)) -> dict:
    try:
        emp = crud.insertar(ctx.conn, "empleados", {**datos.model_dump(), "empresa_id": ctx.empresa_id})
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(409, "Ya existe un empleado con esa identificación") from exc
    crud.insertar(ctx.conn, "movimientos_empleado", {"empresa_id": ctx.empresa_id, "empleado_id": emp["id"],
                                                     "tipo": "ingreso", "fecha": datos.fecha_ingreso,
                                                     "detalle": f"Ingreso como {datos.puesto or 'colaborador'}"})
    return emp


@r.get("/{id}")
def ver(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    emp = crud.obtener(ctx.conn, "empleados", id, ctx.empresa_id)
    emp["historial_salarial"] = ctx.conn.execute(
        "select * from historial_salarial where empleado_id = %s order by fecha_efectiva desc", (id,)).fetchall()
    emp["prestamos_activos"] = ctx.conn.execute(
        "select * from prestamos where empleado_id = %s and estado = 'activo'", (id,)).fetchall()
    return emp


@r.patch("/{id}")
def editar(id: UUID, datos: EmpleadoUpd, ctx: Contexto = Depends(operador)) -> dict:
    fila = crud.actualizar(ctx.conn, "empleados", id, datos.model_dump(exclude_unset=True), ctx.empresa_id)
    if not fila:
        raise HTTPException(404, "No encontrado")
    return fila


@r.delete("/{id}", status_code=204, summary="Borrar (solo si no tiene boletas; si no, márquelo inactivo)")
def borrar(id: UUID, ctx: Contexto = Depends(operador)) -> None:
    try:
        n = ctx.conn.execute("delete from empleados where id = %s and empresa_id = %s", (id, ctx.empresa_id)).rowcount
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(409, "El empleado tiene boletas: márquelo como inactivo") from exc
    if not n:
        raise HTTPException(404, "No encontrado")


# ------------------------------------------------------- Historial salarial --
def _aplicar_si_vigente(conn, cambio: dict) -> None:
    if cambio["estado"] == "autorizado" and cambio["fecha_efectiva"] <= hoy_cr():
        conn.execute("update empleados set salario_mensual = %s where id = %s",
                     (cambio["salario_nuevo"], cambio["empleado_id"]))


@r.post("/{id}/salario", status_code=201, summary="Solicitar cambio salarial (lo autoriza un aprobador)")
def cambio_salarial(id: UUID, datos: CambioSalarialIn, ctx: Contexto = Depends(operador)) -> dict:
    emp = crud.obtener(ctx.conn, "empleados", id, ctx.empresa_id)
    cambio = crud.insertar(ctx.conn, "historial_salarial", {
        "empresa_id": ctx.empresa_id, "empleado_id": id, "fecha_efectiva": datos.fecha_efectiva,
        "salario_anterior": emp["salario_mensual"], "salario_nuevo": datos.salario_nuevo, "motivo": datos.motivo})
    return cambio


@r.post("/historial/{cambio_id}/decision", summary="Autorizar o rechazar un cambio salarial")
def decidir_cambio(cambio_id: UUID, datos: DecisionIn, ctx: Contexto = Depends(aprobador)) -> dict:
    cambio = ctx.conn.execute("update historial_salarial set estado = %s where id = %s and empresa_id = %s "
                              "and estado = 'pendiente' returning *",
                              ("autorizado" if datos.aprobar else "rechazado", cambio_id, ctx.empresa_id)).fetchone()
    if not cambio:
        raise HTTPException(404, "Cambio no encontrado o ya decidido")
    _aplicar_si_vigente(ctx.conn, cambio)
    return cambio


@r.post("/aumento-masivo", summary="Aumento salarial masivo (vista previa o aplicar)")
def aumento_masivo(datos: AumentoMasivoIn, ctx: Contexto = Depends(operador)) -> dict:
    q = sql.SQL("select id, nombre, apellidos, salario_mensual from empleados where empresa_id = %s and estado = 'activo'")
    p: list = [ctx.empresa_id]
    if datos.empleado_ids:
        q += sql.SQL(" and id = any(%s)"); p.append(list(datos.empleado_ids))
    filas = []
    for e in ctx.conn.execute(q, p).fetchall():
        actual = Decimal(e["salario_mensual"])
        nuevo = r2(actual * (1 + datos.porcentaje / 100)) if datos.porcentaje is not None else r2(actual + datos.monto)
        filas.append({"empleado_id": e["id"], "nombre": f"{e['nombre']} {e['apellidos']}".strip(),
                      "salario_actual": actual, "salario_nuevo": nuevo, "diferencia": nuevo - actual})
    if datos.aplicar:
        auto = ctx.puede_aprobar
        for f in filas:
            cambio = crud.insertar(ctx.conn, "historial_salarial", {
                "empresa_id": ctx.empresa_id, "empleado_id": f["empleado_id"], "fecha_efectiva": datos.fecha_efectiva,
                "salario_anterior": f["salario_actual"], "salario_nuevo": f["salario_nuevo"], "motivo": datos.motivo})
            if auto:
                cambio = ctx.conn.execute("update historial_salarial set estado='autorizado' where id=%s returning *",
                                          (cambio["id"],)).fetchone()
                _aplicar_si_vigente(ctx.conn, cambio)
    return {"aplicado": datos.aplicar, "requiere_aprobacion": datos.aplicar and not ctx.puede_aprobar,
            "total_diferencia_mensual": sum((f["diferencia"] for f in filas), Decimal(0)), "empleados": filas}


# ------------------------------------------------- Documentos y movimientos --
@r.get("/{id}/documentos")
def documentos(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> list[dict]:
    return ctx.conn.execute("select * from documentos_empleado where empleado_id = %s and empresa_id = %s "
                            "order by vence nulls last", (id, ctx.empresa_id)).fetchall()


@r.post("/{id}/documentos", status_code=201)
def crear_documento(id: UUID, datos: DocumentoIn, ctx: Contexto = Depends(operador)) -> dict:
    return crud.insertar(ctx.conn, "documentos_empleado", {**datos.model_dump(), "empresa_id": ctx.empresa_id,
                                                           "empleado_id": id})


@r.get("/{id}/movimientos")
def movimientos(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> list[dict]:
    return ctx.conn.execute("select * from movimientos_empleado where empleado_id = %s and empresa_id = %s "
                            "order by fecha desc", (id, ctx.empresa_id)).fetchall()


@r.post("/{id}/movimientos", status_code=201)
def crear_movimiento(id: UUID, datos: MovimientoIn, ctx: Contexto = Depends(operador)) -> dict:
    return crud.insertar(ctx.conn, "movimientos_empleado", {**datos.model_dump(), "empresa_id": ctx.empresa_id,
                                                            "empleado_id": id})
