"""Seguridad avanzada: cabeceras, límites, idempotencia, MFA, cifrado e inyección SQL."""
import time
import uuid

import jwt
import os
import pytest

from app.config import settings


def _tok(uid, **extra):
    t = jwt.encode({"sub": str(uid), "aud": "authenticated", "role": "authenticated", "exp": int(time.time()) + 600,
                    "iss": "https://proyecto.supabase.co/auth/v1", **extra}, os.environ["SUPABASE_JWT_SECRET"], "HS256")
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="module")
def emp(client, usuarios):
    a = usuarios["admin"]["h"]
    e = client.post("/empresas", json={"nombre": "Segura SA"}, headers=a).json()["id"]
    client.post(f"/empresas/{e}/empleados", json={"identificacion": "7-7777-7777", "nombre": "Ola", "fecha_ingreso": "2025-01-01",
                                                  "salario_mensual": "500000"}, headers=a)
    return e


def test_cabeceras_seguras(client, usuarios):
    r = client.get("/yo", headers=usuarios["admin"]["h"])
    for k in ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy",
              "Referrer-Policy", "X-Request-ID"):
        assert k in r.headers
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("ataque", ["' or 1=1 --", "x'; drop table empleados; --", "%' union select * from auth.users --"])
def test_inyeccion_sql_en_busqueda_no_tiene_efecto(client, usuarios, emp, ataque):
    r = client.get(f"/empresas/{emp}/empleados", params={"buscar": ataque, "estado": ataque}, headers=usuarios["admin"]["h"])
    assert r.status_code == 200 and r.json() == []
    assert len(client.get(f"/empresas/{emp}/empleados", headers=usuarios["admin"]["h"]).json()) == 1


def test_inyeccion_en_ruta_y_ordenes_rechazada(client, usuarios, emp):
    h = usuarios["admin"]["h"]
    assert client.get(f"/empresas/{emp}' or '1'='1/empleados", headers=h).status_code in (404, 422)
    assert client.get(f"/empresas/{emp}/planillas", params={"anio": "2026 or 1=1"}, headers=h).status_code == 422


def test_limite_de_tasa(client, usuarios):
    from app.seguridad import limitador
    limitador._eventos.clear()
    object.__setattr__(settings, "limite_por_minuto", 3)
    try:
        h = _tok(uuid.uuid4())
        codigos = [client.get("/yo", headers=h).status_code for _ in range(5)]
        assert 429 in codigos and codigos[:3] != [429, 429, 429]
    finally:
        object.__setattr__(settings, "limite_por_minuto", 100000)


def test_idempotencia_no_repite_la_operacion(client, usuarios, emp):
    h = {**usuarios["admin"]["h"], "Idempotency-Key": "clave-" + uuid.uuid4().hex}
    datos = {"nombre": "Sucursal Única"}
    r1 = client.post(f"/empresas/{emp}/sucursales", json=datos, headers=h)
    r2 = client.post(f"/empresas/{emp}/sucursales", json=datos, headers=h)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"] and r2.headers.get("Idempotent-Replayed") == "true"
    lista = client.get(f"/empresas/{emp}/sucursales", headers=usuarios["admin"]["h"]).json()
    assert [s["nombre"] for s in lista].count("Sucursal Única") == 1
    r3 = client.post(f"/empresas/{emp}/sucursales", json={"nombre": "Otra"}, headers=h)
    assert r3.status_code == 422


def test_idempotencia_con_token_falso_no_lee_respuestas_ajenas(client, usuarios, emp):
    clave = "clave-" + uuid.uuid4().hex
    h = {**usuarios["admin"]["h"], "Idempotency-Key": clave}
    client.post(f"/empresas/{emp}/sucursales", json={"nombre": "Privada"}, headers=h)
    falso = jwt.encode({"sub": str(usuarios["admin"]["id"]), "aud": "authenticated", "exp": int(time.time()) + 60},
                       "otro-secreto-que-no-es-el-real-1234567890", "HS256")
    r = client.post(f"/empresas/{emp}/sucursales", json={"nombre": "Privada"},
                    headers={"Authorization": f"Bearer {falso}", "Idempotency-Key": clave})
    assert r.status_code == 401


