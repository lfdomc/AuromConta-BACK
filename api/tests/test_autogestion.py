"""Fase 8 · soporte (tickets + bot de ayuda), Telegram, artículos, normativa, salud y tareas programadas."""
import json
import os
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal as D

import httpx
import jwt
import psycopg
import pytest

CHAT_DUENA = "999000"
CHAT_CONTA = "777000"
SECRETO_TG = "secreto-webhook"
SECRETO_CRON = "secreto-cron"


def _token(uid, email):
    t = jwt.encode({"sub": str(uid), "email": email, "aud": "authenticated", "role": "authenticated", "iss": "https://proyecto.supabase.co/auth/v1",
                    "exp": int(time.time()) + 3600}, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {t}"}


class Resp:
    def __init__(self, status=200, datos=None):
        self.status_code = status
        self._d = datos if datos is not None else {"ok": True, "result": {"message_id": 1}, "id": "msg-1"}
        self.text = json.dumps(self._d)

    def json(self):
        return self._d


@pytest.fixture
def http(monkeypatch):
    """Simula todo el HTTP saliente (Telegram, Resend, chequeos). Nada sale a internet."""
    llamadas: list[dict] = []
    caidos: set[str] = set()

    def registrar(metodo, url, **kw):
        llamadas.append({"metodo": metodo, "url": url, **kw})
        if any(c in url for c in caidos):
            raise httpx.ConnectError("sin conexión")
        return Resp()

    monkeypatch.setattr(httpx, "post", lambda url, **kw: registrar("POST", url, **kw))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: registrar("GET", url, **kw))
    monkeypatch.setattr(httpx, "request", lambda metodo, url, **kw: registrar(metodo, url, **kw))
    llamadas_obj = type("Http", (), {"llamadas": llamadas, "caidos": caidos,
                                     "telegram": lambda self, chat=None: [c["json"] for c in llamadas
                                                                          if "sendMessage" in c["url"]
                                                                          and (chat is None or str(c["json"]["chat_id"]) == str(chat))]})()
    return llamadas_obj


@pytest.fixture(autouse=True)
def ajustes():
    from app.config import settings
    nuevos = {"telegram_bot_token": "TOKEN-PRUEBA", "telegram_chat_duena": CHAT_DUENA,
              "telegram_webhook_secret": SECRETO_TG, "cron_secret": SECRETO_CRON,
              "telegram_bot_username": "AuromPruebaBot", "resend_api_key": "re_prueba"}
    previos = {k: getattr(settings, k) for k in nuevos}
    for k, v in nuevos.items():
        object.__setattr__(settings, k, v)
    yield settings
    for k, v in previos.items():
        object.__setattr__(settings, k, v)


@pytest.fixture(scope="module")
def m(client, usuarios, dsn):
    """Empresa A (con contador de soporte) y empresa B (sin contador)."""
    ids = {k: uuid.uuid4() for k in ["cli_a", "cli_b", "conta2", "oper_a"]}
    with psycopg.connect(dsn, autocommit=True) as c:
        for k, v in ids.items():
            c.execute("insert into auth.users (id, email) values (%s, %s)", (v, f"{k}@auto.cr"))
        socio = c.execute("""insert into socios (usuario_id, tipo, nombre, correo, codigo_referido, telegram_chat_id)
                             values (%s,'contador','Conta Dos','conta2@auto.cr','CONTA002',%s) returning id""",
                          (ids["conta2"], CHAT_CONTA)).fetchone()[0]
    h = {k: _token(v, f"{k}@auto.cr") for k, v in ids.items()}
    ea = client.post("/empresas", json={"nombre": "Autogestión A SA"}, headers=h["cli_a"]).json()["id"]
    eb = client.post("/empresas", json={"nombre": "Autogestión B SA"}, headers=h["cli_b"]).json()["id"]
    r = client.post(f"/plataforma/socios/{socio}/asignar", json={"empresa_id": ea, "funciones": ["supervision", "soporte"],
                                                                "permiso": "operacion", "soporte_principal": True},
                    headers=usuarios["duena"]["h"])
    assert r.status_code == 201, r.text
    client.post(f"/empresas/{ea}/usuarios", json={"correo": "oper_a@auto.cr", "rol": "operador"}, headers=h["cli_a"])
    return {"ids": ids, "h": h, "ea": ea, "eb": eb, "socio": socio}


