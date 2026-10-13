"""Límites de las tareas programadas (/cron/*), para que no generen uso ni gastos de más.

pg_cron (Supabase) llama a la API con el secreto CRON_SECRET. Aunque el horario esté bien puesto, una
tarea puede llegar repetida (un reintento, alguien que la llama a mano, un secreto filtrado). Por eso
cada tarea pasa por `ejecutar()`, que:

1. No la deja correr dos veces A LA VEZ (índice único en negocio.ejecuciones_cron). Lo más importante
   para los cobros con tarjeta: dos corridas simultáneas podrían cobrar dos veces.
2. No la repite antes de su INTERVALO MÍNIMO (por ejemplo, el cobro diario no corre dos veces en el día).
3. Pone un TOPE DIARIO de ejecuciones por tarea, aunque se pida un reproceso con fecha.
4. Si una corrida quedó colgada (el servidor se reinició), a la hora se marca «abandonada» y se libera.

Las tareas omitidas responden 200 con {"omitida": true, "motivo": ...}: pg_cron no reintenta.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import psycopg

from .. import db
from ..config import settings

log = logging.getLogger("aurom.tareas")


@dataclass(frozen=True)
class Limite:
    intervalo_min: int       # minutos mínimos entre dos corridas exitosas
    tope_diario: int         # corridas máximas en 24 h (incluye reprocesos con fecha)


LIMITES: dict[str, Limite] = {
    "salud": Limite(intervalo_min=30, tope_diario=48),            # programada cada hora = 24/día
    "diario": Limite(intervalo_min=20 * 60, tope_diario=6),       # 7:00 a. m.
    "cobros": Limite(intervalo_min=20 * 60, tope_diario=4),       # 8:30 a. m.
    "facturas": Limite(intervalo_min=50, tope_diario=30),         # 6:10 a. m. (reintento 6:15 solo si la primera no corrió)
    "ancla-auditoria": Limite(intervalo_min=20 * 60, tope_diario=4),
}
ABANDONO = "1 hour"


def ejecutar(tarea: str, fn: Callable[[], Any], *, reproceso: bool = False) -> Any:
    """Corre `fn` si los límites lo permiten. `reproceso` (se pidió una fecha a mano) salta el intervalo
    mínimo pero NO el candado ni el tope diario."""
    lim = LIMITES[tarea]
    controlar = settings.cron_limites
    with db.sistema() as conn:
        conn.execute("""update negocio.ejecuciones_cron set estado = 'abandonada', fin = now(),
                               detalle = 'No terminó (reinicio o caída del servidor)'
                         where tarea = %s and estado = 'en_curso' and inicio < now() - %s::interval""",
                     (tarea, ABANDONO))
        if controlar:
            n = conn.execute("""select count(*)::int n,
                                       max(inicio) filter (where estado = 'ok') as ultima_ok
                                  from negocio.ejecuciones_cron where tarea = %s and inicio > now() - interval '24 hours'""",
                             (tarea,)).fetchone()
            if n["n"] >= lim.tope_diario:
                return _omitida(tarea, f"Llegó al tope de {lim.tope_diario} ejecuciones en 24 horas")
            if not reproceso and n["ultima_ok"] is not None:
                espera = conn.execute("select %s::timestamptz + make_interval(mins => %s) > now() as pronto",
                                      (n["ultima_ok"], lim.intervalo_min)).fetchone()["pronto"]
                if espera:
                    return _omitida(tarea, f"Ya corrió hace menos de {lim.intervalo_min} minutos")
        try:
            eid = conn.execute("insert into negocio.ejecuciones_cron (tarea) values (%s) returning id",
                               (tarea,)).fetchone()["id"]
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return _omitida(tarea, "Ya hay una ejecución en curso")

    inicio = time.monotonic()
    estado, detalle = "ok", None
    try:
        return fn()
    except Exception as exc:
        estado, detalle = "error", str(getattr(exc, "detail", exc))[:500]
        raise
    finally:
        with db.sistema() as conn:
            conn.execute("""update negocio.ejecuciones_cron set estado = %s, fin = now(), duracion_ms = %s, detalle = %s
                             where id = %s""", (estado, int((time.monotonic() - inicio) * 1000), detalle, eid))


def _omitida(tarea: str, motivo: str) -> dict:
    log.info("Tarea %s omitida: %s", tarea, motivo)
    return {"omitida": True, "tarea": tarea, "motivo": motivo}
