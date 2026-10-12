"""Fase 6 · panel de la plataforma, socios, parámetros legales, invitación y portal del empleado."""
import json
import os
import time
import uuid

import httpx
import jwt
import psycopg
import pytest


def _token(uid, email):
    t = jwt.encode({"sub": str(uid), "email": email, "aud": "authenticated", "role": "authenticated", "iss": "https://proyecto.supabase.co/auth/v1",
                    "exp": int(time.time()) + 3600}, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module")
def m(client, dsn):
    ids = {k: uuid.uuid4() for k in ["jefa", "eva", "socio_nuevo", "invitado"]}
    with psycopg.connect(dsn, autocommit=True) as c:
        for k in ("jefa", "eva", "socio_nuevo"):
            c.execute("insert into auth.users (id, email) values (%s, %s)", (ids[k], f"{k}@saas.cr"))
    h = {k: _token(v, f"{k}@saas.cr") for k, v in ids.items()}
    e = client.post("/empresas", json={"nombre": "Portal Pruebas SA", "codigo_referido": "CONTA001"}, headers=h["jefa"])
    assert e.status_code == 201, e.text
    eid = e.json()["id"]
    base = {"fecha_ingreso": "2024-01-01", "frecuencia": "monthly", "iban": "CR05015202001026284066"}
    eva = client.post(f"/empresas/{eid}/empleados", json={**base, "identificacion": "5-5555-5555", "nombre": "Eva",
                                                          "apellidos": "Rojas", "salario_mensual": "800000",
                                                          "correo": "eva@saas.cr"}, headers=h["jefa"]).json()
    otto = client.post(f"/empresas/{eid}/empleados", json={**base, "identificacion": "6-6666-6666", "nombre": "Otto",
                                                           "apellidos": "Mora", "salario_mensual": "700000"},
                       headers=h["jefa"]).json()
    return {"ids": ids, "h": h, "e": eid, "eva": eva["id"], "otto": otto["id"]}


def test_rutas_de_plataforma_solo_duena(client, usuarios, m):
    for quien in ("admin", "contador", "otro", "empleado"):
        h = usuarios[quien]["h"]
        assert client.get("/plataforma/resumen", headers=h).status_code == 403
        assert client.get("/plataforma/empresas", headers=h).status_code == 403
        assert client.get("/plataforma/socios", headers=h).status_code == 403
        assert client.post("/plataforma/parametros", json={"vigente_desde": "2031-01-01", "datos": {}, "fuente": "x"},
                           headers=h).status_code in (403, 422)
    assert client.patch(f"/plataforma/empresas/{m['e']}", json={"estado_servicio": "baja"},
                        headers=m["h"]["jefa"]).status_code == 403
    assert client.get("/plataforma/resumen").status_code == 401


def test_resumen_y_empresas(client, usuarios, m):
    d = usuarios["duena"]["h"]
    res = client.get("/plataforma/resumen", headers=d).json()
    assert res["clientes"]["total"] >= 1 and res["clientes"]["por_estado"].get("prueba", 0) >= 1
    assert res["empleados_activos"] >= 2 and "mrr" in res and "abiertos" in res["tickets"]
    lista = client.get("/plataforma/empresas", params={"buscar": "Portal Pruebas"}, headers=d).json()
    assert len(lista) == 1 and lista[0]["empleados"] == 2 and lista[0]["socios"][0]["nombre"] == "Conta Uno"
    r = client.patch(f"/plataforma/empresas/{m['e']}", json={"estado_servicio": "activo", "plan": "profesional"}, headers=d)
    assert r.status_code == 200 and r.json()["estado_servicio"] == "activo" and r.json()["plan"] == "profesional"
    assert client.patch(f"/plataforma/empresas/{m['e']}", json={"estado_servicio": "otro"}, headers=d).status_code == 422


def test_socios_crud_y_asignar(client, usuarios, m):
    d = usuarios["duena"]["h"]
    r = client.post("/plataforma/socios", json={"tipo": "vendedor", "nombre": "Vera Ventas", "correo": "socio_nuevo@saas.cr",
                                                "codigo_referido": "vera2026"}, headers=d)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["cuenta_vinculada"] and s["usuario_id"] == str(m["ids"]["socio_nuevo"]) and s["codigo_referido"] == "VERA2026"
    assert s["enlace_registro"].endswith("/registro?ref=VERA2026")
    # sin cuenta y sin invitaciones configuradas: queda pendiente de vincular
    r = client.post("/plataforma/socios", json={"tipo": "contador", "nombre": "Sin Cuenta", "correo": "nadie@saas.cr"}, headers=d)
    assert r.status_code == 201 and not r.json()["cuenta_vinculada"]
    assert client.post("/plataforma/socios", json={"tipo": "vendedor", "nombre": "Dup", "correo": "x@saas.cr",
                                                   "codigo_referido": "VERA2026"}, headers=d).status_code == 409
    r = client.patch(f"/plataforma/socios/{s['id']}", json={"certificado": True, "telefono": "8888-0000"}, headers=d)
    assert r.json()["certificado"] and r.json()["telefono"] == "8888-0000"
    a = client.post(f"/plataforma/socios/{s['id']}/asignar", json={"empresa_id": m["e"], "funciones": ["ventas", "implementacion"],
                                                                 "permiso": "lectura", "soporte_principal": False}, headers=d)
    assert a.status_code == 201, a.text
    assert client.post(f"/plataforma/socios/{s['id']}/asignar", json={"empresa_id": m["e"]}, headers=d).status_code == 409
    det = client.get(f"/plataforma/socios/{s['id']}", headers=d).json()
    assert det["asignaciones"][0]["empresa_nombre"] == "Portal Pruebas SA"
    # el vendedor asignado ya ve la empresa como contador_lectura
    yo = client.get("/yo", headers=m["h"]["socio_nuevo"]).json()
    assert any(e["id"] == m["e"] and e["rol"] == "contador_lectura" for e in yo["empresas"])
    # el soporte principal sigue siendo el contador original
    cont = client.get(f"/empresas/{m['e']}/contadores", headers=m["h"]["jefa"]).json()
    assert next(c for c in cont if c["soporte_principal"])["nombre"] == "Conta Uno"


def test_parametros_se_validan(client, usuarios):
    d = usuarios["duena"]["h"]
    r = client.post("/plataforma/parametros", json={"vigente_desde": "2031-01-01", "fuente": "Prueba",
                                                    "datos": {"ccss_patronal": "26.83"}}, headers=d)
    assert r.status_code == 422 and "ccss_patronal" in r.json()["detail"]
    r = client.post("/plataforma/parametros", json={"vigente_desde": "2031-01-01", "fuente": "Prueba",
                                                    "datos": {"credito_hijo": "1800", "inventado": 1}}, headers=d)
    assert r.status_code == 422 and "inventado" in r.json()["detail"]
    r = client.post("/plataforma/parametros", json={"vigente_desde": "2031-01-01", "fuente": "Prueba",
                                                    "datos": {"credito_hijo": "1800"}}, headers=d)
    assert r.status_code == 201, r.text
    assert r.json()["datos"]["credito_hijo"] == "1800" and r.json()["validacion"]["ok"]
    assert client.post("/plataforma/parametros", json={"vigente_desde": "2031-01-01", "fuente": "Prueba",
                                                       "datos": {"credito_hijo": "1800"}}, headers=d).status_code == 409


def test_invitar_al_portal(client, usuarios, m, dsn, monkeypatch):
    from app.config import settings
    url = f"/empresas/{m['e']}/empleados"
    assert client.post(f"{url}/{m['eva']}/invitar-portal", headers=usuarios["contador"]["h"]).status_code == 403
    r = client.post(f"{url}/{m['eva']}/invitar-portal", headers=m["h"]["jefa"])
    assert r.status_code == 200 and r.json()["estado"] == "vinculado", r.text
    assert r.json()["usuario_id"] == str(m["ids"]["eva"])
    # Otto no tiene correo
    assert client.post(f"{url}/{m['otto']}/invitar-portal", headers=m["h"]["jefa"]).status_code == 409
    # correo sin cuenta y sin Supabase Admin configurado
    assert client.post(f"{url}/{m['otto']}/invitar-portal", json={"correo": "otto@saas.cr"},
                       headers=m["h"]["jefa"]).status_code == 409
    # con Supabase Admin (simulado): se invita y se vincula la cuenta nueva
    nuevo = m["ids"]["invitado"]
    llamadas = []

    def post(u, **kw):
        llamadas.append((u, kw))
        with psycopg.connect(dsn, autocommit=True) as c:   # Supabase crea la cuenta al invitar
            c.execute("insert into auth.users (id, email) values (%s, 'otto@saas.cr')", (nuevo,))
        return type("R", (), {"status_code": 200, "text": "", "json": lambda self: {"id": str(nuevo)}})()

    monkeypatch.setattr(httpx, "post", post)
    for k, v in {"supabase_url": "https://proyecto.supabase.co", "supabase_service_key": "service"}.items():
        object.__setattr__(settings, k, v)
    try:
        r = client.post(f"{url}/{m['otto']}/invitar-portal", json={"correo": "otto@saas.cr"}, headers=m["h"]["jefa"])
    finally:
        object.__setattr__(settings, "supabase_url", "")
        object.__setattr__(settings, "supabase_service_key", "")
    assert r.status_code == 200 and r.json()["estado"] == "invitado", r.text
    assert llamadas[0][0] == "https://proyecto.supabase.co/auth/v1/invite"
    assert llamadas[0][1]["json"]["data"]["empleado_id"] == m["otto"]
    ficha = client.get(f"{url}/{m['otto']}", headers=m["h"]["jefa"]).json()
    assert ficha["usuario_id"] == str(nuevo) and ficha["correo"] == "otto@saas.cr"


@pytest.fixture(scope="module")
def planillas(client, m):
    j = m["h"]["jefa"]
    cerrada = client.post(f"/empresas/{m['e']}/planillas", json={"desde": "2026-02-01", "hasta": "2026-02-28",
                                                                 "frecuencia": "monthly", "fecha_pago": "2026-02-28"}, headers=j)
    assert cerrada.status_code == 201, cerrada.text
    pid = cerrada.json()["id"]
    for paso in ("calcular", "aprobar", "cerrar"):
        r = client.post(f"/empresas/{m['e']}/planillas/{pid}/{paso}", headers=j)
        assert r.status_code == 200, (paso, r.text)
    abierta = client.post(f"/empresas/{m['e']}/planillas", json={"desde": "2026-03-01", "hasta": "2026-03-31",
                                                                 "frecuencia": "monthly"}, headers=j).json()
    assert client.post(f"/empresas/{m['e']}/planillas/{abierta['id']}/calcular", headers=j).status_code == 200
    return {"cerrada": pid, "abierta": abierta["id"]}


def test_portal_solo_boletas_propias_cerradas(client, usuarios, m, planillas):
    he = m["h"]["eva"]
    perfil = client.get("/portal/perfil", headers=he)
    assert perfil.status_code == 200, perfil.text
    assert perfil.json()["nombre"] == "Eva" and perfil.json()["empresa_nombre"] == "Portal Pruebas SA"
    boletas = client.get("/portal/boletas", headers=he).json()
    assert len(boletas) == 1 and boletas[0]["planilla_id"] == planillas["cerrada"]
    assert boletas[0]["empleado_id"] == m["eva"] and boletas[0]["hasta"] == "2026-02-28"
    pdf = client.get(f"/portal/boletas/{boletas[0]['id']}/pdf", headers=he)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    # boletas de otros empleados o de planillas abiertas: no existen para Eva
    det = client.get(f"/empresas/{m['e']}/planillas/{planillas['cerrada']}", headers=m["h"]["jefa"]).json()
    de_otto = next(b["id"] for b in det["boletas"] if b["nombre"] == "Otto")
    abierta = client.get(f"/empresas/{m['e']}/planillas/{planillas['abierta']}", headers=m["h"]["jefa"]).json()
    eva_abierta = next(b["id"] for b in abierta["boletas"] if b["nombre"] == "Eva")
    assert client.get(f"/portal/boletas/{de_otto}/pdf", headers=he).status_code == 404
    assert client.get(f"/portal/boletas/{eva_abierta}/pdf", headers=he).status_code == 404
    # el empleado no ve la empresa ni las planillas por la vía normal
    assert client.get(f"/empresas/{m['e']}", headers=he).status_code == 404
    assert client.get(f"/empresas/{m['e']}/planillas", headers=he).status_code == 404
    # quien no es empleado no tiene portal
    assert client.get("/portal/perfil", headers=usuarios["otro"]["h"]).status_code == 404
    assert client.get("/portal/boletas", headers=usuarios["otro"]["h"]).json() == []


def test_portal_vacaciones(client, m):
    he = m["h"]["eva"]
    pedido = {"dias": "2", "desde": "2026-11-02", "hasta": "2026-11-03"}
    assert client.post("/portal/vacaciones", json=pedido, headers=he).status_code == 409        # saldo 0
    client.patch(f"/empresas/{m['e']}/empleados/{m['eva']}", json={"saldo_vacaciones": "5"}, headers=m["h"]["jefa"])
    assert client.post("/portal/vacaciones", json={**pedido, "hasta": "2026-11-01"}, headers=he).status_code == 422
    r = client.post("/portal/vacaciones", json=pedido, headers=he)
    assert r.status_code == 201 and r.json()["estado"] == "solicitada", r.text
    v = client.get("/portal/vacaciones", headers=he).json()
    assert v["saldo"] == "5.00" and len(v["solicitudes"]) == 1
    # la empresa ve la solicitud pendiente de aprobar
    lista = client.get(f"/empresas/{m['e']}/vacaciones", params={"estado": "solicitada"}, headers=m["h"]["jefa"]).json()
    assert any(x["id"] == r.json()["id"] for x in lista)