# ----------------------------------------------------------------- tickets ---
@pytest.fixture(scope="module")
def tickets(client, m):
    return {}


def test_ticket_se_asigna_al_contador(client, m, http, tickets):
    r = client.post(f"/empresas/{m['ea']}/tickets", json={"asunto": "No sé cómo enviar las boletas por correo",
                                                         "mensaje": "Las boletas no llegan"}, headers=m["h"]["cli_a"])
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["asignado_socio_id"] == str(m["socio"]) and t["nivel"] == "contador" and t["asignado_nombre"] == "Conta Dos"
    autores = [x["autor"] for x in t["conversacion"]]
    assert autores[0] == "cliente" and "bot" in autores          # el bot sugiere artículos
    assert t["ayuda"]["articulos"][0]["titulo"] == "Cómo enviar las boletas de pago por correo"
    assert http.telegram(CHAT_CONTA), "el contador debe recibir aviso por Telegram"
    assert not http.telegram(CHAT_DUENA)                          # ticket normal: no molesta a la dueña
    tickets["a"] = t["id"]


def test_ticket_sin_contador_va_a_plataforma(client, m, usuarios, http, dsn, tickets):
    r = client.post(f"/empresas/{m['eb']}/tickets", json={"asunto": "Error al cerrar planilla", "prioridad": "urgente"},
                    headers=m["h"]["cli_b"])
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["nivel"] == "plataforma" and t["asignado_socio_id"] is None
    assert http.telegram(CHAT_DUENA)
    al = client.get("/plataforma/alertas?tipo=ticket", headers=usuarios["duena"]["h"]).json()
    assert any(t["id"][:8] in a["mensaje"] and a["enviada_en"] for a in al)
    tickets["b"] = t["id"]


def test_contador_ve_y_responde_solo_sus_clientes(client, m, http, tickets):
    hc = m["h"]["conta2"]
    bandeja = client.get("/soporte/tickets", headers=hc).json()
    ids = {t["id"] for t in bandeja}
    assert tickets["a"] in ids and tickets["b"] not in ids
    assert client.get(f"/empresas/{m['eb']}/tickets/{tickets['b']}", headers=hc).status_code == 404
    assert client.post(f"/empresas/{m['eb']}/tickets/{tickets['b']}/mensajes", json={"texto": "hola"},
                       headers=hc).status_code == 404
    r = client.post(f"/empresas/{m['ea']}/tickets/{tickets['a']}/mensajes", json={"texto": "Revise el correo del empleado"},
                    headers=hc)
    assert r.status_code == 201 and r.json()["autor"] == "contador"
    t = client.get(f"/empresas/{m['ea']}/tickets/{tickets['a']}", headers=hc).json()
    assert t["estado"] == "en_proceso"
    r = client.patch(f"/empresas/{m['ea']}/tickets/{tickets['a']}", json={"minutos_invertidos": 15}, headers=hc)
    assert r.status_code == 200 and r.json()["minutos_invertidos"] == 15
    # el cliente no registra tiempo ni marca resuelto; tampoco ve tickets de otras empresas
    assert client.patch(f"/empresas/{m['ea']}/tickets/{tickets['a']}", json={"minutos_invertidos": 1},
                        headers=m["h"]["cli_a"]).status_code == 403
    assert client.patch(f"/empresas/{m['ea']}/tickets/{tickets['a']}", json={"estado": "resuelto"},
                        headers=m["h"]["cli_a"]).status_code == 403
    assert client.get(f"/empresas/{m['ea']}/tickets", headers=m["h"]["cli_b"]).status_code == 404


