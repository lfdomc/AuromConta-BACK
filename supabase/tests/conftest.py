"""Crea una base limpia en un Postgres local con las migraciones aplicadas.

Variable PG_ADMIN_DSN (por defecto el Postgres de pruebas en el puerto 54322).
"""
import os
import pathlib
import uuid

import psycopg
import pytest

ADMIN_DSN = os.environ.get("PG_ADMIN_DSN", "host=/tmp port=54322 user=postgres dbname=postgres")
RAIZ = pathlib.Path(__file__).resolve().parents[1]


def aplicar_migraciones(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute((RAIZ / "tests" / "supabase_stub.sql").read_text())
        for f in sorted((RAIZ / "migrations").glob("*.sql")):
            c.execute(f.read_text())


@pytest.fixture(scope="session")
def db_dsn():
    nombre = f"t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"create database {nombre}")
    dsn = ADMIN_DSN.replace("dbname=postgres", f"dbname={nombre}")
    aplicar_migraciones(dsn)
    yield dsn
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"drop database {nombre} with (force)")
