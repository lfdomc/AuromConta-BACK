"""Hallazgos de la auditoría del 12/10/2026."""
from datetime import datetime, timedelta, timezone

from app import fechas


def test_hoy_es_la_fecha_de_costa_rica(monkeypatch):
    # 23:30 en Costa Rica = 05:30 UTC del día siguiente: la fecha debe ser la de Costa Rica.
    class Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            utc = datetime(2026, 10, 13, 5, 30, tzinfo=timezone.utc)
            return utc.astimezone(tz) if tz else utc.replace(tzinfo=None)
    monkeypatch.setattr(fechas, "datetime", Reloj)
    assert str(fechas.hoy_cr()) == "2026-10-12"
    assert fechas.ahora_cr().utcoffset() == timedelta(hours=-6)


def test_la_base_trabaja_en_hora_de_costa_rica(dsn):
    from app import db
    db.configurar_pool(dsn)
    with db.sistema() as c:
        assert c.execute("show timezone").fetchone()["TimeZone"] == "America/Costa_Rica"


def test_enlaces_del_back_apuntan_a_paginas_del_front():
    import pathlib
    import re
    app = pathlib.Path(__file__).resolve().parents[1] / "app"
    front = pathlib.Path(__file__).resolve().parents[3] / "front" / "src" / "app"
    if not front.exists():
        return   # el paquete del back se puede probar sin el front
    rutas = set()
    for p in front.rglob("page.tsx"):
        r = "/" + "/".join(x for x in p.relative_to(front).parent.parts if not x.startswith("("))
        rutas.add(re.sub(r"\[[^\]]+\]", "*", r).rstrip("/") or "/")
    rutas.add("/auth/callback")
    malos = []
    for p in app.rglob("*.py"):
        for m in re.finditer(r"app_url[^}]*\}(/[^\"'?{\s]*(?:\{[^}]+\}[^\"'?{\s]*)*)", p.read_text()):
            u = re.sub(r"\{[^}]+\}", "*", m.group(1)).rstrip("/") or "/"
            if not any(re.fullmatch(re.escape(r).replace(r"\*", "[^/]+"), u) for r in rutas):
                malos.append(f"{p.name}: {m.group(1)}")
    assert not malos, malos


def test_respuesta_de_odoo_con_tope_de_tamano(monkeypatch):
    import http.server
    import threading
    import xmlrpc.client

    import pytest
    from fastapi import HTTPException

    from app.routers import importaciones as imp

    cuerpo = {"n": 10}

    class Servidor(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            xml = xmlrpc.client.dumps(("x" * cuerpo["n"],), methodresponse=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(xml)))
            self.end_headers()
            self.wfile.write(xml)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Servidor)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://odoo.prueba:{srv.server_port}/xmlrpc/2/common"
        proxy = xmlrpc.client.ServerProxy(url, transport=imp._transporte(False, "127.0.0.1"))
        assert proxy.version() == "x" * 10
        monkeypatch.setattr(imp, "MAX_RESPUESTA_ODOO", 4096)
        cuerpo["n"] = 100_000
        proxy = xmlrpc.client.ServerProxy(url, transport=imp._transporte(False, "127.0.0.1"))
        with pytest.raises(HTTPException) as e:
            proxy.version()
        assert e.value.status_code == 413
    finally:
        srv.shutdown()
