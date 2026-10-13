"""Aguinaldo y liquidaciones (Fase 3)."""
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import planilla_motor as M
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from psycopg.types.json import Jsonb

from .. import crud
from ..auth import Contexto, aprobador, contexto_empresa, operador, requiere_modulo
from ..esquemas import AguinaldoIn, LiquidacionIn
from ..fechas import hoy_cr
from ..servicios import citas_legales, marca, pdf

r = APIRouter(prefix="/empresas/{empresa_id}", tags=["anuales"], dependencies=[Depends(requiere_modulo("planilla"))])


def salarios_por_mes(conn, empleado_id, desde: date, hasta: date) -> dict[str, Decimal]:
    filas = conn.execute("""
        select to_char(p.hasta, 'YYYY-MM') as mes, sum(b.bruto) as total
          from boletas b join planillas p on p.id = b.planilla_id
         where b.empleado_id = %s and p.tipo in ('ordinaria','extraordinaria') and p.estado in ('aprobada','cerrada')
           and p.hasta between %s and %s
         group by 1""", (empleado_id, desde, hasta)).fetchall()
    return {f["mes"]: Decimal(f["total"]) for f in filas}


# --------------------------------------------------------------- Aguinaldo --
def aguinaldo_previo(emp: dict, desde: date, hasta: date) -> tuple[Decimal, date]:
    """Saldo de aguinaldo traído de antes del sistema (como el «Aguinaldo acumulado» de Odoo).

    Si la fecha de corte cae dentro del período, se toma el monto inicial y los salarios
    se cuentan solo desde el día siguiente al corte."""
    corte = emp.get("aguinaldo_corte")
    inicial = Decimal(emp.get("aguinaldo_inicial") or 0)
    if corte and inicial > 0 and desde <= corte <= hasta:
        return inicial, corte + timedelta(days=1)
    return Decimal(0), desde

