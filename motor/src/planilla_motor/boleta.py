"""Calculo completo de una boleta de pago (Costa Rica).

Orden del calculo (igual que el modulo Odoo planilla_cr):
  1. Salario base del periodo (fijo x frecuencia x proporcional, o por horas).
  2. Extras: horas extra, vacaciones pagadas en dinero, bonos.
  3. Incapacidades: subsidios, dias patronales, salario cotizable.
  4. Bruto.
  5. Base cotizable (resta HE no afectas y licencias sin goce/ausencias).
  6. CCSS obrero/patronal con piso BMC, INS, renta (+ reconciliacion mensual).
  7. ROP, pensiones alimentarias y embargos (tope del Art. 172 CT).
  8. Provisiones: aguinaldo, cesantia, vacaciones.
  9. Totales: deducciones, neto, costo patronal.
"""
from __future__ import annotations

from datetime import date

from .dinero import CERO, D, r, r2, suma
from .incapacidades import calcular_incapacidades
from .modelos import Categoria, EntradaBoleta, Linea, ResultadoBoleta
from .parametros import ParametrosLegales, parametros_para
from .renta import calcular_renta, creditos_periodo, reconciliar_mes


def _edad(nacimiento: date, ref: date) -> int:
    return ref.year - nacimiento.year - ((ref.month, ref.day) < (nacimiento.month, nacimiento.day))


def tasa_cesantia(e: EntradaBoleta, p: ParametrosLegales):
    """Tasa de provision de cesantia segun Art. 29 CT (dias / 360)."""
    if e.config.cesantia_modo == "custom":
        return r(e.config.cesantia_tasa / 100, 6)
    ingreso = e.empleado.fecha_ingreso
    if not ingreso:
        return p.prov_cesantia_defecto
    anios = (e.periodo.desde - ingreso).days // 365
    anio_servicio = max(1, min(anios + 1, p.cesantia_max_anios))
    dias = p.cesantia_tabla.get(anio_servicio, p.cesantia_tabla[p.cesantia_max_anios])
    return r(dias / 360, 6)


def tope_embargable(liquido, minimo):
    """Máximo embargable de un salario líquido según el Art. 172 del Código de Trabajo."""
    liquido, minimo = D(liquido), D(minimo)
    if liquido <= minimo:
        return CERO
    tramo1 = min(liquido, minimo * 3) - minimo
    tramo2 = max(liquido - minimo * 3, CERO)
    return r2(tramo1 / 8 + tramo2 / 4)


