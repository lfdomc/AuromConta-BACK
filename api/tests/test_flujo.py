"""Flujo completo de las Fases 1 a 3 a través de la API."""
from datetime import date
from decimal import Decimal as D

import pytest

import planilla_motor as M


@pytest.fixture(scope="module")
def empresa(client, usuarios):
    a = usuarios["admin"]["h"]
    r = client.post("/empresas", json={"nombre": "Constructora Prueba SA", "cedula_juridica": "3-101-123456",
                                       "codigo_referido": "conta001"}, headers=a)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    for u, rol in (("operador", "operador"), ("aprobador", "aprobador")):
        r = client.post(f"/empresas/{eid}/usuarios", json={"correo": f"{u}@prueba.cr", "rol": rol}, headers=a)
        assert r.status_code == 201 and r.json()["estado"] == "agregado", r.text
    return eid


def test_yo_y_aislamiento(client, usuarios, empresa):
    yo = client.get("/yo", headers=usuarios["admin"]["h"]).json()
    assert [e["rol"] for e in yo["empresas"]] == ["admin"]
    c = client.get("/yo", headers=usuarios["contador"]["h"]).json()
    assert c["empresas"][0]["id"] == empresa and c["empresas"][0]["rol"] == "contador_operacion"
    assert c["socio"]["tipo"] == "contador"
    assert client.get(f"/empresas/{empresa}", headers=usuarios["otro"]["h"]).status_code == 404
    assert client.get(f"/empresas/{empresa}/empleados", headers=usuarios["otro"]["h"]).status_code == 404
    assert client.get("/yo").status_code == 401


def test_contadores_asignados(client, usuarios, empresa):
    lista = client.get(f"/empresas/{empresa}/contadores", headers=usuarios["admin"]["h"]).json()
    assert len(lista) == 1 and lista[0]["nombre"] == "Conta Uno" and lista[0]["soporte_principal"]
    assert "soporte" in lista[0]["funciones"]


@pytest.fixture(scope="module")
def empleados(client, usuarios, empresa):
    op = usuarios["operador"]["h"]
    base = {"fecha_ingreso": "2024-01-01", "frecuencia": "biweekly", "iban": "CR05015202001026284066",
            "correo": "x@prueba.cr"}
    ana = client.post(f"/empresas/{empresa}/empleados", json={**base, "identificacion": "1-1111-1111", "nombre": "Ana",
                                                               "apellidos": "Mora", "salario_mensual": "600000"}, headers=op)
    beto = client.post(f"/empresas/{empresa}/empleados", json={**base, "identificacion": "2-2222-2222", "nombre": "Beto",
                                                                "apellidos": "Solís", "salario_mensual": "2000000",
                                                                "hijos": 1}, headers=op)
    assert ana.status_code == 201 and beto.status_code == 201, (ana.text, beto.text)
    return {"ana": ana.json()["id"], "beto": beto.json()["id"]}


@pytest.fixture(scope="module")
def planilla(client, usuarios, empresa, empleados):
    op, ap = usuarios["operador"]["h"], usuarios["aprobador"]["h"]
    he = client.post(f"/empresas/{empresa}/horas-extra", json={"empleado_id": empleados["ana"], "fecha": "2026-03-05",
                                                               "horas": "4", "estado": "aprobada"}, headers=op)
    assert he.status_code == 201 and he.json()["estado"] == "pendiente"     # el operador no aprueba
    assert he.json()["monto"] == "15000.00"                                  # 600000/30/8 = 2500 x 1.5 x 4
    r = client.post(f"/empresas/{empresa}/horas-extra/aprobar", json=[he.json()["id"]], headers=ap)
    assert r.json()["actualizadas"] == 1
    pr = client.post(f"/empresas/{empresa}/prestamos", json={"empleado_id": empleados["beto"], "monto": "100000",
                                                             "cuota": "25000", "primera_rebaja": "2026-03-01"}, headers=op)
    assert pr.status_code == 201 and pr.json()["saldo"] == "100000.00"
    inc = client.post(f"/empresas/{empresa}/incapacidades", json={"empleado_id": empleados["ana"], "tipo": "ccss",
                                                                  "desde": "2026-03-10", "hasta": "2026-03-14"}, headers=op)
    assert inc.status_code == 201, inc.text
    pl = client.post(f"/empresas/{empresa}/planillas", json={"desde": "2026-03-01", "hasta": "2026-03-15",
                                                             "fecha_pago": "2026-03-15"}, headers=op)
    assert pl.status_code == 201, pl.text
    return {"id": pl.json()["id"], "prestamo": pr.json()["id"], "he": he.json()["id"]}


