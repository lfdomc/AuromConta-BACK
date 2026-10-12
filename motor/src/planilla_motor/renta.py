"""Impuesto al salario (Art. 33-34 LIR, tramos mensuales).

Porta `_calc_income_tax` y `_reconcile_monthly_income_tax` del modulo Odoo.
"""
from __future__ import annotations

from decimal import Decimal

from .dinero import CERO, D, r2
from .modelos import BoletaAnterior, Empleado, Frecuencia
from .parametros import ParametrosLegales


def impuesto_mensual(base_mensual: Decimal, p: ParametrosLegales) -> Decimal:
    """Impuesto progresivo sobre un ingreso mensual (sin creditos)."""
    impuesto = CERO
    for t in p.tramos_renta:
        if base_mensual <= t.desde:
            break
        tope = t.hasta if t.hasta is not None else base_mensual
        gravable = min(base_mensual, tope) - t.desde
        if gravable > 0:
            impuesto += gravable * t.tasa / 100
    return impuesto


def creditos_periodo(emp: Empleado, p: ParametrosLegales, factor: Decimal) -> tuple[Decimal, Decimal]:
    """(credito_hijos, credito_conyuge) proporcionales al periodo."""
    hijos = D(emp.hijos) * p.credito_hijo * factor
    conyuge = p.credito_conyuge * factor if emp.conyuge else CERO
    return hijos, conyuge


def calcular_renta(
    base: Decimal,
    emp: Empleado,
    p: ParametrosLegales,
    ccss_obrero: Decimal = CERO,
    base_renta: str = "gross",
    ya_mensual: bool = False,
) -> tuple[Decimal, Decimal]:
    """Devuelve (renta_neta, creditos_aplicados) del periodo.

    - Proyecta el ingreso del periodo al mes (x periodos_por_mes), aplica
      tramos y divide de vuelta. Todo el bruto se proyecta igual (bonos
      puntuales incluidos).
    - `ya_mensual=True`: la base ya es el ingreso real del mes completo
      (reconciliacion mensual consolidada).
    - Los creditos se restan del impuesto y nunca generan saldo a favor.
    """
    if base_renta == "net_ccss":
        base = max(base - ccss_obrero, CERO)

    frec: Frecuencia = emp.frecuencia
    ppm = frec.periodos_por_mes
    mensual = base if ya_mensual else base * ppm
    imp_mes = impuesto_mensual(mensual, p)
    imp_periodo = imp_mes if ya_mensual else (imp_mes / ppm if ppm else CERO)

    factor = D(1) if ya_mensual else frec.factor
    hijos, conyuge = creditos_periodo(emp, p, factor)
    total_cred = hijos + conyuge
    if emp.num_empleadores > 1:
        total_cred = r2(total_cred / emp.num_empleadores)

    aplicados = min(total_cred, imp_periodo)
    neta = max(imp_periodo - total_cred, CERO)
    return neta, aplicados


def reconciliar_mes(
    base_propia: Decimal,
    ccss_propia: Decimal,
    anteriores: list[BoletaAnterior],
    emp: Empleado,
    p: ParametrosLegales,
    base_renta: str = "gross",
) -> tuple[Decimal, Decimal]:
    """Metodo mensual consolidado: en la ultima boleta del mes calcula la renta
    sobre el ingreso real del mes y resta lo ya retenido. Puede dar negativo
    (devolucion al empleado)."""
    base_mes = base_propia + sum((b.base_cotizable for b in anteriores), CERO)
    ccss_mes = ccss_propia + sum((b.ccss_obrero for b in anteriores), CERO)
    retenido = sum((b.renta for b in anteriores), CERO)
    cred_usados = sum((b.creditos for b in anteriores), CERO)

    renta_mes, cred_mes = calcular_renta(base_mes, emp, p, ccss_mes, base_renta, ya_mensual=True)
    return renta_mes - retenido, max(cred_mes - cred_usados, CERO)
