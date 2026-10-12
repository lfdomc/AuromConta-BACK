"""Reglas de la base para módulos y Contabilidad: aislamiento, partida doble, inmutabilidad y periodos."""
import json
import uuid
from contextlib import contextmanager

import psycopg
import pytest


@contextmanager
def como(dsn, usuario, via_api=True, aal="aal1", autocommit=False):
    with psycopg.connect(dsn, autocommit=autocommit) as c:
        c.execute("set role authenticated")
        c.execute("select set_config('request.jwt.claims', %s, false)",
                  (json.dumps({"sub": str(usuario), "role": "authenticated", "aal": aal}),))
        if via_api:
            c.execute("select set_config('aurom.via_api', 'si', false)")
        yield c
        if not autocommit:
            c.rollback()


@pytest.fixture(scope="module")
def conta(db_dsn):
    ids = {k: uuid.uuid4() for k in ["admin_a", "oper_a", "lect_a", "admin_b"]}
    with psycopg.connect(db_dsn, autocommit=True) as c:
        for k, v in ids.items():
            c.execute("insert into auth.users (id, email) values (%s, %s)", (v, f"{k}-c@x.cr"))
        ea = c.execute("insert into empresas (nombre) values ('Conta A') returning id").fetchone()[0]
        eb = c.execute("insert into empresas (nombre) values ('Conta B') returning id").fetchone()[0]
        for u, e, r in [("admin_a", ea, "admin"), ("oper_a", ea, "operador"), ("lect_a", ea, "lectura"), ("admin_b", eb, "admin")]:
            c.execute("insert into usuarios_empresa (empresa_id, usuario_id, rol) values (%s,%s,%s)", (e, ids[u], r))
    for u, e in (("admin_a", ea), ("admin_b", eb)):
        with como(db_dsn, ids[u], autocommit=True) as c:
            c.execute("select public.cambiar_modulo(%s, 'contabilidad', true)", (e,))
    return {"ids": ids, "ea": ea, "eb": eb}


def cuenta(c, empresa, codigo):
    return c.execute("select id from contabilidad.cuentas where empresa_id = %s and codigo = %s", (empresa, codigo)).fetchone()[0]


def nuevo_asiento(c, empresa, fecha="2026-03-10", lineas=(("1103", 1000, 0), ("4102", 0, 1000))):
    diario = c.execute("select id from contabilidad.diarios where empresa_id = %s and codigo = 'GEN'", (empresa,)).fetchone()[0]
    a = c.execute("""insert into contabilidad.asientos (empresa_id, diario_id, fecha, descripcion)
                     values (%s, %s, %s, 'Prueba') returning id""", (empresa, diario, fecha)).fetchone()[0]
    for cod, d, cr in lineas:
        c.execute("insert into contabilidad.lineas (empresa_id, asiento_id, cuenta_id, debito, credito) values (%s,%s,%s,%s,%s)",
                  (empresa, a, cuenta(c, empresa, cod), d, cr))
    return a


def test_modulos_por_defecto_y_catalogo_inicial(db_dsn, conta):
    with como(db_dsn, conta["ids"]["admin_a"]) as c:
        assert c.execute("select public.modulos_activos(%s)", (conta["ea"],)).fetchone()[0] == ["planilla", "contabilidad"]
        n, grupos = c.execute("select count(*), count(*) filter (where not acepta_movimientos) from contabilidad.cuentas "
                              "where empresa_id = %s", (conta["ea"],)).fetchone()
        assert n > 80 and grupos > 15
        # Jerarquía armada por prefijo
        padre = c.execute("""select p.codigo from contabilidad.cuentas c join contabilidad.cuentas p on p.id = c.padre_id
                              where c.empresa_id = %s and c.codigo = '5101'""", (conta["ea"],)).fetchone()[0]
        assert padre == "51"


