from datetime import date
from decimal import Decimal as D

import pytest

from planilla_motor import (Categoria, Embargo, Empleado, EntradaBoleta, EntradaLiquidacion, Frecuencia,
                            Incapacidad, Linea, Periodo, TipoIncapacidad, a_json, antiguedad,
                            asiento_planilla, calcular_aguinaldo, calcular_boleta, calcular_liquidacion,
                            cuadra, desde_json, PARAMETROS_2026)


# ------------------------------------------------------------- Aguinaldo ----
def test_aguinaldo_anio_completo():
    meses = {f"2025-12": D("600000"), **{f"2026-{m:02d}": D("600000") for m in range(1, 12)}}
    r = calcular_aguinaldo(2026, meses)
    assert r.total_salarios == D("7200000.00") and r.monto == D("600000.00")
    assert r.detalle[0]["mes"] == "2025-12" and r.detalle[-1]["mes"] == "2026-11"


def test_aguinaldo_ingreso_en_marzo_ignora_diciembre():
    meses = {f"2026-{m:02d}": D("600000") for m in range(3, 13)}  # incluye dic 2026 (otro periodo)
    assert calcular_aguinaldo(2026, meses).monto == D("450000.00")


# ----------------------------------------------------------- Liquidación ----
def test_antiguedad():
    assert antiguedad(date(2020, 3, 1), date(2026, 2, 28)) == (6, 0, 0)
    assert antiguedad(date(2022, 7, 1), date(2026, 1, 31)) == (3, 7, 0)
    assert antiguedad(date(2026, 1, 15), date(2026, 3, 10)) == (0, 1, 24)


def liq(ingreso, salida, motivo="despido_con_responsabilidad", **kw):
    base = dict(salarios_ultimos_6_meses=[D("600000")] * 6)
    base.update(kw)
    return calcular_liquidacion(EntradaLiquidacion(ingreso, salida, motivo, **base))


def test_despido_con_responsabilidad_6_anios():
    r = liq(date(2020, 3, 1), date(2026, 2, 28), dias_vacaciones_pendientes=D(5),
            salarios_periodo_aguinaldo=D("1800000"))
    assert r.salario_diario == D("20000.00")
    assert r.preaviso == D("600000.00")
    assert r.dias_cesantia == D("129")                   # 21.5 x 6
    assert r.cesantia == D("2580000.00")
    assert r.vacaciones == D("100000.00") and r.ccss_obrero == D("10830.00")
    assert r.aguinaldo == D("150000.00")
    assert r.total == D("3419170.00")


def test_tope_8_anios_con_tabla_de_10():
    r = liq(date(2016, 7, 1), date(2026, 6, 30))
    assert r.anios == 10 and r.dias_cesantia == D("172")  # 21.5 x 8


def test_fraccion_mayor_6_meses_cuenta_como_anio():
    r = liq(date(2022, 7, 1), date(2026, 1, 31))
    assert (r.anios, r.meses) == (3, 7) and r.dias_cesantia == D("84")  # 21 x 4


def test_cuatro_meses():
    r = liq(date(2025, 10, 1), date(2026, 1, 31))
    assert r.dias_preaviso == D(7) and r.dias_cesantia == D(7)


def test_menos_de_3_meses_sin_derechos_de_despido():
    r = liq(date(2025, 12, 1), date(2026, 1, 31))
    assert r.preaviso == 0 and r.cesantia == 0


def test_renuncia_solo_vacaciones_y_aguinaldo():
    r = liq(date(2020, 3, 1), date(2026, 2, 28), motivo="renuncia", dias_vacaciones_pendientes=D(2))
    assert r.preaviso == 0 and r.cesantia == 0 and r.vacaciones == D("40000.00")
    assert r.notas


def test_preaviso_otorgado_no_se_paga():
    assert liq(date(2020, 3, 1), date(2026, 2, 28), preaviso_otorgado=True).preaviso == 0


def test_pension_paga_cesantia_sin_preaviso():
    r = liq(date(2020, 3, 1), date(2026, 2, 28), motivo="pension")
    assert r.preaviso == 0 and r.cesantia == D("2580000.00")


def test_deduccion_de_prestamo():
    r = liq(date(2020, 3, 1), date(2026, 2, 28), motivo="renuncia", deducciones=D("50000"))
    assert r.total == D("-50000.00") and len(r.notas) == 2


# -------------------------------------------------------------- Parámetros --
def test_parametros_ida_y_vuelta_json():
    p = desde_json(date(2026, 1, 1), a_json(PARAMETROS_2026))
    assert p == PARAMETROS_2026


# ------------------------------------------------------------- Asiento ------
def test_asiento_cuadra_y_montos():
    e = EntradaBoleta(Empleado(D("700000"), Frecuencia.MENSUAL), Periodo(date(2026, 1, 1), date(2026, 1, 31)))
    e.embargos = [Embargo("fijo", D("50000"))]
    e.lineas = [Linea("deduccion", Categoria.PRESTAMO, D("20000"))]
    b1 = calcular_boleta(e)
    e2 = EntradaBoleta(Empleado(D("600000"), Frecuencia.QUINCENAL, aplica_rop=True),
                       Periodo(date(2026, 3, 1), date(2026, 3, 15)))
    e2.incapacidades = [Incapacidad(TipoIncapacidad.ENFERMEDAD, date(2026, 3, 2), date(2026, 3, 6), D("20000"))]
    e2.lineas = [Linea("deduccion", Categoria.AUSENCIA, D("10000"))]
    b2 = calcular_boleta(e2)
    lineas = asiento_planilla([b1, b2])
    assert cuadra(lineas)
    por = {l.concepto: l for l in lineas}
    assert por["embargos_por_pagar"].credito == D("44432.34")   # tope Art. 172
    assert por["prestamos_empleados"].credito == D("20000.00")
    assert por["subsidios_por_cobrar"].debito == D("24000.00")
    assert por["gasto_incapacidades"].debito == D("30000.00")
    assert por["gasto_salarios"].debito == b1.bruto + b2.bruto - D("10000")


# ------------------------------------------------- Pensiones y serialización --
def test_pension_porcentaje_antes_que_embargo():
    from planilla_motor import a_dict, resultado_desde_dict
    e = EntradaBoleta(Empleado(D("700000"), Frecuencia.MENSUAL), Periodo(date(2026, 1, 1), date(2026, 1, 31)))
    e.pensiones = [Embargo("porcentaje", D("30"), "P-1", referencia="pen1")]
    e.embargos = [Embargo("porcentaje", D("25"), "E-1")]
    r = calcular_boleta(e)
    pen = [l for l in r.lineas if l.categoria == Categoria.PENSION_ALIMENTARIA][0]
    assert pen.monto == D("187257.00") and pen.referencia == "pen1"   # 30 % de 624,190
    assert r.embargos == D("21025.21")                                # (436 933 - 268 731,31) / 8
    assert resultado_desde_dict(a_dict(r)) == r
