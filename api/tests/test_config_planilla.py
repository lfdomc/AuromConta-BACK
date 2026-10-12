"""Opciones de Configuración › Planilla equivalentes a las del módulo Odoo (migración 0019)."""
import subprocess
from datetime import date
from decimal import Decimal as D

import psycopg
import pytest
from psycopg.rows import dict_row


def _texto_pdf(contenido: bytes) -> str:
    return subprocess.run(["pdftotext", "-layout", "-", "-"], input=contenido, capture_output=True).stdout.decode()


@pytest.fixture(scope="module")
def e(client, dsn):
    import uuid

    from conftest import token
    uid = uuid.uuid4()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"cfg-{uid.hex[:6]}@prueba.cr"))
    a = token(uid, "cfg@prueba.cr")
    eid = client.post("/empresas", json={"nombre": "Config Odoo SA", "cedula_juridica": "3-101-424242"}, headers=a).json()["id"]
    base = {"fecha_ingreso": "2024-03-10", "frecuencia": "biweekly", "salario_mensual": "900000",
            "iban": "CR05015202001026284066", "correo": "x@config.cr"}
    ana = client.post(f"/empresas/{eid}/empleados", json={**base, "identificacion": "1-0101-0101", "nombre": "Ana",
                                                           "apellidos": "Rojas"}, headers=a).json()
    beto = client.post(f"/empresas/{eid}/empleados", json={**base, "identificacion": "2-0202-0202", "nombre": "Beto",
                                                            "apellidos": "Solano"}, headers=a).json()
    return {"id": eid, "h": a, "ana": ana["id"], "beto": beto["id"]}


def test_guardar_todas_las_opciones(client, e):
    cambios = {
        "bac_plan": "DQX6", "bac_envio": 6, "bac_concepto": "Pago de Planilla",
        "he_desde_asistencia": True, "tolerancia_tardia_min": 15, "asiento_modo": "empleado",
        "correo_asunto": "Boleta {periodo}", "correo_cuerpo": "Hola {colaborador}", "correo_firma": "RRHH",
        "firmante_nombre": "Lic. Saúl Barrantes Porras", "firmante_cargo": "Encargado de Recursos Humanos",
        "firmante_telefono": "2431-2431", "firmante_correo": "rh@config.cr", "firmante_lugar": "INVU Las Cañas, Alajuela",
        "boleta_saldo_vacaciones": True, "boleta_detalle_subsidio": True, "liquidacion_sin_ccss": True,
        "vacaciones_excluir_incapacidad": True, "vacaciones_dias_aniversario": "3",
        "alertas_documentos_correos": "rh@config.cr", "alertas_documentos_frecuencia": "mensual", "alertas_documentos_dias": 30,
    }
    r = client.patch(f"/empresas/{e['id']}/configuracion", json=cambios, headers=e["h"])
    assert r.status_code == 200, r.text
    c = client.get(f"/empresas/{e['id']}/configuracion", headers=e["h"]).json()
    for k, v in cambios.items():
        assert str(c[k]).rstrip("0").rstrip(".") == str(v).rstrip("0").rstrip("."), k


