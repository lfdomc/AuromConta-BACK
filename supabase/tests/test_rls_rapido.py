"""La RLS rápida (migración 0024) da exactamente el mismo acceso que las funciones originales.

Para cada usuario y cada empresa se compara puede_ver / puede_operar / empresa_escribible (lo que usaban
las políticas antes) con empresas_visibles / empresas_operables / empresas_escribibles (lo que usan ahora),
incluidos los casos de prioridad: un usuario de la empresa que además es contador o administrador de
la plataforma, contadores con permiso de lectura u operación, asignaciones revocadas, socios inactivos y
empresas suspendidas.
"""
import json
import uuid

import psycopg


def _mundo(c):
    u = {k: uuid.uuid4() for k in ["admin", "oper", "lect", "aprob", "conta_op", "conta_lec", "conta_rev", "conta_inact",
                                   "duena", "duena_lectura", "conta_y_lectura", "nadie", "inactivo"]}
    for k, v in u.items():
        c.execute("insert into auth.users (id, email) values (%s, %s)", (v, f"rr-{k}-{v.hex[:4]}@x.cr"))
    for k in ("duena", "duena_lectura"):
        c.execute("insert into plataforma_admins values (%s)", (u[k],))
    e = [c.execute("insert into empresas (nombre) values (%s) returning id", (f"RR {i}",)).fetchone()[0] for i in range(4)]
    c.execute("update empresas set estado_servicio = 'suspendido' where id = %s", (e[3],))
    for quien, emp, rol, activo in [("admin", e[0], "admin", True), ("oper", e[0], "operador", True),
                                    ("lect", e[1], "lectura", True), ("aprob", e[3], "aprobador", True),
                                    ("duena_lectura", e[0], "lectura", True), ("conta_y_lectura", e[1], "lectura", True),
                                    ("inactivo", e[0], "admin", False)]:
        c.execute("insert into usuarios_empresa (empresa_id, usuario_id, rol, activo) values (%s,%s,%s,%s)",
                  (emp, u[quien], rol, activo))
    socios = {}
    for k, estado in [("conta_op", "activo"), ("conta_lec", "activo"), ("conta_rev", "activo"),
                      ("conta_inact", "suspendido"), ("conta_y_lectura", "activo")]:
        socios[k] = c.execute("""insert into socios (usuario_id, tipo, nombre, correo, estado)
                                 values (%s,'contador',%s,%s,%s) returning id""",
                              (u[k], k, f"{k}-{uuid.uuid4().hex[:4]}@x.cr", estado)).fetchone()[0]
    for k, emp, permiso, revocado in [("conta_op", e[0], "operacion", False), ("conta_op", e[2], "lectura", False),
                                      ("conta_lec", e[1], "lectura", False), ("conta_rev", e[0], "operacion", True),
                                      ("conta_inact", e[0], "operacion", False),
                                      ("conta_y_lectura", e[1], "operacion", False),
                                      ("conta_op", e[3], "operacion", False)]:
        c.execute("""insert into asignaciones_contador (empresa_id, socio_id, permiso, revocado_en)
                     values (%s,%s,%s,%s)""", (emp, socios[k], permiso, "2026-01-01" if revocado else None))
    return u, e


def test_mismo_acceso_que_antes(db_dsn):
    with psycopg.connect(db_dsn, autocommit=True) as c:
        usuarios, empresas = _mundo(c)
    for nombre, uid in usuarios.items():
        with psycopg.connect(db_dsn) as c:
            c.execute("set role authenticated")
            c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": str(uid)}),))
            ver = {r[0] for r in c.execute("select public.empresas_visibles()")}
            operar = {r[0] for r in c.execute("select public.empresas_operables()")}
            escribir = {r[0] for r in c.execute("select public.empresas_escribibles()")}
            for emp in empresas:
                antes = c.execute("select public.puede_ver(%s), public.puede_operar(%s), public.empresa_escribible(%s)",
                                  (emp, emp, emp)).fetchone()
                # NULL en una política equivale a negar (igual que false).
                assert tuple(bool(x) for x in antes) == (emp in ver, emp in operar, emp in escribir), (nombre, emp, antes)
            c.rollback()


def test_politicas_sin_funciones_por_fila(db_dsn):
    with psycopg.connect(db_dsn) as c:
        quedan = c.execute("""select schemaname || '.' || tablename || '.' || policyname from pg_policies
                               where coalesce(qual, '') || coalesce(with_check, '')
                                     ~ '(puede_ver|puede_operar|empresa_escribible)\\(empresa_id\\)'""").fetchall()
    assert quedan == []