def calcular_boleta(e: EntradaBoleta, p: ParametrosLegales | None = None) -> ResultadoBoleta:
    emp, per, cfg = e.empleado, e.periodo, e.config
    p = p or parametros_para(per.desde)
    res = ResultadoBoleta()
    ff = emp.frecuencia.factor

    # 1. Salario base ---------------------------------------------------------
    res.dias_periodo = (per.hasta - per.desde).days + 1
    if per.dias_trabajados is not None and res.dias_periodo > 0:
        res.factor_proporcional = r(D(per.dias_trabajados) / res.dias_periodo, 4)
    prop = res.factor_proporcional
    salario_mes = e.salario_cambio_en_periodo or emp.salario_mensual

    if emp.por_asistencia:
        res.salario_base = r2(emp.tarifa_hora * per.horas_asistencia) if emp.salario_mensual else CERO
    else:
        res.salario_base = r2(salario_mes * ff * prop)

    # 2. Extras ---------------------------------------------------------------
    res.horas_extra = suma(h.monto for h in e.horas_extra)
    res.horas_extra_ccss = suma(h.monto for h in e.horas_extra if h.afecto_ccss)
    res.vacaciones = D(e.vacaciones_dinero)
    bonos = [l for l in e.lineas if l.tipo == "ingreso" and l.categoria == Categoria.BONO]
    res.bono_salarial = r2(suma(l.monto for l in bonos if l.afecto_ccss))
    res.bono_base_prestaciones = r2(suma(l.monto for l in bonos if l.afecto_salario_base))

    # 3. Incapacidades --------------------------------------------------------
    inc = calcular_incapacidades(e.incapacidades, per.desde, per.hasta,
                                 salario_mes if emp.salario_mensual else CERO,
                                 emp.frecuencia, prop, p)
    res.dias_incapacidad = inc.dias
    res.subsidio_ccss = inc.subsidio_ccss
    res.subsidio_ins = inc.subsidio_ins
    res.costo_patrono_incapacidad = inc.costo_patrono
    if inc.salario_cotizable is not None:
        res.salario_cotizable = inc.salario_cotizable
    elif emp.salario_mensual:
        res.salario_cotizable = r2(salario_mes * ff * prop)

    # 4. Bruto ----------------------------------------------------------------
    if inc.hay_incapacidad:
        sal = CERO if inc.maternidad_total else res.salario_cotizable
    else:
        sal = res.salario_base
    res.bruto = r2(sal + res.horas_extra + res.vacaciones
                   + D(e.otros_ingresos_salariales) + res.bono_salarial)

    # 5. Base cotizable -------------------------------------------------------
    if inc.hay_incapacidad:
        g = res.salario_cotizable + res.bono_salarial + res.horas_extra_ccss
    else:
        g = res.bruto - (res.horas_extra - res.horas_extra_ccss)
    lic_sg = r2(suma(l.monto for l in e.lineas if l.tipo == "deduccion"
                     and l.categoria in (Categoria.LICENCIA_SIN_GOCE, Categoria.AUSENCIA)))
    if lic_sg > 0:
        g = max(r2(g - lic_sg), CERO)
    res.base_cotizable = g

    # 6a. CCSS con piso BMC ---------------------------------------------------
    bmc_mes = cfg.bmc_monto or p.bmc_mensual
    bmc_periodo = r2(bmc_mes * ff)
    reducida = bool(emp.medio_tiempo and emp.fecha_nacimiento
                    and _edad(emp.fecha_nacimiento, per.desde) < p.edad_limite_bmc_reducida)
    base_ccss = g
    if (cfg.aplicar_bmc and not reducida and not inc.hay_incapacidad
            and lic_sg <= 0 and not emp.exento and g > 0 and g < bmc_periodo):
        base_ccss = bmc_periodo
        res.bmc_aplicada = True
    res.base_ccss = base_ccss
    res.bmc_reducida = reducida and g < bmc_periodo

    tasa_obr = p.ccss_obrero_pensionado_estado if emp.pensionado == "estado" else p.ccss_obrero
    res.ccss_obrero = CERO if emp.exento else r2(base_ccss * tasa_obr)

    if e.dias_paternidad > 0:
        # Diario = salario mensual / 30. Diferencia con Odoo (hallazgo H4):
        # Odoo usa g / 30 con g del periodo, que en quincenal da la mitad.
        res.paternidad = r2(r2(g / ff / p.dias_mes) * e.dias_paternidad)

    # 6b. Renta ---------------------------------------------------------------
    if emp.exento:
        renta, cred = CERO, CERO
    else:
        renta, cred = calcular_renta(g, emp, p, res.ccss_obrero, cfg.base_renta)
        if cfg.metodo_renta == "monthly_consolidated" and per.ultima_del_mes:
            renta, cred = reconciliar_mes(g, res.ccss_obrero, e.boletas_anteriores_mes,
                                          emp, p, cfg.base_renta)
    res.renta = r2(renta)
    res.creditos_renta = r2(cred)
    hijos, conyuge = creditos_periodo(emp, p, ff)
    res.credito_hijos, res.credito_conyuge = r2(hijos), r2(conyuge)

    # 6c. Cargas patronales ---------------------------------------------------
    if not emp.exento:
        res.ccss_patronal = r2(base_ccss * p.ccss_patronal)
        tasa_ins = p.ins_tasas.get(emp.clase_riesgo_ins or p.ins_clase_defecto,
                                   p.ins_tasas[p.ins_clase_defecto])
        res.ins_patronal = r2(g * tasa_ins)

    lineas: list[Linea] = list(e.lineas)

    # 7a. ROP (opcional por empleado; base = bruto, como en Odoo) -------------
    if emp.aplica_rop and res.bruto > 0:
        res.rop_obrero = r2(res.bruto * p.rop_obrero)
        res.rop_patronal = r2(res.bruto * p.rop_patronal)
        lineas.append(Linea("deduccion", Categoria.ROP, res.rop_obrero, "Aporte adicional de pensión 1 %"))

    def _cat(cat):
        return suma(l.monto for l in lineas if l.categoria == cat)

    # 7b. Pensiones alimentarias: primero, sobre bruto - CCSS - renta ---------
    for pen in e.pensiones:
        disp = max(CERO, res.bruto - res.ccss_obrero - res.renta - D(e.rebajo_renta)
                   - _cat(Categoria.PENSION_ALIMENTARIA))
        monto = D(pen.valor) if pen.tipo == "fijo" else r2(disp * D(pen.valor) / 100)
        monto = r2(min(monto, disp))
        if monto > 0:
            lineas.append(Linea("deduccion", Categoria.PENSION_ALIMENTARIA, monto,
                                f"Pensión alimentaria {pen.expediente}".strip(), referencia=pen.referencia))
            if monto < D(pen.valor) and pen.tipo == "fijo":
                res.notas.append(f"Pensión {pen.expediente}: se rebajó solo {monto} por falta de salario disponible.")

    # 7c. Embargos: inembargable hasta el menor salario mínimo; 1/8 hasta 3 veces ese
    #     monto y 1/4 del exceso (Art. 172 CT). La pensión alimentaria no tiene ese límite.

    if e.embargos:
        disponible = max(CERO, res.bruto - res.ccss_obrero - res.renta - D(e.rebajo_renta)
                         - _cat(Categoria.PENSION_ALIMENTARIA) - _cat(Categoria.AUSENCIA)
                         - _cat(Categoria.LICENCIA_SIN_GOCE))
        tope = tope_embargable(disponible, p.salario_minimo_inembargable * ff)
        ya = _cat(Categoria.EMBARGO)
        for emb in e.embargos:
            if emb.tipo == "fijo":
                monto = min(D(emb.valor), tope)
            else:
                monto = min(r2(disponible * D(emb.valor) / 100), tope)
            monto = min(monto, max(CERO, tope - ya))
            if monto <= 0:
                continue
            lineas.append(Linea("deduccion", Categoria.EMBARGO, monto,
                                f"Embargo judicial {emb.expediente}".strip(), referencia=emb.referencia))
            ya += monto
        res.embargos = r2(_cat(Categoria.EMBARGO))

    # 8. Provisiones ----------------------------------------------------------
    # Diferencia con Odoo (hallazgo H3): Odoo resta ademas el subsidio CCSS de
    # la base de aguinaldo, pero `g` ya excluye los dias incapacitados, asi que
    # lo restaba dos veces. Aqui la base de aguinaldo es `g`.
    base_agui = g
    solo_base = max(r2(res.bono_base_prestaciones - res.bono_salarial), CERO)
    res.tasa_cesantia = tasa_cesantia(e, p)
    res.prov_aguinaldo = r2((base_agui + solo_base) * p.prov_aguinaldo)
    res.prov_cesantia = r2((g + solo_base) * res.tasa_cesantia)
    res.prov_vacaciones = r2((g + solo_base) * p.prov_vacaciones)

    # 9. Totales --------------------------------------------------------------
    otros_ing = suma(l.monto for l in lineas if l.tipo == "ingreso"
                     and not (l.categoria == Categoria.BONO and l.afecto_ccss)
                     and l.categoria not in (Categoria.LICENCIA_CON_GOCE, Categoria.VACACIONES))
    res.otros_ingresos = r2(otros_ing)
    ded_lineas = suma(l.monto for l in lineas if l.tipo == "deduccion")
    res.total_deducciones = r2(res.ccss_obrero + res.renta + D(e.rebajo_renta) + ded_lineas)
    res.neto = r2(res.bruto - res.total_deducciones + res.subsidio_ccss
                  + res.costo_patrono_incapacidad + res.paternidad + res.otros_ingresos)

    # Costo total patronal (FIX16 del módulo Odoo, ver HALLAZGOS H2 y H11):
    #   - el bruto sin los días de permiso sin goce ni ausencias (el patrono no los paga);
    #   - los ingresos no salariales que sí paga (bonos no afectos, otros ingresos);
    #   - la incapacidad solo por los días 1-3 al 50 % (el subsidio lo paga la CCSS);
    #   - las licencias con goce NO se suman: ya están dentro del salario del periodo.
    costo_mat = CERO
    for i in e.incapacidades:
        if i.tipo.value == "maternity" and i.maternidad_50_50 and max(per.desde, i.desde) <= min(per.hasta, i.hasta):
            costo_mat = r2(res.subsidio_ccss / 2)
    costo_bruto = max(r2(res.bruto - lic_sg), CERO)
    res.costo_total_patrono = r2(costo_bruto + res.otros_ingresos + res.ccss_patronal + res.ins_patronal
                                 + res.rop_patronal + res.prov_aguinaldo + res.prov_cesantia + res.prov_vacaciones
                                 + res.paternidad + res.costo_patrono_incapacidad + costo_mat)
    res.lineas = lineas
    return res
