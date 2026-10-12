"""Fase 8 · portal de socios: perfil, clientes asignados, comisiones propias, tickets y prospectos."""
import os
import time
import uuid
from datetime import date, timedelta

import jwt
import psycopg
import pytest


def _token(uid, email):
    t = jwt.encode({"sub": str(uid), "email": email, "aud": "authenticated", "role": "authenticated", "iss": "https://proyecto.supabase.co/auth/v1",
                    "exp": int(time.time()) + 3600}, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module")
def m(client, dsn):
    ids = {k: uuid.uuid4() for k in ["soc1", "soc2", "cli_x", "cli_y", "nadie"]}
    with psycopg.connect(dsn, autocommit=True) as c:
        for k, v in ids.items():
            c.execute("insert into auth.users (id, email) values (%s, %s)", (v, f"{k}@socios.cr"))
        s1 = c.execute("insert into socios (usuario_id, tipo, nombre, correo, codigo_referido) "
                       "values (%s,'contador','Sofía Contadora','soc1@socios.cr','SOCUNO01') returning id",
                       (ids["soc1"],)).fetchone()[0]
        s2 = c.execute("insert into socios (usuario_id, tipo, nombre, correo, codigo_referido) "
                       "values (%s,'vendedor','Victor Vendedor','soc2@socios.cr','SOCDOS02') returning id",
                       (ids["soc2"],)).fetchone()[0]
    h = {k: _token(v, f"{k}@socios.cr") for k, v in ids.items()}
    x = client.post("/empresas", json={"nombre": "Cliente Equis SA", "codigo_referido": "socuno01"}, headers=h["cli_x"]).json()["id"]
    y = client.post("/empresas", json={"nombre": "Cliente Ye SA"}, headers=h["cli_y"]).json()["id"]
    with psycopg.connect(dsn, autocommit=True) as c:
        for socio, emp, monto, periodo in ((s1, x, "11000.00", "2026-09-01"), (s1, x, "11000.00", "2026-10-01"),
                                           (s2, y, "55000.00", "2026-09-01")):
            c.execute("""insert into comisiones (socio_id, empresa_id, concepto, periodo, base, porcentaje, monto)
                         values (%s,%s,'recurrente_contador',%s,55000,20,%s)""", (socio, emp, periodo, monto))
        c.execute("insert into liquidaciones_socio (socio_id, mes, total) values (%s, '2026-08-01', 22000)", (s1,))
        c.execute("insert into liquidaciones_socio (socio_id, mes, total) values (%s, '2026-08-01', 99000)", (s2,))
    return {"ids": ids, "h": h, "x": x, "y": y, "s1": s1, "s2": s2}


def test_perfil_del_socio(client, m):
    from app.config import settings
    r = client.get("/socios/yo", headers=m["h"]["soc1"])
    assert r.status_code == 200, r.text
    yo = r.json()
    assert yo["codigo_referido"] == "SOCUNO01" and yo["enlace_registro"] == f"{settings.app_url}/registro?ref=SOCUNO01"
    assert yo["clientes"] == 1 and yo["comisiones"]["pendiente"] == "22000.00"
    assert client.get("/socios/yo", headers=m["h"]["nadie"]).status_code == 403
    assert client.get("/socios/clientes", headers=m["h"]["cli_x"]).status_code == 403


def test_editar_perfil(client, m):
    h = m["h"]["soc1"]
    assert client.patch("/socios/yo", json={"iban": "CR00123"}, headers=h).status_code == 422
    assert client.patch("/socios/yo", json={"iban": "CR05015202001026284067"}, headers=h).status_code == 422   # verificador
    assert client.patch("/socios/yo", json={"estado": "activo"}, headers=h).status_code == 422                 # no editable
    r = client.patch("/socios/yo", json={"iban": "cr05 0152 0200 1026 2840 66", "telefono": "8999-1111",
                                         "telegram_chat_id": "123456"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["iban"] == "CR05015202001026284066" and r.json()["telefono"] == "8999-1111"
    assert r.json()["telegram_vinculado"]


def test_socio_ve_solo_sus_clientes(client, m):
    c1 = client.get("/socios/clientes", headers=m["h"]["soc1"]).json()
    assert [c["empresa_id"] for c in c1] == [m["x"]]
    c = c1[0]
    assert c["nombre"] == "Cliente Equis SA" and c["estado_servicio"] == "prueba" and c["empleados"] == 0
    assert "soporte" in c["funciones"] and c["permiso"] == "operacion" and c["tickets_abiertos"] == 0
    assert client.get("/socios/clientes", headers=m["h"]["soc2"]).json() == []
    # y por la vía normal tampoco puede entrar a la empresa de otro
    assert client.get(f"/empresas/{m['y']}", headers=m["h"]["soc1"]).status_code == 404


def test_comisiones_y_liquidaciones_propias(client, m):
    todas = client.get("/socios/comisiones", headers=m["h"]["soc1"]).json()
    assert len(todas) == 2 and {c["socio_id"] for c in todas} == {str(m["s1"])}
    assert todas[0]["empresa_nombre"] == "Cliente Equis SA"
    sep = client.get("/socios/comisiones", params={"mes": "2026-09"}, headers=m["h"]["soc1"]).json()
    assert len(sep) == 1 and sep[0]["monto"] == "11000.00"
    assert client.get("/socios/comisiones", params={"mes": "septiembre"}, headers=m["h"]["soc1"]).status_code == 422
    otro = client.get("/socios/comisiones", headers=m["h"]["soc2"]).json()
    assert len(otro) == 1 and otro[0]["monto"] == "55000.00"
    liq = client.get("/socios/liquidaciones", headers=m["h"]["soc1"]).json()
    assert [l["total"] for l in liq] == ["22000.00"]


def test_tickets_de_sus_clientes(client, m):
    tx = client.post(f"/empresas/{m['x']}/tickets", json={"asunto": "Duda con el aguinaldo"}, headers=m["h"]["cli_x"]).json()
    ty = client.post(f"/empresas/{m['y']}/tickets", json={"asunto": "Duda con vacaciones"}, headers=m["h"]["cli_y"]).json()
    assert tx["asignado_socio_id"] == str(m["s1"])
    ids = {t["id"] for t in client.get("/socios/tickets", headers=m["h"]["soc1"]).json()}
    assert tx["id"] in ids and ty["id"] not in ids
    assert client.get("/socios/clientes", headers=m["h"]["soc1"]).json()[0]["tickets_abiertos"] == 1
    assert client.get("/socios/tickets", headers=m["h"]["soc2"]).json() == []


def test_prospectos_con_atribucion(client, m):
    r = client.post("/socios/prospectos", json={"empresa_nombre": "Panadería  La Espiga", "contacto": "Doña Marta",
                                                "correo": "marta@espiga.cr"}, headers=m["h"]["soc1"])
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["empresa_nombre"] == "Panadería La Espiga" and p["vence_atribucion"] == (date.today() + timedelta(days=90)).isoformat()
    assert p["enlace_registro"].endswith("ref=SOCUNO01")
    otro = client.post("/socios/prospectos", json={"empresa_nombre": "panaderia la espiga"}, headers=m["h"]["soc2"])
    assert otro.status_code == 409 and "Otro socio" in otro.json()["detail"]
    assert client.post("/socios/prospectos", json={"empresa_nombre": "PANADERÍA LA ESPIGA"},
                       headers=m["h"]["soc1"]).status_code == 409
    assert client.post("/socios/prospectos", json={"empresa_nombre": "Ferretería El Clavo"},
                       headers=m["h"]["soc2"]).status_code == 201
    mios = client.get("/socios/prospectos", headers=m["h"]["soc1"]).json()
    assert [x["empresa_nombre"] for x in mios] == ["Panadería La Espiga"] and mios[0]["dias_restantes"] == 90
    assert [x["empresa_nombre"] for x in client.get("/socios/prospectos", headers=m["h"]["soc2"]).json()] == ["Ferretería El Clavo"]
    assert client.post("/socios/prospectos", json={"empresa_nombre": "X"}, headers=m["h"]["nadie"]).status_code in (403, 422)