def test_calcular_igual_al_motor(client, usuarios, empresa, empleados, planilla):
    op = usuarios["operador"]["h"]
    r = client.post(f"/empresas/{empresa}/planillas/{planilla['id']}/calcular", headers=op)
    assert r.status_code == 200, r.text
    assert r.json()["totales"]["empleados"] == 2
    det = client.get(f"/empresas/{empresa}/planillas/{planilla['id']}", headers=op).json()
    assert det["estado"] == "calculada"
    por = {b["nombre"]: b for b in det["boletas"]}

    # Ana: mismo cálculo directo con el motor
    e = M.EntradaBoleta(M.Empleado(D("600000"), M.Frecuencia.QUINCENAL, fecha_ingreso=date(2024, 1, 1)),
                        M.Periodo(date(2026, 3, 1), date(2026, 3, 15), ultima_del_mes=False))
    e.horas_extra = [M.HoraExtra(D("15000"), D("4"))]
    e.incapacidades = [M.Incapacidad(M.TipoIncapacidad.ENFERMEDAD, date(2026, 3, 10), date(2026, 3, 14), D("20000"))]
    esperado = M.calcular_boleta(e)
    assert D(por["Ana"]["neto"]) == esperado.neto
    assert D(por["Ana"]["ccss_obrero"]) == esperado.ccss_obrero
    # Beto: renta quincenal con 1 hijo y cuota de préstamo
    assert D(por["Beto"]["renta"]) == D("70425") - D("855")
    bol = client.get(f"/empresas/{empresa}/planillas/{planilla['id']}/boletas/{por['Beto']['id']}", headers=op).json()
    assert any(l["categoria"] == "loan" and l["monto"] == "25000.00" for l in bol["lineas"])
    assert bol["parametros"]["ccss_obrero"] == "0.1083"


def test_operador_no_aprueba(client, usuarios, empresa, planilla):
    r = client.post(f"/empresas/{empresa}/planillas/{planilla['id']}/aprobar", headers=usuarios["operador"]["h"])
    assert r.status_code == 403


def test_aprobar_cerrar_reabrir(client, usuarios, empresa, empleados, planilla):
    ap, op = usuarios["aprobador"]["h"], usuarios["operador"]["h"]
    base = f"/empresas/{empresa}/planillas/{planilla['id']}"
    assert client.post(f"{base}/aprobar", headers=ap).json()["estado"] == "aprobada"
    assert client.post(f"{base}/calcular", headers=op).status_code == 409   # aprobada: no se recalcula
    assert client.post(f"{base}/cerrar", headers=ap).json()["estado"] == "cerrada"

    pr = client.get(f"/empresas/{empresa}/prestamos/{planilla['prestamo']}", headers=op).json()
    assert pr["saldo"] == "75000.00"
    he = client.get(f"/empresas/{empresa}/horas-extra/{planilla['he']}", headers=op).json()
    assert he["estado"] == "pagada"
    a = client.get(f"{base}/asiento", headers=op).json()
    assert a["total_debito"] == a["total_credito"]
    assert {l["cuenta_codigo"] for l in a["lineas"]} >= {"5101", "2101", "2102", "1205"}

    pdf = client.get(f"{base}/boletas/{client.get(base, headers=op).json()['boletas'][0]['id']}/pdf", headers=op)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"

    assert client.post(f"{base}/reabrir", json={"motivo": "no"}, headers=ap).status_code == 422
    assert client.post(f"{base}/reabrir", json={"motivo": "Faltaron horas extra"}, headers=op).status_code == 403
    r = client.post(f"{base}/reabrir", json={"motivo": "Faltaron horas extra"}, headers=ap)
    assert r.status_code == 200 and r.json()["estado"] == "calculada", r.text
    assert client.get(f"/empresas/{empresa}/prestamos/{planilla['prestamo']}", headers=op).json()["saldo"] == "100000.00"
    assert client.get(f"{base}/asiento", headers=op).status_code == 404
    # volver a cerrar para las pruebas de aguinaldo
    client.post(f"{base}/calcular", headers=op)
    client.post(f"{base}/aprobar", headers=ap)
    assert client.post(f"{base}/cerrar", headers=ap).json()["estado"] == "cerrada"


def test_aguinaldo(client, usuarios, empresa, empleados, planilla):
    op = usuarios["operador"]["h"]
    r = client.post(f"/empresas/{empresa}/aguinaldo/calcular", json={"anio": 2026}, headers=op)
    assert r.status_code == 200, r.text
    det = client.get(f"/empresas/{empresa}/planillas/{planilla['id']}", headers=op).json()
    bruto_beto = D(next(b for b in det["boletas"] if b["nombre"] == "Beto")["bruto"])
    beto = next(x for x in r.json()["empleados"] if x["nombre"].startswith("Beto"))
    assert D(beto["monto"]) == (bruto_beto / 12).quantize(D("0.01"))
    assert r.json()["planilla"]["tipo"] == "aguinaldo" and r.json()["planilla"]["estado"] == "calculada"


