"""Novedades del periodo y obligaciones del empleado (Fase 2)."""
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from planilla_motor.dinero import r2

from .. import crud
from ..auth import Contexto, aprobador, operador, requiere_modulo
from ..esquemas import (
    BonoIn,
    EmbargoIn,
    HoraExtraIn,
    IncapacidadIn,
    MarcaIn,
    OtraDeduccionIn,
    PensionIn,
    PermisoIn,
    PrestamoIn,
    RebajoRentaIn,
    VacacionIn,
)

r = APIRouter(prefix="/empresas/{empresa_id}", tags=["novedades"], dependencies=[Depends(requiere_modulo("planilla"))])


def _empleado(ctx: Contexto, empleado_id: UUID) -> dict:
    return crud.obtener(ctx.conn, "empleados", empleado_id, ctx.empresa_id)


def tarifa_hora(ctx: Contexto, emp: dict) -> Decimal:
    if emp["por_asistencia"] and emp["tarifa_hora"]:
        return Decimal(emp["tarifa_hora"])
    horas = Decimal("8")
    if emp.get("horario_id"):
        h = ctx.conn.execute("select horas_diarias from tipos_horario where id = %s", (emp["horario_id"],)).fetchone()
        if h:
            horas = Decimal(h["horas_diarias"])
    return r2(Decimal(emp["salario_mensual"]) / 30 / horas)


@r.post("/horas-extra", status_code=201, tags=["novedades"], summary="Registrar horas extra (calcula el monto)")
def crear_hora_extra(datos: HoraExtraIn, ctx: Contexto = Depends(operador)) -> dict:
    emp = _empleado(ctx, datos.empleado_id)
    cfg = ctx.conn.execute("select factor_he_diurna, factor_he_doble from configuracion_planilla where empresa_id=%s",
                           (ctx.empresa_id,)).fetchone() or {"factor_he_diurna": Decimal("1.5"), "factor_he_doble": Decimal("2")}
    factor = datos.factor or (cfg["factor_he_doble"] if datos.tipo == "feriado" else cfg["factor_he_diurna"])
    tarifa = tarifa_hora(ctx, emp)
    monto = datos.monto if datos.monto is not None else r2(datos.horas * tarifa * Decimal(factor))
    estado = datos.estado if ctx.puede_aprobar else "pendiente"
    return crud.insertar(ctx.conn, "horas_extra", {**datos.model_dump(), "factor": factor, "tarifa_hora": tarifa,
                                                   "monto": monto, "estado": estado, "empresa_id": ctx.empresa_id})


@r.post("/horas-extra/aprobar", tags=["novedades"], summary="Aprobar o rechazar horas extra en lote")
def aprobar_horas(ids: list[UUID], aprobar: bool = True, ctx: Contexto = Depends(aprobador)) -> dict:
    n = ctx.conn.execute("update horas_extra set estado = %s where empresa_id = %s and id = any(%s) and estado = 'pendiente'",
                         ("aprobada" if aprobar else "rechazada", ctx.empresa_id, ids)).rowcount
    return {"actualizadas": n}


@r.post("/vacaciones/{id}/decision", tags=["novedades"], summary="Aprobar o rechazar una solicitud de vacaciones")
def decidir_vacaciones(id: UUID, aprobar: bool, ctx: Contexto = Depends(aprobador)) -> dict:
    v = ctx.conn.execute("update vacaciones set estado = %s where id = %s and empresa_id = %s and estado = 'solicitada' "
                         "returning *", ("aprobada" if aprobar else "rechazada", id, ctx.empresa_id)).fetchone()
    if not v:
        raise HTTPException(404, "Solicitud no encontrada o ya decidida")
    return v


@r.post("/prestamos", status_code=201, tags=["obligaciones"])
def crear_prestamo(datos: PrestamoIn, ctx: Contexto = Depends(operador)) -> dict:
    _empleado(ctx, datos.empleado_id)
    d = datos.model_dump()
    if d["fecha_otorgado"] is None:
        d.pop("fecha_otorgado")
    return crud.insertar(ctx.conn, "prestamos", {**d, "saldo": datos.monto, "empresa_id": ctx.empresa_id})


@r.get("/prestamos/{id}/pagos", tags=["obligaciones"])
def pagos_prestamo(id: UUID, ctx: Contexto = Depends(operador)) -> list[dict]:
    return ctx.conn.execute("select * from prestamo_pagos where prestamo_id = %s and empresa_id = %s order by fecha",
                            (id, ctx.empresa_id)).fetchall()


routers = [
    r,
    crud.crud_router("marcas", "marcas_asistencia", MarcaIn, filtros=("empleado_id",), campo_fecha="entrada",
                     orden="entrada desc", etiqueta="novedades"),
    crud.crud_router("horas-extra", "horas_extra", HoraExtraIn, campo_fecha="fecha", orden="fecha desc",
                     etiqueta="novedades", estado_pendiente="pendiente"),
    crud.crud_router("incapacidades", "incapacidades", IncapacidadIn, campo_fecha="desde", orden="desde desc",
                     etiqueta="novedades"),
    crud.crud_router("permisos", "permisos", PermisoIn, campo_fecha="desde", orden="desde desc", etiqueta="novedades"),
    crud.crud_router("vacaciones", "vacaciones", VacacionIn, campo_fecha="desde", orden="creado_en desc",
                     etiqueta="novedades", estado_pendiente="solicitada"),
    crud.crud_router("bonos", "bonos", BonoIn, filtros=("empleado_id",), orden="desde desc", etiqueta="novedades"),
    crud.crud_router("otras-deducciones", "otras_deducciones", OtraDeduccionIn, filtros=("empleado_id",),
                     orden="desde desc", etiqueta="novedades"),
    crud.crud_router("rebajos-renta", "rebajos_renta", RebajoRentaIn, filtros=("empleado_id",), orden="desde desc",
                     etiqueta="novedades"),
    crud.crud_router("prestamos", "prestamos", PrestamoIn, orden="creado_en desc", etiqueta="obligaciones"),
    crud.crud_router("embargos", "embargos", EmbargoIn, orden="desde desc", etiqueta="obligaciones"),
    crud.crud_router("pensiones", "pensiones_alimentarias", PensionIn, orden="desde desc", etiqueta="obligaciones"),
]
