"""Envío de correos con Resend (https://resend.com/docs/api-reference/emails/send-email)."""
from __future__ import annotations

import base64

import httpx

from ..config import settings


class CorreoNoConfigurado(RuntimeError):
    pass


def enviar(para: str | list[str], asunto: str, html: str, adjuntos: list[tuple[str, bytes]] | None = None,
           remitente: str | None = None, responder_a: str | None = None) -> str:
    """Devuelve el id del mensaje. Lanza excepción si falla."""
    if not settings.resend_api_key:
        raise CorreoNoConfigurado("RESEND_API_KEY no está configurada")
    cuerpo = {
        "from": remitente or settings.correo_remitente,
        "to": [para] if isinstance(para, str) else para,
        "subject": una_linea(asunto),
        "html": html,
    }
    if responder_a:
        cuerpo["reply_to"] = responder_a
    if adjuntos:
        cuerpo["attachments"] = [{"filename": n, "content": base64.b64encode(b).decode()} for n, b in adjuntos]
    r = httpx.post("https://api.resend.com/emails", json=cuerpo, timeout=30,
                   headers={"Authorization": f"Bearer {settings.resend_api_key}"})
    if r.status_code >= 300:
        raise RuntimeError(f"Resend {r.status_code}: {r.text[:300]}")
    return r.json().get("id", "")


ASUNTO_BOLETA = "Boleta de pago {periodo} · {empresa}"
CUERPO_BOLETA = ("Estimado/a {colaborador}:\n\nAdjunto encontrará su boleta de pago correspondiente al periodo {periodo}.\n\n"
                 "Si tiene alguna consulta, no dude en contactar al departamento de Recursos Humanos.\n\nAtentamente,\n{empresa}")


# Variables del módulo Odoo ({period}, {company}, {employee}) para quien copie su plantilla de allá.
ALIAS_VARIABLES = {"period": "periodo", "company": "empresa", "employee": "colaborador", "nombre": "colaborador"}


def plantilla(texto: str | None, defecto: str, **datos: str) -> str:
    """Rellena {periodo}, {empresa} y {colaborador} (y sus equivalentes de Odoo). Solo reemplaza nombres
    simples: una llave desconocida o cualquier otra cosa entre llaves se deja tal cual (nunca falla)."""
    import re

    def poner(m: re.Match[str]) -> str:
        k = m.group(1)
        k = ALIAS_VARIABLES.get(k, k)
        return str(datos[k]) if k in datos else m.group(0)
    return re.sub(r"\{(\w+)\}", poner, texto or defecto)


def una_linea(texto: str) -> str:
    """Asunto de correo: sin saltos de línea (evita cabeceras inyectadas) y de largo razonable."""
    return " ".join((texto or "").split())[:200]


def a_html(texto: str) -> str:
    """Texto escrito por la empresa → HTML seguro (escapa y respeta los saltos de línea)."""
    import html as _html
    return "<p>" + _html.escape(texto).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
