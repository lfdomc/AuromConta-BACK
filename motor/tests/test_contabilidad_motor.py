"""Motor contable: validación, comprobación, estados financieros y cierre."""
from decimal import Decimal as D

import pytest

from contabilidad_motor import (Cuenta, Linea, Totales, asiento_cierre, asiento_reverso, balance_comprobacion,
                                balance_general, estado_resultados, naturaleza_de, saldo_natural, validar_asiento)


def cta(id, codigo, nombre, tipo, padre=None, movimientos=True, naturaleza=None):
    return Cuenta(id, codigo, nombre, tipo, naturaleza or naturaleza_de(tipo), padre, movimientos)


CATALOGO = [
    cta("1", "1", "Activo", "activo", movimientos=False),
    cta("11", "11", "Activo corriente", "activo", "1", False),
    cta("bancos", "1103", "Bancos", "activo", "11"),
    cta("cxc", "1201", "Clientes", "activo", "11"),
    cta("iva_c", "1203", "IVA soportado", "activo", "11"),
    cta("ppe", "1503", "Mobiliario", "activo", "1"),
    cta("dep", "1509", "Depreciación acumulada", "activo", "1", naturaleza="acreedora"),
    cta("2", "2", "Pasivo", "pasivo", movimientos=False),
    cta("prov", "2120", "Proveedores", "pasivo", "2"),
    cta("iva_d", "2121", "IVA por pagar", "pasivo", "2"),
    cta("3", "3", "Patrimonio", "patrimonio", movimientos=False),
    cta("capital", "3101", "Capital social", "patrimonio", "3"),
    cta("util", "3301", "Utilidades acumuladas", "patrimonio", "3"),
    cta("4", "4", "Ingresos", "ingreso", movimientos=False),
    cta("ventas", "4102", "Ventas de servicios", "ingreso", "4"),
    cta("devol", "4103", "Devoluciones sobre ventas", "ingreso", "4", naturaleza="deudora"),
    cta("5", "5", "Gastos", "gasto", movimientos=False),
    cta("alquiler", "5201", "Alquileres", "gasto", "5"),
    cta("depg", "5210", "Depreciación", "gasto", "5"),
    cta("6", "6", "Costos", "costo", movimientos=False),
    cta("costo", "6102", "Costo de servicios", "costo", "6"),
]
POR_ID = {c.id: c for c in CATALOGO}


def T(d=0, c=0):
    return Totales(D(str(d)), D(str(c)))


def test_naturaleza_por_tipo():
    assert naturaleza_de("activo") == "deudora" and naturaleza_de("gasto") == "deudora"
    assert naturaleza_de("pasivo") == "acreedora" and naturaleza_de("ingreso") == "acreedora"
    with pytest.raises(ValueError):
        naturaleza_de("otro")
    assert saldo_natural("acreedora", 100, 250) == D("150.00")


def test_asiento_valido():
    lineas = [Linea("bancos", debito=D("113000")), Linea("ventas", credito=D("100000")), Linea("iva_d", credito=D("13000"))]
    assert validar_asiento(lineas, POR_ID) == []


@pytest.mark.parametrize("lineas,mensaje", [
    ([Linea("bancos", debito=D(100))], "al menos dos"),
    ([Linea("bancos", debito=D(100)), Linea("ventas", credito=D(90))], "no cuadra"),
    ([Linea("bancos", debito=D(100), credito=D(100)), Linea("ventas")], "no ambos"),
    ([Linea("bancos", debito=D(-5)), Linea("ventas", credito=D(-5))], "negativos"),
    ([Linea("1", debito=D(5)), Linea("ventas", credito=D(5))], "agrupación"),
    ([Linea("xx", debito=D(5)), Linea("ventas", credito=D(5))], "no existe"),
    ([Linea("bancos", debito=D("1.005")), Linea("ventas", credito=D("1.005"))], "dos decimales"),
])
def test_asiento_invalido(lineas, mensaje):
    assert any(mensaje in e for e in validar_asiento(lineas, POR_ID))


def test_reverso_intercambia():
    rev = asiento_reverso([Linea("bancos", debito=D(10), descripcion="cobro"), Linea("cxc", credito=D(10))])
    assert rev[0].credito == D(10) and rev[0].debito == 0 and rev[0].descripcion.startswith("Reversión")
    assert rev[1].debito == D(10)