def test_escalar_a_plataforma(client, m, usuarios, http, tickets):
    url = f"/empresas/{m['ea']}/tickets/{tickets['a']}"
    assert client.post(f"{url}/escalar", json={"motivo": "x"}, headers=m["h"]["oper_a"]).status_code == 403
    r = client.post(f"{url}/escalar", json={"motivo": "Parece un error del sistema"}, headers=m["h"]["conta2"])
    assert r.status_code == 200, r.text
    assert r.json()["nivel"] == "plataforma" and r.json()["estado"] == "escalado"
    assert r.json()["conversacion"][-1]["texto"].startswith("Escalado a A.U.R.O.M.")
    assert any("Ticket escalado" in x["text"] for x in http.telegram(CHAT_DUENA))
    assert client.post(f"{url}/escalar", headers=m["h"]["conta2"]).status_code == 409
    duena = client.get("/soporte/tickets", headers=usuarios["duena"]["h"]).json()
    assert {tickets["a"], tickets["b"]} <= {t["id"] for t in duena}
    assert client.get("/soporte/tickets?nivel=plataforma", headers=usuarios["duena"]["h"]).json()[0]["prioridad"] == "urgente"
    # la plataforma responde y resuelve
    r = client.patch(url, json={"estado": "resuelto"}, headers=usuarios["duena"]["h"])
    assert r.json()["estado"] == "resuelto" and r.json()["resuelto_por"] == "plataforma"
    # un usuario sin acceso no ve la bandeja de soporte
    assert client.get("/soporte/tickets", headers=usuarios["otro"]["h"]).status_code == 403


# ------------------------------------------------------------------- ayuda ---
def test_busqueda_de_ayuda(client, m):
    r = client.get("/ayuda/buscar", params={"q": "¿Cómo envío las boletas por correo?"}, headers=m["h"]["cli_a"])
    assert r.status_code == 200
    d = r.json()
    assert d["articulos"][0]["titulo"] == "Cómo enviar las boletas de pago por correo"
    assert d["respuesta_sugerida"]["titulo"] == d["articulos"][0]["titulo"]
    sin_tilde = client.get("/ayuda/buscar", params={"q": "liquidacion por despido"}, headers=m["h"]["cli_a"]).json()
    assert sin_tilde["articulos"][0]["titulo"] == "Cómo hacer una liquidación laboral"
    assert client.get("/ayuda/buscar", params={"q": "zzzz qqqq"}, headers=m["h"]["cli_a"]).json()["respuesta_sugerida"] is None
    assert client.get("/ayuda/buscar", params={"q": "boletas"}).status_code == 401


def test_articulos_solo_plataforma(client, m, usuarios):
    d = usuarios["duena"]["h"]
    nuevo = {"titulo": "Cómo exportar el archivo SICERE", "contenido": "En Reportes › Exportaciones elija SICERE y el mes.",
             "categoria": "reportes", "palabras_clave": ["sicere", "ccss"], "publicado": False}
    assert client.post("/plataforma/articulos", json=nuevo, headers=m["h"]["cli_a"]).status_code == 403
    r = client.post("/plataforma/articulos", json=nuevo, headers=d)
    assert r.status_code == 201
    aid = r.json()["id"]
    # no publicado: el cliente no lo encuentra
    assert all(a["id"] != aid for a in client.get("/ayuda/buscar", params={"q": "exportar sicere"},
                                                    headers=m["h"]["cli_a"]).json()["articulos"])
    r = client.patch(f"/plataforma/articulos/{aid}", json={"publicado": True}, headers=d)
    assert r.json()["version"] == 2
    assert client.get("/ayuda/buscar", params={"q": "exportar sicere"}, headers=m["h"]["cli_a"]).json()["articulos"][0]["id"] == aid
    assert client.delete(f"/plataforma/articulos/{aid}", headers=d).status_code == 204
    assert client.get(f"/plataforma/articulos/{aid}", headers=d).status_code == 404


# ---------------------------------------------------------------- Telegram ---
def _update(uid, chat, texto):
    return {"update_id": uid, "message": {"message_id": uid, "date": 0, "text": texto,
                                          "chat": {"id": chat, "type": "private"}, "from": {"id": chat}}}


