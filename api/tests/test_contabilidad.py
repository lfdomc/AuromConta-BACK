"""Módulos por empresa, cobro por módulo y módulo de Contabilidad (C1-C3) a través de la API."""
import uuid
from datetime import date
from decimal import Decimal as D

import psycopg
import pytest

from conftest import token

HOY = date.today()


@pytest.fixture(scope="module")
def gente(dsn):
    ids = {k: uuid.uuid4() for k in ["admin", "oper", "lect", "otro"]}
    with psycopg.connect(dsn, autocommit=True) as c:
        for k, v in ids.items():
            c.execute("insert into auth.users (id, email) values (%s, %s)", (v, f"conta-{k}@prueba.cr"))
    return {k: {"id": v, "h": token(v, f"conta-{k}@prueba.cr")} for k, v in ids.items()}


@pytest.fixture(scope="module")
def emp(client, gente):
    a = gente["admin"]["h"]
    r = client.post("/empresas", json={"nombre": "Contable Prueba SA", "modulos": ["planilla", "contabilidad"]}, headers=a)
    assert r.status_code == 201, r.text
    assert r.json()["modulos"] == ["planilla", "contabilidad"]
    eid = r.json()["id"]
    for u, rol in (("oper", "operador"), ("lect", "lectura")):
        r = client.post(f"/empresas/{eid}/usuarios", json={"correo": f"conta-{u}@prueba.cr", "rol": rol}, headers=a)
        assert r.status_code == 201, r.text
    return eid


def base(eid):
    return f"/empresas/{eid}/contabilidad"


def cuentas(client, h, eid):
    return {c["codigo"]: c for c in client.get(f"{base(eid)}/cuentas", headers=h).json()}


def asiento(client, h, eid, lineas, fecha=None, contabilizar=True, **extra):
    cs = cuentas(client, h, eid)
    datos = {"fecha": str(fecha or HOY), "descripcion": extra.pop("descripcion", "Asiento de prueba"),
             "contabilizar": contabilizar, **extra,
             "lineas": [{"cuenta_id": cs[c]["id"], "debito": str(d), "credito": str(cr)} for c, d, cr in lineas]}
    return client.post(f"{base(eid)}/asientos", json=datos, headers=h)


# ------------------------------------------------------------------- Módulos
def test_catalogo_de_modulos_y_empresa(client, gente, emp):
    cat = client.get("/modulos", headers=gente["admin"]["h"]).json()
    assert [m["codigo"] for m in cat] == ["planilla", "contabilidad"]
    assert cat[1]["precio_mensual"] == "45000.00" and cat[1]["unidad"] == "asientos"
    e = client.get(f"/empresas/{emp}", headers=gente["oper"]["h"]).json()
    assert e["modulos"] == ["planilla", "contabilidad"]
    # Por defecto una empresa nueva solo trae Planilla
    r = client.post("/empresas", json={"nombre": "Solo Planilla SA"}, headers=gente["otro"]["h"])
    assert r.json()["modulos"] == ["planilla"]


def test_catalogo_inicial_y_subcuentas(client, gente, emp):
    op = gente["oper"]["h"]
    cs = cuentas(client, op, emp)
    assert cs["1"]["nivel"] == 0 and cs["11"]["nivel"] == 1 and cs["1103"]["nivel"] == 2
    assert not cs["11"]["acepta_movimientos"] and cs["1103"]["acepta_movimientos"]
    # Subcuenta bajo una cuenta de detalle sin movimientos: el padre pasa a agrupación
    r = client.post(f"{base(emp)}/cuentas", json={"codigo": "1103.01", "nombre": "Banco Nacional", "tipo": "activo",
                                                 "padre_id": cs["1103"]["id"]}, headers=op)
    assert r.status_code == 201, r.text
    cs = cuentas(client, op, emp)
    assert not cs["1103"]["acepta_movimientos"] and cs["1103.01"]["nivel"] == 3
    # Tipo distinto al del grupo: rechazado
    r = client.post(f"{base(emp)}/cuentas", json={"codigo": "1199", "nombre": "Mal ubicada", "tipo": "gasto",
                                                 "padre_id": cs["11"]["id"]}, headers=op)
    assert r.status_code == 409 and "mismo tipo" in r.json()["detail"]
    # Código repetido
    r = client.post(f"{base(emp)}/cuentas", json={"codigo": "1101", "nombre": "Otra caja", "tipo": "activo"}, headers=op)
    assert r.status_code == 409
    # Lectura no crea
    r = client.post(f"{base(emp)}/cuentas", json={"codigo": "1109", "nombre": "X caja", "tipo": "activo"},
                    headers=gente["lect"]["h"])
    assert r.status_code == 403


