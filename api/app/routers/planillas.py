"""Planillas: crear, calcular, aprobar, cerrar, reabrir, boletas, PDF y correo."""
from decimal import Decimal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from psycopg import sql

from .. import crud
from ..auth import Contexto, aprobador, contexto_empresa, operador, requiere_modulo
from ..esquemas import EnviarIn, PlanillaIn, ReabrirIn
from ..servicios import calculo, cierre, correo, marca, pdf

r = APIRouter(prefix="/empresas/{empresa_id}/planillas", tags=["planillas"], dependencies=[Depends(requiere_modulo("planilla"))])


def _planilla(ctx: Contexto, id: UUID) -> dict:
    return crud.obtener(ctx.conn, "planillas", id, ctx.empresa_id)


def _bloquear(ctx: Contexto, id: UUID) -> dict:
    """Bloquea la fila: dos cierres o reaperturas simultáneos se ejecutan uno tras otro."""
    pl = ctx.conn.execute("select * from planillas where id = %s and empresa_id = %s for update",
                          (id, ctx.empresa_id)).fetchone()
    if not pl:
        raise HTTPException(404, "No encontrado")
    return pl


# Días que puede abarcar una planilla ordinaria según su frecuencia.
DIAS_FRECUENCIA = {"monthly": (28, 31), "biweekly": (13, 16), "weekly": (7, 7), "bimonthly": (59, 62)}
NOMBRE_FRECUENCIA = {"monthly": "mensual", "biweekly": "quincenal", "weekly": "semanal", "bimonthly": "bimensual"}


def _validar_periodo(frecuencia: str, desde, hasta) -> None:
    """Evita, p. ej., una planilla mensual del 1 al 15: pagaría el salario de un mes en media quincena."""
    dias = (hasta - desde).days + 1
    minimo, maximo = DIAS_FRECUENCIA[frecuencia]
    if not minimo <= dias <= maximo:
        rango = f"{minimo}" if minimo == maximo else f"entre {minimo} y {maximo}"
        raise HTTPException(422, f"Una planilla {NOMBRE_FRECUENCIA[frecuencia]} abarca {rango} días y este periodo "
                                 f"tiene {dias}. Revise las fechas o el calendario elegido.")


def _error_db(exc: Exception) -> HTTPException:
    msg = getattr(getattr(exc, "diag", None), "message_primary", None) or str(exc)
    return HTTPException(409, msg)


@r.get("")
def listar(ctx: Contexto = Depends(contexto_empresa), estado: str | None = None, tipo: str | None = None,
           anio: int | None = None) -> list[dict]:
    conds = [sql.SQL("empresa_id = %s")]
    p: list = [ctx.empresa_id]
    if estado:
        conds.append(sql.SQL("estado = %s")); p.append(estado)
    if tipo:
        conds.append(sql.SQL("tipo = %s")); p.append(tipo)
    if anio:
        conds.append(sql.SQL("extract(year from hasta) = %s")); p.append(anio)
    q = sql.SQL("select * from planillas where {} order by desde desc, creado_en desc").format(sql.SQL(" and ").join(conds))
    return ctx.conn.execute(q, p).fetchall()


@r.post("", status_code=201)
def crear(datos: PlanillaIn, ctx: Contexto = Depends(operador)) -> dict:
    d = datos.model_dump()
    if datos.calendario_id:
        cal = crud.obtener(ctx.conn, "calendarios_pago", datos.calendario_id, ctx.empresa_id)
        d["frecuencia"] = cal["frecuencia"]
    if d["tipo"] == "ordinaria":
        _validar_periodo(d["frecuencia"], d["desde"], d["hasta"])
    dup = ctx.conn.execute("""select id from planillas where empresa_id = %s and tipo = %s and desde = %s and hasta = %s
                              and calendario_id is not distinct from %s and sucursal_id is not distinct from %s""",
                           (ctx.empresa_id, d["tipo"], d["desde"], d["hasta"], d["calendario_id"], d["sucursal_id"])).fetchone()
    if dup and d["tipo"] == "ordinaria":
        raise HTTPException(409, "Ya existe una planilla ordinaria para ese periodo")
    return crud.insertar(ctx.conn, "planillas", {**d, "empresa_id": ctx.empresa_id})