def test_webhook_rechaza_secreto(client, http):
    assert client.post("/webhooks/telegram", json=_update(1, 1, "/start")).status_code == 401
    assert client.post("/webhooks/telegram", json=_update(1, 1, "/start"),
                       headers={"X-Telegram-Bot-Api-Secret-Token": "otro"}).status_code == 401


def test_vincular_y_comandos(client, m, dsn, http):
    tg = {"X-Telegram-Bot-Api-Secret-Token": SECRETO_TG}
    v = client.post("/telegram/vincular", headers=m["h"]["cli_a"])
    assert v.status_code == 200, v.text
    codigo = v.json()["codigo"]
    assert v.json()["enlace"] == f"https://t.me/AuromPruebaBot?start={codigo}" and v.json()["rol"] == "cliente"
    chat = 5550001
    base = int(time.time() * 1000) % 10_000_000

    # un chat sin vincular recibe instrucciones
    assert client.post("/webhooks/telegram", json=_update(base, chat, "/estado"), headers=tg).json() == {"ok": True}
    assert "vincule su cuenta" in http.telegram(chat)[-1]["text"]
    # /start con código vincula
    r = client.post("/webhooks/telegram", json=_update(base + 1, chat, f"/start {codigo}"), headers=tg)
    assert r.json() == {"ok": True}
    assert "Cuenta vinculada" in http.telegram(chat)[-1]["text"]
    with psycopg.connect(dsn) as c:
        fila = c.execute("select chat_id, activo from telegram_vinculos where codigo_vinculacion = %s", (codigo,)).fetchone()
    assert fila == (str(chat), True)
    # idempotencia por update_id
    assert client.post("/webhooks/telegram", json=_update(base + 1, chat, f"/start {codigo}"), headers=tg).json()["duplicado"]
    # el código ya no sirve para otro chat
    client.post("/webhooks/telegram", json=_update(base + 2, 5550002, f"/start {codigo}"), headers=tg)
    assert "no es válido" in http.telegram(5550002)[-1]["text"]

    client.post("/webhooks/telegram", json=_update(base + 3, chat, "/estado"), headers=tg)
    assert "Autogestión A SA" in http.telegram(chat)[-1]["text"]
    client.post("/webhooks/telegram", json=_update(base + 4, chat, "/ayuda ¿cómo registro una incapacidad?"), headers=tg)
    assert "Cómo registrar una incapacidad" in http.telegram(chat)[-1]["text"]
    antes = len(http.telegram(CHAT_CONTA))
    client.post("/webhooks/telegram", json=_update(base + 5, chat, "/ticket El reporte del INS sale vacío"), headers=tg)
    assert "Conta Dos" in http.telegram(chat)[-1]["text"]
    assert len(http.telegram(CHAT_CONTA)) == antes + 1
    lista = client.get(f"/empresas/{m['ea']}/tickets", headers=m["h"]["cli_a"]).json()
    assert any(t["canal"] == "telegram" and t["asunto"] == "El reporte del INS sale vacío" for t in lista)
    # comandos de la dueña no aplican a clientes
    client.post("/webhooks/telegram", json=_update(base + 6, chat, "/resumen"), headers=tg)
    assert "Resumen de la plataforma" not in http.telegram(chat)[-1]["text"]
    with psycopg.connect(dsn) as c:
        ev = c.execute("select count(*) from negocio.eventos_webhook where origen='telegram' and error is not null").fetchone()[0]
    assert ev == 0


