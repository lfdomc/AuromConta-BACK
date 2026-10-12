"""Envío de correos con Resend (https://resend.com/docs/api-reference/emails/send-email)."""
from __future__ import annotations

import base64

import httpx

from ..config import settings


class CorreoNoConfigurado(RuntimeError):
    pass


def enviar(para: str | list[str], asunto: str, html: str, adjuntos: list[tuple[str, bytes]] | None = None,
           remitente: str | None = None) -> str:
    """Devuelve el id del mensaje. Lanza excepción si falla."""
    if not settings.resend_api_key:
        raise CorreoNoConfigurado("RESEND_API_KEY no está configurada")
    cuerpo = {
        "from": remitente or settings.correo_remitente,
        "to": [para] if isinstance(para, str) else para,
        "subject": asunto,
        "html": html,
    }
    if adjuntos:
        cuerpo["attachments"] = [{"filename": n, "content": base64.b64encode(b).decode()} for n, b in adjuntos]
    r = httpx.post("https://api.resend.com/emails", json=cuerpo, timeout=30,
                   headers={"Authorization": f"Bearer {settings.resend_api_key}"})
    if r.status_code >= 300:
        raise RuntimeError(f"Resend {r.status_code}: {r.text[:300]}")
    return r.json().get("id", "")