def test_asiento_manual_y_validaciones(client, gente, emp):
    op = gente["oper"]["h"]
    r = asiento(client, op, emp, [("1103.01", 1_000_000, 0), ("3101", 0, 1_000_000)], descripcion="Aporte de capital")
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["estado"] == "contabilizado" and a["numero"] == f"GEN-{HOY.year}-000001" and a["cuadra"]
    r = asiento(client, op, emp, [("1103.01", 100, 0), ("4102", 0, 90)])
    assert r.status_code == 422 and "no cuadra" in r.json()["detail"]
    r = asiento(client, op, emp, [("1103", 100, 0), ("4102", 0, 100)])
    assert r.status_code == 422 and "agrupación" in r.json()["detail"]
    r = client.post(f"{base(emp)}/asientos", json={"fecha": str(HOY), "descripcion": "Ambos lados", "lineas": [
        {"cuenta_id": cuentas(client, op, emp)["1101"]["id"], "debito": "5", "credito": "5"},
        {"cuenta_id": cuentas(client, op, emp)["4102"]["id"], "credito": "5"}]}, headers=op)
    assert r.status_code == 422
    # Borrador: se edita, se contabiliza y queda inmutable
    b = asiento(client, op, emp, [("1101", 50_000, 0), ("4102", 0, 50_000)], contabilizar=False).json()
    assert b["estado"] == "borrador" and b["numero"] is None
    cs = cuentas(client, op, emp)
    r = client.put(f"{base(emp)}/asientos/{b['id']}", headers=op, json={
        "fecha": str(HOY), "descripcion": "Venta de contado", "contabilizar": True, "lineas": [
            {"cuenta_id": cs["1101"]["id"], "debito": "113000"},
            {"cuenta_id": cs["4102"]["id"], "credito": "100000"}, {"cuenta_id": cs["2121"]["id"], "credito": "13000"}]})
    assert r.status_code == 200 and r.json()["estado"] == "contabilizado" and r.json()["total_debito"] == "113000.00"
    assert client.delete(f"{base(emp)}/asientos/{b['id']}", headers=op).status_code == 409


def test_reversion(client, gente, emp):
    op = gente["oper"]["h"]
    a = asiento(client, op, emp, [("5201", 200_000, 0), ("1103.01", 0, 200_000)], descripcion="Alquiler").json()
    r = client.post(f"{base(emp)}/asientos/{a['id']}/revertir", json={"motivo": "Se registró dos veces"}, headers=op)
    assert r.status_code == 200, r.text
    rev = r.json()
    assert rev["origen"] == "reversion" and rev["revierte_a"] == a["id"] and rev["estado"] == "contabilizado"
    assert {(l["cuenta_codigo"], l["credito"]) for l in rev["lineas"]} >= {("5201", "200000.00")}
    orig = client.get(f"{base(emp)}/asientos/{a['id']}", headers=op).json()
    assert orig["estado"] == "revertido" and orig["revertido_por"] == rev["id"]
    assert client.post(f"{base(emp)}/asientos/{a['id']}/revertir", json={"motivo": "otra vez"}, headers=op).status_code == 409
    # El mayor de alquileres queda en cero
    m = client.get(f"{base(emp)}/reportes/mayor", params={"cuenta_id": cuentas(client, op, emp)["5201"]["id"]}, headers=op).json()
    assert m["saldo_final"] == "0.00" and len(m["movimientos"]) == 2


