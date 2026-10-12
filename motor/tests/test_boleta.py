"""Pruebas del motor. Los montos esperados estan calculados a mano en cada
caso (no se toman del propio motor). Los casos 01-09, 14, 18, 19, 21 son los
del modulo Odoo (tests/test_payslip_compute.py) con valores exactos."""
from datetime import date
from decimal import Decimal as D

import pytest

from planilla_motor import (BoletaAnterior, Categoria, ConfigEmpresa, Embargo, Empleado,
                            EntradaBoleta, Frecuencia, HoraExtra, Incapacidad, Linea, Periodo,
                            TipoIncapacidad, calcular_boleta, impuesto_mensual,
                            PARAMETROS_2026, parametros_para)

M, Q = Frecuencia.MENSUAL, Frecuencia.QUINCENAL


def mensual(salario, **kw):
    return EntradaBoleta(Empleado(D(salario), M, **kw), Periodo(date(2026, 1, 1), date(2026, 1, 31)))


def quincena(salario, desde=date(2026, 3, 1), hasta=date(2026, 3, 15), **kw):
    return EntradaBoleta(Empleado(D(salario), Q, **kw), Periodo(desde, hasta))


# --- Casos del modulo Odoo: empleado 700,000 mensual -----------------------

@pytest.fixture
def base700():
    return calcular_boleta(mensual(700_000))


def test_01_03_ccss_obrero(base700):
    assert base700.ccss_obrero == D("75810.00")


def test_02_04_ccss_patronal(base700):
    assert base700.ccss_patronal == D("187810.00")


def test_05_renta_exento(base700):
    assert base700.renta == 0


def test_06_neto(base700):
    assert base700.neto == D("624190.00")
    assert base700.neto < base700.bruto


def test_07_09_provisiones(base700):
    assert base700.prov_aguinaldo == D("58310.00")
    assert base700.prov_cesantia == D("37310.00")      # 5.33 % sin fecha de ingreso
    assert base700.prov_vacaciones == D("29166.67")    # 12/288


def test_18_provisiones_17_82(base700):
    total = base700.prov_aguinaldo + base700.prov_cesantia + base700.prov_vacaciones
    assert abs(total - base700.bruto * D("0.1782")) < base700.bruto * D("0.005")


def test_ins_clase_II(base700):
    assert base700.ins_patronal == D("10430.00")


def test_costo_total_patrono(base700):
    assert base700.costo_total_patrono == D("700000") + D("187810") + D("10430") + \
        D("58310") + D("37310") + D("29166.67")


# --- Renta -----------------------------------------------------------------

def test_tramos_2026():
    assert impuesto_mensual(D("918000"), PARAMETROS_2026) == 0
    assert impuesto_mensual(D("1347000"), PARAMETROS_2026) == D("42900")
    # 2,000,000: 42,900 + 653,000 x 15 %
    assert impuesto_mensual(D("2000000"), PARAMETROS_2026) == D("140850")


def test_14_renta_quincenal():
    r = calcular_boleta(quincena(2_000_000, date(2026, 1, 1), date(2026, 1, 15)))
    assert r.bruto == D("1000000.00")
    assert r.renta == D("70425.00")   # 140,850 / 2


def test_creditos_hijos_conyuge():
    r = calcular_boleta(mensual(1_500_000, hijos=2, conyuge=True))
    # 42,900 + 153,000 x 15 % = 65,850 ; creditos 2 x 1,710 + 2,590 = 6,010
    assert r.renta == D("59840.00")
    assert r.creditos_renta == D("6010.00")


def test_creditos_no_generan_devolucion():
    r = calcular_boleta(mensual(920_000, hijos=3))
    # impuesto 200 ; creditos 5,130 -> renta 0, aplicados 200
    assert r.renta == 0
    assert r.creditos_renta == D("200.00")


def test_multiempleo_reparte_creditos():
    r = calcular_boleta(mensual(1_500_000, hijos=2, conyuge=True, num_empleadores=2))
    assert r.renta == D("65850") - D("3005")


def test_renta_base_neta_de_ccss():
    e = mensual(1_500_000)
    e.config = ConfigEmpresa(base_renta="net_ccss")
    r = calcular_boleta(e)
    # base = 1,500,000 - 162,450 (CCSS) = 1,337,550 -> solo tramo 10 %
    assert r.renta == D("41955.00")


