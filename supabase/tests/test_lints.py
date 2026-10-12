"""Revisiones tipo "Security Advisor" de Supabase, como pruebas automáticas."""
import json
import uuid

import psycopg


def q(dsn, sql, params=None):
    with psycopg.connect(dsn) as c:
        return c.execute(sql, params).fetchall()


def test_todas_las_tablas_tienen_rls(db_dsn):
    sin = q(db_dsn, """select n.nspname || '.' || c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
                       where c.relkind = 'r' and n.nspname in ('public','negocio','contabilidad') and not c.relrowsecurity""")
    assert sin == []


def test_funciones_security_definer_fijan_search_path(db_dsn):
    sin = q(db_dsn, """select n.nspname || '.' || p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                       where n.nspname in ('public','negocio','contabilidad') and p.prosecdef
                         and not exists (select 1 from unnest(coalesce(p.proconfig, '{}')) c where c like 'search_path=%')""")
    assert sin == []


def test_anon_no_tiene_privilegios(db_dsn):
    tablas = q(db_dsn, """select table_schema || '.' || table_name || ':' || privilege_type
                          from information_schema.role_table_grants
                          where grantee in ('anon','PUBLIC') and table_schema in ('public','negocio','contabilidad')""")
    assert tablas == []
    funciones = q(db_dsn, """select n.nspname || '.' || p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                             where n.nspname in ('public','negocio','contabilidad') and has_function_privilege('anon', p.oid, 'execute')""")
    assert funciones == []


def test_vistas_usan_security_invoker(db_dsn):
    malas = q(db_dsn, """select n.nspname || '.' || c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
                         where c.relkind = 'v' and n.nspname in ('public','negocio','contabilidad')
                           and not coalesce('security_invoker=true' = any(c.reloptions), false)""")
    assert malas == []


def test_auditoria_encadenada_detecta_alteraciones(db_dsn):
    u = uuid.uuid4()
    with psycopg.connect(db_dsn, autocommit=True) as c:
        c.execute("insert into auth.users (id, email) values (%s, %s)", (u, f"{u}@x.cr"))
        c.execute("insert into plataforma_admins values (%s)", (u,))
        e = c.execute("insert into empresas (nombre) values ('Audit SA') returning id").fetchone()[0]
        for i in range(3):
            c.execute("insert into usuarios_empresa (empresa_id, usuario_id, rol) values (%s,%s,'admin') "
                      "on conflict (empresa_id, usuario_id) do update set rol = 'lectura'", (e, u))
        ok = c.execute("select * from verificar_auditoria()").fetchone()
        assert ok[0] is None and ok[1] >= 3
        # Nadie puede editar la bitácora...
        try:
            c.execute("update auditoria set accion = 'X' where id = (select max(id) from auditoria)")
            raise AssertionError("debió fallar")
        except psycopg.errors.RaiseException:
            pass
        # ...y si alguien con acceso total la altera saltándose los triggers, se detecta.
        c.execute("alter table auditoria disable trigger auditoria_inmutable")
        c.execute("update auditoria set accion = 'DELETE' where id = (select min(id) from auditoria where hash is not null)")
        c.execute("alter table auditoria enable trigger auditoria_inmutable")
        roto = c.execute("select * from verificar_auditoria()").fetchone()
        assert roto[0] is not None


def test_mfa_exigida_para_aprobar(db_dsn):
    u = uuid.uuid4()
    with psycopg.connect(db_dsn, autocommit=True) as c:
        c.execute("insert into auth.users (id, email) values (%s, %s)", (u, f"{u}@x.cr"))
        e = c.execute("insert into empresas (nombre) values ('MFA SA') returning id").fetchone()[0]
        c.execute("insert into usuarios_empresa (empresa_id, usuario_id, rol) values (%s,%s,'admin')", (e, u))
        p = c.execute("insert into planillas (empresa_id, desde, hasta, estado) values (%s,'2026-01-01','2026-01-15','calculada') "
                      "returning id", (e,)).fetchone()[0]
    for aal, debe_pasar in (("aal1", False), ("aal2", True)):
        with psycopg.connect(db_dsn) as c:
            c.execute("set role authenticated")
            c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": str(u), "aal": aal}),))
            c.execute("select set_config('aurom.via_api', 'si', true)")
            c.execute("select set_config('aurom.mfa_requerida', 'si', true)")
            try:
                c.execute("update planillas set estado = 'aprobada' where id = %s", (p,))
                paso = True
            except psycopg.errors.RaiseException as exc:
                assert "dos pasos" in str(exc)
                paso = False
            c.rollback()
        assert paso is debe_pasar


def test_llaves_foraneas_con_indice(db_dsn):
    """Auditoría: toda llave foránea tiene índice (RLS filtra por empresa_id; borrados en cascada)."""
    sin = q(db_dsn, """select c.conrelid::regclass::text || ' ' || c.conname from pg_constraint c
                       where c.contype = 'f' and c.connamespace in ('public'::regnamespace, 'negocio'::regnamespace,
                                                                    'contabilidad'::regnamespace)
                         and not exists (select 1 from pg_index i where i.indrelid = c.conrelid
                                           and (i.indkey::int2[])[0:array_length(c.conkey, 1) - 1] @> c.conkey)""")
    assert sin == []
