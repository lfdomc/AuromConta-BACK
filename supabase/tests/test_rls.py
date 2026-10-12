"""Pruebas de seguridad multiempresa (RLS) y reglas de la base."""
import json
import uuid
from contextlib import contextmanager

import psycopg
import pytest


@pytest.fixture(scope="module")
def mundo(db_dsn):
    """Dos empresas, sus usuarios, un contador asignado a A, la dueña y un empleado con portal."""
    ids = {k: uuid.uuid4() for k in ["admin_a", "oper_a", "lect_a", "admin_b", "contador", "vendedor", "duena", "empleado"]}
    with psycopg.connect(db_dsn, autocommit=True) as c:
        for k, v in ids.items():
            c.execute("insert into auth.users (id, email) values (%s, %s)", (v, f"{k}@x.cr"))
        c.execute("insert into plataforma_admins values (%s)", (ids["duena"],))
        ea = c.execute("insert into empresas (nombre) values ('Empresa A') returning id").fetchone()[0]
        eb = c.execute("insert into empresas (nombre) values ('Empresa B') returning id").fetchone()[0]
        for u, e, r in [("admin_a", ea, "admin"), ("oper_a", ea, "operador"), ("lect_a", ea, "lectura"), ("admin_b", eb, "admin")]:
            c.execute("insert into usuarios_empresa (empresa_id, usuario_id, rol) values (%s,%s,%s)", (e, ids[u], r))
        sc = c.execute("insert into socios (usuario_id, tipo, nombre, correo) values (%s,'contador','Conta','c@x.cr') returning id",
                       (ids["contador"],)).fetchone()[0]
        sv = c.execute("insert into socios (usuario_id, tipo, nombre, correo) values (%s,'vendedor','Vende','v@x.cr') returning id",
                       (ids["vendedor"],)).fetchone()[0]
        c.execute("insert into asignaciones_contador (empresa_id, socio_id, permiso) values (%s,%s,'operacion')", (ea, sc))
        for e, n in [(ea, "Ana"), (eb, "Beto")]:
            c.execute("""insert into empleados (empresa_id, identificacion, nombre, fecha_ingreso, salario_mensual)
                         values (%s, %s, %s, '2024-01-01', 600000)""", (e, n, n))
        c.execute("update empleados set usuario_id=%s where nombre='Ana'", (ids["empleado"],))
    return {"ids": ids, "ea": ea, "eb": eb, "socio_contador": sc, "socio_vendedor": sv}


@contextmanager
def como(dsn, usuario, via_api=True):
    with psycopg.connect(dsn) as c:
        c.execute("set role authenticated")
        c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": str(usuario), "role": "authenticated"}),))
        if via_api:
            c.execute("select set_config('aurom.via_api', 'si', true)")
        yield c
        c.rollback()


def empresas_visibles(c):
    return {r[0] for r in c.execute("select nombre from empresas")}


def test_cada_usuario_ve_solo_su_empresa(db_dsn, mundo):
    i = mundo["ids"]
    with como(db_dsn, i["admin_a"]) as c:
        assert empresas_visibles(c) == {"Empresa A"}
        assert {r[0] for r in c.execute("select nombre from empleados")} == {"Ana"}
    with como(db_dsn, i["admin_b"]) as c:
        assert empresas_visibles(c) == {"Empresa B"}
        assert {r[0] for r in c.execute("select nombre from empleados")} == {"Beto"}


def test_no_puede_insertar_en_otra_empresa(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["admin_a"]) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("insert into empleados (empresa_id, identificacion, nombre, fecha_ingreso) values (%s,'X','X','2026-01-01')",
                      (mundo["eb"],))


def test_contador_asignado_ve_su_cliente_y_nada_mas(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["contador"]) as c:
        assert empresas_visibles(c) == {"Empresa A"}
        assert c.execute("select rol_en_empresa(%s)", (mundo["ea"],)).fetchone()[0] == "contador_operacion"
        c.execute("update empleados set puesto='Bodega' where nombre='Ana'")
        assert c.execute("select puesto from empleados where nombre='Ana'").fetchone()[0] == "Bodega"


def test_contador_revocado_pierde_acceso(db_dsn, mundo):
    with psycopg.connect(db_dsn) as c:
        c.execute("update asignaciones_contador set revocado_en=now() where socio_id=%s", (mundo["socio_contador"],))
        c.execute("set role authenticated")
        c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": str(mundo["ids"]["contador"])}),))
        assert empresas_visibles(c) == set()
        c.rollback()


def test_vendedor_sin_asignacion_no_ve_nada(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["vendedor"]) as c:
        assert empresas_visibles(c) == set()


def test_lectura_no_puede_escribir(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["lect_a"]) as c:
        assert empresas_visibles(c) == {"Empresa A"}
        assert c.execute("update empleados set puesto='X' returning id").fetchall() == []