def test_reconciliacion_mensual_consolidada():
    # Q1 de 500,000 (proyeccion 1,000,000 -> 8,200/2 = 4,100 retenido)
    # Q2 con bono: 900,000 -> mes real 1,400,000 -> 42,900 + 7,950 = 50,850
    e = quincena(1_000_000, date(2026, 3, 16), date(2026, 3, 31))
    e.lineas = [Linea("ingreso", Categoria.BONO, D("400000"), afecto_ccss=True)]
    e.config = ConfigEmpresa(metodo_renta="monthly_consolidated")
    e.boletas_anteriores_mes = [BoletaAnterior(D("500000"), D("54150"), D("4100"), D("0"))]
    r = calcular_boleta(e)
    assert r.renta == D("50850") - D("4100")


# --- CCSS: BMC -------------------------------------------------------------

def test_bmc_piso():
    r = calcular_boleta(mensual(200_000, fecha_nacimiento=date(1980, 1, 1)))
    assert r.bmc_aplicada
    assert r.base_ccss == D("333328")
    assert r.ccss_obrero == D("36099.42")
    assert r.ccss_patronal == D("89431.90")


def test_bmc_reducida_medio_tiempo_menor_35():
    r = calcular_boleta(mensual(200_000, medio_tiempo=True, fecha_nacimiento=date(2000, 1, 1)))
    assert not r.bmc_aplicada and r.bmc_reducida
    assert r.ccss_obrero == D("21660.00")


def test_bmc_se_puede_apagar():
    e = mensual(200_000)
    e.config = ConfigEmpresa(aplicar_bmc=False)
    assert calcular_boleta(e).ccss_obrero == D("21660.00")


def test_pensionado_estado():
    assert calcular_boleta(mensual(700_000, pensionado="estado")).ccss_obrero == D("45500.00")


def test_exento_sin_ccss_ni_renta():
    r = calcular_boleta(mensual(3_000_000, exento=True))
    assert r.ccss_obrero == r.renta == r.ccss_patronal == r.ins_patronal == 0
    assert r.neto == D("3000000.00")


# --- Proporcional, horas extra, bonos, licencias ----------------------------

def test_proporcional_ingreso_a_mitad():
    e = mensual(600_000)
    e.periodo.dias_trabajados = 10   # 10 / 31 = 0.3226
    r = calcular_boleta(e)
    assert r.factor_proporcional == D("0.3226")
    assert r.salario_base == D("193560.00")


def test_horas_extra_no_afectas():
    e = quincena(600_000)
    e.horas_extra = [HoraExtra(D("50000")), HoraExtra(D("20000"), afecto_ccss=False)]
    r = calcular_boleta(e)
    assert r.bruto == D("370000.00")
    assert r.base_cotizable == D("350000.00")
    assert r.ccss_obrero == D("37905.00")


def test_19_bono_salarial_suma_al_bruto():
    e = mensual(700_000)
    e.lineas = [Linea("ingreso", Categoria.BONO, D("50000"), afecto_ccss=True)]
    r = calcular_boleta(e)
    assert r.bruto == D("750000.00")
    assert r.ccss_obrero == D("81225.00")


def test_bono_no_salarial_solo_prestaciones():
    e = quincena(600_000)
    e.lineas = [Linea("ingreso", Categoria.BONO, D("30000"), afecto_salario_base=True)]
    r = calcular_boleta(e)
    assert r.bruto == D("300000.00")
    assert r.otros_ingresos == D("30000.00")
    assert r.prov_aguinaldo == D("27489.00")    # (300,000 + 30,000) x 8.33 %
    assert r.neto == D("300000") - D("32490") + D("30000")


def test_licencia_sin_goce_reduce_base():
    e = quincena(600_000)
    e.lineas = [Linea("deduccion", Categoria.LICENCIA_SIN_GOCE, D("20000"))]
    r = calcular_boleta(e)
    assert r.base_cotizable == D("280000.00")
    assert r.ccss_obrero == D("30324.00")
    assert r.neto == D("300000") - D("30324") - D("20000")


def test_vacaciones_en_dinero():
    e = quincena(600_000)
    e.vacaciones_dinero = D("40000")
    assert calcular_boleta(e).bruto == D("340000.00")


def test_cambio_salarial_en_periodo():
    e = quincena(600_000)
    e.salario_cambio_en_periodo = D("700000")
    assert calcular_boleta(e).salario_base == D("350000.00")


def test_por_asistencia():
    e = quincena(600_000, por_asistencia=True, tarifa_hora=D("2500"))
    e.periodo.horas_asistencia = D("96")
    assert calcular_boleta(e).salario_base == D("240000.00")


# --- Incapacidades ---------------------------------------------------------

def inc(tipo, desde, hasta, diario=20_000, **kw):
    return Incapacidad(tipo, desde, hasta, D(diario), **kw)