def test_aislamiento_entre_empresas(db_dsn, conta):
    with como(db_dsn, conta["ids"]["admin_b"]) as c:
        assert c.execute("select count(*) from contabilidad.cuentas where empresa_id = %s", (conta["ea"],)).fetchone()[0] == 0
        # Usar una cuenta de A en un asiento de B: la llave compuesta lo impide aunque conozca el id.
        with psycopg.connect(db_dsn) as sup:
            ajena = sup.execute("select id from contabilidad.cuentas where empresa_id = %s and codigo = '1103'",
                                (conta["ea"],)).fetchone()[0]
        diario = c.execute("select id from contabilidad.diarios where empresa_id = %s and codigo = 'GEN'",
                           (conta["eb"],)).fetchone()[0]
        a = c.execute("insert into contabilidad.asientos (empresa_id, diario_id, fecha, descripcion) values (%s,%s,'2026-01-05','Ajena') "
                      "returning id", (conta["eb"], diario)).fetchone()[0]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("insert into contabilidad.lineas (empresa_id, asiento_id, cuenta_id, debito) values (%s,%s,%s,10)",
                      (conta["eb"], a, ajena))
    with psycopg.connect(db_dsn) as sup:
        diario_a = sup.execute("select id from contabilidad.diarios where empresa_id = %s and codigo = 'GEN'",
                               (conta["ea"],)).fetchone()[0]
    with como(db_dsn, conta["ids"]["admin_b"]) as c:
        # Ni siquiera ve el catálogo de A...
        assert c.execute("select count(*) from contabilidad.diarios where empresa_id = %s", (conta["ea"],)).fetchone()[0] == 0
        # ...y no puede escribir en A aunque conozca los ids.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into contabilidad.asientos (empresa_id, diario_id, fecha, descripcion) values (%s,%s,'2026-01-05','Intruso')",
                      (conta["ea"], diario_a))


def test_partida_doble_e_inmutabilidad(db_dsn, conta):
    with como(db_dsn, conta["ids"]["oper_a"]) as c:
        a = nuevo_asiento(c, conta["ea"], lineas=(("1103", 1000, 0), ("4102", 0, 900)))
        with pytest.raises(psycopg.errors.RaiseException, match="no cuadra"):
            c.execute("update contabilidad.asientos set estado = 'contabilizado' where id = %s", (a,))
    with como(db_dsn, conta["ids"]["oper_a"]) as c:
        a = nuevo_asiento(c, conta["ea"])
        c.execute("update contabilidad.asientos set estado = 'contabilizado' where id = %s", (a,))
        numero = c.execute("select numero from contabilidad.asientos where id = %s", (a,)).fetchone()[0]
        assert numero.startswith("GEN-2026-")
        for sentencia in ("update contabilidad.asientos set descripcion = 'cambio' where id = %s",
                          "update contabilidad.lineas set debito = 5 where asiento_id = %s",
                          "delete from contabilidad.lineas where asiento_id = %s",
                          "delete from contabilidad.asientos where id = %s"):
            c.execute("savepoint s")
            with pytest.raises(psycopg.errors.RaiseException):
                c.execute(sentencia, (a,))
            c.execute("rollback to savepoint s")


def test_cuenta_de_agrupacion_no_recibe_movimientos(db_dsn, conta):
    with como(db_dsn, conta["ids"]["oper_a"]) as c:
        a = nuevo_asiento(c, conta["ea"], lineas=(("11", 10, 0), ("4102", 0, 10)))
        with pytest.raises(psycopg.errors.RaiseException, match="no reciben movimientos"):
            c.execute("update contabilidad.asientos set estado = 'contabilizado' where id = %s", (a,))


def test_periodo_cerrado_y_mfa(db_dsn, conta):
    with psycopg.connect(db_dsn, autocommit=True) as sup:
        sup.execute("update ajustes_seguridad set valor = 'si' where clave = 'mfa_requerida'")
    try:
        with como(db_dsn, conta["ids"]["oper_a"]) as c:
            with pytest.raises(psycopg.errors.RaiseException, match="aprobador"):
                c.execute("insert into contabilidad.periodos (empresa_id, anio, mes, estado) values (%s, 2026, 4, 'cerrado')",
                          (conta["ea"],))
        with como(db_dsn, conta["ids"]["admin_a"]) as c:
            with pytest.raises(psycopg.errors.RaiseException, match="dos pasos"):
                c.execute("insert into contabilidad.periodos (empresa_id, anio, mes, estado) values (%s, 2026, 4, 'cerrado')",
                          (conta["ea"],))
        with como(db_dsn, conta["ids"]["admin_a"], aal="aal2") as c:
            c.execute("insert into contabilidad.periodos (empresa_id, anio, mes, estado) values (%s, 2026, 4, 'cerrado')",
                      (conta["ea"],))
            a = nuevo_asiento(c, conta["ea"], fecha="2026-04-15")
            with pytest.raises(psycopg.errors.RaiseException, match="cerrado"):
                c.execute("update contabilidad.asientos set estado = 'contabilizado' where id = %s", (a,))
    finally:
        with psycopg.connect(db_dsn, autocommit=True) as sup:
            sup.execute("update ajustes_seguridad set valor = 'no' where clave = 'mfa_requerida'")