def test_cuenta_con_movimientos_no_se_borra(client, gente, emp):
    op = gente["oper"]["h"]
    cs = cuentas(client, op, emp)
    assert cs["1103.01"]["tiene_movimientos"]
    assert client.delete(f"{base(emp)}/cuentas/{cs['1103.01']['id']}", headers=op).status_code == 409
    r = client.patch(f"{base(emp)}/cuentas/{cs['1103.01']['id']}", json={"tipo": "pasivo"}, headers=op)
    assert r.status_code == 409 and "movimientos" in r.json()["detail"]
    assert client.patch(f"{base(emp)}/cuentas/{cs['1103.01']['id']}", json={"nombre": "Banco Nacional CRC"},
                        headers=op).status_code == 200
    nueva = client.post(f"{base(emp)}/cuentas", json={"codigo": "5298", "nombre": "Temporal", "tipo": "gasto",
                                                       "padre_id": cs["52"]["id"]}, headers=op).json()
    assert client.delete(f"{base(emp)}/cuentas/{nueva['id']}", headers=op).status_code == 204


def test_reportes_cuadran(client, gente, emp):
    op = gente["oper"]["h"]
    desde = str(date(HOY.year, 1, 1))
    bc = client.get(f"{base(emp)}/reportes/comprobacion", params={"desde": desde}, headers=op).json()
    assert bc["totales"]["cuadra"] and bc["totales"]["debitos"] == bc["totales"]["creditos"]
    er = client.get(f"{base(emp)}/reportes/resultados", params={"desde": desde}, headers=op).json()
    assert er["ingresos"]["total"] == "100000.00" and er["utilidad_neta"] == "100000.00"
    bg = client.get(f"{base(emp)}/reportes/balance", headers=op).json()
    assert bg["cuadra"] and bg["activo"]["total"] == "1113000.00" and bg["resultado_ejercicio"] == "100000.00"
    di = client.get(f"{base(emp)}/reportes/diario", params={"desde": desde}, headers=op).json()
    assert di["totales"]["debitos"] == di["totales"]["creditos"] and len(di["asientos"]) >= 4
    for tipo in ("diario", "comprobacion", "resultados", "balance"):
        x = client.get(f"{base(emp)}/reportes/{tipo}/exportar", params={"formato": "xlsx", "desde": desde}, headers=op)
        assert x.status_code == 200 and x.content[:2] == b"PK", tipo
        p = client.get(f"{base(emp)}/reportes/{tipo}/exportar", params={"formato": "pdf", "desde": desde}, headers=op)
        assert p.status_code == 200 and p.content[:4] == b"%PDF", tipo
    cta = cuentas(client, op, emp)["1103"]["id"]   # grupo: suma sus subcuentas
    m = client.get(f"{base(emp)}/reportes/mayor/exportar", params={"formato": "xlsx", "cuenta_id": cta}, headers=op)
    assert m.status_code == 200
    res = client.get(f"{base(emp)}/resumen", headers=gente["lect"]["h"]).json()
    assert res["cuadra"] and res["anio"]["utilidad"] == "100000.00" and res["efectivo"] == "1113000.00"


def test_otra_empresa_no_ve_nada(client, gente, emp):
    otro = gente["otro"]["h"]
    assert client.get(f"{base(emp)}/cuentas", headers=otro).status_code == 404
    assert client.get(f"{base(emp)}/reportes/balance", headers=otro).status_code == 404
    mia = client.post("/empresas", json={"nombre": "Ajena Conta SA", "modulos": ["contabilidad"]}, headers=otro).json()
    cta_ajena = cuentas(client, gente["oper"]["h"], emp)["1101"]["id"]
    caja = cuentas(client, otro, mia["id"])["4102"]["id"]
    r = client.post(f"{base(mia['id'])}/asientos", headers=otro, json={
        "fecha": str(HOY), "descripcion": "Intento", "contabilizar": True,
        "lineas": [{"cuenta_id": cta_ajena, "debito": "10"}, {"cuenta_id": caja, "credito": "10"}]})
    assert r.status_code in (409, 422)
    # Solo contabilidad: Planilla bloqueada para escribir
    r = client.post(f"/empresas/{mia['id']}/empleados", headers=otro, json={
        "identificacion": "9-9999-9999", "nombre": "Nadie", "fecha_ingreso": "2026-01-01", "salario_mensual": "500000"})
    assert r.status_code == 403 and r.headers["X-Aurom-Error"] == "modulo_inactivo"