def test_incapacidad_ccss_dias_patrono():
    e = quincena(600_000)
    e.incapacidades = [inc(TipoIncapacidad.ENFERMEDAD, date(2026, 3, 2), date(2026, 3, 6))]
    r = calcular_boleta(e)
    assert r.dias_incapacidad == 5
    assert r.costo_patrono_incapacidad == D("30000.00")   # 3 x 20,000 x 50 %
    assert r.subsidio_ccss == D("24000.00")               # 2 x 20,000 x 60 %
    assert r.salario_cotizable == D("200000.00")          # 300,000 - 5 x 20,000
    assert r.bruto == D("200000.00")
    assert r.ccss_obrero == D("21660.00")
    assert not r.bmc_aplicada
    assert r.neto == D("200000") - D("21660") + D("24000") + D("30000")
    assert r.prov_aguinaldo == D("16660.00")


def test_prorroga_comparte_dias_patrono():
    e = quincena(600_000)
    e.incapacidades = [
        inc(TipoIncapacidad.ENFERMEDAD, date(2026, 3, 2), date(2026, 3, 6)),
        inc(TipoIncapacidad.ENFERMEDAD, date(2026, 3, 7), date(2026, 3, 10)),
    ]
    r = calcular_boleta(e)
    assert r.costo_patrono_incapacidad == D("30000.00")
    assert r.subsidio_ccss == D("72000.00")                # 6 x 12,000


def test_incapacidad_que_viene_del_periodo_anterior():
    e = quincena(600_000)
    e.incapacidades = [
        inc(TipoIncapacidad.ENFERMEDAD, date(2026, 2, 26), date(2026, 3, 9)),
        inc(TipoIncapacidad.ENFERMEDAD, date(2026, 3, 10), date(2026, 3, 13)),
    ]
    r = calcular_boleta(e)
    assert r.costo_patrono_incapacidad == 0
    assert r.subsidio_ccss == D("156000.00")               # 13 x 12,000


def test_marcada_como_prorroga():
    e = quincena(600_000)
    e.incapacidades = [inc(TipoIncapacidad.ENFERMEDAD, date(2026, 3, 2), date(2026, 3, 4),
                           es_prorroga=True)]
    r = calcular_boleta(e)
    assert r.costo_patrono_incapacidad == 0
    assert r.subsidio_ccss == D("36000.00")


def test_ins_fuera_de_planilla():
    e = quincena(600_000)
    e.incapacidades = [inc(TipoIncapacidad.INS, date(2026, 3, 2), date(2026, 3, 4))]
    r = calcular_boleta(e)
    assert r.subsidio_ins == D("36000.00")
    assert r.subsidio_ccss == 0 and r.costo_patrono_incapacidad == 0
    assert r.bruto == D("240000.00")
    assert r.neto == D("240000") - D("25992")


def test_maternidad_periodo_completo():
    e = quincena(600_000, date(2026, 3, 16), date(2026, 3, 31))
    e.incapacidades = [inc(TipoIncapacidad.MATERNIDAD, date(2026, 3, 1), date(2026, 6, 30))]
    r = calcular_boleta(e)
    assert r.subsidio_ccss == D("300000.00")   # 20,000 x 30 x 0.5 (aunque sean 16 dias)
    assert r.bruto == 0 and r.ccss_obrero == 0
    assert r.neto == D("300000.00")


def test_maternidad_ccss_por_patrono():
    e = quincena(600_000, date(2026, 3, 16), date(2026, 3, 31))
    e.incapacidades = [inc(TipoIncapacidad.MATERNIDAD, date(2026, 3, 1), date(2026, 6, 30),
                           maternidad_ccss_por_patrono=True)]
    r = calcular_boleta(e)
    assert r.base_cotizable == D("300000.00")
    assert r.ccss_obrero == D("32490.00")
    assert r.neto == D("267510.00")


# --- Embargos --------------------------------------------------------------

def test_embargo_tope_25():
    e = mensual(700_000)
    e.embargos = [Embargo("porcentaje", D("40"), "EXP-1")]
    r = calcular_boleta(e)
    assert r.embargos == D("44432.34")    # (624 190 - 268 731,31) / 8


def test_embargos_comparten_tope():
    e = mensual(700_000)
    e.embargos = [Embargo("fijo", D("100000"), "A"), Embargo("fijo", D("100000"), "B")]
    r = calcular_boleta(e)
    assert r.embargos == D("44432.34")
    assert [l.monto for l in r.lineas if l.categoria == Categoria.EMBARGO] == [D("44432.34")]


def test_15_pension_y_embargo_no_superan_neto():
    e = mensual(700_000)
    e.lineas = [Linea("deduccion", Categoria.PENSION_ALIMENTARIA, D("200000"))]
    e.embargos = [Embargo("porcentaje", D("25"))]
    r = calcular_boleta(e)
    # disponible = 700,000 - 75,810 - 200,000 = 424,190 -> (424 190 - 268 731,31) / 8
    assert r.embargos == D("19432.34")
    assert r.neto == D("624190") - D("200000") - D("19432.34")
    assert r.neto > 0


