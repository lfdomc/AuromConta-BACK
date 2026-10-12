"""Cliente mínimo de la Bot API de Telegram (https://core.telegram.org/bots/api)."""
from __future__ import annotations

import html as _html
import logging
import time

import httpx

from ..config import settings

log = logging.getLogger("aurom.telegram")

API = "https://api.telegram.org"


def enviar_mensaje(chat_id: str | int, texto: str, *, html: bool = True, botones: list[list[dict]] | None = None) -> bool:
    """Envía un mensaje. Devuelve False (sin lanzar) si el bot no está configurado o falla."""
    if not settings.telegram_bot_token or not chat_id:
        return False
    cuerpo: dict = {"chat_id": chat_id, "text": texto[:4096], "disable_web_page_preview": True}
    if html:
        cuerpo["parse_mode"] = "HTML"
    if botones:
        cuerpo["reply_markup"] = {"inline_keyboard": botones}
    try:
        r = httpx.post(f"{API}/bot{settings.telegram_bot_token}/sendMessage", json=cuerpo, timeout=10)
        return r.status_code == 200 and r.json().get("ok", False)
    except httpx.HTTPError as exc:
        log.warning("Telegram no disponible: %s", exc)
        return False


def notificar_duena(texto: str) -> bool:
    return enviar_mensaje(settings.telegram_chat_duena, texto)


# ------------------------------------------------------------- utilidades ---
def esc(texto) -> str:
    """Escapa texto para parse_mode HTML."""
    return _html.escape(str(texto if texto is not None else ""), quote=False)


def enlace_vinculacion(codigo: str) -> str:
    return f"https://t.me/{settings.telegram_bot_username.lstrip('@')}?start={codigo}"


def get_me() -> tuple[bool, int | None, str]:
    """Chequeo de salud del bot: (ok, latencia_ms, detalle)."""
    if not settings.telegram_bot_token:
        return False, None, "TELEGRAM_BOT_TOKEN no configurado"
    t0 = time.monotonic()
    try:
        r = httpx.get(f"{API}/bot{settings.telegram_bot_token}/getMe", timeout=10)
        ms = int((time.monotonic() - t0) * 1000)
        ok = r.status_code == 200 and r.json().get("ok", False)
        return ok, ms, "" if ok else f"HTTP {r.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        return False, None, str(exc)[:200]


def notificar_socio(conn, socio_id, texto: str) -> bool:
    """Avisa a un contador/vendedor por Telegram si lo tiene vinculado.
    `conn` puede ser la del usuario: el socio asignado es visible por RLS."""
    if not socio_id:
        return False
    fila = conn.execute("select telegram_chat_id from public.socios where id = %s", (socio_id,)).fetchone()
    return bool(fila and fila["telegram_chat_id"]) and enviar_mensaje(fila["telegram_chat_id"], texto)


def notificar_usuario_empresa(conn, empresa_id, usuario_id, texto: str) -> bool:
    """Avisa a un usuario de la empresa (p. ej. quien abrió un ticket) si vinculó Telegram."""
    if not usuario_id:
        return False
    fila = conn.execute("select negocio.chat_usuario_empresa(%s, %s) as chat", (empresa_id, usuario_id)).fetchone()
    return bool(fila and fila["chat"]) and enviar_mensaje(fila["chat"], texto)


def chats_admins_empresa(conn_sistema, empresa_id, aviso: str = "recordatorios") -> list[str]:
    """Chats de Telegram de los administradores de la empresa que aceptan ese tipo de aviso.
    Solo para tareas de sistema (cron): lee vínculos de otros usuarios."""
    filas = conn_sistema.execute("""
        select distinct v.chat_id from public.telegram_vinculos v
          join public.usuarios_empresa ue on ue.usuario_id = v.usuario_id and ue.activo and ue.rol = 'admin'
         where ue.empresa_id = %s and v.activo and v.chat_id is not null
           and coalesce((v.avisos ->> %s)::boolean, true)""", (empresa_id, aviso)).fetchall()
    return [f["chat_id"] for f in filas]