def test_liquidacion(client, usuarios, empresa, empleados):
    op, ap = usuarios["operador"]["h"], usuarios["aprobador"]["h"]
    datos = {"empleado_id": empleados["beto"], "fecha_salida": "2026-03-31", "motivo": "despido_con_responsabilidad",
             "dias_vacaciones_pendientes": "3"}
    sim = client.post(f"/empresas/{empresa}/liquidaciones/simular", json=datos, headers=op)
    assert sim.status_code == 200, sim.text
    res = sim.json()["resultado"]
    # Solo hay una quincena cerrada (1 000 000) como salario de referencia
    assert res["salario_promedio_mensual"] == "1000000.00"
    assert res["dias_preaviso"] == "30" and D(res["dias_cesantia"]) == 40   # 2 años 3 meses: 20 x 2
    assert D(res["deducciones"]) >= D("75000")                                 # saldo del préstamo
    liq = client.post(f"/empresas/{empresa}/liquidaciones", json=datos, headers=op).json()
    assert client.post(f"/empresas/{empresa}/liquidaciones/{liq['id']}/aprobar", headers=op).status_code == 403
    assert client.post(f"/empresas/{empresa}/liquidaciones/{liq['id']}/aprobar", headers=ap).status_code == 200
    emp = client.get(f"/empresas/{empresa}/empleados/{empleados['beto']}", headers=op).json()
    assert emp["estado"] == "liquidado"
    assert client.get(f"/empresas/{empresa}/liquidaciones/{liq['id']}/pdf", headers=op).content[:4] == b"%PDF"


def test_cambio_salarial_requiere_aprobacion(client, usuarios, empresa, empleados):
    op, ap = usuarios["operador"]["h"], usuarios["aprobador"]["h"]
    c = client.post(f"/empresas/{empresa}/empleados/{empleados['ana']}/salario",
                    json={"fecha_efectiva": "2026-01-01", "salario_nuevo": "650000"}, headers=op).json()
    assert c["estado"] == "pendiente"
    assert client.post(f"/empresas/{empresa}/empleados/historial/{c['id']}/decision", json={"aprobar": True},
                       headers=op).status_code == 403
    r = client.post(f"/empresas/{empresa}/empleados/historial/{c['id']}/decision", json={"aprobar": True}, headers=ap)
    assert r.json()["estado"] == "autorizado"
    assert client.get(f"/empresas/{empresa}/empleados/{empleados['ana']}", headers=op).json()["salario_mensual"] == "650000.00"


def test_contador_lectura_no_escribe(client, usuarios, empresa, dsn):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("update asignaciones_contador set permiso = 'lectura' where empresa_id = %s", (empresa,))
    h = usuarios["contador"]["h"]
    assert client.get(f"/empresas/{empresa}/empleados", headers=h).status_code == 200
    r = client.post(f"/empresas/{empresa}/sucursales", json={"nombre": "Nueva"}, headers=h)
    assert r.status_code == 403


def test_yo_muestra_empleos_del_portal(client, usuarios, empresa, empleados, dsn):
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("update empleados set usuario_id = %s where id = %s", (usuarios["empleado"]["id"], empleados["ana"]))
    yo = client.get("/yo", headers=usuarios["empleado"]["h"]).json()
    assert [e["empresa"] for e in yo["empleos"]] == ["Constructora Prueba SA"]


def test_periodo_y_calendario_coherentes(client, usuarios, empresa, empleados):
    """Caso real: calendario mensual con colaborador quincenal y planilla del 1 al 15 pagó el salario
    de un mes prorrateado. Ahora ambas combinaciones se rechazan con un mensaje claro."""
    op, adm = usuarios["operador"]["h"], usuarios["admin"]["h"]
    r = client.post(f"/empresas/{empresa}/planillas", json={"frecuencia": "monthly", "desde": "2026-04-01",
                                                            "hasta": "2026-04-15"}, headers=op)
    assert r.status_code == 422 and "mensual" in r.json()["detail"]
    r = client.post(f"/empresas/{empresa}/planillas", json={"tipo": "extraordinaria", "frecuencia": "monthly",
                                                            "desde": "2026-04-01", "hasta": "2026-04-15"}, headers=op)
    assert r.status_code == 201, r.text
    cal = client.post(f"/empresas/{empresa}/calendarios", json={"nombre": "Mensual", "frecuencia": "monthly"}, headers=adm)
    assert cal.status_code == 201, cal.text
    r = client.patch(f"/empresas/{empresa}/empleados/{empleados['ana']}", json={"calendario_id": cal.json()["id"]},
                     headers=op)
    assert r.status_code == 422 and "quincenal" in r.json()["detail"]
