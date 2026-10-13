"""Incapacidades: subsidios, dias a cargo del patrono y salario cotizable.

Porta la parte de incapacidades de `_compute_extras` del modulo Odoo:
  - CCSS enfermedad (Art. 79 CT): dias 1-3 los paga el patrono al 50 %; desde
    el dia 4 la CCSS subsidia al porcentaje del certificado (60 % por defecto).
    Los certificados consecutivos (prorrogas) comparten los 3 dias.
  - INS (riesgo del trabajo): 60 % desde el dia 1, pagado fuera de planilla.
  - Maternidad (Art. 94-95 CT): subsidio sobre el salario promedio; si cubre
    todo el periodo se calcula como diario x 30 x factor de frecuencia.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .dinero import CERO, D, r, r2
from .modelos import Frecuencia, Incapacidad, TipoIncapacidad
from .parametros import ParametrosLegales


VENTANA_RECAIDA_DIAS = 30


@dataclass
class ResultadoIncapacidad:
    dias: Decimal = CERO
    subsidio_ccss: Decimal = CERO
    subsidio_ins: Decimal = CERO
    costo_patrono: Decimal = CERO
    hay_incapacidad: bool = False
    maternidad_total: bool = False     # maternidad cubre todo el periodo
    salario_cotizable: Decimal | None = None  # None = sin incapacidad


def _solapa(desde: date, hasta: date, inc: Incapacidad) -> tuple[date, date] | None:
    ini, fin = max(desde, inc.desde), min(hasta, inc.hasta)
    return (ini, fin) if fin >= ini else None


def calcular_incapacidades(
    incapacidades: list[Incapacidad],
    desde: date,
    hasta: date,
    salario_mensual: Decimal,
    frecuencia: Frecuencia,
    factor_prop: Decimal,
    p: ParametrosLegales,
) -> ResultadoIncapacidad:
    res = ResultadoIncapacidad()
    dias_periodo = (hasta - desde).days + 1
    validas = sorted(incapacidades, key=lambda i: i.desde)

    # Agrupar prorrogas: una incapacidad que empieza a mas tardar el dia
    # siguiente al fin de la anterior es el mismo evento. Ademas, en enfermedad
    # CCSS, una nueva incapacidad dentro de los 30 dias posteriores a la anterior
    # se subsidia desde el primer dia (Reglamento del Seguro de Salud, art. 35):
    # el patrono no vuelve a pagar los dias 1-3.
    def _mismo_evento(prev: Incapacidad, inc: Incapacidad) -> bool:
        if inc.desde <= prev.hasta + timedelta(days=1):
            return True
        ccss = (TipoIncapacidad.MATERNIDAD, TipoIncapacidad.INS)
        return (prev.tipo not in ccss and inc.tipo not in ccss
                and inc.desde <= prev.hasta + timedelta(days=VENTANA_RECAIDA_DIAS))

    grupos: list[list[Incapacidad]] = []
    for inc in validas:
        if grupos and _mismo_evento(grupos[-1][-1], inc):
            grupos[-1].append(inc)
        else:
            grupos.append([inc])

    subsidio_ccss = CERO
    subsidio_ins = CERO
    costo_patrono = CERO
    dias = CERO
    for grupo in grupos:
        inicio_evento = grupo[0].desde
        for inc in grupo:
            s = _solapa(desde, hasta, inc)
            if not s:
                continue
            ini, fin = s
            d = D((fin - ini).days + 1)
            if inc.medio_dia_extra and desde <= inc.desde <= hasta:
                d += D("0.5")
            dias += d

            if inc.tipo == TipoIncapacidad.MATERNIDAD:
                diario = inc.salario_promedio_maternidad or inc.salario_diario
            else:
                diario = inc.salario_diario

            if inc.tipo == TipoIncapacidad.INS:
                subsidio_ins += r2(d * diario * inc.porcentaje_subsidio / 100)
            elif inc.tipo == TipoIncapacidad.MATERNIDAD:
                if d >= dias_periodo:
                    subsidio_ccss += r2(diario * p.dias_mes * frecuencia.factor)
                else:
                    subsidio_ccss += r2(d * diario)
            else:
                if inc.es_prorroga:
                    d_patrono = CERO
                else:
                    transcurridos = (ini - inicio_evento).days
                    restantes = max(p.dias_incapacidad_patrono - transcurridos, 0)
                    d_patrono = min(d, D(restantes))
                d_subsidio = d - d_patrono
                costo_patrono += r2(d_patrono * diario * D("0.5"))
                subsidio_ccss += r2(d_subsidio * diario * inc.porcentaje_subsidio / 100)

    res.dias = dias
    res.subsidio_ccss = r2(subsidio_ccss)
    res.subsidio_ins = r2(subsidio_ins)
    res.costo_patrono = r2(costo_patrono)

    en_periodo = [i for i in validas if _solapa(desde, hasta, i)]
    res.hay_incapacidad = bool(en_periodo)
    if not en_periodo:
        return res

    # Maternidad total para el bruto: todas son maternidad y los dias (sin el
    # medio dia extra) cubren el periodo.
    dias_sin_medio = sum(((_solapa(desde, hasta, i)[1] - _solapa(desde, hasta, i)[0]).days + 1)
                         for i in en_periodo)
    res.maternidad_total = (all(i.tipo == TipoIncapacidad.MATERNIDAD for i in en_periodo)
                            and dias_sin_medio >= dias_periodo)

    if dias > 0 and salario_mensual > 0:
        diario_sal = r(salario_mensual / p.dias_mes, 4)
        dias_trab = max(D(dias_periodo) - dias, CERO)
        if all(i.tipo == TipoIncapacidad.MATERNIDAD for i in en_periodo) and dias_trab == 0:
            por_patrono = any(i.maternidad_ccss_por_patrono for i in en_periodo)
            res.salario_cotizable = res.subsidio_ccss if por_patrono else CERO
        elif all(i.tipo == TipoIncapacidad.INS for i in en_periodo) and dias_trab == 0:
            res.salario_cotizable = CERO
        else:
            base_periodo = r2(salario_mensual * frecuencia.factor * factor_prop)
            res.salario_cotizable = r2(base_periodo - dias * diario_sal)
    return res
