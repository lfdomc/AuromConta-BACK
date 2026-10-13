"""Cálculo de una planilla: arma la entrada del motor con los datos de la base,
corre el motor y guarda boletas, líneas, totales y advertencias.

Es el único lugar donde se calculan montos de planilla.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import planilla_motor as M
from planilla_motor.dinero import CERO, r2
from psycopg import sql
from psycopg.types.json import Jsonb

from ..db import Conexion
from . import cache, licencias

FACTOR = {"monthly": Decimal("1"), "biweekly": Decimal("0.5"), "weekly": Decimal("0.25"), "bimonthly": Decimal("2")}


def _solapa(a1: date, a2: date, b1: date, b2: date | None) -> bool:
    return a1 <= (b2 or date.max) and b1 <= a2


def _dias(a1: date, a2: date, b1: date, b2: date) -> int:
    ini, fin = max(a1, b1), min(a2, b2)
    return max((fin - ini).days + 1, 0)


def promedio_diario_3_meses(conn: Conexion, empleado_id, antes_de: date, defecto: Decimal) -> Decimal:
    """Salario promedio diario de los tres meses calendario anteriores (base de la licencia de paternidad y
    adopción, Art. 95 CT). Sin historial suficiente, el salario diario actual."""
    ini = (antes_de.replace(day=1) - timedelta(days=1)).replace(day=1)
    for _ in range(2):
        ini = (ini - timedelta(days=1)).replace(day=1)
    fin = antes_de.replace(day=1) - timedelta(days=1)
    fila = conn.execute("""select count(distinct date_trunc('month', p.hasta)) as meses, coalesce(sum(b.bruto), 0) as total
                             from boletas b join planillas p on p.id = b.planilla_id
                            where b.empleado_id = %s and p.tipo = 'ordinaria' and p.estado in ('aprobada','cerrada')
                              and p.hasta between %s and %s""", (empleado_id, ini, fin)).fetchone()
    if not fila or fila["meses"] < 3:
        return defecto
    return r2(Decimal(fila["total"]) / 3 / 30)


def parametros(conn: Conexion, fecha: date) -> M.ParametrosLegales:
    """Parámetros legales vigentes en `fecha` (en caché 5 minutos: son globales y casi nunca cambian;
    al aprobar un juego nuevo desde la plataforma se borra la caché)."""
    def cargar() -> M.ParametrosLegales:
        fila = conn.execute("select * from public.parametros_vigentes(%s)", (fecha,)).fetchone()
        if not fila:
            raise ValueError(f"No hay parámetros legales vigentes para {fecha}")
        return M.desde_json(fila["vigente_desde"], fila["datos"])
    return cache.obtener(f"parametros:{fecha.isoformat()}", 300, cargar)


def configuracion(conn, empresa_id) -> M.ConfigEmpresa:
    c = conn.execute("select * from configuracion_planilla where empresa_id = %s", (empresa_id,)).fetchone()
    if not c:
        return M.ConfigEmpresa()
    return M.ConfigEmpresa(aplicar_bmc=c["aplicar_bmc"], bmc_monto=c["bmc_monto"], base_renta=c["base_renta"],
                           metodo_renta=c["metodo_renta"], cesantia_modo=c["cesantia_modo"],
                           cesantia_tasa=Decimal(c["cesantia_tasa"]))


def empleados_de_planilla(conn, pl: dict) -> list[dict]:
    p: dict[str, Any] = {"e": pl["empresa_id"], "d": pl["desde"], "h": pl["hasta"]}
    if pl.get("calendario_id"):
        conds = [sql.SQL("calendario_id = %(c)s")]; p["c"] = pl["calendario_id"]
    else:
        conds = [sql.SQL("frecuencia = %(f)s")]; p["f"] = pl["frecuencia"]
    if pl.get("sucursal_id"):
        conds.append(sql.SQL("sucursal_id = %(s)s")); p["s"] = pl["sucursal_id"]
    q = sql.SQL("""select * from empleados where empresa_id = %(e)s and fecha_ingreso <= %(h)s
             and (fecha_salida is null or fecha_salida >= %(d)s)
             and (estado = 'activo' or (fecha_salida is not null and fecha_salida >= %(d)s))
             and {} order by apellidos, nombre""").format(sql.SQL(" and ").join(conds))
    return conn.execute(q, p).fetchall()


def precargar(conn, pl: dict, ids: list) -> dict[str, dict]:
    """Novedades del periodo de TODOS los colaboradores de la planilla, con una consulta por tipo.
    Antes eran ~15 consultas por colaborador (300 colaboradores = ~4 500 idas y vueltas a la base);
    ahora son 13 en total, sin importar cuántos colaboradores haya. Devuelve {tipo: {empleado_id: [filas]}}."""
    d, h = pl["desde"], pl["hasta"]
    params = {"ids": list(ids), "d": d, "h": h, "h1": h + timedelta(days=1)}

    def agrupar(cur) -> dict:
        grupo: dict = {}
        for fila in cur.fetchall():
            grupo.setdefault(fila["empleado_id"], []).append(fila)
        return grupo

    return {
        "cambio": agrupar(conn.execute("""select distinct on (empleado_id) empleado_id, salario_nuevo from historial_salarial
                       where empleado_id = any(%(ids)s) and estado = 'autorizado' and fecha_efectiva between %(d)s and %(h)s
                       order by empleado_id, fecha_efectiva desc""", params)),
        "marcas": agrupar(conn.execute("""select empleado_id, coalesce(sum(extract(epoch from (salida - entrada)) / 3600), 0) as h
                       from marcas_asistencia where empleado_id = any(%(ids)s) and salida is not null
                        and entrada >= %(d)s and entrada < %(h1)s group by empleado_id""", params)),
        "horas_extra": agrupar(conn.execute("""select * from horas_extra where empleado_id = any(%(ids)s) and estado = 'aprobada'
                            and fecha between %(d)s and %(h)s and boleta_id is null order by empleado_id, fecha, id""", params)),
        "vac_dinero": agrupar(conn.execute("""select * from vacaciones where empleado_id = any(%(ids)s) and estado = 'aprobada' and tipo = 'dinero'
                           and boleta_id is null and coalesce(desde, %(d)s) <= %(h)s order by empleado_id, desde, id""", params)),
        "vac_disfrute": agrupar(conn.execute("""select id, empleado_id from vacaciones where empleado_id = any(%(ids)s) and estado = 'aprobada'
                             and tipo = 'disfrute' and boleta_id is null and desde <= %(h)s order by empleado_id, desde, id""", params)),
        "incapacidades": agrupar(conn.execute("""select * from incapacidades where empleado_id = any(%(ids)s) and estado = 'confirmada'
                              and desde <= %(h)s and hasta >= %(d)s::date - 40 order by empleado_id, desde""", params)),
        "permisos": agrupar(conn.execute("""select * from permisos where empleado_id = any(%(ids)s) and estado = 'aprobado'
                         and desde <= %(h)s and hasta >= %(d)s order by empleado_id, desde, id""", params)),
        "bonos": agrupar(conn.execute("""select * from bonos where empleado_id = any(%(ids)s) and activo and desde <= %(h)s
                      and (hasta is null or hasta >= %(d)s) order by empleado_id, desde, id""", params)),
        "otras": agrupar(conn.execute("""select * from otras_deducciones where empleado_id = any(%(ids)s) and activo and desde <= %(h)s
                      and (hasta is null or hasta >= %(d)s) order by empleado_id, desde, id""", params)),
        "rebajos": agrupar(conn.execute("""select empleado_id, monto from rebajos_renta where empleado_id = any(%(ids)s)
                        and desde <= %(h)s and hasta >= %(d)s""", params)),
        "prestamos": agrupar(conn.execute("""select * from prestamos where empleado_id = any(%(ids)s) and estado = 'activo' and saldo > 0
                          and primera_rebaja <= %(h)s order by empleado_id, primera_rebaja, id""", params)),
        "pensiones": agrupar(conn.execute("""select * from pensiones_alimentarias where empleado_id = any(%(ids)s) and estado = 'activo'
                          and desde <= %(h)s and (hasta is null or hasta >= %(d)s) order by empleado_id, desde, id""", params)),
        "embargos": agrupar(conn.execute("""select * from embargos where empleado_id = any(%(ids)s) and estado = 'activo'
                         and desde <= %(h)s and (hasta is null or hasta >= %(d)s) order by empleado_id, desde, id""", params)),
    }


def boletas_del_mes(conn, pl: dict, ids: list) -> dict:
    """Boletas del mismo mes en otras planillas (método mensual consolidado), de todos a la vez."""
    grupo: dict = {}
    for b in conn.execute("""select distinct on (b.empleado_id, p.desde, p.hasta) b.empleado_id, b.base_cotizable,
                                    b.ccss_obrero, b.renta, b.creditos_renta
                               from boletas b join planillas p on p.id = b.planilla_id
                              where b.empleado_id = any(%s) and p.id <> %s and p.tipo = 'ordinaria'
                                and p.frecuencia = %s and p.hasta < %s
                                and date_trunc('month', p.hasta) = date_trunc('month', %s::date)
                              order by b.empleado_id, p.desde, p.hasta,
                                       array_position(array['cerrada','aprobada','calculada','borrador'], p.estado),
                                       p.creado_en""",
                          (list(ids), pl["id"], pl["frecuencia"], pl["desde"], pl["hasta"])).fetchall():
        grupo.setdefault(b["empleado_id"], []).append(b)
    return grupo


def armar_entrada(conn, pl: dict, emp: dict, cfg: M.ConfigEmpresa,
                  datos: dict[str, dict] | None = None) -> tuple[M.EntradaBoleta, dict]:
    """Devuelve la entrada del motor y las referencias usadas (para marcar al cerrar).
    `datos` = resultado de precargar() para toda la planilla; si no viene, se carga solo este colaborador."""
    d, h, eid = pl["desde"], pl["hasta"], emp["id"]
    frec = M.Frecuencia(pl["frecuencia"])
    ff = FACTOR[frec.value]
    salario = Decimal(emp["salario_mensual"])
    diario = r2(salario / 30)
    refs: dict[str, list] = {"horas_extra": [], "vacaciones": [], "prestamos": []}
    if datos is None:
        datos = precargar(conn, pl, [eid])

    def filas(clave: str) -> list:
        return datos[clave].get(eid, [])

    # Proporcional por ingreso o salida dentro del periodo
    ini = max(d, emp["fecha_ingreso"])
    fin = min(h, emp["fecha_salida"]) if emp["fecha_salida"] else h
    dias_trab = (fin - ini).days + 1 if (ini > d or fin < h) else None

    cambio = next(iter(filas("cambio")), None)

    horas_asist = CERO
    if emp["por_asistencia"]:
        fila = next(iter(filas("marcas")), None)
        horas_asist = r2(Decimal(str(fila["h"]))) if fila else CERO

    empleado = M.Empleado(
        salario_mensual=salario, frecuencia=frec, fecha_ingreso=emp["fecha_ingreso"],
        fecha_nacimiento=emp["fecha_nacimiento"], pensionado=emp["pensionado"],
        clase_riesgo_ins=emp["clase_riesgo_ins"] or pl.get("_clase_riesgo_empresa") or "II",
        hijos=emp["hijos"], conyuge=emp["conyuge"], num_empleadores=emp["num_empleadores"],
        # No asegurado en la CCSS = sin CCSS, renta ni INS (la «planilla SP» del módulo Odoo).
        exento=emp["exento"] or not emp.get("asegurado_ccss", True),
        por_asistencia=emp["por_asistencia"], tarifa_hora=Decimal(emp["tarifa_hora"]), medio_tiempo=emp["medio_tiempo"],
        aplica_rop=emp["aplica_rop"])

    # Último periodo del mes (para el método mensual consolidado)
    ultima = (h + timedelta(days=1)).month != h.month or (emp["fecha_salida"] is not None and emp["fecha_salida"] <= h)
    periodo = M.Periodo(d, h, dias_trabajados=dias_trab, horas_asistencia=horas_asist, ultima_del_mes=ultima)
    e = M.EntradaBoleta(empleado, periodo, cfg, salario_cambio_en_periodo=cambio["salario_nuevo"] if cambio else None)

    # Horas extra aprobadas del periodo, aún no pagadas
    for x in filas("horas_extra"):
        e.horas_extra.append(M.HoraExtra(Decimal(x["monto"]), Decimal(x["horas"]), x["afecto_ccss"], x["tipo"] == "feriado"))
        refs["horas_extra"].append(x["id"])

    # Vacaciones pagadas en dinero
    for v in filas("vac_dinero"):
        e.vacaciones_dinero += Decimal(v["monto"]) if v["monto"] is not None else r2(Decimal(v["dias"]) * diario)
        refs["vacaciones"].append(v["id"])
    for v in filas("vac_disfrute"):
        refs["vacaciones"].append(v["id"])

    # Incapacidades
    for i in filas("incapacidades"):
        e.incapacidades.append(M.Incapacidad(
            M.TipoIncapacidad(i["tipo"]), i["desde"], i["hasta"], Decimal(i["salario_diario"] or diario),
            Decimal(i["salario_promedio_diario"] or 0), Decimal(i["porcentaje_subsidio"]), i["es_prorroga"],
            i["medio_dia_extra"], i["maternidad_ccss_por_patrono"], i["maternidad_50_50"]))
    # Solo se pasan las que tocan el periodo o son parte del mismo evento (prórrogas previas).
    e.incapacidades = [i for i in e.incapacidades if i.hasta >= d - timedelta(days=40)]

    # Permisos
    for p in filas("permisos"):
        dias = Decimal(_dias(d, h, p["desde"], p["hasta"]))
        total_dias = Decimal((p["hasta"] - p["desde"]).days + 1)
        # Los días del permiso se reparten en su rango: una paternidad de 8 días en 4 semanas
        # cae 4 y 4 en dos quincenas (antes se tomaban los 8 en cada una).
        dias_ef = (Decimal(p["dias"]) * dias / total_dias).quantize(Decimal("0.01")) if total_dias else dias
        lic = licencias.tipo(p["tipo"])
        if lic and lic.pago == "50_50" and p["con_goce"]:
            # Paternidad y adopción (Art. 95 CT, Ley 10.211): los días de ley no se pagan como salario;
            # el patrono paga el 50 % del salario promedio de los 3 meses anteriores y la CCSS el otro 50 %.
            dias_ley = dias_ef * min(Decimal(1), Decimal(lic.dias_ley) / Decimal(p["dias"])) if lic.dias_ley else dias_ef
            if dias_ley > 0:
                prom = promedio_diario_3_meses(conn, eid, p["desde"], diario)
                e.lineas.append(M.Linea("deduccion", M.Categoria.LICENCIA_SIN_GOCE, r2(dias_ley * diario),
                                        f"{lic.nombre.split(' (')[0]}: {dias_ley.normalize():f} día(s) fuera del salario",
                                        referencia=str(p["id"])))
                e.lineas.append(M.Linea("ingreso", M.Categoria.OTRO, r2(dias_ley * prom / 2),
                                        f"Licencia de {lic.nombre.split(' (')[0].lower()} — 50 % patrono ({lic.base_legal})",
                                        referencia=str(p["id"])))
            continue                          # los días por encima de la ley se pagan como salario normal
        if lic and lic.unidad == "horas" and p["con_goce"]:
            continue                          # lactancia: la hora diaria se paga dentro del salario
        monto = r2(Decimal(p["monto"]) * dias_ef / Decimal(p["dias"])) if p["monto"] is not None and p["dias"] else r2(dias_ef * diario)
        if p["con_goce"]:
            e.lineas.append(M.Linea("ingreso", M.Categoria.LICENCIA_CON_GOCE, monto, f"Permiso con goce ({p['tipo']})",
                                    referencia=str(p["id"])))
        else:
            cat = M.Categoria.AUSENCIA if p["ausencia"] else M.Categoria.LICENCIA_SIN_GOCE
            e.lineas.append(M.Linea("deduccion", cat, monto,
                                    "Ausencia" if p["ausencia"] else f"Permiso sin goce ({p['tipo']})",
                                    referencia=str(p["id"])))

    # Bonos: recurrentes en cada periodo de su vigencia; únicos cuando su fecha cae en el periodo
    for b in filas("bonos"):
        if b["recurrente"] or d <= b["desde"] <= h:
            e.lineas.append(M.Linea("ingreso", M.Categoria.BONO, Decimal(b["monto"]), b["nombre"],
                                    afecto_ccss=b["afecto_ccss"], afecto_salario_base=b["afecto_salario_base"],
                                    referencia=str(b["id"])))

    for o in filas("otras"):
        if o["recurrente"] or d <= o["desde"] <= h:
            e.lineas.append(M.Linea("deduccion", M.Categoria.OTRO, Decimal(o["monto"]), o["descripcion"],
                                    referencia=str(o["id"])))

    for x in filas("rebajos"):
        e.rebajo_renta += Decimal(x["monto"])

    # Préstamos: cuota hasta el saldo
    for pr in filas("prestamos"):
        cuota = min(Decimal(pr["cuota"]), Decimal(pr["saldo"]))
        e.lineas.append(M.Linea("deduccion", M.Categoria.PRESTAMO, cuota, pr["descripcion"], referencia=str(pr["id"])))
        refs["prestamos"].append({"id": pr["id"], "monto": str(cuota)})

    # Pensiones alimentarias y embargos (montos fijos son mensuales: se prorratean)
    for p in filas("pensiones"):
        valor = r2(Decimal(p["valor"]) * ff) if p["tipo"] == "fijo" else Decimal(p["valor"])
        e.pensiones.append(M.Embargo(p["tipo"], valor, p["expediente"], referencia=str(p["id"])))
    for x in filas("embargos"):
        valor = r2(Decimal(x["valor"]) * ff) if x["tipo"] == "fijo" else Decimal(x["valor"])
        e.embargos.append(M.Embargo(x["tipo"], valor, x["expediente"], referencia=str(x["id"])))

    # Boletas del mismo mes en otras planillas (método mensual consolidado).
    # Igual que el módulo Odoo (FIX17): cuentan aunque no estén aprobadas; si no, al calcular las dos
    # quincenas antes de aprobar, la última del mes calculaba la renta sobre una sola quincena.
    # Si hubiera dos planillas del mismo periodo, cuenta una sola: cerrada > aprobada > calculada > borrador.
    if cfg.metodo_renta == "monthly_consolidated" and ultima:
        if "mes" not in datos:
            datos["mes"] = boletas_del_mes(conn, pl, [eid])
        for b in datos["mes"].get(eid, []):
            e.boletas_anteriores_mes.append(M.BoletaAnterior(b["base_cotizable"], b["ccss_obrero"], b["renta"],
                                                             b["creditos_renta"]))
    return e, refs


def lineas_boleta(res: M.ResultadoBoleta) -> list[dict]:
    """Detalle legible de la boleta (ingresos, deducciones y aportes patronales)."""
    out: list[dict] = []

    def add(tipo, cat, desc, monto, base=None, tasa=None, ref=None):
        if monto:
            out.append({"tipo": tipo, "categoria": cat, "descripcion": desc, "monto": monto, "base": base,
                        "tasa": tasa, "referencia_id": ref, "orden": len(out)})

    sal = res.salario_cotizable if res.dias_incapacidad else res.salario_base
    add("ingreso", "salario", "Salario del periodo", sal)
    add("ingreso", "horas_extra", "Horas extra", res.horas_extra)
    add("ingreso", "vacaciones", "Vacaciones pagadas", res.vacaciones)
    for l in res.lineas:
        if l.tipo == "ingreso" and l.categoria != M.Categoria.LICENCIA_CON_GOCE:
            add("ingreso", l.categoria.value, l.descripcion, l.monto, ref=l.referencia)
    add("ingreso", "subsidio_ccss", "Subsidio por incapacidad (CCSS)", res.subsidio_ccss)
    add("ingreso", "incapacidad_patrono", "Incapacidad días 1-3 (patrono 50 %)", res.costo_patrono_incapacidad)
    add("ingreso", "paternidad", "Licencia de paternidad", res.paternidad)
    add("deduccion", "ccss", "CCSS obrero", res.ccss_obrero, res.base_ccss)
    add("deduccion", "renta", "Impuesto al salario", res.renta, res.base_cotizable)
    for l in res.lineas:
        if l.tipo == "deduccion":
            add("deduccion", l.categoria.value, l.descripcion, l.monto, ref=l.referencia)
    add("patronal", "ccss_patronal", "CCSS patronal", res.ccss_patronal, res.base_ccss)
    add("patronal", "ins", "INS riesgos del trabajo", res.ins_patronal, res.base_cotizable)
    add("patronal", "rop_patronal", "ROP patronal", res.rop_patronal)
    add("patronal", "prov_aguinaldo", "Provisión aguinaldo", res.prov_aguinaldo)
    add("patronal", "prov_cesantia", "Provisión cesantía", res.prov_cesantia, tasa=res.tasa_cesantia)
    add("patronal", "prov_vacaciones", "Provisión vacaciones", res.prov_vacaciones)
    return out


def advertencias_de(emp: dict, res: M.ResultadoBoleta) -> list[dict]:
    nombre = f"{emp['nombre']} {emp['apellidos']}".strip()
    av = []
    if res.neto < 0:
        av.append({"nivel": "error", "empleado_id": str(emp["id"]), "mensaje": f"{nombre}: neto negativo ({res.neto})"})
    if not emp.get("iban"):
        av.append({"nivel": "aviso", "empleado_id": str(emp["id"]), "mensaje": f"{nombre}: sin cuenta IBAN para el pago"})
    if not emp.get("correo"):
        av.append({"nivel": "info", "empleado_id": str(emp["id"]), "mensaje": f"{nombre}: sin correo para enviar la boleta"})
    if res.bmc_aplicada:
        av.append({"nivel": "info", "empleado_id": str(emp["id"]),
                   "mensaje": f"{nombre}: CCSS calculada sobre la base mínima contributiva"})
    if res.renta < 0:
        av.append({"nivel": "aviso", "empleado_id": str(emp["id"]),
                   "mensaje": f"{nombre}: la reconciliación mensual devuelve renta ({res.renta})"})
    for n in res.notas:
        av.append({"nivel": "aviso", "empleado_id": str(emp["id"]), "mensaje": f"{nombre}: {n}"})
    return av


def detectar_horas_extra(conn: Conexion, pl: dict, empleados: list[dict]) -> int:
    """Configuración › Planilla › «Horas extra desde las marcas» (como el módulo Odoo).
    Por cada colaborador y día del periodo: horas marcadas por encima de su jornada diaria,
    si pasan de la tolerancia, quedan como hora extra PENDIENTE de aprobar (no se pagan solas).
    Una por día (índice horas_extra_automatica_dia): recalcular no las duplica."""
    cfg = conn.execute("""select he_desde_asistencia, tolerancia_tardia_min, factor_he_diurna
                            from configuracion_planilla where empresa_id = %s""", (pl["empresa_id"],)).fetchone()
    if not cfg or not cfg["he_desde_asistencia"]:
        return 0
    tolerancia = Decimal(cfg["tolerancia_tardia_min"]) / 60
    nuevas = 0
    ids = [e["id"] for e in empleados if not e["por_asistencia"]]
    if not ids:
        return 0
    horarios = list({e["horario_id"] for e in empleados if e.get("horario_id")})
    jornadas = {x["id"]: Decimal(x["horas_diarias"]) for x in conn.execute(
        "select id, horas_diarias from tipos_horario where id = any(%s)", (horarios,)).fetchall()} if horarios else {}
    marcas: dict = {}
    for x in conn.execute("""select empleado_id, (entrada at time zone 'America/Costa_Rica')::date as dia,
                                    sum(extract(epoch from (salida - entrada)) / 3600) as horas
                               from marcas_asistencia where empleado_id = any(%s) and salida is not null
                                and (entrada at time zone 'America/Costa_Rica')::date between %s and %s
                              group by 1, 2 order by 1, 2""", (ids, pl["desde"], pl["hasta"])).fetchall():
        marcas.setdefault(x["empleado_id"], []).append(x)
    for emp in empleados:
        if emp["por_asistencia"]:
            continue                              # a quien se le paga por hora, las horas ya son su salario
        jornada = Decimal("8")
        if emp.get("horario_id") in jornadas:
            jornada = jornadas[emp["horario_id"]]
        tarifa = r2(Decimal(emp["salario_mensual"]) / 30 / jornada) if jornada else CERO
        for d in marcas.get(emp["id"], []):
            extra = r2(Decimal(str(d["horas"])) - jornada)
            if extra <= tolerancia or extra <= 0:
                continue
            factor = Decimal(cfg["factor_he_diurna"])
            nuevas += conn.execute("""insert into horas_extra (empresa_id, empleado_id, fecha, horas, tipo, factor, tarifa_hora,
                                         monto, origen, estado)
                                      values (%s,%s,%s,%s,'diurna',%s,%s,%s,'automatico','pendiente')
                                      on conflict (empleado_id, fecha) where origen = 'automatico' do nothing""",
                                   (pl["empresa_id"], emp["id"], d["dia"], extra, factor, tarifa,
                                    r2(extra * tarifa * factor))).rowcount
    return nuevas


_COLS_BOLETA = ("id", "empresa_id", "planilla_id", "empleado_id", "salario_base", "horas_extra", "vacaciones", "bonos",
                "bruto", "base_cotizable", "base_ccss", "ccss_obrero", "renta", "creditos_renta", "rop_obrero",
                "total_deducciones", "subsidio_ccss", "subsidio_ins", "costo_patrono_incapacidad", "neto",
                "ccss_patronal", "ins_patronal", "rop_patronal", "prov_aguinaldo", "prov_cesantia", "prov_vacaciones",
                "costo_total_patrono", "dias_incapacidad", "resultado", "entrada", "parametros", "version_motor")
_COLS_LINEA = ("tipo", "categoria", "descripcion", "base", "tasa", "monto", "referencia_id", "orden")


def _json(v) -> str:
    """JSON para jsonb_populate_recordset: montos Decimal como texto exacto (sin pasar por float)."""
    return json.dumps(v, default=str, ensure_ascii=False)


def calcular_planilla(conn: Conexion, planilla_id) -> dict:
    pl = conn.execute("select * from planillas where id = %s for update", (planilla_id,)).fetchone()
    if not pl:
        raise LookupError("Planilla no encontrada")
    if pl["estado"] not in ("borrador", "calculada"):
        raise PermissionError(f"La planilla está {pl['estado']}: no se puede recalcular")
    if pl["tipo"] not in ("ordinaria", "extraordinaria"):
        raise PermissionError("Esta planilla se calcula desde su propio proceso (aguinaldo o liquidación)")
    emp_cls = conn.execute("select clase_riesgo_ins from empresas where id = %s", (pl["empresa_id"],)).fetchone()
    pl["_clase_riesgo_empresa"] = emp_cls["clase_riesgo_ins"] if emp_cls else "II"

    p = parametros(conn, pl["desde"])
    cfg = configuracion(conn, pl["empresa_id"])
    conn.execute("delete from boletas where planilla_id = %s", (planilla_id,))

    tot = {k: Decimal(0) for k in ("bruto", "ccss_obrero", "renta", "total_deducciones", "neto", "ccss_patronal",
                                   "ins_patronal", "rop_patronal", "provisiones", "costo_total_patrono",
                                   "subsidio_ccss", "embargos")}
    advertencias: list[dict] = []
    empleados = empleados_de_planilla(conn, pl)
    detectadas = detectar_horas_extra(conn, pl, empleados)
    pendientes = conn.execute("""select count(*) as n from horas_extra where empresa_id = %s and origen = 'automatico'
                                   and estado = 'pendiente' and fecha between %s and %s""",
                              (pl["empresa_id"], pl["desde"], pl["hasta"])).fetchone()["n"]
    if pendientes:
        advertencias.append({"nivel": "aviso", "mensaje":
            f"Hay {pendientes} día(s) con horas extra detectadas en las marcas ({detectadas} nuevas) pendientes de aprobar. "
            "Apruébelas en Novedades › Horas extra y recalcule para pagarlas en esta planilla."})
    n = 0
    filas_boletas: list[dict] = []
    filas_lineas: list[dict] = []
    datos = precargar(conn, pl, [emp["id"] for emp in empleados])
    if cfg.metodo_renta == "monthly_consolidated":
        datos["mes"] = boletas_del_mes(conn, pl, [emp["id"] for emp in empleados])
    for emp in empleados:
        entrada, refs = armar_entrada(conn, pl, emp, cfg, datos)
        res = M.calcular_boleta(entrada, p)
        n += 1
        boleta_id = uuid.uuid4()
        filas_boletas.append(dict(zip(_COLS_BOLETA,
            (boleta_id, pl["empresa_id"], planilla_id, emp["id"], res.salario_base, res.horas_extra, res.vacaciones,
             res.bono_salarial + res.otros_ingresos, res.bruto, res.base_cotizable, res.base_ccss, res.ccss_obrero,
             res.renta, res.creditos_renta, res.rop_obrero, res.total_deducciones, res.subsidio_ccss, res.subsidio_ins,
             res.costo_patrono_incapacidad, res.neto, res.ccss_patronal, res.ins_patronal, res.rop_patronal,
             res.prov_aguinaldo, res.prov_cesantia, res.prov_vacaciones, res.costo_total_patrono, res.dias_incapacidad,
             M.a_dict(res), {**M.a_dict(entrada), "_refs": M.a_dict(refs)}, M.a_json(p),
             M.__version__))))
        filas_lineas += [{"empresa_id": pl["empresa_id"], "boleta_id": boleta_id, **{c: l[c] for c in _COLS_LINEA}}
                         for l in lineas_boleta(res)]
        for k in tot:
            if k == "provisiones":
                tot[k] += res.prov_aguinaldo + res.prov_cesantia + res.prov_vacaciones
            else:
                tot[k] += getattr(res, k)
        advertencias += advertencias_de(emp, res)

    # Se guardan todas las boletas y sus líneas en pocas sentencias (antes: dos por colaborador y una por línea).
    # Una sola sentencia por bloque hace que la RLS calcule los permisos una vez y no por cada fila.
    for k in range(0, len(filas_boletas), 200):
        conn.execute("""insert into boletas (id, empresa_id, planilla_id, empleado_id, salario_base, horas_extra, vacaciones,
                   bonos, bruto, base_cotizable, base_ccss, ccss_obrero, renta, creditos_renta, rop_obrero,
                   total_deducciones, subsidio_ccss, subsidio_ins, costo_patrono_incapacidad, neto, ccss_patronal,
                   ins_patronal, rop_patronal, prov_aguinaldo, prov_cesantia, prov_vacaciones, costo_total_patrono,
                   dias_incapacidad, resultado, entrada, parametros, version_motor)
            select id, empresa_id, planilla_id, empleado_id, salario_base, horas_extra, vacaciones,
                   bonos, bruto, base_cotizable, base_ccss, ccss_obrero, renta, creditos_renta, rop_obrero,
                   total_deducciones, subsidio_ccss, subsidio_ins, costo_patrono_incapacidad, neto, ccss_patronal,
                   ins_patronal, rop_patronal, prov_aguinaldo, prov_cesantia, prov_vacaciones, costo_total_patrono,
                   dias_incapacidad, resultado, entrada, parametros, version_motor
              from jsonb_populate_recordset(null::public.boletas, %s)""",
                     (Jsonb(filas_boletas[k:k + 200], dumps=_json),))
    for k in range(0, len(filas_lineas), 2000):
        conn.execute("""insert into boleta_lineas (empresa_id, boleta_id, tipo, categoria, descripcion, base, tasa,
                            monto, referencia_id, orden)
                        select empresa_id, boleta_id, tipo, categoria, descripcion, base, tasa, monto, referencia_id, orden
                          from jsonb_populate_recordset(null::public.boleta_lineas, %s)""",
                     (Jsonb(filas_lineas[k:k + 2000], dumps=_json),))

    if n == 0:
        advertencias.append({"nivel": "error", "mensaje": "No hay empleados para este calendario y periodo"})
    totales = {"empleados": n, **{k: str(v) for k, v in tot.items()}}
    conn.execute("""update planillas set estado = 'calculada', totales = %s, advertencias = %s, calculada_en = now()
                    where id = %s""", (Jsonb(totales), Jsonb(advertencias), planilla_id))
    return {"totales": totales, "advertencias": advertencias}