def test_duena_ve_todo(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["duena"]) as c:
        assert empresas_visibles(c) >= {"Empresa A", "Empresa B"}
        assert c.execute("select count(*) from negocio.categorias_gasto").fetchone()[0] > 0


def test_clientes_no_ven_negocio(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["admin_a"]) as c:
        assert c.execute("select count(*) from negocio.categorias_gasto").fetchone()[0] == 0
        assert c.execute("select count(*) from negocio.configuracion").fetchone()[0] == 0


def test_empleado_portal_ve_solo_su_ficha(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["empleado"]) as c:
        assert empresas_visibles(c) == set()
        assert {r[0] for r in c.execute("select nombre from empleados")} == {"Ana"}


def test_flujo_estados_y_aprobacion(db_dsn, mundo):
    ea = mundo["ea"]
    with como(db_dsn, mundo["ids"]["oper_a"]) as c:
        pid = c.execute("insert into planillas (empresa_id, desde, hasta) values (%s,'2026-03-01','2026-03-15') returning id",
                        (ea,)).fetchone()[0]
        c.execute("update planillas set estado='calculada' where id=%s", (pid,))
        with pytest.raises(psycopg.errors.RaiseException, match="Solo un aprobador"):
            c.execute("update planillas set estado='aprobada' where id=%s", (pid,))


def test_planilla_cerrada_inmutable_y_reapertura(db_dsn, mundo):
    ea = mundo["ea"]
    with como(db_dsn, mundo["ids"]["admin_a"]) as c:
        emp = c.execute("select id from empleados where nombre='Ana'").fetchone()[0]
        pid = c.execute("insert into planillas (empresa_id, desde, hasta) values (%s,'2026-04-01','2026-04-15') returning id",
                        (ea,)).fetchone()[0]
        c.execute("insert into boletas (empresa_id, planilla_id, empleado_id, neto) values (%s,%s,%s,100)", (ea, pid, emp))
        for e in ("calculada", "aprobada", "cerrada"):
            c.execute("update planillas set estado=%s where id=%s", (e, pid))
        with pytest.raises(psycopg.errors.RaiseException, match="no se pueden modificar"):
            c.execute("update boletas set neto=1 where planilla_id=%s", (pid,))
        c.execute("rollback")


def test_reabrir_exige_motivo(db_dsn, mundo):
    ea = mundo["ea"]
    with como(db_dsn, mundo["ids"]["admin_a"]) as c:
        pid = c.execute("insert into planillas (empresa_id, desde, hasta) values (%s,'2026-05-01','2026-05-15') returning id",
                        (ea,)).fetchone()[0]
        for e in ("calculada", "aprobada", "cerrada"):
            c.execute("update planillas set estado=%s where id=%s", (e, pid))
        c.execute("savepoint s")
        with pytest.raises(psycopg.errors.RaiseException, match="motivo"):
            c.execute("update planillas set estado='calculada' where id=%s", (pid,))
        c.execute("rollback to savepoint s")
        c.execute("insert into reaperturas (empresa_id, planilla_id, motivo) values (%s,%s,'Error en horas extra')", (ea, pid))
        c.execute("update planillas set estado='calculada' where id=%s", (pid,))


def test_crear_empresa_con_referido_asigna_contador(db_dsn, mundo):
    nuevo = uuid.uuid4()
    with psycopg.connect(db_dsn, autocommit=True) as c:
        c.execute("insert into auth.users (id, email) values (%s,'nuevo@x.cr')", (nuevo,))
        c.execute("update asignaciones_contador set revocado_en=null where socio_id=%s", (mundo["socio_contador"],))
        codigo = c.execute("select codigo_referido from socios where id=%s", (mundo["socio_contador"],)).fetchone()[0]
    with como(db_dsn, nuevo) as c:
        eid = c.execute("select crear_empresa('Nueva SA', '3-101-000000', %s)", (codigo.lower(),)).fetchone()[0]
        assert c.execute("select rol_en_empresa(%s)", (eid,)).fetchone()[0] == "admin"
        assert c.execute("select count(*) from cuentas_contables where empresa_id=%s", (eid,)).fetchone()[0] == 18
        tid = c.execute("insert into tickets (empresa_id, asunto) values (%s,'No me sale la boleta') returning asignado_socio_id",
                        (eid,)).fetchone()[0]
        assert tid == mundo["socio_contador"]
        c.connection.commit()
    with como(db_dsn, mundo["ids"]["contador"]) as c:
        assert "Nueva SA" in empresas_visibles(c)
        assert c.execute("select count(*) from tickets").fetchone()[0] == 1


