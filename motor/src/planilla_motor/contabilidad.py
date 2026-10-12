"""Asiento contable básico de una planilla (para el contador del cliente).

Débitos = gastos del patrono + derechos de cobro; créditos = lo que se debe
pagar (neto, CCSS, INS, renta, embargos...) + provisiones. Siempre cuadra.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal

from .dinero import CERO, r2
from .modelos import Categoria, ResultadoBoleta


@dataclass
class LineaAsiento:
    concepto: str
    descripcion: str
    debito: Decimal = CERO
    credito: Decimal = CERO


CONCEPTO_DEDUCCION = {
    Categoria.PENSION_ALIMENTARIA: "embargos_por_pagar",
    Categoria.EMBARGO: "embargos_por_pagar",
    Categoria.PRESTAMO: "prestamos_empleados",
    Categoria.ROP: "rop_por_pagar",
    Categoria.LICENCIA_SIN_GOCE: "gasto_salarios",   # reduce el gasto
    Categoria.AUSENCIA: "gasto_salarios",
    Categoria.OTRO: "otras_retenciones",
}

DESCRIPCIONES = {
    "gasto_salarios": "Sueldos y salarios", "gasto_cargas_sociales": "Cargas sociales patronales",
    "gasto_aguinaldo": "Provisión aguinaldo", "gasto_vacaciones": "Provisión vacaciones",
    "gasto_cesantia": "Provisión cesantía", "gasto_incapacidades": "Incapacidades a cargo del patrono",
    "subsidios_por_cobrar": "Subsidios CCSS pagados por planilla", "salarios_por_pagar": "Salarios netos por pagar",
    "ccss_por_pagar": "CCSS obrero y patronal", "ins_por_pagar": "INS riesgos del trabajo",
    "renta_por_pagar": "Impuesto al salario retenido", "rop_por_pagar": "ROP obrero y patronal",
    "embargos_por_pagar": "Embargos y pensiones alimentarias", "prestamos_empleados": "Abono a préstamos de empleados",
    "otras_retenciones": "Otras retenciones", "prov_aguinaldo": "Provisión aguinaldo",
    "prov_vacaciones": "Provisión vacaciones", "prov_cesantia": "Provisión cesantía",
}


def asiento_planilla(boletas: list[ResultadoBoleta]) -> list[LineaAsiento]:
    deb: "OrderedDict[str, Decimal]" = OrderedDict()
    cre: "OrderedDict[str, Decimal]" = OrderedDict()

    def d(k, v):
        if v:
            deb[k] = deb.get(k, CERO) + v

    def c(k, v):
        if v:
            cre[k] = cre.get(k, CERO) + v

    for b in boletas:
        d("gasto_salarios", b.bruto + b.otros_ingresos + b.paternidad)
        d("gasto_cargas_sociales", b.ccss_patronal + b.ins_patronal + b.rop_patronal)
        d("gasto_aguinaldo", b.prov_aguinaldo)
        d("gasto_vacaciones", b.prov_vacaciones)
        d("gasto_cesantia", b.prov_cesantia)
        d("gasto_incapacidades", b.costo_patrono_incapacidad)
        d("subsidios_por_cobrar", b.subsidio_ccss)

        c("salarios_por_pagar", b.neto)
        c("ccss_por_pagar", b.ccss_obrero + b.ccss_patronal)
        c("ins_por_pagar", b.ins_patronal)
        c("renta_por_pagar", b.renta)
        c("rop_por_pagar", b.rop_patronal)
        c("prov_aguinaldo", b.prov_aguinaldo)
        c("prov_vacaciones", b.prov_vacaciones)
        c("prov_cesantia", b.prov_cesantia)
        for l in b.lineas:
            if l.tipo == "deduccion":
                c(CONCEPTO_DEDUCCION.get(l.categoria, "otras_retenciones"), l.monto)
        # Rebajo consolidado de renta (si lo hubiera) va a renta por pagar.
        rebajo = b.total_deducciones - b.ccss_obrero - b.renta - sum(
            (l.monto for l in b.lineas if l.tipo == "deduccion"), CERO)
        c("renta_por_pagar", rebajo)

    lineas = [LineaAsiento(k, DESCRIPCIONES.get(k, k), debito=r2(v)) for k, v in deb.items()]
    lineas += [LineaAsiento(k, DESCRIPCIONES.get(k, k), credito=r2(v)) for k, v in cre.items()]
    # Una cuenta con débito y crédito (gasto_salarios por ausencias) se neta.
    netas: "OrderedDict[str, LineaAsiento]" = OrderedDict()
    for l in lineas:
        if l.concepto in netas:
            x = netas[l.concepto]
            saldo = (x.debito - x.credito) + (l.debito - l.credito)
            x.debito, x.credito = (saldo, CERO) if saldo >= 0 else (CERO, -saldo)
        else:
            netas[l.concepto] = l
    return [l for l in netas.values() if l.debito or l.credito]


def cuadra(lineas: list[LineaAsiento]) -> bool:
    return sum((l.debito for l in lineas), CERO) == sum((l.credito for l in lineas), CERO)