def test_periodos_cierre_y_reapertura(client, gente, emp):
    adm, op = gente["admin"]["h"], gente["oper"]["h"]
    mes_pasado = date(HOY.year - 1, 12, 1)
    asiento(client, op, emp, [("1101", 10_000, 0), ("4102", 0, 10_000)], fecha=mes_pasado)
    assert client.post(f"{base(emp)}/periodos/{mes_pasado.year}/12/cerrar", headers=op).status_code == 403
    r = client.post(f"{base(emp)}/periodos/{mes_pasado.year}/12/cerrar", headers=adm)
    assert r.status_code == 200 and r.json()["estado"] == "cerrado"
    r = asiento(client, op, emp, [("1101", 1, 0), ("4102", 0, 1)], fecha=mes_pasado)
    assert r.status_code == 409 and "cerrado" in r.json()["detail"]
    p = client.get(f"{base(emp)}/periodos", params={"anio": mes_pasado.year}, headers=op).json()
    assert p["meses"][11]["estado"] == "cerrado" and p["meses"][11]["asientos"] == 1
    assert client.post(f"{base(emp)}/periodos/{mes_pasado.year}/12/reabrir", json={"motivo": "x"}, headers=adm).status_code == 422
    r = client.post(f"{base(emp)}/periodos/{mes_pasado.year}/12/reabrir", json={"motivo": "Falta una factura"}, headers=adm)
    assert r.status_code == 200 and r.json()["estado"] == "abierto"


def test_cierre_anual(client, gente, emp):
    adm, op = gente["admin"]["h"], gente["oper"]["h"]
    anio = HOY.year - 1
    asiento(client, op, emp, [("5201", 4_000, 0), ("1101", 0, 4_000)], fecha=date(anio, 12, 15))
    r = client.post(f"{base(emp)}/cierre-anual/{anio}", headers=adm)
    assert r.status_code == 200, r.text
    assert r.json()["utilidad"] == "6000.00" and r.json()["asiento"]["numero"].startswith(f"CIE-{anio}")
    # El año queda bloqueado y sus resultados en utilidades acumuladas
    assert asiento(client, op, emp, [("1101", 1, 0), ("4102", 0, 1)], fecha=date(anio, 6, 1)).status_code == 409
    bg = client.get(f"{base(emp)}/reportes/balance", params={"fecha": str(date(anio, 12, 31))}, headers=op).json()
    assert bg["cuadra"] and bg["resultado_ejercicio"] == "0.00"
    er = client.get(f"{base(emp)}/reportes/resultados", params={"desde": str(date(anio, 1, 1)), "hasta": str(date(anio, 12, 31))},
                    headers=op).json()
    assert er["utilidad_neta"] == "6000.00"   # el estado de resultados no incluye el asiento de cierre
    assert client.post(f"{base(emp)}/cierre-anual/{anio}", headers=adm).status_code == 409
    cierre = r.json()["asiento"]["id"]
    r2 = client.post(f"{base(emp)}/asientos/{cierre}/revertir", json={"motivo": "atajo indebido", "fecha": str(HOY)}, headers=op)
    assert r2.status_code == 409 and "reabriendo el año" in r2.json()["detail"]
    assert client.post(f"{base(emp)}/periodos/{anio}/3/reabrir", json={"motivo": "Probar bloqueo"}, headers=adm).status_code == 409
    r = client.post(f"{base(emp)}/cierre-anual/{anio}/reabrir", json={"motivo": "Ajuste de auditoría"}, headers=adm)
    assert r.status_code == 200, r.text
    bg = client.get(f"{base(emp)}/reportes/balance", params={"fecha": str(date(anio, 12, 31))}, headers=op).json()
    assert bg["resultado_ejercicio"] == "6000.00" and bg["cuadra"]