# --- ROP, paternidad, cesantia, vigencias ----------------------------------

def test_rop():
    r = calcular_boleta(mensual(700_000, aplica_rop=True))
    assert r.rop_obrero == D("7000.00") and r.rop_patronal == D("22750.00")
    assert r.neto == D("624190") - D("7000")


def test_21_paternidad_mensual():
    e = mensual(700_000)
    e.dias_paternidad = 8
    assert calcular_boleta(e).paternidad == D("186666.64")   # 23,333.33 x 8


def test_paternidad_quincenal_usa_diario_mensual():
    e = quincena(600_000)
    e.dias_paternidad = 2
    assert calcular_boleta(e).paternidad == D("40000.00")


@pytest.mark.parametrize("ingreso,tasa", [
    (date(2025, 10, 1), D("0.054167")),   # ano 1: 19.5 / 360
    (date(2020, 3, 1), D("0.061111")),    # ano 7: 22 / 360
    (date(2010, 1, 1), D("0.061111")),    # tope 8 anos
])
def test_cesantia_por_antiguedad(ingreso, tasa):
    e = quincena(600_000, fecha_ingreso=ingreso)
    assert calcular_boleta(e).tasa_cesantia == tasa


def test_cesantia_personalizada():
    e = quincena(600_000)
    e.config = ConfigEmpresa(cesantia_modo="custom", cesantia_tasa=D("4.8"))
    assert calcular_boleta(e).prov_cesantia == D("14400.00")


def test_parametros_sin_vigencia():
    with pytest.raises(ValueError):
        parametros_para(date(2025, 12, 31))


def test_dinero_siempre_decimal(base700):
    for nombre, valor in vars(base700).items():
        if isinstance(valor, (int, float)) and not isinstance(valor, bool) and nombre != "dias_periodo":
            pytest.fail(f"{nombre} no es Decimal: {valor!r}")


@pytest.mark.parametrize("liquido,esperado", [
    (D("250000"), D("0")),                                   # bajo el mínimo: inembargable
    (D("500000"), D("28908.59")),                            # (500 000 - 268 731,31) / 8
    (D("1000000"), D("115634.35")),                          # 537 462,62 / 8 + (1 000 000 - 806 193,93) / 4
])
def test_tope_art_172(liquido, esperado):
    from planilla_motor import tope_embargable
    assert tope_embargable(liquido, D("268731.31")) == esperado


def test_tope_quincenal_usa_minimo_proporcional():
    e = quincena(1_000_000)
    e.embargos = [Embargo("porcentaje", D("25"))]
    r = calcular_boleta(e)
    # líquido 500 000 - 54 150 (CCSS) - 4 100 (renta) = 441 750; mínimo quincenal 134 365,655
    # 268 731,31 / 8 + (441 750 - 403 096,965) / 4 = 33 591,41 + 9 663,26
    assert r.embargos == D("43254.67")


# --- FIX16 (módulo Odoo chk88): costo total patronal ------------------------
def test_costo_patronal_excluye_permiso_sin_goce():
    e = quincena(600_000)
    e.lineas = [Linea("deduccion", Categoria.LICENCIA_SIN_GOCE, D("20000"))]
    r = calcular_boleta(e)
    cargas = r.ccss_patronal + r.ins_patronal + r.prov_aguinaldo + r.prov_cesantia + r.prov_vacaciones
    assert r.costo_total_patrono == D("280000") + cargas


def test_costo_patronal_incapacidad_sin_duplicar():
    e = quincena(600_000)
    e.incapacidades = [Incapacidad(TipoIncapacidad.ENFERMEDAD, date(2026, 3, 2), date(2026, 3, 6), D("20000"))]
    r = calcular_boleta(e)
    cargas = r.ccss_patronal + r.ins_patronal + r.prov_aguinaldo + r.prov_cesantia + r.prov_vacaciones
    # bruto 200 000 + días 1-3 al 50 % (30 000); el subsidio CCSS (24 000) no es costo del patrono
    assert r.costo_total_patrono == D("200000") + D("30000") + cargas


def test_costo_patronal_incluye_ingreso_no_salarial():
    e = quincena(600_000)
    e.lineas = [Linea("ingreso", Categoria.BONO, D("30000"), afecto_salario_base=True)]
    r = calcular_boleta(e)
    cargas = r.ccss_patronal + r.ins_patronal + r.prov_aguinaldo + r.prov_cesantia + r.prov_vacaciones
    assert r.costo_total_patrono == D("300000") + D("30000") + cargas