@r.get("/{id}")
def ver(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    pl = _planilla(ctx, id)
    pl["boletas"] = ctx.conn.execute("""
        select b.id, b.empleado_id, e.nombre, e.apellidos, e.identificacion, e.correo, b.salario_base, b.horas_extra,
               b.bruto, b.ccss_obrero, b.renta, b.total_deducciones, b.neto, b.costo_total_patrono, b.dias_incapacidad,
               (select max(fecha) from envios_boleta v where v.boleta_id = b.id and v.estado = 'enviado') as enviada_en
          from boletas b join empleados e on e.id = b.empleado_id
         where b.planilla_id = %s order by e.apellidos, e.nombre""", (id,)).fetchall()
    pl["mi_rol"] = ctx.rol
    return pl


@r.delete("/{id}", status_code=204)
def borrar(id: UUID, ctx: Contexto = Depends(operador)) -> None:
    try:
        n = ctx.conn.execute("delete from planillas where id = %s and empresa_id = %s", (id, ctx.empresa_id)).rowcount
    except psycopg.errors.RaiseException as exc:
        raise _error_db(exc) from exc
    if not n:
        raise HTTPException(404, "No encontrada")


@r.post("/{id}/calcular", summary="Calcular o recalcular todas las boletas (borrador o calculada)")
def calcular(id: UUID, ctx: Contexto = Depends(operador)) -> dict:
    _planilla(ctx, id)
    try:
        return calculo.calcular_planilla(ctx.conn, id)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@r.get("/{id}/advertencias")
def advertencias(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> list[dict]:
    return _planilla(ctx, id)["advertencias"]


def _cambiar_estado(ctx: Contexto, id: UUID, nuevo: str) -> dict:
    try:
        return ctx.conn.execute("update planillas set estado = %s where id = %s and empresa_id = %s returning *",
                                (nuevo, id, ctx.empresa_id)).fetchone()
    except (psycopg.errors.RaiseException, psycopg.errors.InsufficientPrivilege) as exc:
        raise _error_db(exc) from exc


@r.post("/{id}/aprobar")
def aprobar(id: UUID, ctx: Contexto = Depends(aprobador)) -> dict:
    pl = _bloquear(ctx, id)
    if pl["estado"] != "calculada":
        raise HTTPException(409, "Solo se aprueba una planilla calculada")
    errores = [a for a in pl["advertencias"] if a.get("nivel") == "error"]
    if errores:
        raise HTTPException(409, "Corrija los errores antes de aprobar: " + "; ".join(a["mensaje"] for a in errores))
    _exigir_renta_al_dia(ctx, pl)
    return _cambiar_estado(ctx, id, "aprobada")


def _exigir_renta_al_dia(ctx: Contexto, pl: dict) -> None:
    """Método mensual consolidado: la renta de la última planilla del mes depende de las anteriores del mes.
    Si alguna se recalculó después de esta, la renta quedó vieja: hay que recalcular antes de aprobar
    (equivalente al recálculo al confirmar del módulo Odoo, FIX17)."""
    cfg = ctx.conn.execute("select metodo_renta from configuracion_planilla where empresa_id = %s",
                           (ctx.empresa_id,)).fetchone()
    if pl["tipo"] != "ordinaria" or not cfg or cfg["metodo_renta"] != "monthly_consolidated" or not pl["calculada_en"]:
        return
    otra = ctx.conn.execute("""select desde, hasta from planillas
                                where empresa_id = %s and id <> %s and tipo = 'ordinaria' and frecuencia = %s
                                  and hasta < %s and date_trunc('month', hasta) = date_trunc('month', %s::date)
                                  and calculada_en > %s
                                order by calculada_en desc limit 1""",
                            (ctx.empresa_id, pl["id"], pl["frecuencia"], pl["desde"], pl["hasta"], pl["calculada_en"])).fetchone()
    if otra:
        raise HTTPException(409, f"La planilla del {otra['desde']:%d/%m} al {otra['hasta']:%d/%m} se recalculó después de esta "
                                 "y la renta del mes depende de ella. Use «Recalcular» y luego apruebe.")


@r.post("/{id}/devolver", summary="Devolver una planilla aprobada a revisión")
def devolver(id: UUID, ctx: Contexto = Depends(aprobador)) -> dict:
    if _planilla(ctx, id)["estado"] != "aprobada":
        raise HTTPException(409, "Solo se devuelve una planilla aprobada")
    return _cambiar_estado(ctx, id, "calculada")


@r.post("/{id}/cerrar", summary="Cerrar el periodo: marca pagos, abona préstamos y genera el asiento")
def cerrar(id: UUID, ctx: Contexto = Depends(aprobador)) -> dict:
    pl = _bloquear(ctx, id)
    if pl["estado"] != "aprobada":
        raise HTTPException(409, "Solo se cierra una planilla aprobada")
    try:
        cierre.aplicar_cierre(ctx.conn, pl)
    except cierre.ConflictoCierre as exc:
        raise HTTPException(409, str(exc)) from exc
    return _cambiar_estado(ctx, id, "cerrada")


@r.post("/{id}/reabrir", summary="Reabrir una planilla cerrada (queda registrado el motivo)")
def reabrir(id: UUID, datos: ReabrirIn, ctx: Contexto = Depends(aprobador)) -> dict:
    pl = _bloquear(ctx, id)
    if pl["estado"] != "cerrada":
        raise HTTPException(409, "Solo se reabre una planilla cerrada")
    ctx.conn.execute("insert into reaperturas (empresa_id, planilla_id, motivo) values (%s,%s,%s)",
                     (ctx.empresa_id, id, datos.motivo))
    cierre.revertir_cierre(ctx.conn, pl)
    return _cambiar_estado(ctx, id, "calculada")


@r.get("/{id}/asiento", summary="Asiento contable vigente de la planilla")
def asiento(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    todos = ctx.conn.execute("""select * from asientos where origen_id = %s and empresa_id = %s and estado = 'vigente'
                                 order by descripcion""", (id, ctx.empresa_id)).fetchall()
    if not todos:
        raise HTTPException(404, "La planilla aún no tiene asiento (se genera al cerrar)")
    a = dict(todos[0])
    # Con «un asiento por boleta» se muestran todas las líneas juntas; cada línea dice de quién es.
    a["cantidad"] = len(todos)
    a["lineas"] = []
    for x in todos:
        quien = x["descripcion"].split(" · ", 1)[1] if len(todos) > 1 and " · " in x["descripcion"] else None
        for l in ctx.conn.execute("select * from asiento_lineas where asiento_id = %s order by orden", (x["id"],)).fetchall():
            a["lineas"].append({**l, "descripcion": f"{quien} · {l['descripcion']}" if quien else l["descripcion"]})
    if len(todos) > 1:
        a["descripcion"] = f"{len(todos)} asientos (uno por boleta)"
        a["total_debito"] = sum((x["total_debito"] for x in todos), Decimal(0))
        a["total_credito"] = sum((x["total_credito"] for x in todos), Decimal(0))
    return a


# ----------------------------------------------------------------- Boletas --
def _boleta(ctx: Contexto, planilla_id: UUID, boleta_id: UUID) -> dict:
    b = ctx.conn.execute("select * from boletas where id = %s and planilla_id = %s and empresa_id = %s",
                         (boleta_id, planilla_id, ctx.empresa_id)).fetchone()
    if not b:
        raise HTTPException(404, "Boleta no encontrada")
    return b


@r.get("/{id}/boletas/{boleta_id}")
def ver_boleta(id: UUID, boleta_id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    b = _boleta(ctx, id, boleta_id)
    b["lineas"] = ctx.conn.execute("select * from boleta_lineas where boleta_id = %s order by orden", (boleta_id,)).fetchall()
    b["empleado"] = ctx.conn.execute("select id, nombre, apellidos, identificacion, puesto, correo, iban, salario_mensual "
                                     "from empleados where id = %s", (b["empleado_id"],)).fetchone()
    return b


def _pdf_boleta(conn, empresa_id, planilla: dict, boleta: dict) -> bytes:
    empresa = conn.execute("select * from empresas where id = %s", (empresa_id,)).fetchone()
    empleado = conn.execute("select * from empleados where id = %s", (boleta["empleado_id"],)).fetchone()
    lineas = conn.execute("select * from boleta_lineas where boleta_id = %s order by orden", (boleta["id"],)).fetchall()
    cfg = conn.execute("""select nota_boleta, boleta_saldo_vacaciones, boleta_detalle_subsidio
                            from configuracion_planilla where empresa_id = %s""", (empresa_id,)).fetchone() or {}
    return pdf.boleta_pdf(empresa, planilla, empleado, boleta, lineas, cfg.get("nota_boleta"),
                          logo=marca.logo(conn, empresa_id), saldo_vacaciones=bool(cfg.get("boleta_saldo_vacaciones")),
                          detalle_subsidio=bool(cfg.get("boleta_detalle_subsidio")))


@r.get("/{id}/boletas/{boleta_id}/pdf", response_class=Response)
def boleta_pdf(id: UUID, boleta_id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> Response:
    pl = _planilla(ctx, id)
    b = _boleta(ctx, id, boleta_id)
    contenido = _pdf_boleta(ctx.conn, ctx.empresa_id, pl, b)
    return Response(contenido, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="boleta-{pl["hasta"]}-{b["empleado_id"]}.pdf"'})


@r.post("/{id}/enviar", summary="Enviar boletas por correo (planilla aprobada o cerrada)")
def enviar(id: UUID, datos: EnviarIn, ctx: Contexto = Depends(operador)) -> dict:
    pl = _planilla(ctx, id)
    if pl["estado"] not in ("aprobada", "cerrada"):
        raise HTTPException(409, "Las boletas se envían cuando la planilla está aprobada o cerrada")
    empresa = ctx.conn.execute("select nombre from empresas where id = %s", (ctx.empresa_id,)).fetchone()
    cfg = ctx.conn.execute("""select correo_asunto, correo_cuerpo, correo_firma, correo_remitente
                                from configuracion_planilla where empresa_id = %s""", (ctx.empresa_id,)).fetchone() or {}
    periodo = f"{pl['desde']:%d/%m/%Y} al {pl['hasta']:%d/%m/%Y}"
    q = sql.SQL("select * from boletas where planilla_id = %s")
    p: list = [id]
    if datos.boleta_ids:
        q += sql.SQL(" and id = any(%s)"); p.append(list(datos.boleta_ids))
    resumen = {"enviadas": 0, "errores": 0, "sin_correo": 0}
    for b in ctx.conn.execute(q, p).fetchall():
        emp = ctx.conn.execute("select nombre, apellidos, correo from empleados where id = %s", (b["empleado_id"],)).fetchone()
        if not emp["correo"]:
            resumen["sin_correo"] += 1
            ctx.conn.execute("insert into envios_boleta (empresa_id, boleta_id, estado, error) values (%s,%s,'omitido','Sin correo')",
                             (ctx.empresa_id, b["id"]))
            continue
        try:
            archivo = _pdf_boleta(ctx.conn, ctx.empresa_id, pl, b)
            datos = {"periodo": periodo, "empresa": empresa["nombre"],
                     "colaborador": f"{emp['nombre']} {emp.get('apellidos') or ''}".strip()}
            cuerpo = correo.plantilla(cfg.get("correo_cuerpo"), correo.CUERPO_BOLETA, **datos)
            if cfg.get("correo_firma"):
                cuerpo += "\n\n" + cfg["correo_firma"]
            correo.enviar(emp["correo"], correo.plantilla(cfg.get("correo_asunto"), correo.ASUNTO_BOLETA, **datos),
                          correo.a_html(cuerpo), [(f"boleta-{pl['hasta']}.pdf", archivo)],
                          responder_a=cfg.get("correo_remitente"))
            ctx.conn.execute("insert into envios_boleta (empresa_id, boleta_id, destino, estado) values (%s,%s,%s,'enviado')",
                             (ctx.empresa_id, b["id"], emp["correo"]))
            resumen["enviadas"] += 1
        except Exception as exc:  # se registra y sigue con las demás
            ctx.conn.execute("insert into envios_boleta (empresa_id, boleta_id, destino, estado, error) values (%s,%s,%s,'error',%s)",
                             (ctx.empresa_id, b["id"], emp["correo"], str(exc)[:500]))
            resumen["errores"] += 1
    return resumen
