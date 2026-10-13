"""Licencias especiales de ley (paternidad, adopción, duelo, lactancia) y exentos fuera de SICERE/INS."""
import uuid
from decimal import Decimal as D

import psycopg
import pytest


@pytest.fixture(scope="module")
def e(client, dsn):
    from conftest import token
    uid = uuid.uuid4()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"lic-{uid.hex[:6]}@prueba.cr"))
    h = token(uid, "lic@prueba.cr")
    eid = client.post("/empresas", json={"nombre": "Licencias SA", "cedula_juridica": "3-101-737373"}, headers=h).json()["id"]
    client.patch(f"/empresas/{eid}", json={"numero_patronal": "2-1234-567-001"}, headers=h)
    base = {"fecha_ingreso": "2022-01-10", "frecuencia": "biweekly", "salario_mensual": "900000"}
    ids = {}
    for k, ced, extra in (("papa", "1-0101-0202", {}), ("duelo", "1-0303-0404", {}), ("exento", "1-0505-0606", {"exento": True})):
        r = client.post(f"/empresas/{eid}/empleados", json={**base, **extra, "identificacion": ced, "nombre": k.title()},
                        headers=h)
        assert r.status_code == 201, r.text
        ids[k] = r.json()["id"]
    return {"id": eid, "h": h, **ids}