@r.post("/aguinaldo/calcular", summary="Calcular aguinaldo del año (dic a nov) y crear su planilla")
def calcular_aguinaldo(datos: AguinaldoIn, ctx: Contexto = Depends(operador)) -> dict:
    desde, hasta = M.periodo_aguinaldo(datos.anio)
    previa = ctx.conn.execute("select id, estado from planillas where empresa_id = %s and tipo = 'aguinaldo' "
                              "and desde = %s and hasta = %s", (ctx.empresa_id, desde, hasta)).fetchone()
    if previa and previa["estado"] in ("aprobada", "cerrada"):
        raise HTTPException(409, f"La planilla de aguinaldo {datos.anio} ya está {previa['estado']}")
    if previa:
        ctx.conn.execute("delete from planillas where id = %s", (previa["id"],))
    # Se rehacen todos los que no estén pagados (incluye los que quedaron "aprobado" tras reabrir).
    ctx.conn.execute("delete from aguinaldos where empresa_id = %s and anio = %s and estado <> 'pagado'",
                     (ctx.empresa_id, datos.anio))

    empleados = ctx.conn.execute("""
        select e.* from empleados e
         where e.empresa_id = %(e)s
           and (exists (select 1 from boletas b join planillas p on p.id = b.planilla_id
                         where b.empleado_id = e.id and p.hasta between %(d)s and %(h)s
                           and p.tipo in ('ordinaria','extraordinaria') and p.estado in ('aprobada','cerrada'))
                or (e.aguinaldo_inicial > 0 and e.aguinaldo_corte between %(d)s and %(h)s))
           and not exists (select 1 from liquidaciones l where l.empleado_id = e.id and l.estado in ('aprobada','pagada')
                           and l.fecha_salida between %(d)s and %(h)s)
         order by e.apellidos, e.nombre""", {"e": ctx.empresa_id, "d": desde, "h": hasta}).fetchall()
    planilla = None
    if datos.crear_planilla:
        planilla = crud.insertar(ctx.conn, "planillas", {
            "empresa_id": ctx.empresa_id, "tipo": "aguinaldo", "frecuencia": "monthly", "desde": desde, "hasta": hasta,
            "fecha_pago": datos.fecha_pago or date(datos.anio, 12, 20), "descripcion": f"Aguinaldo {datos.anio}"})
    filas, total = [], Decimal(0)
    for e in empleados:
        inicial, inicio = aguinaldo_previo(e, desde, hasta)
        res = M.calcular_aguinaldo(datos.anio, salarios_por_mes(ctx.conn, e["id"], inicio, hasta))
        if inicial:
            # Aguinaldo acumulado antes de usar el sistema (dato de la ficha) + lo calculado desde el corte.
            res.monto += inicial
            res.total_salarios += inicial * 12
            res.detalle = [{"mes": f"Acumulado al {e['aguinaldo_corte']}", "monto": str(inicial * 12),
                            "nota": f"Aguinaldo acumulado antes del sistema: {inicial}"}, *res.detalle]
        if res.monto <= 0:
            continue
        ag = crud.insertar(ctx.conn, "aguinaldos", {
            "empresa_id": ctx.empresa_id, "anio": datos.anio, "empleado_id": e["id"], "detalle": Jsonb(res.detalle),
            "total_salarios": res.total_salarios, "monto": res.monto, "planilla_id": planilla["id"] if planilla else None})
        if planilla:
            rb = M.ResultadoBoleta(bruto=res.monto, neto=res.monto, costo_total_patrono=res.monto)
            b = crud.insertar(ctx.conn, "boletas", {
                "empresa_id": ctx.empresa_id, "planilla_id": planilla["id"], "empleado_id": e["id"], "bruto": res.monto,
                "neto": res.monto, "costo_total_patrono": res.monto, "resultado": Jsonb(M.a_dict(rb)),
                "entrada": Jsonb({"aguinaldo": res.detalle}), "version_motor": M.__version__})
            crud.insertar(ctx.conn, "boleta_lineas", {
                "empresa_id": ctx.empresa_id, "boleta_id": b["id"], "tipo": "ingreso", "categoria": "aguinaldo",
                "descripcion": f"Aguinaldo {datos.anio} (salarios {res.total_salarios} / 12)", "monto": res.monto})
        total += res.monto
        filas.append({**ag, "nombre": f"{e['nombre']} {e['apellidos']}".strip()})
    if planilla:
        tot = {"empleados": len(filas), "bruto": str(total), "neto": str(total), "total_deducciones": "0",
               "costo_total_patrono": str(total)}
        planilla = ctx.conn.execute("update planillas set estado = 'calculada', totales = %s, calculada_en = now() "
                                    "where id = %s returning *", (Jsonb(tot), planilla["id"])).fetchone()
    return {"anio": datos.anio, "desde": desde, "hasta": hasta, "total": total, "planilla": planilla, "empleados": filas}


@r.get("/aguinaldo/{anio}")
def ver_aguinaldo(anio: int, ctx: Contexto = Depends(contexto_empresa)) -> list[dict]:
    return ctx.conn.execute("""select a.*, e.nombre, e.apellidos, e.identificacion from aguinaldos a
                               join empleados e on e.id = a.empleado_id where a.empresa_id = %s and a.anio = %s
                               order by e.apellidos""", (ctx.empresa_id, anio)).fetchall()