def test_planilla_entra_sola_a_contabilidad_y_se_revierte(client, gente, emp):
    adm, op = gente["admin"]["h"], gente["oper"]["h"]
    r = client.post(f"/empresas/{emp}/empleados", headers=op, json={
        "identificacion": "3-3333-3333", "nombre": "Carla", "apellidos": "Ruiz", "fecha_ingreso": "2024-01-01",
        "salario_mensual": "600000"})
    assert r.status_code == 201, r.text
    ini, fin = HOY.replace(day=1), HOY.replace(day=15)
    pl = client.post(f"/empresas/{emp}/planillas", headers=op, json={"desde": str(ini), "hasta": str(fin),
                                                                    "fecha_pago": str(fin)}).json()
    b = f"/empresas/{emp}/planillas/{pl['id']}"
    assert client.post(f"{b}/calcular", headers=op).status_code == 200
    assert client.post(f"{b}/aprobar", headers=adm).status_code == 200
    assert client.post(f"{b}/cerrar", headers=adm).status_code == 200
    lista = client.get(f"{base(emp)}/asientos", params={"origen": "planilla"}, headers=op).json()["asientos"]
    assert len(lista) == 1 and lista[0]["estado"] == "contabilizado" and lista[0]["diario_codigo"] == "PLA"
    det = client.get(f"{base(emp)}/asientos/{lista[0]['id']}", headers=op).json()
    assert det["cuadra"] and {"5101", "2101", "2102"} <= {l["cuenta_codigo"] for l in det["lineas"]}
    assert det["total_debito"] == client.get(f"{b}/asiento", headers=op).json()["total_debito"]
    r = client.post(f"{b}/reabrir", json={"motivo": "Revisar horas"}, headers=adm)
    assert r.status_code == 200, r.text
    todos = client.get(f"{base(emp)}/asientos", params={"buscar": "Planilla"}, headers=op).json()["asientos"]
    estados = sorted((a["origen"], a["estado"]) for a in todos)
    assert estados == [("planilla", "revertido"), ("reversion", "contabilizado")]


def test_cuenta_de_planilla_inexistente_se_crea_sola(client, gente, emp):
    adm, op = gente["admin"]["h"], gente["oper"]["h"]
    r = client.put(f"/empresas/{emp}/cuentas-contables", headers=op,
                   json=[{"concepto": "gasto_salarios", "codigo": "5107", "nombre": "Salarios obra"}])
    assert r.status_code == 200, r.text
    pl = client.get(f"/empresas/{emp}/planillas", headers=op).json()[0]
    b = f"/empresas/{emp}/planillas/{pl['id']}"
    client.post(f"{b}/calcular", headers=op)
    client.post(f"{b}/aprobar", headers=adm)
    assert client.post(f"{b}/cerrar", headers=adm).status_code == 200
    a = [x for x in client.get(f"{base(emp)}/asientos", params={"origen": "planilla", "estado": "contabilizado"},
                               headers=op).json()["asientos"]][0]
    assert "5107" in (a["advertencia"] or "")
    cs = cuentas(client, op, emp)
    assert cs["5107"]["tipo"] == "gasto" and cs["5107"]["padre_id"] == cs["51"]["id"]


