"""Base legal: cada regla del sistema con su ley, y datos 2026 corregidos (migración 0023)."""
from decimal import Decimal as D


def test_catalogo_legible_por_cualquier_usuario(client, usuarios):
    h = usuarios["operador"]["h"]
    bl = {x["clave"]: x for x in client.get("/base-legal", headers=h).json()}
    for clave in ("ccss_cuotas", "ccss_bmc", "ccss_bmc_reducida", "renta_tramos", "renta_208", "salario_minimo", "aguinaldo",
                  "vacaciones", "preaviso", "cesantia", "incapacidad_ccss", "incapacidad_recaida", "maternidad",
                  "paternidad", "duelo", "lactancia", "embargos"):
        assert clave in bl, clave
        assert bl[clave]["norma"] and bl[clave]["regla"]
    assert "45333-H" in bl["renta_tramos"]["norma"] and "10895" in bl["lactancia"]["norma"]
    assert "45303-MTSS" in bl["salario_minimo"]["norma"]
    assert all((x["enlace"] or "https://").startswith("https://") for x in bl.values())


def test_solo_la_plataforma_edita(client, usuarios):
    cuerpo = {"clave": "prueba_x", "tema": "Pruebas", "titulo": "Regla de prueba", "regla": "Algo", "norma": "Ley 0",
              "donde": ["Boleta"]}
    assert client.put("/plataforma/base-legal/prueba_x", json=cuerpo, headers=usuarios["operador"]["h"]).status_code == 403
    h = usuarios["duena"]["h"]
    r = client.put("/plataforma/base-legal/prueba_x", json=cuerpo, headers=h)
    assert r.status_code == 200, r.text
    assert client.put("/plataforma/base-legal/prueba_x", json={**cuerpo, "norma": "Ley 1"}, headers=h).json()["norma"] == "Ley 1"
    assert client.put("/plataforma/base-legal/otra", json=cuerpo, headers=h).status_code == 422
    assert client.delete("/plataforma/base-legal/prueba_x", headers=h).status_code == 204


def test_salarios_minimos_y_bmc_2026(client, usuarios):
    h = usuarios["operador"]["h"]
    sm = {x["categoria"]: D(x["monto"]) for x in client.get("/salarios-minimos", headers=h).json()
          if x["vigente_desde"] == "2026-01-01"}
    assert sm["Trabajador en ocupación no calificada"] == D("373092.30")
    assert sm["Licenciado universitario"] == D("796921.00") and sm["Trabajo doméstico"] == D("268731.31")
    assert len(sm) == 10


def test_citas_en_reportes():
    from app.servicios import citas_legales
    assert "45333-H" in citas_legales.de_reporte("renta") and "0042-2025" in citas_legales.de_reporte("retenciones-208")
    assert citas_legales.de_reporte("inexistente") == ""
    assert set(citas_legales.CITAS) >= {c for v in citas_legales.POR_REPORTE.values() for c in v}