def test_bot_duena_y_socio(client, m, usuarios, dsn, http):
    tg = {"X-Telegram-Bot-Api-Secret-Token": SECRETO_TG}
    base = int(time.time() * 1000) % 10_000_000 + 20_000_000
    for quien, chat in (("duena", 6660001), ("conta2", 6660002)):
        h = usuarios["duena"]["h"] if quien == "duena" else m["h"]["conta2"]
        codigo = client.post("/telegram/vincular", headers=h).json()["codigo"]
        client.post("/webhooks/telegram", json=_update(base, chat, f"/start {codigo}"), headers=tg)
        base += 1
    client.post("/webhooks/telegram", json=_update(base, 6660001, "/resumen"), headers=tg)
    assert "Resumen de la plataforma" in http.telegram(6660001)[-1]["text"]
    client.post("/webhooks/telegram", json=_update(base + 1, 6660001, "/alertas"), headers=tg)
    assert "lertas" in http.telegram(6660001)[-1]["text"]
    client.post("/webhooks/telegram", json=_update(base + 2, 6660002, "/clientes"), headers=tg)
    texto = http.telegram(6660002)[-1]["text"]
    assert "Autogestión A SA" in texto and "Autogestión B SA" not in texto
    with psycopg.connect(dsn) as c:
        assert c.execute("select telegram_chat_id from socios where id = %s", (m["socio"],)).fetchone()[0] == "6660002"
        c.execute("update socios set telegram_chat_id = %s where id = %s", (CHAT_CONTA, m["socio"]))


# --------------------------------------------------------------- normativa ---
def test_normativa_valida_con_el_motor(client, m, usuarios, dsn):
    d = usuarios["duena"]["h"]
    assert client.get("/plataforma/normativa", headers=m["h"]["cli_a"]).status_code == 403
    mala = client.post("/plataforma/normativa", json={"fuente": "La Gaceta", "resumen": "Nueva tasa CCSS",
                                                      "parametros_propuestos": {"ccss_obrero": "10.83"},
                                                      "vigente_desde": "2033-01-01"}, headers=d)
    assert mala.status_code == 201, mala.text
    r = client.post(f"/plataforma/normativa/{mala.json()['id']}/aprobar", headers=d)
    assert r.status_code == 409 and "ccss_obrero" in r.json()["detail"]
    assert r.json()["resultado_pruebas"]["ok"] is False
    guardada = next(n for n in client.get("/plataforma/normativa", headers=d).json() if n["id"] == mala.json()["id"])
    assert guardada["resultado_pruebas_json"]["ok"] is False and guardada["estado"] == "detectado"
    # tramos de renta con hueco: también se rechazan
    r = client.post(f"/plataforma/normativa/{mala.json()['id']}/aprobar", json={"parametros_propuestos": {
        "tramos_renta": [{"desde": "0", "hasta": "900000", "tasa": "0"}, {"desde": "950000", "hasta": None, "tasa": "10"}]}},
        headers=d)
    assert r.status_code == 409 and "tramos_renta" in r.json()["detail"]
    assert client.post(f"/plataforma/normativa/{mala.json()['id']}/rechazar", json={"motivo": "Error de digitación"},
                       headers=d).json()["estado"] == "rechazado"

    buena = client.post("/plataforma/normativa", json={
        "fuente": "CCSS", "enlace": "https://www.ccss.sa.cr", "resumen": "Nueva base mínima contributiva",
        "parametros_propuestos": {"bmc_mensual": "360000"}, "vigente_desde": "2034-01-01"}, headers=d).json()
    r = client.post(f"/plataforma/normativa/{buena['id']}/aprobar", headers=d)
    assert r.status_code == 200, r.text
    assert r.json()["normativa"]["estado"] == "aplicado" and r.json()["validacion"]["ok"]
    assert len(r.json()["validacion"]["casos"]) >= 20
    pl = r.json()["parametros"]
    assert pl["vigente_desde"] == "2034-01-01" and pl["datos"]["bmc_mensual"] == "360000"
    assert pl["datos"]["ccss_obrero"] == "0.1083"          # el resto se hereda del juego vigente
    with psycopg.connect(dsn) as c:
        assert c.execute("select count(*) from parametros_legales where vigente_desde = '2034-01-01'").fetchone()[0] == 1
        assert c.execute("select count(*) from parametros_legales where vigente_desde = '2033-01-01'").fetchone()[0] == 0
    assert client.post(f"/plataforma/normativa/{buena['id']}/aprobar", headers=d).status_code == 409