def test_lectura_y_modulo_apagado_no_escriben(db_dsn, conta):
    with como(db_dsn, conta["ids"]["lect_a"]) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            nuevo_asiento(c, conta["ea"])
    with como(db_dsn, conta["ids"]["admin_b"]) as c:
        c.execute("select public.cambiar_modulo(%s, 'contabilidad', false)", (conta["eb"],))
        c.execute("savepoint s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            nuevo_asiento(c, conta["eb"])
        c.execute("rollback to savepoint s")
        # Lo que había se sigue viendo
        assert c.execute("select count(*) from contabilidad.cuentas where empresa_id = %s", (conta["eb"],)).fetchone()[0] > 0


def test_siempre_queda_un_modulo_y_solo_admin_los_cambia(db_dsn, conta):
    with como(db_dsn, conta["ids"]["oper_a"]) as c:
        with pytest.raises(psycopg.errors.RaiseException, match="administrador"):
            c.execute("select public.cambiar_modulo(%s, 'planilla', false)", (conta["ea"],))
    with como(db_dsn, conta["ids"]["admin_a"]) as c:
        c.execute("select public.cambiar_modulo(%s, 'contabilidad', false)", (conta["ea"],))
        with pytest.raises(psycopg.errors.RaiseException, match="al menos un módulo"):
            c.execute("select public.cambiar_modulo(%s, 'planilla', false)", (conta["ea"],))


def test_postgrest_no_escribe_en_contabilidad(db_dsn, conta):
    with psycopg.connect(db_dsn) as c:
        c.execute("set session authorization authenticator")
        c.execute("set role authenticated")
        c.execute("select set_config('request.jwt.claims', %s, true)",
                  (json.dumps({"sub": str(conta["ids"]["admin_a"]), "role": "authenticated"}),))
        with pytest.raises(psycopg.errors.RaiseException, match="aplicación"):
            nuevo_asiento(c, conta["ea"])
        c.rollback()


def test_planilla_apagada_no_crea_planillas(db_dsn, conta):
    with como(db_dsn, conta["ids"]["admin_a"]) as c:
        c.execute("select public.cambiar_modulo(%s, 'contabilidad', true)", (conta["ea"],))
        c.execute("select public.cambiar_modulo(%s, 'planilla', false)", (conta["ea"],))
        with pytest.raises(psycopg.errors.RaiseException, match="Planilla no está activo"):
            c.execute("insert into planillas (empresa_id, desde, hasta) values (%s, '2026-05-01', '2026-05-15')", (conta["ea"],))


def test_auditoria_sigue_integra_tras_transaccion_deshecha(db_dsn, conta):
    with como(db_dsn, conta["ids"]["admin_a"]) as c:
        c.execute("select public.cambiar_modulo(%s, 'planilla', true)", (conta["ea"],))
        c.rollback()   # consume un número de la bitácora y lo descarta
    with como(db_dsn, conta["ids"]["admin_a"], autocommit=True) as c:
        c.execute("select public.cambiar_modulo(%s, 'planilla', true)", (conta["ea"],))
    with psycopg.connect(db_dsn) as sup:
        assert sup.execute("select id_alterado from verificar_auditoria()").fetchone()[0] is None


def test_borrar_empresa_borra_su_contabilidad(db_dsn):
    u = uuid.uuid4()
    with psycopg.connect(db_dsn, autocommit=True) as sup:
        sup.execute("insert into auth.users (id, email) values (%s, %s)", (u, f"{u}@x.cr"))
        e = sup.execute("insert into empresas (nombre) values ('Se va SA') returning id").fetchone()[0]
        sup.execute("insert into usuarios_empresa (empresa_id, usuario_id, rol) values (%s,%s,'admin')", (e, u))
    with como(db_dsn, u, autocommit=True) as c:
        c.execute("select public.cambiar_modulo(%s, 'contabilidad', true)", (e,))
        a = nuevo_asiento(c, e)
        c.execute("update contabilidad.asientos set estado = 'contabilizado' where id = %s", (a,))
    with psycopg.connect(db_dsn, autocommit=True) as sup:
        sup.execute("delete from empresas where id = %s", (e,))
        assert sup.execute("select count(*) from contabilidad.asientos where empresa_id = %s", (e,)).fetchone()[0] == 0