# ------------------------------------------------------------ Liquidaciones --
def _entrada_liquidacion(ctx: Contexto, datos: LiquidacionIn) -> tuple[dict, M.EntradaLiquidacion, Decimal]:
    emp = crud.obtener(ctx.conn, "empleados", datos.empleado_id, ctx.empresa_id)
    salida = datos.fecha_salida
    y, m = salida.year, salida.month - 5
    if m <= 0:
        y, m = y - 1, m + 12
    seis = date(y, m, 1)
    meses = salarios_por_mes(ctx.conn, emp["id"], seis, salida)
    ultimos = [meses[k] for k in sorted(meses)][-6:] or [Decimal(emp["salario_mensual"])]
    ini_ag = date(salida.year if salida.month == 12 else salida.year - 1, 12, 1)
    inicial, inicio_ag = aguinaldo_previo(emp, ini_ag, salida)
    periodo_ag = inicial * 12 + sum(salarios_por_mes(ctx.conn, emp["id"], inicio_ag, salida).values(), Decimal(0))
    saldo_prest = Decimal(0)
    if datos.incluir_saldo_prestamos:
        saldo_prest = Decimal(ctx.conn.execute("select coalesce(sum(saldo),0) as s from prestamos where empleado_id = %s "
                                               "and estado = 'activo'", (emp["id"],)).fetchone()["s"])
    entrada = M.EntradaLiquidacion(
        fecha_ingreso=emp["fecha_ingreso"], fecha_salida=salida, motivo=datos.motivo,
        salarios_ultimos_6_meses=ultimos, salarios_periodo_aguinaldo=periodo_ag,
        dias_vacaciones_pendientes=datos.dias_vacaciones_pendientes if datos.dias_vacaciones_pendientes is not None
        else Decimal(emp["saldo_vacaciones"]),
        preaviso_otorgado=datos.preaviso_otorgado, salarios_pendientes=datos.salarios_pendientes,
        otros_pagos=datos.otros_pagos, deducciones=saldo_prest + datos.deducciones_adicionales,
        aplicar_ccss_obrero=not (ctx.conn.execute(
            "select liquidacion_sin_ccss from configuracion_planilla where empresa_id = %s",
            (ctx.empresa_id,)).fetchone() or {}).get("liquidacion_sin_ccss", False))
    return emp, entrada, saldo_prest


@r.post("/liquidaciones/simular", summary="Simular una liquidación sin guardar nada")
def simular(datos: LiquidacionIn, ctx: Contexto = Depends(contexto_empresa)) -> dict:
    emp, entrada, saldo = _entrada_liquidacion(ctx, datos)
    res = M.calcular_liquidacion(entrada)
    return {"empleado": {"id": emp["id"], "nombre": f"{emp['nombre']} {emp['apellidos']}".strip()},
            "entrada": M.a_dict(entrada), "resultado": M.a_dict(res), "saldo_prestamos": saldo}


@r.post("/liquidaciones", status_code=201)
def crear_liquidacion(datos: LiquidacionIn, ctx: Contexto = Depends(operador)) -> dict:
    emp, entrada, saldo = _entrada_liquidacion(ctx, datos)
    res = M.calcular_liquidacion(entrada)
    return crud.insertar(ctx.conn, "liquidaciones", {
        "empresa_id": ctx.empresa_id, "empleado_id": emp["id"], "fecha_salida": datos.fecha_salida,
        "motivo": datos.motivo, "preaviso_otorgado": datos.preaviso_otorgado,
        "detalle": Jsonb({"entrada": M.a_dict(entrada), "resultado": M.a_dict(res), "saldo_prestamos": str(saldo)}),
        "salario_promedio": res.salario_promedio_mensual, "preaviso": res.preaviso, "cesantia": res.cesantia,
        "vacaciones": res.vacaciones, "aguinaldo": res.aguinaldo, "otros_pagos": res.otros_pagos + res.salarios_pendientes,
        "deducciones": res.deducciones, "total": res.total})


@r.get("/liquidaciones")
def listar_liquidaciones(ctx: Contexto = Depends(contexto_empresa)) -> list[dict]:
    return ctx.conn.execute("""select l.*, e.nombre, e.apellidos, e.identificacion from liquidaciones l
                               join empleados e on e.id = l.empleado_id where l.empresa_id = %s
                               order by l.creado_en desc""", (ctx.empresa_id,)).fetchall()


@r.post("/liquidaciones/{id}/aprobar", summary="Aprobar: el empleado queda liquidado y se saldan sus préstamos")
def aprobar_liquidacion(id: UUID, ctx: Contexto = Depends(aprobador)) -> dict:
    l = ctx.conn.execute("update liquidaciones set estado = 'aprobada', aprobada_por = auth.uid() "
                         "where id = %s and empresa_id = %s and estado = 'borrador' returning *", (id, ctx.empresa_id)).fetchone()
    if not l:
        raise HTTPException(404, "Liquidación no encontrada o ya aprobada")
    ctx.conn.execute("update empleados set estado = 'liquidado', fecha_salida = %s, saldo_vacaciones = 0 where id = %s",
                     (l["fecha_salida"], l["empleado_id"]))
    if Decimal(l["detalle"].get("saldo_prestamos", "0")) > 0:
        for p in ctx.conn.execute("update prestamos set estado = 'pagado' where empleado_id = %s and estado = 'activo' "
                                  "returning id, saldo", (l["empleado_id"],)).fetchall():
            ctx.conn.execute("insert into prestamo_pagos (empresa_id, prestamo_id, fecha, monto) values (%s,%s,%s,%s)",
                             (ctx.empresa_id, p["id"], l["fecha_salida"], p["saldo"]))
            ctx.conn.execute("update prestamos set saldo = 0 where id = %s", (p["id"],))
    ctx.conn.execute("insert into movimientos_empleado (empresa_id, empleado_id, tipo, fecha, detalle) values (%s,%s,'salida',%s,%s)",
                     (ctx.empresa_id, l["empleado_id"], l["fecha_salida"], f"Liquidación: {l['motivo'].replace('_', ' ')}"))
    return l


