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
        _pool = ConnectionPool(settings.database_url, min_size=1, max_size=10, open=True,
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
            # current_date, now()::date y los valores por defecto de fecha se calculan en hora de Costa Rica.
            conn.execute("select set_config('timezone', 'America/Costa_Rica', true)")
            conn.execute("set local role authenticated")
            conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
            conn.execute("select set_config('request.jwt.claim.sub', %s, true)", (claims.get("sub", ""),))
            # Marca de que la escritura viene de la API (ver migración 0010: PostgREST no puede fijarla).
            conn.execute("select set_config('aurom.via_api', 'si', true)")
            # MFA obligatoria en acciones críticas (la base la verifica con el claim aal, migración 0011).
            if settings.mfa_obligatoria:
                conn.execute("select set_config('aurom.mfa_requerida', 'si', true)")
            # Ninguna consulta de usuario puede bloquear la base: límites por transacción.
            conn.execute("select set_config('statement_timeout', %s, true)", (f"{settings.db_statement_timeout_ms}ms",))
            conn.execute("select set_config('lock_timeout', '5s', true)")
            conn.execute("select set_config('idle_in_transaction_session_timeout', '60s', true)")
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
