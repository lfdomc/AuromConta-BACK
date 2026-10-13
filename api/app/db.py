"""Acceso a Postgres (Supabase) respetando RLS.

Cada petición abre una transacción y se presenta ante la base como el usuario
que hizo la llamada (rol `authenticated` + claims del JWT). Así las políticas
RLS se aplican igual que si el usuario consultara Supabase directamente: la API
nunca usa la llave service_role para datos de clientes.

`sistema()` es solo para tareas sin usuario (webhooks, cron) y queda auditado
en el código que la usa.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, LiteralString, TypeAlias, cast

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from .config import settings

# Todas las conexiones devuelven filas como diccionario (row_factory=dict_row).
Conexion: TypeAlias = psycopg.Connection[DictRow]

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(settings.database_url, min_size=settings.db_pool_min, max_size=settings.db_pool_max,
                               max_idle=300, timeout=15, open=True,
                               kwargs={"row_factory": dict_row, "autocommit": False,
                                       "prepare_threshold": None})  # compatible con el pooler de Supabase
    return _pool


def configurar_pool(dsn: str) -> None:
    """Para pruebas: reemplaza el pool por uno hacia otra base."""
    global _pool
    if _pool is not None:
        _pool.close()
    _pool = ConnectionPool(dsn, min_size=1, max_size=5, open=True,
                           kwargs={"row_factory": dict_row, "autocommit": False, "prepare_threshold": None})


@contextmanager
def como_usuario(claims: dict[str, Any]) -> Iterator[Conexion]:
    with pool().connection() as c:
        conn = cast(Conexion, c)
        try:
            # Todo en una sola ida y vuelta a la base (antes eran 8 comandos por petición):
            # - zona horaria de Costa Rica para current_date y fechas por defecto;
            # - rol authenticated + claims del JWT, para que RLS se aplique como al usuario;
            # - marca de que la escritura viene de la API (migración 0010) y MFA obligatoria (0011);
            # - límites por transacción: ninguna consulta de usuario puede bloquear la base.
            conn.execute("""select set_config('timezone', 'America/Costa_Rica', true),
                                   set_config('role', 'authenticated', true),
                                   set_config('request.jwt.claims', %s, true),
                                   set_config('request.jwt.claim.sub', %s, true),
                                   set_config('aurom.via_api', 'si', true),
                                   set_config('aurom.mfa_requerida', %s, true),
                                   set_config('statement_timeout', %s, true),
                                   set_config('lock_timeout', '5s', true),
                                   set_config('idle_in_transaction_session_timeout', '60s', true)""",
                         (json.dumps(claims), claims.get("sub", ""), "si" if settings.mfa_obligatoria else "",
                          f"{settings.db_statement_timeout_ms}ms"))
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@contextmanager
def sistema() -> Iterator[Conexion]:
    with pool().connection() as c:
        conn = cast(Conexion, c)
        try:
            conn.execute("select set_config('timezone', 'America/Costa_Rica', true)")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def uno(conn: Conexion, consulta: LiteralString | sql.SQL | sql.Composed, params=None) -> dict | None:
    return conn.execute(consulta, params).fetchone()


def todos(conn: Conexion, consulta: LiteralString | sql.SQL | sql.Composed, params=None) -> list[dict]:
    return conn.execute(consulta, params).fetchall()