# ------------------------------------------------------------------- cron ----
def test_cron_exige_secreto(client, ajustes):
    for ruta in ("/cron/diario", "/cron/salud"):
        assert client.post(ruta).status_code == 401
        assert client.post(ruta, headers={"X-Cron-Secret": "malo"}).status_code == 401
    object.__setattr__(ajustes, "cron_secret", "")
    assert client.post("/cron/diario", headers={"X-Cron-Secret": ""}).status_code == 401


def test_cron_salud(client, usuarios, http, dsn):
    h = {"X-Cron-Secret": SECRETO_CRON}
    r = client.post("/cron/salud", headers=h)
    assert r.status_code == 200 and r.json()["ok"], r.text
    servicios = {s["servicio"] for s in r.json()["servicios"]}
    assert {"base_datos", "frontend", "telegram", "resend"} <= servicios
    http.caidos.add("localhost:3000")
    r = client.post("/cron/salud", headers=h).json()
    assert not r["ok"] and not next(s for s in r["servicios"] if s["servicio"] == "frontend")["ok"]
    assert any("frontend no responde" in x["text"] for x in http.telegram(CHAT_DUENA))
    s = client.get("/plataforma/salud", headers=usuarios["duena"]["h"]).json()
    assert next(x for x in s["servicios"] if x["servicio"] == "frontend")["ok"] is False
    assert client.get("/plataforma/salud", headers=usuarios["admin"]["h"]).status_code == 403
    http.caidos.clear()
    client.post("/cron/salud", headers=h)
    assert any("frontend se recuperó" in x["text"] for x in http.telegram(CHAT_DUENA))


def test_meses_cumplidos():
    from app.routers.autogestion import meses_cumplidos
    assert meses_cumplidos(date(2024, 1, 31), date(2026, 1, 31), date(2026, 3, 31)) == 2   # 28/02 y 31/03
    assert meses_cumplidos(date(2024, 5, 10), date(2026, 5, 10), date(2026, 5, 10)) == 0
    assert meses_cumplidos(date(2024, 5, 10), date(2026, 5, 9), date(2026, 5, 10)) == 1
    assert meses_cumplidos(date(2025, 1, 1), date(2025, 1, 1), date(2026, 1, 1)) == 12     # 12 días por año (Art. 153)


