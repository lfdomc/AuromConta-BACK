"""Fecha y hora de Costa Rica.

El servidor (Railway) y la base (Supabase) trabajan en UTC: entre las 6 p. m. y la medianoche
`date.today()` ya es "mañana" en Costa Rica. Toda fecha de negocio sale de aquí.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

ZONA = "America/Costa_Rica"
CR = timezone(timedelta(hours=-6))   # Costa Rica no usa horario de verano


def ahora_cr() -> datetime:
    return datetime.now(CR)


def hoy_cr() -> date:
    return ahora_cr().date()
