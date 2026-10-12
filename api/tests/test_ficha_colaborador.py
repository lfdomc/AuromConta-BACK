"""Ficha del colaborador como la pestaña «Planilla CR» de Odoo (migración 0020)."""
import uuid
from decimal import Decimal as D

import psycopg
import pytest


@pytest.fixture(scope="module")
def e(client, dsn):
    from conftest import token
    uid = uuid.uuid4()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"ficha-{uid.hex[:6]}@prueba.cr"))
    h = token(uid, "ficha@prueba.cr")
    eid = client.post("/empresas", json={"nombre": "Ficha Odoo SA", "cedula_juridica": "3-101-515151"}, headers=h).json()["id"]
    base = {"fecha_ingreso": "2020-01-10", "frecuencia": "biweekly", "salario_mensual": "1200000",
            "iban": "CR05015202001026284066"}
    asegurado = client.post(f"/empresas/{eid}/empleados", json={
        **base, "identificacion": "1-1111-1111", "nombre": "Carla", "apellidos": "Mora", "numero_ccss": "111111111",
        "tipo_empleado": "indefinido", "departamento": "Ventas", "tipo_cuenta": "ahorros",
        "aguinaldo_inicial": "100000", "aguinaldo_corte": "2026-01-31"}, headers=h)
    assert asegurado.status_code == 201, asegurado.text
    no_aseg = client.post(f"/empresas/{eid}/empleados", json={
        **base, "identificacion": "2-2222-2222", "nombre": "Diego", "apellidos": "Vargas", "asegurado_ccss": False,
        "tipo_empleado": "servicios_profesionales", "sinpe_movil": "88887777"}, headers=h)
    assert no_aseg.status_code == 201, no_aseg.text
    return {"id": eid, "h": h, "carla": asegurado.json()["id"], "diego": no_aseg.json()["id"]}


def test_campos_guardados(client, e):
    c = client.get(f"/empresas/{e['id']}/empleados/{e['carla']}", headers=e["h"]).json()
    assert c["numero_ccss"] == "111111111" and c["departamento"] == "Ventas" and c["asegurado_ccss"] is True
    d = client.get(f"/empresas/{e['id']}/empleados/{e['diego']}", headers=e["h"]).json()
    assert d["asegurado_ccss"] is False and d["sinpe_movil"] == "88887777"
    r = client.patch(f"/empresas/{e['id']}/empleados/{e['diego']}", json={"sinpe_movil": "123"}, headers=e["h"])
    assert r.status_code == 422


def test_no_asegurado_sin_ccss_ni_renta_y_fuera_del_reporte(client, e):
    h, eid = e["h"], e["id"]
    pl = client.post(f"/empresas/{eid}/planillas", json={"desde": "2026-02-01", "hasta": "2026-02-15"}, headers=h).json()["id"]
    client.post(f"/empresas/{eid}/planillas/{pl}/calcular", headers=h)
    bol = {b["empleado_id"]: b for b in client.get(f"/empresas/{eid}/planillas/{pl}", headers=h).json()["boletas"]}
    assert D(bol[e["diego"]]["ccss_obrero"]) == 0 and D(bol[e["diego"]]["renta"]) == 0
    assert D(bol[e["carla"]]["ccss_obrero"]) > 0
    assert client.post(f"/empresas/{eid}/planillas/{pl}/aprobar", headers=h).status_code == 200
    rep = client.get(f"/empresas/{eid}/reportes/ccss", params={"planilla_id": pl}, headers=h).json()
    ids = [f["identificacion"] for f in rep["filas"]]
    assert "1-1111-1111" in ids and "2-2222-2222" not in ids


def test_aguinaldo_con_acumulado_inicial(client, e):
    h, eid = e["h"], e["id"]
    r = client.post(f"/empresas/{eid}/aguinaldo/calcular", json={"anio": 2026, "crear_planilla": False}, headers=h)
    assert r.status_code == 200, r.text
    carla = [x for x in r.json()["empleados"] if x["empleado_id"] == e["carla"]][0]
    # 100 000 traídos de antes + 1/12 de la quincena de febrero (posterior al corte del 31/01)
    assert D(carla["monto"]) == D("100000") + D("600000") / 12


def test_saldo_inicial_de_vacaciones(client, e):
    h, eid = e["h"], e["id"]
    r = client.patch(f"/empresas/{eid}/empleados/{e['carla']}",
                     json={"vacaciones_saldo_inicial": "5", "vacaciones_corte": "2026-07-31"}, headers=h)
    assert r.status_code == 200, r.text
    res = client.get(f"/empresas/{eid}/empleados/{e['carla']}/vacaciones", headers=h).json()
    assert D(res["saldo_inicial"]) == 5 and res["corte"] == "2026-07-31"
    assert D(res["acumulado"]) >= 2                       # mesiversarios 10/08, 10/09, ...
    assert D(res["saldo"]) == D(res["saldo_inicial"]) + D(res["acumulado"]) - D(res["tomados"])
    # Volver a guardar no duplica
    client.patch(f"/empresas/{eid}/empleados/{e['carla']}", json={"vacaciones_saldo_inicial": "5"}, headers=h)
    assert client.get(f"/empresas/{eid}/empleados/{e['carla']}/vacaciones", headers=h).json()["saldo"] == res["saldo"]