MOV = {
    "capital": T(0, 1_000_000), "bancos": T(1_000_000 + 1_130_000, 200_000 + 339_000),
    "ventas": T(0, 1_000_000), "devol": T(50_000, 0), "iva_d": T(0, 130_000),
    "cxc": T(0, 0), "alquiler": T(200_000, 0), "costo": T(300_000, 0), "iva_c": T(39_000, 0),
    "ppe": T(500_000, 0), "prov": T(0, 500_000), "depg": T(10_000, 0), "dep": T(0, 10_000),
}
# Ajuste: la devolución salió de bancos.
MOV["bancos"] = T(2_130_000, 589_000)


def test_comprobacion_cuadra_y_suma_grupos():
    b = balance_comprobacion(CATALOGO, {}, MOV)
    tot = b["totales"]
    assert tot["cuadra"] and tot["debitos"] == tot["creditos"]
    filas = {f["codigo"]: f for f in b["filas"]}
    assert filas["1103"]["saldo_final"] == D("1541000.00")
    # El grupo 11 suma bancos + IVA soportado
    assert filas["11"]["saldo_final"] == D("1580000.00") and filas["11"]["es_grupo"] and filas["11"]["nivel"] == 1
    # Correctora: depreciación acumulada tiene saldo acreedor positivo en su naturaleza
    assert filas["1509"]["saldo_final"] == D("10000.00") and filas["1509"]["saldo_acreedor"] == D("10000.00")
    assert "1201" not in filas    # sin movimientos ni saldo inicial no aparece


def test_comprobacion_con_saldo_inicial():
    b = balance_comprobacion(CATALOGO, {"bancos": T(500, 0), "capital": T(0, 500)}, {"bancos": T(100, 0), "ventas": T(0, 100)})
    f = {x["codigo"]: x for x in b["filas"]}
    assert f["1103"]["saldo_inicial"] == D("500.00") and f["1103"]["saldo_final"] == D("600.00")
    assert b["totales"]["cuadra"]


def test_estado_resultados_con_correctora():
    er = estado_resultados(CATALOGO, MOV)
    assert er["ingresos"]["total"] == D("950000.00")      # ventas 1 000 000 - devoluciones 50 000
    assert er["costos"]["total"] == D("300000.00")
    assert er["gastos"]["total"] == D("210000.00")
    assert er["utilidad_bruta"] == D("650000.00") and er["utilidad_neta"] == D("440000.00")


def test_balance_general_cuadra_con_resultado():
    bg = balance_general(CATALOGO, MOV)
    assert bg["resultado_ejercicio"] == D("440000.00")
    assert bg["activo"]["total"] == D("2070000.00")       # 1 541 000 + 39 000 + 500 000 - 10 000
    assert bg["pasivo"]["total"] == D("630000.00")
    assert bg["cuadra"]


def test_cierre_anual_deja_resultados_en_cero_y_balance_sigue_cuadrando():
    lineas = asiento_cierre(CATALOGO, MOV, "util")
    assert validar_asiento(lineas, POR_ID) == []
    util = [l for l in lineas if l.cuenta_id == "util"][0]
    assert util.credito == D("440000.00") and util.descripcion == "Utilidad del ejercicio"
    despues = dict(MOV)
    for l in lineas:
        despues[l.cuenta_id] = despues.get(l.cuenta_id, T()).sumar(T(l.debito, l.credito))
    assert estado_resultados(CATALOGO, despues)["utilidad_neta"] == 0
    bg = balance_general(CATALOGO, despues)
    assert bg["resultado_ejercicio"] == 0 and bg["cuadra"]
    assert bg["patrimonio"]["total"] == D("1440000.00")


def test_cierre_con_perdida_y_sin_movimientos():
    lineas = asiento_cierre(CATALOGO, {"alquiler": T(100, 0)}, "util")
    assert lineas[-1].debito == D("100.00") and lineas[-1].descripcion == "Pérdida del ejercicio"
    assert asiento_cierre(CATALOGO, {}, "util") == []
    empate = asiento_cierre(CATALOGO, {"alquiler": T(100, 0), "ventas": T(0, 100)}, "util")
    assert len(empate) == 2 and validar_asiento(empate, POR_ID) == []


def test_catalogo_con_ciclo_no_se_cuelga():
    ciclo = [cta("a", "1", "A", "activo", "b"), cta("b", "2", "B", "activo", "a")]
    assert balance_comprobacion(ciclo, {}, {"a": T(1, 0)})["filas"] == []