def test_catalogo_y_reglas_de_ley(client, e):
    h, eid = e["h"], e["id"]
    cat = {t["codigo"]: t for t in client.get(f"/empresas/{eid}/tipos-licencia", headers=h).json()}
    assert cat["paternidad"]["dias_ley"] == 8 and cat["paternidad"]["pago"] == "50_50"
    assert cat["duelo_primer_grado"]["dias_ley"] == 3 and "10589" in cat["duelo_primer_grado"]["base_legal"]
    assert cat["matrimonio"]["pago"] == "empresa"
    url = f"/empresas/{eid}/permisos"
    base = {"empleado_id": e["duelo"], "desde": "2026-03-04", "hasta": "2026-03-06", "dias": "3"}
    assert client.post(url, json={**base, "tipo": "duelo_primer_grado", "fecha_evento": "2026-03-03"}, headers=h).status_code == 422
    assert client.post(url, json={**base, "tipo": "lactancia", "fecha_evento": "2026-01-03"}, headers=h).status_code == 422
    assert client.post(url, json={**base, "tipo": "paternidad", "fecha_evento": "2026-03-10"}, headers=h).status_code == 422
    r = client.post(url, json={**base, "tipo": "duelo_primer_grado", "fecha_evento": "2026-03-03", "parentesco": "Madre",
                               "con_goce": False}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["con_goce"] is True          # la ley dice que es remunerada


def test_paternidad_50_50_repartida_en_quincenas(client, e):
    h, eid = e["h"], e["id"]
    r = client.post(f"/empresas/{eid}/permisos", json={
        "empleado_id": e["papa"], "tipo": "paternidad", "desde": "2026-03-02", "hasta": "2026-03-29", "dias": "8",
        "fecha_evento": "2026-03-01", "numero_documento": "CCSS-123"}, headers=h)
    assert r.status_code == 201, r.text
    pl = client.post(f"/empresas/{eid}/planillas", json={"desde": "2026-03-01", "hasta": "2026-03-15"}, headers=h).json()["id"]
    client.post(f"/empresas/{eid}/planillas/{pl}/calcular", headers=h)
    bol = {b["empleado_id"]: b for b in client.get(f"/empresas/{eid}/planillas/{pl}", headers=h).json()["boletas"]}
    det = client.get(f"/empresas/{eid}/planillas/{pl}/boletas/{bol[e['papa']]['id']}", headers=h).json()
    lineas = {l["descripcion"]: D(l["monto"]) for l in det["lineas"]}
    # 4 de los 8 días caen en esta quincena: salario diario 30 000
    assert lineas["Paternidad: 4 día(s) fuera del salario"] == D("120000")
    assert any(k.startswith("Licencia de paternidad — 50 % patrono") and v == D("60000") for k, v in lineas.items())
    # Duelo de primer grado: con goce, el salario no cambia
    assert D(bol[e["duelo"]]["bruto"]) == D("450000") and D(bol[e["duelo"]]["neto"]) < D("450000")
    assert D(bol[e["papa"]]["ccss_obrero"]) < D(bol[e["duelo"]]["ccss_obrero"])   # la base CCSS excluye esos días
    assert client.post(f"/empresas/{eid}/planillas/{pl}/aprobar", headers=h).status_code == 200
    e["pl"] = pl


def test_exentos_fuera_de_sicere_ins_y_ccss(client, e):
    h, eid = e["h"], e["id"]
    sic = client.get(f"/empresas/{eid}/exportaciones/sicere", params={"anio": 2026, "mes": 3}, headers=h)
    assert sic.status_code == 200, sic.text
    assert "0101050606" not in sic.text and "0101010202" in sic.text
    for rep in ("ins", "ccss"):
        filas = client.get(f"/empresas/{eid}/reportes/{rep}", params={"planilla_id": e["pl"]}, headers=h).json()["filas"]
        assert "1-0505-0606" not in [f["identificacion"] for f in filas]
    assert client.get(f"/empresas/{eid}/exportaciones/eddi7", params={"anio": 2026, "mes": 3}, headers=h).status_code == 200


def test_reporte_208_138(client, e):
    h, eid = e["h"], e["id"]
    client.patch(f"/empresas/{eid}/empleados/{e['duelo']}", json={"num_empleadores": 2}, headers=h)
    rep = client.get(f"/empresas/{eid}/reportes/retenciones-208", params={"desde": "2026-03-01", "hasta": "2026-03-31"},
                     headers=h).json()
    por = {f["identificacion"]: f for f in rep["filas"]}
    assert set(por) == {"1-0101-0202", "1-0303-0404", "1-0505-0606"}
    assert por["1-0505-0606"]["condicion"] == "Exento" and D(por["1-0505-0606"]["total_retenido"]) == 0
    assert por["1-0303-0404"]["multi_txt"].startswith("Sí")
    assert D(rep["totales"]["total_retenido"]) == sum(D(f["renta"]) + D(f["rebajo"]) for f in rep["filas"])
    assert any("138" in n for n in rep["notas"]) and any("15/04/2026" in n for n in rep["notas"])
    x = client.get(f"/empresas/{eid}/reportes/retenciones-208", params={"desde": "2026-03-01", "hasta": "2026-03-31",
                   "formato": "xlsx"}, headers=h)
    assert x.status_code == 200 and x.content[:2] == b"PK"


def test_lactancia_ley_10895(client, e):
    h, eid = e["h"], e["id"]
    url = f"/empresas/{eid}/permisos"
    base = {"empleado_id": e["duelo"], "tipo": "lactancia", "fecha_evento": "2026-01-10", "dias": "30"}
    # Dentro del primer año: sin certificado
    ok = client.post(url, json={**base, "desde": "2026-03-01", "hasta": "2026-12-31", "horas": "1"}, headers=h)
    assert ok.status_code == 201, ok.text
    assert ok.json()["con_goce"] is True and ok.json()["unidad"] == "horas"
    # Después del primer año: pide certificado; cada certificado cubre tres meses
    sin_cert = client.post(url, json={**base, "desde": "2027-01-11", "hasta": "2027-04-10", "horas": "2"}, headers=h)
    assert sin_cert.status_code == 422 and "certificado" in sin_cert.text
    largo = client.post(url, json={**base, "desde": "2027-01-11", "hasta": "2027-08-10", "horas": "2",
                                   "numero_documento": "CM-1"}, headers=h)
    assert largo.status_code == 422 and "tres meses" in largo.text
    assert client.post(url, json={**base, "desde": "2027-01-11", "hasta": "2027-04-10", "horas": "2",
                                  "numero_documento": "CM-1"}, headers=h).status_code == 201
