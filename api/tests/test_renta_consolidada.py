"""Renta mensual consolidada (equivalente al FIX17 del módulo Odoo, oct-2026)."""
from decimal import Decimal as D


def test_renta_consolidada_cuenta_quincenas_sin_aprobar(client, usuarios):
    a = usuarios["admin"]["h"]
    eid = client.post("/empresas", json={"nombre": "Renta Mes SA", "cedula_juridica": "3-101-909090"}, headers=a).json()["id"]
    assert client.patch(f"/empresas/{eid}/configuracion", json={"metodo_renta": "monthly_consolidated"}, headers=a).status_code == 200
    emp = client.post(f"/empresas/{eid}/empleados", json={
        "identificacion": "1-1400-1400", "nombre": "Luis", "apellidos": "Mora", "fecha_ingreso": "2025-01-01",
        "frecuencia": "biweekly", "salario_mensual": "1400000"}, headers=a)
    assert emp.status_code == 201, emp.text
    q1 = client.post(f"/empresas/{eid}/planillas", json={"desde": "2026-10-01", "hasta": "2026-10-15"}, headers=a).json()["id"]
    q2 = client.post(f"/empresas/{eid}/planillas", json={"desde": "2026-10-16", "hasta": "2026-10-31"}, headers=a).json()["id"]

    def renta(pid):
        return D(client.get(f"/empresas/{eid}/planillas/{pid}", headers=a).json()["boletas"][0]["renta"])

    assert client.post(f"/empresas/{eid}/planillas/{q1}/calcular", headers=a).status_code == 200
    assert client.post(f"/empresas/{eid}/planillas/{q2}/calcular", headers=a).status_code == 200
    # Impuesto del mes sobre 1.400.000 = 42.900 + 7.950 = 50.850. La 2.ª quincena paga el mes menos lo de la 1.ª,
    # aunque la 1.ª aún no esté aprobada (antes daba 0: calculaba el mes con una sola quincena).
    assert renta(q1) + renta(q2) == D("50850.00")
    assert renta(q2) > 0

    # Si la 1.ª se recalcula después, la 2.ª no se puede aprobar con la renta vieja
    assert client.post(f"/empresas/{eid}/planillas/{q1}/calcular", headers=a).status_code == 200
    r = client.post(f"/empresas/{eid}/planillas/{q2}/aprobar", headers=a)
    assert r.status_code == 409 and "Recalcular" in r.json()["detail"]
    assert client.post(f"/empresas/{eid}/planillas/{q2}/calcular", headers=a).status_code == 200
    assert client.post(f"/empresas/{eid}/planillas/{q2}/aprobar", headers=a).status_code == 200