MOTIVOS = {"renuncia": "Renuncia", "despido_sin_responsabilidad": "Despido sin responsabilidad patronal",
           "despido_con_responsabilidad": "Despido con responsabilidad patronal", "mutuo_acuerdo": "Mutuo acuerdo",
           "pension": "Pensión", "fallecimiento": "Fallecimiento", "fin_contrato": "Fin de contrato"}


@r.get("/liquidaciones/{id}/pdf", response_class=Response)
def liquidacion_pdf(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> Response:
    l = crud.obtener(ctx.conn, "liquidaciones", id, ctx.empresa_id)
    e = ctx.conn.execute("select * from empleados where id = %s", (l["empleado_id"],)).fetchone()
    emp = ctx.conn.execute("select nombre, cedula_juridica from empresas where id = %s", (ctx.empresa_id,)).fetchone()
    res = l["detalle"]["resultado"]
    c = pdf.colones
    secciones = [
        ("Datos", [["Concepto", "Detalle"], ["Colaborador", f"{e['nombre']} {e['apellidos']}".strip()],
                   ["Identificación", e["identificacion"]], ["Ingreso", pdf.fecha(e["fecha_ingreso"])],
                   ["Salida", pdf.fecha(l["fecha_salida"])], ["Motivo", MOTIVOS[l["motivo"]]],
                   ["Antigüedad", f"{res['anios']} años, {res['meses']} meses, {res['dias']} días"],
                   ["Salario promedio (6 meses)", c(res["salario_promedio_mensual"])],
                   ["Salario diario", c(res["salario_diario"])]]),
        ("Derechos", [["Concepto", "Monto"], [f"Preaviso ({res['dias_preaviso']} días) · Art. 28 CT", c(res["preaviso"])],
                      [f"Cesantía ({res['dias_cesantia']} días) · Art. 29 CT", c(res["cesantia"])],
                      ["Vacaciones pendientes · Arts. 153 y 156 CT", c(res["vacaciones"])],
                      ["Aguinaldo proporcional · Ley 2412", c(res["aguinaldo"])],
                      ["Salarios pendientes", c(res["salarios_pendientes"])], ["Otros pagos", c(res["otros_pagos"])],
                      ["Total derechos", c(res["total_bruto"])]]),
        ("Deducciones", [["Concepto", "Monto"], ["CCSS obrero (vacaciones y salarios)", c(res["ccss_obrero"])],
                         ["Préstamos y otras", c(Decimal(res["deducciones"]) - Decimal(res["ccss_obrero"]))],
                         ["Total deducciones", c(res["deducciones"])]]),
        ("", [["Concepto", "Monto"], ["Total a pagar", c(res["total"])]]),
    ]
    pie = (" ".join(res.get("notas", [])) + " "
           + citas_legales.texto(["preaviso", "cesantia", "vacaciones", "aguinaldo", "ccss_cuotas"])
           + "  Recibí conforme: ______________________   Fecha: ____________")
    contenido = pdf.documento_simple(f"Liquidación laboral · {emp['nombre']}",
                                     f"Cédula jurídica {emp['cedula_juridica'] or '—'} · Código de Trabajo Arts. 28-31",
                                     secciones, pie, logo=marca.logo(ctx.conn, ctx.empresa_id))
    return Response(contenido, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="liquidacion-{e["identificacion"]}.pdf"'})


def firma_constancia(ctx: Contexto, empresa_nombre: str) -> tuple[str, str]:
    """(pie de firma, lugar de emisión) con el firmante de Configuración › Planilla, o la empresa si no hay."""
    import html as _h
    f = ctx.conn.execute("""select firmante_nombre, firmante_cargo, firmante_telefono, firmante_correo, firmante_lugar
                              from configuracion_planilla where empresa_id = %s""", (ctx.empresa_id,)).fetchone() or {}
    partes = [f.get("firmante_nombre"), f.get("firmante_cargo") or (None if f.get("firmante_nombre") else "Firma y sello"),
              empresa_nombre if f.get("firmante_nombre") else None,
              " · ".join(x for x in (f.get("firmante_telefono"), f.get("firmante_correo")) if x) or None]
    lugar = f"{_h.escape(f['firmante_lugar'])}, " if f.get("firmante_lugar") else ""
    return ("<br/><br/><br/>______________________________<br/>"
            + "<br/>".join(_h.escape(x) for x in partes if x), lugar)


@r.get("/liquidaciones/{id}/constancia", response_class=Response, summary="Constancia de salida (carta)")
def constancia(id: UUID, ctx: Contexto = Depends(contexto_empresa)) -> Response:
    l = crud.obtener(ctx.conn, "liquidaciones", id, ctx.empresa_id)
    e = ctx.conn.execute("select * from empleados where id = %s", (l["empleado_id"],)).fetchone()
    emp = ctx.conn.execute("select nombre, cedula_juridica from empresas where id = %s", (ctx.empresa_id,)).fetchone()
    texto = (f"{emp['nombre']}, cédula jurídica {emp['cedula_juridica'] or '—'}, hace constar que "
             f"{e['nombre']} {e['apellidos']}, identificación {e['identificacion']}, laboró para la empresa desde el "
             f"{pdf.fecha(e['fecha_ingreso'])} hasta el {pdf.fecha(l['fecha_salida'])}, en el puesto de "
             f"{e['puesto'] or 'colaborador(a)'}. Motivo de la terminación: {MOTIVOS[l['motivo']].lower()}. "
             "Se extiende la presente a solicitud de la persona interesada")
    firma, lugar = firma_constancia(ctx, emp["nombre"])
    texto += f" en {lugar}el {pdf.fecha(hoy_cr())}." if lugar else f" el {pdf.fecha(hoy_cr())}."
    contenido = pdf.documento_simple("Constancia laboral", emp["nombre"], [], texto + firma,
                                     logo=marca.logo(ctx.conn, ctx.empresa_id))
    return Response(contenido, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="constancia-{e["identificacion"]}.pdf"'})


@r.get("/empleados/{id}/constancia", response_class=Response, summary="Constancia laboral de un colaborador activo")
def constancia_activo(id: UUID, salario: bool = False, ctx: Contexto = Depends(contexto_empresa)) -> Response:
    e = crud.obtener(ctx.conn, "empleados", id, ctx.empresa_id)
    emp = ctx.conn.execute("select nombre, cedula_juridica from empresas where id = %s", (ctx.empresa_id,)).fetchone()
    texto = (f"{emp['nombre']}, cédula jurídica {emp['cedula_juridica'] or '—'}, hace constar que "
             f"{e['nombre']} {e['apellidos']}, identificación {e['identificacion']}, labora para la empresa desde el "
             f"{pdf.fecha(e['fecha_ingreso'])}, en el puesto de {e['puesto'] or 'colaborador(a)'}")
    if salario:
        texto += f", con un salario mensual bruto de {pdf.colones(e['salario_mensual'])}"
    texto += ". Se extiende la presente a solicitud de la persona interesada"
    firma, lugar = firma_constancia(ctx, emp["nombre"])
    texto += f" en {lugar}el {pdf.fecha(hoy_cr())}." if lugar else f" el {pdf.fecha(hoy_cr())}."
    contenido = pdf.documento_simple("Constancia laboral", emp["nombre"], [], texto + firma,
                                     logo=marca.logo(ctx.conn, ctx.empresa_id))
    return Response(contenido, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="constancia-{e["identificacion"]}.pdf"'})