def test_mfa_obligatoria(client, usuarios, emp):
    object.__setattr__(settings, "mfa_obligatoria", True)
    try:
        sin = client.patch(f"/empresas/{emp}", json={"telefono": "2222-2222"}, headers=usuarios["admin"]["h"])
        assert sin.status_code == 403 and sin.headers.get("X-Aurom-Error") == "mfa_requerida"
        con = client.patch(f"/empresas/{emp}", json={"telefono": "2222-2222"},
                           headers=_tok(usuarios["admin"]["id"], aal="aal2"))
        assert con.status_code == 200
        # Leer no exige MFA
        assert client.get(f"/empresas/{emp}", headers=usuarios["admin"]["h"]).status_code == 200
    finally:
        object.__setattr__(settings, "mfa_obligatoria", False)


def test_token_sin_vencimiento_rechazado(client):
    t = jwt.encode({"sub": str(uuid.uuid4()), "aud": "authenticated"}, os.environ["SUPABASE_JWT_SECRET"], "HS256")
    assert client.get("/yo", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_token_alg_none_rechazado(client):
    t = jwt.encode({"sub": str(uuid.uuid4()), "aud": "authenticated", "exp": int(time.time()) + 60}, None, "none")
    assert client.get("/yo", headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_cifrado_autenticado_y_ligado_al_contexto():
    from app.servicios import cifrado
    c = cifrado.cifrar("tok_123", "cliente-1")
    assert c.startswith("v1:") and "tok_123" not in c
    assert cifrado.descifrar(c, "cliente-1") == "tok_123"
    with pytest.raises(cifrado.CifradoNoConfigurado):
        cifrado.descifrar(c, "cliente-2")          # copiado a otra fila no sirve
    alterado = c[:-4] + ("AAAA" if not c.endswith("AAAA") else "BBBB")
    with pytest.raises(cifrado.CifradoNoConfigurado):
        cifrado.descifrar(alterado, "cliente-1")   # cualquier cambio se detecta


def test_cuerpo_demasiado_grande(client, usuarios, emp):
    r = client.post(f"/empresas/{emp}/sucursales", content=b"x" * (settings.max_cuerpo_bytes + 1),
                    headers={**usuarios["admin"]["h"], "Content-Type": "application/json"})
    assert r.status_code == 413


def test_token_falso_no_usa_cubeta_ajena(client, usuarios):
    """Un token con firma inválida se limita por IP, no por el `sub` que dice traer."""
    from app.seguridad import _sujeto
    from starlette.requests import Request
    falso = jwt.encode({"sub": str(usuarios["admin"]["id"]), "aud": "authenticated", "exp": int(time.time()) + 60},
                       "no-es-el-secreto-real-0123456789abcdef", "HS256")
    req = Request({"type": "http", "headers": [(b"authorization", f"Bearer {falso}".encode())], "client": ("1.2.3.4", 1)})
    assert _sujeto(req) == "ip:1.2.3.4"


def test_ip_real_es_el_ultimo_salto():
    from app.seguridad import ip_cliente
    from starlette.requests import Request
    req = Request({"type": "http", "headers": [(b"x-forwarded-for", b"6.6.6.6, 200.1.2.3")], "client": ("10.0.0.1", 1)})
    assert ip_cliente(req) == "200.1.2.3"


def test_token_mal_formado_no_rompe(client):
    assert client.get("/yo", headers={"Authorization": "Bearer a.W10.b"}).status_code == 401


def test_cuerpo_por_partes_demasiado_grande(client, usuarios, emp):
    def partes():
        for _ in range(settings.max_cuerpo_bytes // 65536 + 2):
            yield b"x" * 65536
    r = client.post(f"/empresas/{emp}/sucursales", content=partes(),
                    headers={**usuarios["admin"]["h"], "Content-Type": "application/json"})
    assert r.status_code == 413


def test_idempotencia_distingue_parametros(client, usuarios, emp):
    clave = "clave-" + uuid.uuid4().hex
    h = {**usuarios["admin"]["h"], "Idempotency-Key": clave}
    client.post(f"/empresas/{emp}/sucursales?x=1", json={"nombre": "Q1"}, headers=h)
    r = client.post(f"/empresas/{emp}/sucursales?x=2", json={"nombre": "Q1"}, headers=h)
    assert r.status_code == 422


def test_ancla_de_auditoria(client):
    object.__setattr__(settings, "cron_secret", "secreto-cron-de-prueba")
    try:
        assert client.post("/cron/ancla-auditoria").status_code in (401, 403)
        r = client.post("/cron/ancla-auditoria", headers={"X-Cron-Secret": "secreto-cron-de-prueba"})
        assert r.status_code == 200 and r.json()["integra"] is True and len(r.json()["huella"]) == 16
    finally:
        object.__setattr__(settings, "cron_secret", "")