def test_embargo_porcentaje_maximo(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["admin_a"]) as c:
        emp = c.execute("select id from empleados where nombre='Ana'").fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("""insert into embargos (empresa_id, empleado_id, expediente, tipo, valor, desde)
                         values (%s,%s,'E-1','porcentaje',30,'2026-01-01')""", (mundo["ea"], emp))


def test_auditoria_registra_cambios(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["admin_a"]) as c:
        c.execute("update empleados set salario_mensual=650000 where nombre='Ana'")
        r = c.execute("select accion, usuario_id from auditoria where tabla='empleados' order by id desc limit 1").fetchone()
        assert r[0] == "UPDATE" and r[1] == mundo["ids"]["admin_a"]


def test_data_api_no_puede_escribir(db_dsn, mundo):
    """Desde PostgREST (sin la marca de la API) un admin no escribe nada."""
    with como(db_dsn, mundo["ids"]["admin_a"], via_api=False) as c:
        assert empresas_visibles(c) == {"Empresa A"}
        with pytest.raises(psycopg.errors.RaiseException, match="desde la aplicación"):
            c.execute("update empleados set puesto='X' where nombre='Ana'")


def test_admin_no_reactiva_empresa_suspendida(db_dsn, mundo):
    with psycopg.connect(db_dsn, autocommit=True) as c:
        c.execute("update empresas set estado_servicio='suspendido' where id=%s", (mundo["ea"],))
    try:
        with como(db_dsn, mundo["ids"]["admin_a"]) as c:
            with pytest.raises(psycopg.errors.RaiseException, match="Solo la plataforma"):
                c.execute("update empresas set estado_servicio='activo' where id=%s", (mundo["ea"],))
    finally:
        with psycopg.connect(db_dsn, autocommit=True) as c:
            c.execute("update empresas set estado_servicio='activo' where id=%s", (mundo["ea"],))


def test_boleta_no_se_mueve_de_empresa(db_dsn, mundo):
    with como(db_dsn, mundo["ids"]["admin_a"]) as c:
        beto = None
        with psycopg.connect(db_dsn) as s:
            beto = s.execute("select id from empleados where nombre='Beto'").fetchone()[0]
        with pytest.raises(psycopg.errors.Error):
            c.execute("insert into bonos (empresa_id, empleado_id, nombre, monto, desde) values (%s,%s,'x',1,'2026-01-01')",
                      (mundo["ea"], beto))


def _postgrest(db_dsn, usuario, aal="aal1"):
    """Sesión igual a la de PostgREST: login 'authenticator' + set role authenticated."""
    c = psycopg.connect(db_dsn.replace("user=postgres", "user=authenticator"))
    c.execute("set role authenticated")
    c.execute("select set_config('request.jwt.claims', %s, true)",
              (json.dumps({"sub": str(usuario), "role": "authenticated", "aal": aal}),))
    return c


def test_postgrest_no_escribe_ni_por_funciones(db_dsn, mundo):
    c = _postgrest(db_dsn, mundo["ids"]["admin_a"])
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("select agregar_usuario_empresa(%s, 'oper_a@x.cr', 'admin')", (mundo["ea"],))
        c.rollback()
        c.execute("set role authenticated")
        c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": str(mundo["ids"]["admin_a"])}),))
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("select crear_empresa('Por la puerta de atrás')")
        c.rollback()
        c.execute("set role authenticated")
        c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": str(mundo["ids"]["admin_a"])}),))
        with pytest.raises(psycopg.errors.RaiseException, match="Solo la plataforma"):
            c.execute("select * from verificar_auditoria()")
    finally:
        c.close()


def test_mfa_global_y_alta_de_historial_autorizado(db_dsn, mundo):
    with psycopg.connect(db_dsn, autocommit=True) as s:
        s.execute("update ajustes_seguridad set valor = 'si' where clave = 'mfa_requerida'")
        emp = s.execute("select id from empleados where nombre = 'Ana'").fetchone()[0]
    try:
        for aal, pasa in (("aal1", False), ("aal2", True)):
            with psycopg.connect(db_dsn) as c:
                c.execute("set role authenticated")
                c.execute("select set_config('request.jwt.claims', %s, true)",
                          (json.dumps({"sub": str(mundo["ids"]["admin_a"]), "aal": aal}),))
                c.execute("select set_config('aurom.via_api', 'si', true)")
                try:
                    c.execute("""insert into historial_salarial (empresa_id, empleado_id, fecha_efectiva, salario_anterior,
                                 salario_nuevo, estado) values (%s,%s,'2026-01-01',600000,700000,'autorizado')""",
                              (mundo["ea"], emp))
                    ok = True
                except psycopg.errors.RaiseException:
                    ok = False
                c.rollback()
            assert ok is pasa
    finally:
        with psycopg.connect(db_dsn, autocommit=True) as s:
            s.execute("update ajustes_seguridad set valor = 'no' where clave = 'mfa_requerida'")