def test_cron_diario(client, m, usuarios, dsn, http):
    from app.routers.autogestion import hoy_cr, meses_cumplidos
    hoy = hoy_cr()
    a, op = m["h"]["cli_a"], m["h"]["cli_a"]
    try:
        ingreso = hoy.replace(year=hoy.year - 2)
    except ValueError:
        ingreso = hoy.replace(year=hoy.year - 2, day=28)
    emp = client.post(f"/empresas/{m['ea']}/empleados", json={
        "identificacion": "9-0000-0001", "nombre": "Carla", "apellidos": "Vargas", "fecha_ingreso": ingreso.isoformat(),
        "salario_mensual": "500000", "saldo_vacaciones": "3"}, headers=op)
    assert emp.status_code == 201, emp.text
    eid = emp.json()["id"]
    # cambio salarial autorizado con fecha futura; luego "llega" su fecha efectiva
    cambio = client.post(f"/empresas/{m['ea']}/empleados/{eid}/salario", json={
        "fecha_efectiva": (hoy + timedelta(days=5)).isoformat(), "salario_nuevo": "550000", "motivo": "Aumento"}, headers=op)
    assert cambio.status_code == 201, cambio.text
    r = client.post(f"/empresas/{m['ea']}/empleados/historial/{cambio.json()['id']}/decision", json={"aprobar": True}, headers=a)
    assert r.status_code == 200, r.text
    pr = client.post(f"/empresas/{m['ea']}/prestamos", json={"empleado_id": eid, "monto": "10000", "cuota": "5000",
                                                             "primera_rebaja": hoy.isoformat()}, headers=op).json()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("update historial_salarial set fecha_efectiva = %s where id = %s", (hoy, cambio.json()["id"]))
        c.execute("update empleados set creado_en = now() - interval '40 days' where id = %s", (eid,))
        c.execute("update prestamos set saldo = 0 where id = %s", (pr["id"],))
        c.execute("delete from negocio.configuracion where clave = 'cron_diario'")
        creado = c.execute("select (creado_en at time zone 'America/Costa_Rica')::date from empleados where id = %s",
                           (eid,)).fetchone()[0]
    esperado = meses_cumplidos(ingreso, max(creado, ingreso), hoy)
    assert esperado >= 1

    h = {"X-Cron-Secret": SECRETO_CRON}
    r = client.post("/cron/diario", params={"fecha": hoy.isoformat()}, headers=h)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["errores"] == []
    assert any(x["empleado_id"] == eid and D(x["salario_nuevo"]) == D("550000") for x in res["cambios_salariales"])
    ficha = client.get(f"/empresas/{m['ea']}/empleados/{eid}", headers=op).json()
    assert D(ficha["salario_mensual"]) == D("550000")
    assert D(ficha["saldo_vacaciones"]) == D("3") + esperado
    assert client.get(f"/empresas/{m['ea']}/prestamos/{pr['id']}", headers=op).json()["estado"] == "pagado"
    # aviso de aniversario por correo a los administradores y resumen a la dueña por Telegram
    correos = [c["json"] for c in http.llamadas if "api.resend.com/emails" in c["url"]]
    assert any("cli_a@auto.cr" in c["to"] and "Carla Vargas cumple 2 año(s)" in c["html"] for c in correos)
    assert any("Resumen" in x["text"] for x in http.telegram(CHAT_DUENA))

    # segunda corrida el mismo día: no duplica vacaciones ni avisos
    r2 = client.post("/cron/diario", params={"fecha": hoy.isoformat()}, headers=h).json()
    assert r2["avisos_omitidos"] and r2["cambios_salariales"] == []
    assert D(client.get(f"/empresas/{m['ea']}/empleados/{eid}", headers=op).json()["saldo_vacaciones"]) == D("3") + esperado


# ------------------------------------------------- límites de las tareas ----
def test_tareas_con_limites(client, ajustes, http, dsn):
    """Intervalo mínimo, una sola ejecución a la vez y tope diario (servicios/tareas.py)."""
    from app.servicios import tareas
    h = {"X-Cron-Secret": SECRETO_CRON}
    object.__setattr__(ajustes, "cron_limites", True)
    try:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("delete from negocio.ejecuciones_cron")
        primera = client.post("/cron/salud", headers=h).json()
        assert "servicios" in primera
        segunda = client.post("/cron/salud", headers=h).json()
        assert segunda["omitida"] and "minutos" in segunda["motivo"]
        # Una corrida en curso bloquea otra igual (p. ej. dos cobros a la vez)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("insert into negocio.ejecuciones_cron (tarea) values ('cobros')")
        r = client.post("/cron/cobros", params={"fecha": "2026-12-01"}, headers=h).json()
        assert r["omitida"] and "en curso" in r["motivo"]
        # Si quedó colgada más de una hora, se libera sola
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("update negocio.ejecuciones_cron set inicio = now() - interval '2 hours' where tarea = 'cobros'")
        assert "omitida" not in client.post("/cron/cobros", params={"fecha": "2026-12-01"}, headers=h).json()
        with psycopg.connect(dsn, autocommit=True) as c:
            estados = [x[0] for x in c.execute("select estado from negocio.ejecuciones_cron where tarea = 'cobros' order by id")]
        assert estados == ["abandonada", "ok"]
        # Tope diario: ni los reprocesos con fecha pasan de él
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("""insert into negocio.ejecuciones_cron (tarea, estado, fin)
                         select 'cobros', 'ok', now() from generate_series(1, %s)""", (tareas.LIMITES["cobros"].tope_diario,))
        r = client.post("/cron/cobros", params={"fecha": "2026-12-02"}, headers=h).json()
        assert r["omitida"] and "tope" in r["motivo"]
    finally:
        object.__setattr__(ajustes, "cron_limites", False)
