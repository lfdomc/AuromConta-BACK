"""Pruebas de la API contra un Postgres local con las migraciones de Supabase."""
import os
import pathlib
import time
import uuid

os.environ.setdefault("SUPABASE_JWT_SECRET", "secreto-de-pruebas-con-32-caracteres!!")
os.environ.setdefault("ENTORNO", "pruebas")
os.environ.setdefault("CLAVE_CIFRADO", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("LIMITE_POR_MINUTO", "100000")
os.environ.setdefault("LIMITE_SENSIBLE_POR_MINUTO", "100000")

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

ADMIN_DSN = os.environ.get("PG_ADMIN_DSN", "host=/tmp port=54322 user=postgres dbname=postgres")
SUPA = pathlib.Path(__file__).resolve().parents[2] / "supabase"


@pytest.fixture(scope="session")
def dsn():
    nombre = f"api_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"create database {nombre}")
    d = ADMIN_DSN.replace("dbname=postgres", f"dbname={nombre}")
    with psycopg.connect(d, autocommit=True) as c:
        c.execute((SUPA / "tests" / "supabase_stub.sql").read_text())
        for f in sorted((SUPA / "migrations").glob("*.sql")):
            c.execute(f.read_text())
    yield d
    from app import db
    if db._pool is not None:
        db._pool.close()
        db._pool = None
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"drop database {nombre} with (force)")


@pytest.fixture(scope="session")
def client(dsn):
    from app import db
    from app.main import app
    db.configurar_pool(dsn)
    return TestClient(app)


def token(usuario_id, email="x@x.cr") -> dict:
    t = jwt.encode({"sub": str(usuario_id), "email": email, "aud": "authenticated", "role": "authenticated", "iss": "https://proyecto.supabase.co/auth/v1",
                    "exp": int(time.time()) + 3600}, os.environ["SUPABASE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture(scope="session")
def usuarios(dsn):
    ids = {k: uuid.uuid4() for k in ["admin", "operador", "aprobador", "otro", "contador", "duena", "empleado"]}
    with psycopg.connect(dsn, autocommit=True) as c:
        for k, v in ids.items():
            c.execute("insert into auth.users (id, email) values (%s, %s)", (v, f"{k}@prueba.cr"))
        c.execute("insert into plataforma_admins values (%s)", (ids["duena"],))
        c.execute("insert into socios (usuario_id, tipo, nombre, correo, codigo_referido) "
                  "values (%s,'contador','Conta Uno','conta@prueba.cr','CONTA001')", (ids["contador"],))
    return {k: {"id": v, "h": token(v, f"{k}@prueba.cr")} for k, v in ids.items()}
