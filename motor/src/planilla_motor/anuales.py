"""Aguinaldo (Ley 2412) y liquidación laboral (Código de Trabajo Arts. 28-31, 153, 157)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .dinero import CERO, D, r2, suma
from .parametros import ParametrosLegales, parametros_para


# --------------------------------------------------------------- Aguinaldo --
@dataclass
class ResultadoAguinaldo:
    desde: date
    hasta: date
    total_salarios: Decimal
    monto: Decimal
    detalle: list[dict] = field(default_factory=list)


def periodo_aguinaldo(anio: int) -> tuple[date, date]:
    """Del 1 de diciembre del año anterior al 30 de noviembre del año indicado."""
    return date(anio - 1, 12, 1), date(anio, 11, 30)


def calcular_aguinaldo(anio: int, salarios_por_mes: dict[str, Decimal]) -> ResultadoAguinaldo:
    """`salarios_por_mes`: {'2025-12': monto, '2026-01': monto, ...} con todo lo
    devengado (salario ordinario, horas extra, comisiones, bonos salariales).
    No incluye subsidios de incapacidad. El aguinaldo es 1/12 del total y está
    exento de CCSS y renta."""
    desde, hasta = periodo_aguinaldo(anio)
    meses = []
    y, m = desde.year, desde.month
    for _ in range(12):
        meses.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    detalle = [{"mes": k, "monto": str(r2(salarios_por_mes.get(k, CERO)))} for k in meses]
    total = r2(suma(salarios_por_mes.get(k, CERO) for k in meses))
    return ResultadoAguinaldo(desde, hasta, total, r2(total / 12), detalle)


# ------------------------------------------------------------- Liquidación --
CON_CESANTIA = {"despido_con_responsabilidad", "mutuo_acuerdo", "pension", "fallecimiento"}
CON_PREAVISO = {"despido_con_responsabilidad"}


@dataclass
class EntradaLiquidacion:
    fecha_ingreso: date
    fecha_salida: date
    motivo: str
    salarios_ultimos_6_meses: list[Decimal]       # brutos mensuales, más reciente al final
    salarios_periodo_aguinaldo: Decimal = CERO    # devengado desde el 1 dic hasta la salida
    dias_vacaciones_pendientes: Decimal = CERO
    promedio_vacaciones_mensual: Decimal | None = None  # Art. 157 (50 semanas); None = promedio 6 m
    preaviso_otorgado: bool = False
    salarios_pendientes: Decimal = CERO           # días trabajados no pagados
    otros_pagos: Decimal = CERO
    deducciones: Decimal = CERO                   # saldo de préstamos, adelantos
    aplicar_ccss_vacaciones: bool = True


@dataclass
class ResultadoLiquidacion:
    anios: int
    meses: int
    dias: int
    salario_promedio_mensual: Decimal
    salario_diario: Decimal
    dias_preaviso: Decimal
    preaviso: Decimal
    dias_cesantia: Decimal
    cesantia: Decimal
    vacaciones: Decimal
    aguinaldo: Decimal
    salarios_pendientes: Decimal
    otros_pagos: Decimal
    ccss_obrero: Decimal
    deducciones: Decimal
    total_bruto: Decimal
    total: Decimal
    notas: list[str] = field(default_factory=list)


def antiguedad(desde: date, hasta: date) -> tuple[int, int, int]:
    """Años, meses y días completos entre dos fechas (inclusive el último día)."""
    fin = hasta + timedelta(days=1)
    anios = fin.year - desde.year
    meses = fin.month - desde.month
    dias = fin.day - desde.day
    if dias < 0:
        meses -= 1
        prev = (fin.replace(day=1) - timedelta(days=1))
        dias += prev.day
    if meses < 0:
        anios -= 1
        meses += 12
    return anios, meses, dias


def dias_preaviso(anios: int, meses: int) -> Decimal:
    total_meses = anios * 12 + meses
    if total_meses < 3:
        return CERO
    if total_meses < 6:
        return D(7)
    if total_meses < 12:
        return D(15)
    return D(30)


def dias_cesantia(anios: int, meses: int, dias: int, p: ParametrosLegales) -> Decimal:
    """Art. 29 CT. Menos de 3 meses: 0; de 3 a 6 meses: 7 días; de 6 meses a
    1 año: 14 días; desde 1 año: días por año según la antigüedad total
    (tabla) por los años laborados, con tope de 8 años. Una fracción mayor de
    6 meses cuenta como año completo."""
    total_meses = anios * 12 + meses
    if anios == 0:
        if total_meses < 3:
            return CERO
        if total_meses < 6:
            return D(7)
        return D(14)
    anios_contables = anios + (1 if (meses > 6 or (meses == 6 and dias > 0)) else 0)
    clave = min(anios_contables, max(p.cesantia_tabla))
    por_anio = p.cesantia_tabla[clave]
    return por_anio * min(anios_contables, p.cesantia_max_anios)


def calcular_liquidacion(e: EntradaLiquidacion, p: ParametrosLegales | None = None) -> ResultadoLiquidacion:
    p = p or parametros_para(e.fecha_salida)
    a, m, d = antiguedad(e.fecha_ingreso, e.fecha_salida)
    notas: list[str] = []

    salarios = [D(s) for s in e.salarios_ultimos_6_meses if D(s) > 0][-6:]
    promedio = r2(suma(salarios) / len(salarios)) if salarios else CERO
    diario = r2(promedio / p.dias_mes)

    dp = dias_preaviso(a, m) if e.motivo in CON_PREAVISO and not e.preaviso_otorgado else CERO
    preaviso = r2(dp * diario)
    dc = dias_cesantia(a, m, d, p) if e.motivo in CON_CESANTIA else CERO
    cesantia = r2(dc * diario)
    if e.motivo == "renuncia" and not e.preaviso_otorgado and dias_preaviso(a, m) > 0:
        notas.append("La persona renunció sin preaviso: el patrono puede rebajar el preaviso que le correspondía dar (Art. 28 CT).")

    prom_vac = e.promedio_vacaciones_mensual if e.promedio_vacaciones_mensual is not None else promedio
    vacaciones = r2(D(e.dias_vacaciones_pendientes) * r2(D(prom_vac) / p.dias_mes))
    aguinaldo = r2(D(e.salarios_periodo_aguinaldo) / 12)

    # Vacaciones y salarios pendientes son salario: cotizan CCSS obrero.
    # Preaviso, cesantía y aguinaldo no cotizan.
    base_ccss = (vacaciones if e.aplicar_ccss_vacaciones else CERO) + D(e.salarios_pendientes)
    ccss = r2(base_ccss * p.ccss_obrero)

    bruto = r2(preaviso + cesantia + vacaciones + aguinaldo + D(e.salarios_pendientes) + D(e.otros_pagos))
    deducciones = r2(ccss + D(e.deducciones))
    total = r2(bruto - deducciones)
    if total < 0:
        notas.append("Las deducciones superan los derechos: revise el saldo de préstamos.")
    return ResultadoLiquidacion(a, m, d, promedio, diario, dp, preaviso, dc, cesantia, vacaciones, aguinaldo,
                                r2(D(e.salarios_pendientes)), r2(D(e.otros_pagos)), ccss, deducciones,
                                bruto, total, notas)
