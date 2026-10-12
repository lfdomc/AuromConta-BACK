"""Las consultas armadas con psycopg.sql (filtros opcionales, columnas de una lista cerrada)
se ejecutan de verdad contra la base con todos sus filtros encendidos.

Complementa a test_sql_seguro.py: aquel revisa el código; este confirma que las
composiciones producen SQL válido. Corre como superusuario dentro de una
transacción que se deshace al final."""
import uuid
from datetime import date
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

from app import crud
from app.esquemas import AumentoMasivoIn, ConfiguracionIn
from app.routers import (anuales, autogestion, empleados, empresas, importaciones, planillas, reportes, saas,
                         socios)
from app.servicios import calculo

OTRO = uuid.uuid4()   # identificador que no existe: las consultas corren y devuelven vacío
DESDE, HASTA = date(2026, 1, 1), date(2026, 12, 31)


@pytest.fixture()
def conn(dsn, usuarios):
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        c.execute("select set_config('request.jwt.claim.sub', %s, true)", (str(usuarios["duena"]["id"]),))
        yield c
        c.rollback()


@pytest.fixture()
def ctx(conn):
    eid = conn.execute("insert into empresas (nombre) values ('Empresa SQL') returning id").fetchone()["id"]
    return SimpleNamespace(conn=conn, empresa_id=eid, rol="admin", puede_aprobar=True, puede_operar=True,
                           usuario=SimpleNamespace(id=OTRO))


def _endpoint(router, nombre):
    return next(r.endpoint for r in router.routes if r.name == nombre)


def test_crud_generico(ctx):
    assert crud.actualizar(ctx.conn, "empresas", ctx.empresa_id, {})["id"] == ctx.empresa_id

    class M(BaseModel):
        horas: float

    r = crud.crud_router("x", "horas_extra", M, campo_fecha="fecha", orden="fecha desc")
    assert _endpoint(r, "listar")(ctx=ctx, empleado_id=OTRO, estado="aprobada", desde=DESDE, hasta=HASTA,
                                  limite=10, desplazamiento=0) == []
    with pytest.raises(HTTPException) as e:
        _endpoint(r, "borrar")(id=OTRO, ctx=ctx)
    assert e.value.status_code == 404


def test_crud_rechaza_nombres_fuera_de_la_lista(ctx):
    class M(BaseModel):
        x: int

    with pytest.raises(ValueError):
        crud.crud_router("x", "horas_extra", M, orden="fecha desc; drop table empresas")
    with pytest.raises(ValueError):
        crud.crud_router("x", "pg_authid", M)
    with pytest.raises(ValueError):
        crud.crud_router("x", "horas_extra", M, campo_fecha="fecha; --")
    with pytest.raises(ValueError):
        crud.insertar(ctx.conn, "empresas", {"nombre) values ('x') --": 1})
    with pytest.raises(ValueError):
        crud.obtener(ctx.conn, "auth.users", OTRO)
    with pytest.raises(ValueError):
        importaciones._validar_orden("empleados", False)
    with pytest.raises(ValueError):
        reportes._cond_empleado(reportes.Filtros(DESDE, HASTA, empleado_id=OTRO), "y; --")


def test_articulos_alertas_normativa_y_tickets(conn, ctx):
    a = autogestion.crear_articulo(autogestion.ArticuloIn(titulo="Cómo pagar", contenido="Contenido de prueba",
                                                          categoria="pagos"), conn)
    assert autogestion.articulos(conn, categoria="pagos", buscar_texto="pagar")[0]["id"] == a["id"]
    editado = autogestion.editar_articulo(a["id"], autogestion.ArticuloUpd(titulo="Cómo cobrar"), conn)
    assert editado["titulo"] == "Cómo cobrar" and editado["version"] == a["version"] + 1
    assert autogestion.ver_articulo(a["id"], conn)["titulo"] == "Cómo cobrar"
    assert autogestion.listar_alertas(conn, atendida=True, tipo="x", limite=5) == []
    assert autogestion.normativa(conn, estado="x") == []
    assert autogestion.listar_tickets(ctx, estado="abiertos") == []
    assert autogestion.listar_tickets(ctx, estado="cerrado") == []
    assert autogestion.tickets_soporte(conn, estado="cerrado", nivel="plataforma") == []
    assert isinstance(autogestion.tickets_soporte(conn, estado="todos"), list)


def test_avisos_lunes_primero_de_mes(conn):
    assert isinstance(autogestion.avisos_empresas(conn, date(2026, 6, 1)), dict)   # lunes 1.º: todas las ramas


def test_empresas_empleados_planillas(ctx):
    assert empresas.editar_configuracion(ConfiguracionIn(nota_boleta="Gracias"), ctx)["nota_boleta"] == "Gracias"
    assert empresas.auditoria(ctx, tabla="empleados", limite=5) is not None
    assert empresas.feriados(ctx, anio=2026) is not None
    assert empresas.contadores(ctx, incluir_revocados=True) == []
    assert empresas.contadores(ctx, incluir_revocados=False) == []
    assert empleados.listar(ctx, estado="activo", buscar="ana", sucursal_id=OTRO) == []
    assert empleados.aumento_masivo(AumentoMasivoIn(empleado_ids=[OTRO], porcentaje=5, fecha_efectiva=DESDE),
                                    ctx) is not None
    assert planillas.listar(ctx, estado="calculada", tipo="ordinaria", anio=2026) == []
    pl = {"empresa_id": ctx.empresa_id, "desde": DESDE, "hasta": HASTA, "calendario_id": OTRO,
          "sucursal_id": OTRO, "frecuencia": "mensual"}
    assert calculo.empleados_de_planilla(ctx.conn, pl) == []
    assert calculo.empleados_de_planilla(ctx.conn, {**pl, "calendario_id": None, "sucursal_id": None}) == []
    assert anuales.salarios_por_mes(ctx.conn, OTRO, DESDE, HASTA) == {}


def test_reportes_con_todos_los_filtros(ctx):
    f = reportes.Filtros(DESDE, HASTA, sucursal_id=OTRO, empleado_id=OTRO)
    for rep in (reportes.rep_horas_extra, reportes.rep_prestamos, reportes.rep_embargos_pensiones,
                reportes.rep_historial_salarial, reportes.rep_incapacidades):
        assert rep(ctx, f)["filas"] == []
    assert reportes.rep_vacaciones(ctx, reportes.Filtros(DESDE, HASTA, sucursal_id=OTRO))["filas"] == []
    assert reportes.boletas_de(ctx, [{"id": OTRO}], f) == []
    for estado in ("vigente", "exportado", "pendiente"):
        assert reportes.listar_asientos(desde=DESDE, hasta=HASTA, estado=estado, ctx=ctx) == []
    assert reportes.dashboard(anio=2026, mes=6, ctx=ctx)["pendientes"]["horas_extra"] == 0


def test_plataforma_y_socios(conn, usuarios):
    assert saas.empresas(conn, estado="activo", buscar="zzz-ninguna") == []
    assert saas.socios(conn, tipo="contador", estado="activo") is not None
    assert saas.mis_boletas(SimpleNamespace(id=OTRO), conn, empleado_id=OTRO) == []
    u = SimpleNamespace(id=usuarios["contador"]["id"])
    assert socios.comisiones(u, conn, mes="2026-01", estado="pendiente") == []
    assert socios.prospectos(u, conn, estado="activo") == []
    assert socios.editar_yo(socios.PerfilUpd(telefono="8888-0000"), u, conn)["telefono"] == "8888-0000"
