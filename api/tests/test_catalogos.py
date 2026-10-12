"""Catálogos con datos predeterminados (como el menú Configuración del módulo Odoo)."""
import uuid
from datetime import date
from decimal import Decimal as D

import psycopg
import pytest
from psycopg.rows import dict_row


@pytest.fixture(scope="module")
def e(client, dsn):
    from conftest import token
    uid = uuid.uuid4()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"cat-{uid.hex[:6]}@prueba.cr"))
    h = token(uid, "cat@prueba.cr")
    eid = client.post("/empresas", json={"nombre": "Catálogos SA", "cedula_juridica": "3-101-818181"}, headers=h).json()["id"]
    return {"id": eid, "h": h}


def test_predeterminados_editables(client, e):
    h, eid = e["h"], e["id"]
    hor = client.get(f"/empresas/{eid}/horarios", headers=h).json()
    assert len(hor) == 12 and any(x["nombre"] == "Jornada Nocturna (6 horas)" and x["jornada"] == "nocturna" for x in hor)
    tipos = client.get(f"/empresas/{eid}/tipos-empleado", headers=h).json()
    srv = [t for t in tipos if t["codigo"] == "servicios_profesionales"][0]
    assert srv["cotiza_ccss"] is False
    assert len(client.get(f"/empresas/{eid}/tipos-documento", headers=h).json()) == 9
    assert len(client.get(f"/empresas/{eid}/tipos-cobro", headers=h).json()) == 8
    # La empresa puede cambiar, agregar y borrar; «restaurar» repone solo lo que falta
    noct = [x for x in hor if x["nombre"] == "Jornada Nocturna (6 horas)"][0]
    assert client.delete(f"/empresas/{eid}/horarios/{noct['id']}", headers=h).status_code == 204
    assert client.post(f"/empresas/{eid}/tipos-empleado", json={"codigo": "temporada", "nombre": "Temporada"},
                       headers=h).status_code == 201
    assert client.post(f"/empresas/{eid}/catalogos/restaurar", headers=h).status_code == 200
    assert len(client.get(f"/empresas/{eid}/horarios", headers=h).json()) == 12
    assert len(client.get("/salarios-minimos", headers=h).json()) >= 5


def test_tipo_de_colaborador_valido(client, e):
    h, eid = e["h"], e["id"]
    base = {"fecha_ingreso": "2020-10-12", "salario_mensual": "800000", "nombre": "Ana"}
    assert client.post(f"/empresas/{eid}/empleados", json={**base, "identificacion": "1-0909-0909",
                                                           "tipo_empleado": "inventado"}, headers=h).status_code == 422
    ok = client.post(f"/empresas/{eid}/empleados", json={**base, "identificacion": "1-0909-0909",
                                                         "tipo_empleado": "temporada"}, headers=h)
    assert ok.status_code == 201, ok.text
    e["ana"] = ok.json()["id"]


def test_bono_de_antiguedad(client, e, dsn):
    from app.routers.autogestion import bonos_antiguedad
    h, eid = e["h"], e["id"]
    r = client.post(f"/empresas/{eid}/bono-antiguedad", json={"nombre": "5 años o más", "desde_anios": 5,
                                                             "tipo": "porcentaje", "porcentaje": "3"}, headers=h)
    assert r.status_code == 201, r.text
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
        creados = [x for x in bonos_antiguedad(c, date(2026, 10, 12)) if str(x["id"]) == e["ana"]]
        assert creados and creados[0]["monto"] == D("24000.00") and creados[0]["anios"] == 6
        assert not [x for x in bonos_antiguedad(c, date(2026, 10, 12)) if str(x["id"]) == e["ana"]]   # no duplica
        assert not bonos_antiguedad(c, date(2026, 10, 13))
