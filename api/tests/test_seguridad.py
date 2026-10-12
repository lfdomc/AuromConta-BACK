"""Regresiones de la revisión independiente de seguridad y dinero."""
import pytest


@pytest.fixture(scope="module")
def base(client, usuarios):
    a = usuarios["admin"]["h"]
    eid = client.post("/empresas", json={"nombre": "Seguridad SA"}, headers=a).json()["id"]
    for u in ("operador", "aprobador"):
        client.post(f"/empresas/{eid}/usuarios", json={"correo": f"{u}@prueba.cr", "rol": u}, headers=a)
    emp = client.post(f"/empresas/{eid}/empleados", json={"identificacion": "9-9999-9999", "nombre": "Zoe",
                                                          "fecha_ingreso": "2024-01-01", "salario_mensual": "600000",
                                                          "iban": "CR05015202001026284066"}, headers=a).json()
    return {"e": eid, "emp": emp["id"]}


def test_operador_no_se_autoaprueba(client, usuarios, base):
    op = usuarios["operador"]["h"]
    v = client.post(f"/empresas/{base['e']}/vacaciones", json={"empleado_id": base["emp"], "tipo": "dinero", "dias": "3",
                                                               "monto": "999999", "estado": "aprobada"}, headers=op).json()
    assert v["estado"] == "solicitada"
    he = client.post(f"/empresas/{base['e']}/horas-extra", json={"empleado_id": base["emp"], "fecha": "2026-06-03",
                                                                  "horas": "2"}, headers=op).json()
    r = client.patch(f"/empresas/{base['e']}/horas-extra/{he['id']}", json={"estado": "aprobada"}, headers=op)
    assert r.status_code == 403


def test_editar_aprobada_vuelve_a_pendiente(client, usuarios, base):
    op, ap = usuarios["operador"]["h"], usuarios["aprobador"]["h"]
    he = client.post(f"/empresas/{base['e']}/horas-extra", json={"empleado_id": base["emp"], "fecha": "2026-06-04",
                                                                  "horas": "2"}, headers=op).json()
    client.post(f"/empresas/{base['e']}/horas-extra/aprobar", json=[he["id"]], headers=ap)
    r = client.patch(f"/empresas/{base['e']}/horas-extra/{he['id']}", json={"monto": "777777"}, headers=op).json()
    assert r["estado"] == "pendiente"


def test_misma_novedad_no_se_paga_en_dos_planillas(client, usuarios, base):
    op, ap = usuarios["operador"]["h"], usuarios["aprobador"]["h"]
    e = base["e"]
    he = client.post(f"/empresas/{e}/horas-extra", json={"empleado_id": base["emp"], "fecha": "2026-07-03", "horas": "3"},
                     headers=ap).json()
    assert he["estado"] == "pendiente"
    client.post(f"/empresas/{e}/horas-extra/aprobar", json=[he["id"]], headers=ap)
    p1 = client.post(f"/empresas/{e}/planillas", json={"desde": "2026-07-01", "hasta": "2026-07-15"}, headers=op).json()
    p2 = client.post(f"/empresas/{e}/planillas", json={"tipo": "extraordinaria", "desde": "2026-07-01",
                                                       "hasta": "2026-07-15"}, headers=op).json()
    for p in (p1, p2):
        client.post(f"/empresas/{e}/planillas/{p['id']}/calcular", headers=op)
        assert client.post(f"/empresas/{e}/planillas/{p['id']}/aprobar", headers=ap).status_code == 200
    assert client.post(f"/empresas/{e}/planillas/{p1['id']}/cerrar", headers=ap).status_code == 200
    r = client.post(f"/empresas/{e}/planillas/{p2['id']}/cerrar", headers=ap)
    assert r.status_code == 409 and "recalcule" in r.json()["detail"]
    # lo pagado ya no se puede editar ni borrar
    assert client.delete(f"/empresas/{e}/horas-extra/{he['id']}", headers=ap).status_code == 409


def test_bono_con_empleado_de_otra_empresa(client, usuarios, base):
    otra = client.post("/empresas", json={"nombre": "Otra SA"}, headers=usuarios["otro"]["h"]).json()["id"]
    r = client.post(f"/empresas/{otra}/bonos", json={"empleado_id": base["emp"], "nombre": "x", "monto": "1",
                                                     "desde": "2026-01-01"}, headers=usuarios["otro"]["h"])
    assert r.status_code >= 400