def test_horas_extra_desde_marcas_bac_y_asiento_por_boleta(client, e, dsn):
    h, eid = e["h"], e["id"]
    with psycopg.connect(dsn, autocommit=True) as c:
        # Ana marca 10 h el 5 de marzo (2 h extra) y 8 h 10 min el 6 (dentro de la tolerancia de 15 min)
        c.execute("""insert into marcas_asistencia (empresa_id, empleado_id, entrada, salida) values
                     (%(e)s, %(a)s, '2026-03-05 07:00-06', '2026-03-05 17:00-06'),
                     (%(e)s, %(a)s, '2026-03-06 07:00-06', '2026-03-06 15:10-06')""", {"e": eid, "a": e["ana"]})
    pl = client.post(f"/empresas/{eid}/planillas", json={"desde": "2026-03-01", "hasta": "2026-03-15"}, headers=h).json()["id"]
    r = client.post(f"/empresas/{eid}/planillas/{pl}/calcular", headers=h).json()
    assert any("horas extra detectadas" in a["mensaje"] for a in r["advertencias"])
    client.post(f"/empresas/{eid}/planillas/{pl}/calcular", headers=h)          # recalcular no duplica
    he = client.get(f"/empresas/{eid}/horas-extra", headers=h).json()
    auto = [x for x in he if x["origen"] == "automatico"]
    assert len(auto) == 1 and D(auto[0]["horas"]) == D("2") and auto[0]["estado"] == "pendiente"

    assert client.post(f"/empresas/{eid}/planillas/{pl}/aprobar", headers=h).status_code == 200
    # BAC Formato 6 con el plan y el envío guardados; el envío avanza solo
    b = client.get(f"/empresas/{eid}/exportaciones/banco", params={"planilla_id": pl, "banco": "bac_prn"}, headers=h)
    assert b.status_code == 200, b.text
    assert b.text.startswith("BDQX600006") and "INPDQX6 6.PRN" in b.headers["content-disposition"]
    assert client.get(f"/empresas/{eid}/configuracion", headers=h).json()["bac_envio"] == 7

    # Asiento: uno por boleta
    assert client.post(f"/empresas/{eid}/planillas/{pl}/cerrar", headers=h).json()["estado"] == "cerrada"
    a = client.get(f"/empresas/{eid}/planillas/{pl}/asiento", headers=h).json()
    assert a["cantidad"] == 2 and any("Ana Rojas" in l["descripcion"] for l in a["lineas"])
    assert D(a["total_debito"]) == D(a["total_credito"])

    # Boleta en PDF con saldo de vacaciones
    bol = client.get(f"/empresas/{eid}/planillas/{pl}", headers=h).json()["boletas"][0]
    t = _texto_pdf(client.get(f"/empresas/{eid}/planillas/{pl}/boletas/{bol['id']}/pdf", headers=h).content)
    assert "Saldo de vacaciones" in t


def test_constancia_con_firmante_y_liquidacion_sin_ccss(client, e):
    h, eid = e["h"], e["id"]
    t = _texto_pdf(client.get(f"/empresas/{eid}/empleados/{e['beto']}/constancia", params={"salario": True}, headers=h).content)
    assert "Saúl Barrantes" in t and "Encargado de Recursos Humanos" in t and "INVU Las Cañas" in t and "900" in t
    s = client.post(f"/empresas/{eid}/liquidaciones/simular", json={
        "empleado_id": e["beto"], "fecha_salida": "2026-03-31", "motivo": "renuncia", "salarios_pendientes": "100000"},
        headers=h).json()
    assert D(s["resultado"]["ccss_obrero"]) == 0


def test_correo_plantilla():
    from app.servicios import correo
    assert correo.plantilla("Boleta {periodo} de {empresa}", "", periodo="marzo", empresa="X") == "Boleta marzo de X"
    assert correo.plantilla("llave { suelta", "") == "llave { suelta"
    assert "&lt;b&gt;" in correo.a_html("<b>hola</b>")


def test_vacaciones_aniversario_e_incapacidad(client, e, dsn):
    from app.routers.autogestion import acumular_vacaciones
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
        # Beto entró el 10/03/2024: al 10/03/2026 cumple 2 años. Partimos del 09/02/2026.
        c.execute("update empleados set saldo_vacaciones = 0, creado_en = '2026-02-09' where id = %s", (e["beto"],))
        c.execute("""insert into incapacidades (empresa_id, empleado_id, tipo, desde, hasta) values
                     (%s,%s,'ccss','2026-01-25','2026-02-23')""", (e["id"], e["beto"]))
        res = acumular_vacaciones(c, date(2026, 3, 10))
        beto = [x for x in res if str(x["empleado_id"]) == e["beto"]][0]
        # 2 mesiversarios (10/02 y 10/03) + 2 por aniversario (total 3 ese mes) - 1 por 30 días incapacitado
        # (como Odoo: 1 día menos por cada 29 días de incapacidad)
        assert beto["dias"] == D("2") + D("2") - D("1")


def test_alertas_de_documentos(client, e, dsn, monkeypatch):
    from app.routers import autogestion
    enviados = []
    monkeypatch.setattr(autogestion.correo, "enviar", lambda para, asunto, html, adjuntos=None, **k: enviados.append((para, asunto, adjuntos)))
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
        c.execute("insert into documentos_empleado (empresa_id, empleado_id, tipo, vence) values (%s,%s,'Licencia','2026-03-20')",
                  (e["id"], e["ana"]))
        assert autogestion.alertas_documentos(c, date(2026, 3, 5)) >= 1
        assert autogestion.alertas_documentos(c, date(2026, 3, 12)) == 0      # mensual: una vez por mes
    para, asunto, adjuntos = [x for x in enviados if "Config Odoo" in x[1]][0]
    assert para == ["rh@config.cr"] and adjuntos[0][0].endswith(".xlsx")
