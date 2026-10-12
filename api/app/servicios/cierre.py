"""Efectos de cerrar y reabrir una planilla: horas extra y vacaciones pagadas,
abonos a préstamos, saldo de vacaciones y asiento contable."""
from __future__ import annotations

from decimal import Decimal

import planilla_motor as M

from ..db import Conexion
from . import contable


def generar_asiento(conn: Conexion, planilla: dict) -> dict:
    boletas = conn.execute("select resultado, neto from boletas where planilla_id = %s", (planilla["id"],)).fetchall()
    if planilla["tipo"] == "aguinaldo":
        # El aguinaldo ya se provisionó en cada planilla: se paga contra la provisión.
        total_ag = sum((Decimal(b["neto"]) for b in boletas), Decimal(0))
        lineas = [M.LineaAsiento("prov_aguinaldo", "Pago de aguinaldo contra provisión", debito=total_ag),
                  M.LineaAsiento("salarios_por_pagar", "Aguinaldo por pagar", credito=total_ag)]
    else:
        lineas = M.asiento_planilla([M.resultado_desde_dict(b["resultado"]) for b in boletas])
    if not M.cuadra(lineas):
        raise ValueError("El asiento no cuadra")
    cuentas = {c["concepto"]: c for c in conn.execute(
        "select concepto, codigo, nombre from cuentas_contables where empresa_id = %s", (planilla["empresa_id"],)).fetchall()}
    total = sum((l.debito for l in lineas), Decimal(0))
    conn.execute("update asientos set estado = 'reemplazado' where origen = 'planilla' and origen_id = %s and estado = 'vigente'",
                 (planilla["id"],))
    a = conn.execute("""insert into asientos (empresa_id, origen, origen_id, fecha, descripcion, total_debito, total_credito)
                        values (%s, %s, %s, %s, %s, %s, %s) returning *""",
                     (planilla["empresa_id"], "aguinaldo" if planilla["tipo"] == "aguinaldo" else "planilla", planilla["id"],
                      planilla["hasta"], f"Planilla {planilla['tipo']} del {planilla['desde']} al {planilla['hasta']}",
                      total, total)).fetchone()
    with conn.cursor() as cur:
        cur.executemany("""insert into asiento_lineas (empresa_id, asiento_id, cuenta_codigo, cuenta_nombre, descripcion,
                           debito, credito, orden) values (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [(planilla["empresa_id"], a["id"], cuentas.get(l.concepto, {}).get("codigo", l.concepto),
                          cuentas.get(l.concepto, {}).get("nombre", l.descripcion), l.descripcion, l.debito, l.credito, i)
                         for i, l in enumerate(lineas)])
    # Con el módulo de Contabilidad activo, el asiento entra solo al diario de Planillas.
    contable.desde_planilla(conn, planilla, [
        {"concepto": l.concepto, "codigo": cuentas.get(l.concepto, {}).get("codigo", l.concepto),
         "nombre": cuentas.get(l.concepto, {}).get("nombre", l.descripcion), "descripcion": l.descripcion,
         "debito": l.debito, "credito": l.credito} for l in lineas])
    return a


class ConflictoCierre(ValueError):
    """Algo de esta planilla ya se pagó en otra: hay que recalcular antes de cerrar."""


def aplicar_cierre(conn: Conexion, planilla: dict) -> None:
    """Marca como pagado lo que la planilla incluyó. Si una novedad o cuota ya fue
    pagada por otra planilla (dos planillas abiertas del mismo periodo), aborta todo
    el cierre para no pagar dos veces."""
    for b in conn.execute("select id, empleado_id, entrada from boletas where planilla_id = %s", (planilla["id"],)).fetchall():
        refs = (b["entrada"] or {}).get("_refs", {})
        if refs.get("horas_extra"):
            n = conn.execute("""update horas_extra set estado = 'pagada', boleta_id = %s
                                where id = any(%s::uuid[]) and estado = 'aprobada' and boleta_id is null""",
                             (b["id"], refs["horas_extra"])).rowcount
            if n != len(refs["horas_extra"]):
                raise ConflictoCierre("Hay horas extra de esta planilla que ya se pagaron o cambiaron: recalcule la planilla")
        if refs.get("vacaciones"):
            dinero = conn.execute("select count(*) as n from vacaciones where id = any(%s::uuid[]) and tipo = 'dinero'",
                                  (refs["vacaciones"],)).fetchone()["n"]
            vac = conn.execute("""update vacaciones set boleta_id = %s,
                                      estado = case when tipo = 'dinero' then 'pagada' else estado end
                                  where id = any(%s::uuid[]) and boleta_id is null and estado = 'aprobada'
                                  returning dias, tipo""",
                               (b["id"], refs["vacaciones"])).fetchall()
            if sum(1 for v in vac if v["tipo"] == "dinero") != dinero:
                raise ConflictoCierre("Hay vacaciones pagadas en dinero que ya se pagaron en otra planilla: recalcule")
            dias = sum((Decimal(v["dias"]) for v in vac), Decimal(0))
            if dias:
                conn.execute("update empleados set saldo_vacaciones = saldo_vacaciones - %s where id = %s",
                             (dias, b["empleado_id"]))
        for p in refs.get("prestamos", []):
            monto = Decimal(p["monto"])
            n = conn.execute("""update prestamos set saldo = saldo - %s,
                                       estado = case when saldo - %s <= 0 then 'pagado' else estado end
                                 where id = %s and estado = 'activo' and saldo >= %s""",
                             (monto, monto, p["id"], monto)).rowcount
            if n != 1:
                raise ConflictoCierre("El saldo de un préstamo cambió desde el cálculo: recalcule la planilla")
            conn.execute("insert into prestamo_pagos (empresa_id, prestamo_id, boleta_id, fecha, monto) values (%s,%s,%s,%s,%s)",
                         (planilla["empresa_id"], p["id"], b["id"], planilla["hasta"], monto))
    if planilla["tipo"] == "aguinaldo":
        conn.execute("update aguinaldos set estado = 'pagado' where planilla_id = %s", (planilla["id"],))
    generar_asiento(conn, planilla)


def revertir_cierre(conn: Conexion, planilla: dict) -> None:
    ids = [b["id"] for b in conn.execute("select id from boletas where planilla_id = %s", (planilla["id"],)).fetchall()]
    if not ids:
        return
    conn.execute("update horas_extra set estado = 'aprobada', boleta_id = null where boleta_id = any(%s)", (ids,))
    for v in conn.execute("""update vacaciones set boleta_id = null,
                                 estado = case when tipo = 'dinero' then 'aprobada' else estado end
                             where boleta_id = any(%s) returning empleado_id, dias""", (ids,)).fetchall():
        conn.execute("update empleados set saldo_vacaciones = saldo_vacaciones + %s where id = %s", (v["dias"], v["empleado_id"]))
    for p in conn.execute("delete from prestamo_pagos where boleta_id = any(%s) returning prestamo_id, monto", (ids,)).fetchall():
        conn.execute("update prestamos set saldo = saldo + %s, estado = 'activo' where id = %s", (p["monto"], p["prestamo_id"]))
    if planilla["tipo"] == "aguinaldo":
        conn.execute("update aguinaldos set estado = 'aprobado' where planilla_id = %s", (planilla["id"],))
    conn.execute("update asientos set estado = 'reemplazado' where origen_id = %s and estado = 'vigente'", (planilla["id"],))
    contable.revertir_planilla(conn, planilla["empresa_id"], planilla["id"])
