"""Centro de alertas de la plataforma: registra en negocio.alertas y avisa por Telegram."""
from __future__ import annotations

import html
import logging

import psycopg

from ..db import Conexion
from . import telegram

log = logging.getLogger("aurom.alertas")

ICONO = {"info": "ℹ️", "aviso": "⚠️", "critica": "🚨"}


def alertar(conn: Conexion, tipo: str, mensaje: str, severidad: str = "info", *, telegram_ahora: bool = True) -> dict:
    """`conn` debe poder escribir en negocio.alertas (conexión de sistema o de la dueña).
    tipo: pago | factura | error | caida | normativa | ticket | socio | resumen."""
    fila = conn.execute("insert into negocio.alertas (tipo, severidad, mensaje) values (%s, %s, %s) returning *",
                        (tipo, severidad, mensaje)).fetchone()
    if telegram_ahora and telegram.notificar_duena(f"{ICONO.get(severidad, '')} <b>{tipo.upper()}</b>\n{html.escape(mensaje)}"):
        conn.execute("update negocio.alertas set enviada_en = now() where id = %s", (fila["id"],))
    return fila


def alertar_ticket(conn: Conexion, ticket_id, mensaje: str, severidad: str = "aviso") -> str | None:
    """Alerta a la dueña por un ticket escalado o urgente desde la conexión de cualquier usuario con
    acceso a la empresa (usa negocio.alerta_soporte, que valida el permiso). Nunca hace fallar la petición."""
    enviada = telegram.notificar_duena(f"{ICONO.get(severidad, '')} <b>TICKET</b>\n{html.escape(mensaje)}")
    try:
        with conn.transaction():
            fila = conn.execute("select negocio.alerta_soporte(%s, %s, %s, %s) as id",
                                (ticket_id, mensaje, severidad, enviada)).fetchone()
        return fila["id"]
    except psycopg.Error as exc:
        log.warning("No se registró la alerta del ticket %s: %s", ticket_id, exc)
        return None