def test_modulo_apagado_solo_consulta(client, gente, emp):
    adm, op = gente["admin"]["h"], gente["oper"]["h"]
    assert client.put(f"/empresas/{emp}/modulos/contabilidad", json={"activo": False}, headers=op).status_code == 403
    r = client.put(f"/empresas/{emp}/modulos/contabilidad", json={"activo": False}, headers=adm)
    assert r.status_code == 200 and r.json()["activos"] == ["planilla"]
    r = asiento(client, op, emp, [("1101", 1, 0), ("4102", 0, 1)])
    assert r.status_code == 403 and r.headers["X-Aurom-Error"] == "modulo_inactivo"
    assert client.get(f"{base(emp)}/reportes/balance", headers=op).status_code == 200
    r = client.put(f"/empresas/{emp}/modulos/planilla", json={"activo": False}, headers=adm)
    assert r.status_code == 409 and "al menos un módulo" in r.json()["detail"]
    hist = client.get(f"/empresas/{emp}/modulos", headers=adm).json()
    assert any(h["modulo"] == "contabilidad" and h["hasta"] for h in hist["historial"])
    assert client.put(f"/empresas/{emp}/modulos/contabilidad", json={"activo": True}, headers=adm).json()["activos"] == \
        ["planilla", "contabilidad"]


# --------------------------------------------------------------- Cobro
def test_precio_por_modulo_y_factura(client, gente, emp, usuarios, dsn):
    duena = usuarios["duena"]["h"]
    r = client.post("/negocio/clientes", headers=duena, json={
        "empresa_id": emp, "razon_social": "Contable Prueba SA", "tipo_identificacion": "02",
        "identificacion": "3-101-777777", "correo_facturacion": "pagos@contable.cr", "alta": str(HOY.replace(day=1))})
    assert r.status_code == 201, r.text
    cli = r.json()
    p = cli["precio_actual"]
    mods = {m["modulo"]: m for m in p["modulos"]}
    assert mods["planilla"]["subtotal"] == "55000.00" and mods["contabilidad"]["subtotal"] == "45000.00"
    assert p["subtotal"] == "100000.00" and p["iva"] == "13000.00"
    # Precio propio de contabilidad para este cliente y volumen del mes anterior
    r = client.patch(f"/negocio/clientes/{cli['id']}", headers=duena,
                     json={"precios_modulos": {"contabilidad": {"precio_mensual": "30000", "incluidos": 1, "precio_bloque": "2000",
                                                                "tam_bloque": 1}}})
    assert r.status_code == 200, r.text
    sig = (HOY.replace(day=28) + __import__("datetime").timedelta(days=5)).replace(day=1)
    p = client.get(f"/negocio/clientes/{cli['id']}/precio", params={"periodo": str(sig)}, headers=duena).json()
    c = {m["modulo"]: m for m in p["modulos"]}["contabilidad"]
    with psycopg.connect(dsn) as conn:
        n = conn.execute("""select count(*) from contabilidad.asientos where empresa_id = %s
                             and estado in ('contabilizado','revertido') and fecha >= %s and fecha < %s""",
                         (emp, HOY.replace(day=1), sig)).fetchone()[0]
    assert c["unidades"] == n and c["bloques_adicionales"] == max(0, n - 1)
    assert D(c["subtotal"]) == D(30000) + D(2000) * max(0, n - 1)
    # La suscripción vista por la empresa muestra los módulos
    s = client.get(f"/empresas/{emp}/suscripcion", headers=gente["admin"]["h"]).json()
    assert [m["modulo"] for m in s["precio"]["modulos"]] == ["planilla", "contabilidad"]
    # Un módulo apagado se sigue cobrando el mes en que estuvo activo, y el siguiente ya no
    client.put(f"/empresas/{emp}/modulos/contabilidad", json={"activo": False}, headers=gente["admin"]["h"])
    este = client.get(f"/negocio/clientes/{cli['id']}/precio", params={"periodo": str(HOY)}, headers=duena).json()
    assert [m["modulo"] for m in este["modulos"]] == ["planilla", "contabilidad"]
    luego = client.get(f"/negocio/clientes/{cli['id']}/precio", params={"periodo": str(sig)}, headers=duena).json()
    assert [m["modulo"] for m in luego["modulos"]] == ["planilla"]
    client.put(f"/empresas/{emp}/modulos/contabilidad", json={"activo": True}, headers=gente["admin"]["h"])
