"""Fase 7 (negocio de A.U.R.O.M.) y cobros de la Fase 8. Todo HTTP externo está simulado."""
import base64
import dataclasses
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import httpx
import psycopg
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from lxml import etree

from app import config
from app.servicios import correo, facturacion as F, telegram, tilopay
from conftest import token

CABYS_PRUEBA = "8314100000000"   # solo datos de prueba; en producción lo configura la dueña
CRON = {"X-Cron-Secret": "cron-de-prueba"}
NS_FE = "https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.4/facturaElectronica"
DSIG = "http://www.w3.org/2000/09/xmldsig#"


# ------------------------------------------------------------------ apoyo ---
def generar_p12(pin=b"1234"):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SOFIA PRUEBA"),
                        x509.NameAttribute(NameOID.SERIAL_NUMBER, "CPF-01-1111-1111")])
    ahora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre).public_key(k.public_key())
            .serial_number(4242).not_valid_before(ahora - timedelta(days=1)).not_valid_after(ahora + timedelta(days=60))
            .sign(k, hashes.SHA256()))
    return pkcs12.serialize_key_and_certificates(b"prueba", k, cert, None, serialization.BestAvailableEncryption(pin)), cert


class Mock:
    """Hacienda, Tilopay y Resend simulados."""

    def __init__(self):
        self.llamadas: list[tuple[str, str]] = []
        self.cobros: list[dict] = []        # respuestas de processRecurrentPayment en orden
        self.estado_hacienda = "aceptado"
        self.correos: list[dict] = []
        self.consult = {"code": "1", "commission": "3000", "iva_commission": "390", "retention_iva": "0",
                        "retention_rent": "0", "id_tilopay": 999}

    def hacienda(self, req: httpx.Request) -> httpx.Response:
        self.llamadas.append((req.method, str(req.url)))
        u = str(req.url)
        if "openid-connect/token" in u:
            assert b"grant_type=password" in req.content and b"client_id=api-stag" in req.content
            return httpx.Response(200, json={"access_token": "tok-h", "expires_in": 300})
        if req.method == "POST" and u.endswith("/recepcion"):
            j = json.loads(req.content)
            assert len(j["clave"]) == 50 and j["emisor"]["numeroIdentificacion"] == "111111111"
            assert base64.b64decode(j["comprobanteXml"]).startswith(b"<?xml")
            return httpx.Response(202)
        if req.method == "GET" and "/recepcion/" in u:
            clave = u.rsplit("/", 1)[1]
            resp = f"<MensajeHacienda><Clave>{clave}</Clave><DetalleMensaje>Mensaje {self.estado_hacienda}</DetalleMensaje></MensajeHacienda>"
            return httpx.Response(200, json={"clave": clave, "ind-estado": self.estado_hacienda,
                                             "respuesta-xml": base64.b64encode(resp.encode()).decode()})
        return httpx.Response(404)

    def tilopay(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        self.llamadas.append((req.method, p))
        j = json.loads(req.content or b"{}")
        if p == tilopay.EP_LOGIN:
            return httpx.Response(200, json={"access_token": "tok-t", "token_type": "bearer", "expires_in": 3600})
        assert req.headers["Authorization"] == "bearer tok-t"
        if p == tilopay.EP_COBRO_TOKEN:
            assert j["card"] == "tok-tarjeta" and j["capture"] == "1"
            r = self.cobros.pop(0) if self.cobros else {"response": "1", "description": "Transacción aprobada"}
            return httpx.Response(200, json={"type": "200", "auth": "123456", "tpt": 1, "tilopayTransaction": 77,
                                             "order_id": "1", "orderHash": "x" * 64, **r})
        if p == tilopay.EP_CONSULTAR:
            return httpx.Response(200, json={"type": "200", "response": [{"orderNumber": j["orderNumber"], **self.consult}]})
        if p == tilopay.EP_TOKENIZAR:
            assert j["token_version"] == "v2" and "/webhooks/tilopay/retorno?estado=" in j["redirect"]
            return httpx.Response(200, json={"type": "100", "url": "https://secure.tilopay.com/tokenize/abc.html"})
        if p == tilopay.EP_ELIMINAR_TARJETA:
            return httpx.Response(200, json={"type": "200"})
        return httpx.Response(404)


@pytest.fixture(scope="module")
def mock(client, dsn):
    m = Mock()
    p12, cert = generar_p12()
    original = config.settings
    config.settings = dataclasses.replace(
        original, cron_secret=CRON["X-Cron-Secret"], hacienda_ambiente="pruebas",
        hacienda_usuario="cpf-01-1111-1111@stag.comprobanteselectronicos.go.cr", hacienda_clave="clave",
        hacienda_p12_base64=base64.b64encode(p12).decode(), hacienda_p12_pin="1234",
        tilopay_api_user="usuario", tilopay_api_password="clave", tilopay_api_key="llave",
        tilopay_webhook_secret="secreto-webhook", api_url="https://api.prueba.cr", app_url="https://app.prueba.cr")
    mp = pytest.MonkeyPatch()
    mp.setattr(F, "cliente_http", lambda: httpx.Client(transport=httpx.MockTransport(m.hacienda)))
    mp.setattr(tilopay, "cliente_http", lambda: httpx.Client(base_url=tilopay.TILOPAY_HOST,
                                                              transport=httpx.MockTransport(m.tilopay)))
    mp.setattr(telegram, "enviar_mensaje", lambda *a, **k: False)

    def enviar(para, asunto, html, adjuntos=None, remitente=None):
        m.correos.append({"para": para, "asunto": asunto, "adjuntos": [n for n, _ in adjuntos or []]})
        return "msg-1"
    mp.setattr(correo, "enviar", enviar)
    F._tokens.clear()
    tilopay._token.clear()
    with psycopg.connect(dsn, autocommit=True) as c:
        respaldo = c.execute("select clave, valor::text from negocio.configuracion").fetchall()
        emisor = {"nombre": "Sofía Madriz Prueba", "tipo_identificacion": "01", "identificacion": "1-1111-1111",
                  "nombre_comercial": "A.U.R.O.M.", "correo": "facturas@auromtec.com", "telefono": "8888-8888",
                  "provincia": "2", "canton": "5", "distrito": "1", "otras_senas": "Atenas centro, 100 m norte del parque",
                  "codigo_actividad": "722003", "sucursal": "001", "terminal": "00001"}
        for k, v in {"emisor": emisor, "cabys_suscripcion": CABYS_PRUEBA, "cabys_implementacion": CABYS_PRUEBA,
                     "firma_politica": {"url": "https://cdn.comprobanteselectronicos.go.cr/politica.pdf",
                                        "digest": "q1w2e3r4t5y6u7i8o9p0q1w2e3r4t5y6u7i8o9p0abc=", "algoritmo": "sha256"},
                     "tipo_cambio_usd": {"valor": "510.25", "fecha": "2026-10-01"}}.items():
            c.execute("insert into negocio.configuracion values (%s, %s::jsonb) on conflict (clave) do update set valor = excluded.valor",
                      (k, json.dumps(v)))
    m.cert = cert
    yield m
    mp.undo()
    config.settings = original
    with psycopg.connect(dsn, autocommit=True) as c:
        for clave, valor in respaldo:
            c.execute("update negocio.configuracion set valor = %s::jsonb where clave = %s", (valor, clave))


def nuevo_usuario(dsn, nombre):
    uid = uuid.uuid4()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{nombre}-{uid.hex[:6]}@prueba.cr"))
    return {"id": uid, "h": token(uid, f"{nombre}@prueba.cr")}


def nueva_empresa(client, dsn, *, empleados=0, codigo=None, cedula="3-101-555555"):
    adm = nuevo_usuario(dsn, "adm")
    r = client.post("/empresas", json={"nombre": f"Empresa {uuid.uuid4().hex[:5]} SA", "cedula_juridica": cedula,
                                       "codigo_referido": codigo}, headers=adm["h"])
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    if empleados:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("""insert into empleados (empresa_id, identificacion, nombre, fecha_ingreso, salario_mensual)
                         select %s, 'E' || g, 'Emp ' || g, '2025-01-01', 500000 from generate_series(1, %s) g""", (eid, empleados))
    return eid, adm


def crear_cliente(client, duena, eid=None, **extra):
    datos = {"empresa_id": eid, "razon_social": f"Cliente {uuid.uuid4().hex[:5]} SA", "tipo_identificacion": "02",
             "identificacion": "3-101-555555", "correo_facturacion": "pagos@cliente.cr", "alta": "2026-10-01", **extra}
    r = client.post("/negocio/clientes", json={k: v for k, v in datos.items() if v is not None}, headers=duena)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="module")
def duena(usuarios):
    return usuarios["duena"]["h"]


# ----------------------------------------------------------------- precio ---
@pytest.mark.parametrize("n,subtotal,iva", [(0, "55000", "7150"), (50, "55000", "7150"), (51, "66000", "8580"),
                                            (60, "66000", "8580"), (61, "77000", "10010"), (75, "88000", "11440")])
def test_tramos_de_precio(n, subtotal, iva):
    p = F.calcular_precio(n, D(55000), 50, D(11000))
    assert p["subtotal"] == D(subtotal) and p["iva"] == D(iva) and p["total"] == D(subtotal) + D(iva)


def test_precio_cliente_cuenta_empleados_activos(client, dsn, duena, mock):
    eid, _ = nueva_empresa(client, dsn, empleados=51)
    cli = crear_cliente(client, duena, eid)
    assert cli["precio_actual"]["empleados"] == 51 and cli["precio_actual"]["subtotal"] == "66000.00"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("update empleados set estado = 'inactivo' where empresa_id = %s and identificacion = 'E51'", (eid,))
    p = client.get(f"/negocio/clientes/{cli['id']}/precio?periodo=2026-10-01", headers=duena).json()
    assert p["empleados"] == 50 and p["subtotal"] == "55000.00" and p["iva"] == "7150.00" and p["moneda"] == "CRC"


def test_precio_fundador_usd_y_despues_colones(client, dsn, duena, mock):
    eid, _ = nueva_empresa(client, dsn, empleados=3)
    cli = crear_cliente(client, duena, eid, moneda="USD", precio_mensual="100", precio_fundador_hasta="2027-09-30")
    p = client.get(f"/negocio/clientes/{cli['id']}/precio?periodo=2027-09-01", headers=duena).json()
    assert (p["moneda"], p["subtotal"], p["precio_fundador"]) == ("USD", "100.00", True)
    p = client.get(f"/negocio/clientes/{cli['id']}/precio?periodo=2027-10-01", headers=duena).json()
    assert (p["moneda"], p["subtotal"], p["precio_fundador"]) == ("CRC", "55000.00", False)


# --------------------------------------------------------- clave y XML -----
def test_clave_numerica_y_consecutivo():
    cons = F.consecutivo("001", "00001", "01", 7)
    assert cons == "00100001010000000007" and len(cons) == 20
    clave = F.clave_numerica(date(2026, 10, 9), "1-1111-1111", cons, "1", "12345678")
    assert len(clave) == 50 and clave.isdigit()
    assert clave[:3] == "506" and clave[3:9] == "091026" and clave[9:21] == "000111111111"
    assert clave[21:41] == cons and clave[41] == "1" and clave[42:] == "12345678"
    assert len(F.clave_numerica(date(2026, 1, 2), "3101123456", cons)) == 50


def _comp_prueba(tipo="01"):
    return {"tipo": tipo, "clave": "5" * 50, "consecutivo": F.consecutivo("001", "00001", tipo, 1),
            "fecha": datetime(2026, 10, 9, 10, 0, tzinfo=F.CR), "condicion_venta": "01", "medio_pago": "02",
            "moneda": "CRC", "tipo_cambio": D(1),
            "lineas": [{"numero": 1, "cabys": CABYS_PRUEBA, "cantidad": D(1), "unidad": "Sp", "descripcion": "Suscripción",
                        "precio_unitario": D("66000"), "descuento": D(0), "impuesto": D("8580"), "tarifa_iva": D(13),
                        "codigo_tarifa_iva": "08"}]}


EMISOR = {"nombre": "Sofía Prueba", "tipo_identificacion": "01", "identificacion": "111111111", "provincia": "2",
          "canton": "5", "distrito": "1", "otras_senas": "Atenas centro", "correo": "a@auromtec.com", "codigo_actividad": "722003"}
RECEPTOR = {"razon_social": "Cliente SA", "tipo_identificacion": "02", "identificacion": "3101555555",
            "correo_facturacion": "c@cliente.cr", "actividad_receptor": "410001"}


def test_xml_factura_44_estructura():
    xml = F.construir_xml(_comp_prueba(), EMISOR, RECEPTOR, proveedor_sistemas="111111111")
    doc = etree.fromstring(xml)
    assert etree.QName(doc).localname == "FacturaElectronica" and etree.QName(doc).namespace == NS_FE
    hijos = [etree.QName(e).localname for e in doc]
    assert hijos == ["Clave", "ProveedorSistemas", "CodigoActividadEmisor", "CodigoActividadReceptor", "NumeroConsecutivo",
                     "FechaEmision", "Emisor", "Receptor", "CondicionVenta", "DetalleServicio", "ResumenFactura"]
    n = {"f": NS_FE}
    ld = doc.find("f:DetalleServicio/f:LineaDetalle", n)
    assert [etree.QName(e).localname for e in ld] == [
        "NumeroLinea", "CodigoCABYS", "Cantidad", "UnidadMedida", "Detalle", "PrecioUnitario", "MontoTotal", "SubTotal",
        "BaseImponible", "Impuesto", "ImpuestoAsumidoEmisorFabrica", "ImpuestoNeto", "MontoTotalLinea"]
    assert doc.findtext("f:DetalleServicio/f:LineaDetalle/f:Impuesto/f:CodigoTarifaIVA", namespaces=n) == "08"
    rs = doc.find("f:ResumenFactura", n)
    assert rs.findtext("f:MedioPago/f:TipoMedioPago", namespaces=n) == "02"
    assert rs.findtext("f:TotalDesgloseImpuesto/f:TotalMontoImpuesto", namespaces=n) == "8580.00000"
    assert rs.findtext("f:TotalComprobante", namespaces=n) == "74580.00000"
    assert doc.findtext("f:FechaEmision", namespaces=n) == "2026-10-09T10:00:00-06:00"
    assert doc.findtext("f:Emisor/f:Ubicacion/f:Canton", namespaces=n) == "05"


def test_xml_tiquete_y_nota_credito_raiz():
    te = etree.fromstring(F.construir_xml(_comp_prueba("04"), EMISOR, None, proveedor_sistemas="1"))
    assert etree.QName(te).localname == "TiqueteElectronico" and te.find("{*}Receptor") is None
    ref = {"tipo": "01", "clave": "5" * 50, "fecha": datetime(2026, 10, 1, tzinfo=F.CR), "razon": "Cobro duplicado"}
    nc = etree.fromstring(F.construir_xml(_comp_prueba("03"), EMISOR, RECEPTOR, proveedor_sistemas="1", referencia=ref))
    assert etree.QName(nc).localname == "NotaCreditoElectronica"
    assert nc.findtext("{*}InformacionReferencia/{*}Codigo") == "01" and nc.find("{*}ResumenFactura/{*}MedioPago") is None


def test_firma_xades_epes(mock):
    p12, cert = generar_p12(b"9999")
    xml = F.construir_xml(_comp_prueba(), EMISOR, RECEPTOR, proveedor_sistemas="111111111")
    firmado = F.firmar_xades_epes(xml, p12, "9999", {"url": "https://politica", "digest": "abc="})
    doc = etree.fromstring(firmado)
    sig = doc[-1]
    assert sig.tag == f"{{{DSIG}}}Signature"
    assert sig.find(f"{{{DSIG}}}SignedInfo/{{{DSIG}}}SignatureMethod").get("Algorithm").endswith("rsa-sha256")
    assert len(sig.findall(f"{{{DSIG}}}SignedInfo/{{{DSIG}}}Reference")) == 3
    assert doc.xpath("string(//*[local-name()='SigPolicyId']/*[local-name()='Identifier'])") == "https://politica"
    assert F.verificar_firma(firmado)
    # Verificación independiente con signxml (XMLDSig + XAdES).
    from signxml import XMLVerifier
    from signxml.xades import XAdESVerifier
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    XMLVerifier().verify(firmado, x509_cert=pem, expect_references=3)
    XAdESVerifier().verify(firmado, x509_cert=pem, expect_references=3)
    # Alterar el monto invalida la firma.
    assert not F.verificar_firma(firmado.replace(b"74580.00000</TotalComprobante>", b"1.00000</TotalComprobante>"))
    with pytest.raises(Exception):
        F.firmar_xades_epes(xml, p12, "0000", {"url": "u", "digest": "d"})


# ---------------------------------------------------- acceso y emisión -----
def test_solo_la_duena(client, dsn, mock):
    eid, adm = nueva_empresa(client, dsn)
    for ruta in ("/negocio/clientes", "/negocio/tablero", "/negocio/comprobantes", "/negocio/configuracion/emisor",
                 "/negocio/renta/2026", "/negocio/cierres/2026/10"):
        assert client.get(ruta, headers=adm["h"]).status_code == 403, ruta
    assert client.post("/negocio/pagos", json={}, headers=adm["h"]).status_code in (403, 422)
    assert client.get("/negocio/clientes").status_code == 401


def test_emitir_rechaza_sin_emisor_o_cabys(client, dsn, duena, mock):
    eid, _ = nueva_empresa(client, dsn, empleados=2)
    cli = crear_cliente(client, duena, eid)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("update negocio.configuracion set valor = 'null' where clave = 'cabys_implementacion'")
        r = client.post(f"/negocio/facturas/implementacion/{cli['id']}", headers=duena)
        assert r.status_code == 409 and "CABYS" in r.json()["detail"]
        c.execute("update negocio.configuracion set valor = %s where clave = 'cabys_implementacion'", (json.dumps(CABYS_PRUEBA),))
        comp = client.post(f"/negocio/facturas/implementacion/{cli['id']}", headers=duena).json()
        assert comp["subtotal"] == "100000.00" and comp["impuesto"] == "13000.00"
        c.execute("update negocio.configuracion set valor = valor - 'codigo_actividad' - 'nombre' where clave = 'emisor'")
        r = client.post(f"/negocio/comprobantes/{comp['id']}/emitir", headers=duena)
        assert r.status_code == 409 and "faltan datos del emisor" in r.json()["detail"] and "actividad" in r.json()["detail"]
        assert client.get(f"/negocio/comprobantes/{comp['id']}", headers=duena).json()["clave"] is None
        c.execute("""update negocio.configuracion set valor = valor || '{"nombre": "Sofía Madriz Prueba", "codigo_actividad": "722003"}'
                     where clave = 'emisor'""")
    r = client.post("/negocio/comprobantes/{}/anular-borrador".format(comp["id"]), headers=duena)
    assert r.json()["estado"] == "anulado"


def test_generar_emitir_consultar_y_correo(client, dsn, duena, mock):
    eid, _ = nueva_empresa(client, dsn, empleados=75)
    cli = crear_cliente(client, duena, eid)
    r = client.post("/negocio/facturas/generar?periodo=2026-10-01", headers=duena)
    assert r.status_code == 200, r.text
    mia = [x for x in r.json()["creadas"] if x["cliente_id"] == cli["id"]][0]
    assert mia["subtotal"] == "88000.00" and mia["impuesto"] == "11440.00" and mia["total"] == "99440.00"
    det = client.get(f"/negocio/comprobantes/{mia['id']}", headers=duena).json()
    assert len(det["lineas"]) == 2 and det["lineas"][1]["cantidad"] == "3.000"   # 3 bloques de 10
    again = client.post("/negocio/facturas/generar?periodo=2026-10-01", headers=duena).json()
    assert all(x["cliente_id"] != cli["id"] for x in again["creadas"])          # idempotente

    e = client.post(f"/negocio/comprobantes/{mia['id']}/emitir", headers=duena)
    assert e.status_code == 200, e.text
    e = e.json()
    assert e["estado"] == "enviado" and len(e["clave"]) == 50 and e["clave"][21:41] == e["consecutivo"]
    assert e["consecutivo"][:10] == "0010000101"
    xml = client.get(f"/negocio/comprobantes/{mia['id']}/xml", headers=duena)
    doc = etree.fromstring(xml.content)
    assert etree.QName(doc).namespace == NS_FE and doc.findtext("{*}Clave") == e["clave"]
    assert doc.findtext("{*}ProveedorSistemas") == "111111111" and doc[-1].tag == f"{{{DSIG}}}Signature"
    assert F.verificar_firma(xml.content)

    c = client.post(f"/negocio/comprobantes/{mia['id']}/consultar", headers=duena).json()
    assert c["estado"] == "aceptado" and c["enviado_por_correo_en"]
    ult = mock.correos[-1]
    assert ult["para"] == "pagos@cliente.cr" and any(a.endswith(".pdf") for a in ult["adjuntos"])
    assert f"{e['clave']}.xml" in ult["adjuntos"] and f"{e['clave']}_respuesta.xml" in ult["adjuntos"]
    assert client.get(f"/negocio/comprobantes/{mia['id']}/pdf", headers=duena).content[:4] == b"%PDF"
    assert b"MensajeHacienda" in client.get(f"/negocio/comprobantes/{mia['id']}/xml?respuesta=true", headers=duena).content

    # Nota de crédito total: al aceptarse, la factura queda anulada.
    nc = client.post(f"/negocio/comprobantes/{mia['id']}/nota-credito", json={"razon": "Cobro duplicado", "emitir": True},
                     headers=duena).json()
    assert nc["tipo"] == "03" and nc["estado"] == "enviado" and nc["consecutivo"][8:10] == "03"
    ncx = etree.fromstring(client.get(f"/negocio/comprobantes/{nc['id']}/xml", headers=duena).content)
    assert ncx.findtext("{*}InformacionReferencia/{*}Numero") == e["clave"]
    client.post(f"/negocio/comprobantes/{nc['id']}/consultar", headers=duena)
    assert client.get(f"/negocio/comprobantes/{mia['id']}", headers=duena).json()["estado"] == "anulado"


def test_rechazo_de_hacienda_alerta(client, dsn, duena, mock):
    eid, _ = nueva_empresa(client, dsn, empleados=1)
    cli = crear_cliente(client, duena, eid)
    comp = client.post(f"/negocio/facturas/implementacion/{cli['id']}", headers=duena).json()
    client.post(f"/negocio/comprobantes/{comp['id']}/emitir", headers=duena)
    mock.estado_hacienda = "rechazado"
    try:
        c = client.post(f"/negocio/comprobantes/{comp['id']}/consultar", headers=duena).json()
    finally:
        mock.estado_hacienda = "aceptado"
    assert c["estado"] == "rechazado" and "rechazado" in c["mensaje_estado"]
    with psycopg.connect(dsn) as x:
        assert x.execute("select count(*) from negocio.alertas where tipo = 'factura' and severidad = 'critica' "
                         "and mensaje like 'Hacienda RECHAZÓ%%'").fetchone()[0] >= 1


# ---------------------------------------------------------- comisiones -----
def _socio(dsn, tipo):
    cod = f"{tipo[:3].upper()}{uuid.uuid4().hex[:5].upper()}"
    with psycopg.connect(dsn, autocommit=True) as c:
        sid = c.execute("insert into socios (tipo, nombre, correo, codigo_referido) values (%s, %s, %s, %s) returning id",
                        (tipo, f"Socio {cod}", f"{cod}@prueba.cr", cod)).fetchone()[0]
    return sid, cod


def _pagar(client, duena, cliente_id, periodo, monto="62150", **extra):
    r = client.post("/negocio/pagos", json={"cliente_id": cliente_id, "monto": monto, "medio": "sinpe", "periodo": periodo,
                                            "referencia": f"SINPE-{uuid.uuid4().hex[:8]}", **extra}, headers=duena)
    assert r.status_code == 201, r.text
    return r.json()


def test_comisiones_vendedor_solo_tres_meses(client, dsn, duena, mock):
    sid, cod = _socio(dsn, "vendedor")
    eid, _ = nueva_empresa(client, dsn, codigo=cod)
    cli = crear_cliente(client, duena, eid, alta="2026-01-01")
    assert client.get(f"/negocio/comisiones?socio_id={sid}", headers=duena).json() == []   # sin pago, nada
    meses = ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01"]
    pagos = [_pagar(client, duena, cli["id"], m) for m in meses]
    assert [len(p["comisiones"]) for p in pagos] == [1, 1, 1, 0, 0]
    com = client.get(f"/negocio/comisiones?socio_id={sid}", headers=duena).json()
    assert len(com) == 3 and {c["concepto"] for c in com} == {"mensualidad_vendedor"}
    assert all(c["monto"] == "55000.00" and c["porcentaje"] == "100.000" for c in com)
    # Implementación de un cliente de vendedor: A.U.R.O.M. se queda todo.
    assert _pagar(client, duena, cli["id"], None, monto="113000", concepto="implementacion")["comisiones"] == []
    # Idempotente
    assert client.post("/negocio/comisiones/calcular?mes=2026-02", headers=duena).json()["creadas"] == 0


def test_comisiones_contador_recurrente_e_implementacion(client, dsn, duena, mock):
    sid, cod = _socio(dsn, "contador")
    eid, _ = nueva_empresa(client, dsn, codigo=cod)
    cli = crear_cliente(client, duena, eid, alta="2026-01-01")
    impl = _pagar(client, duena, cli["id"], None, monto="113000", concepto="implementacion", fecha="2026-01-10")
    assert [(c["concepto"], c["monto"]) for c in impl["comisiones"]] == [("implementacion", "50000.00")]
    for m in ["2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01"]:
        p = _pagar(client, duena, cli["id"], m)
        assert [(c["concepto"], c["monto"], c["porcentaje"]) for c in p["comisiones"]] == [("recurrente_contador", "11000.00", "20.000")]
    # Se revoca la asignación: ya no hay comisión recurrente.
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("update asignaciones_contador set revocado_en = now() where empresa_id = %s and socio_id = %s", (eid, sid))
    assert _pagar(client, duena, cli["id"], "2026-05-01")["comisiones"] == []

    liqs = client.post("/negocio/liquidaciones-socio/generar?mes=2026-04", headers=duena).json()
    mia = [l for l in liqs if l["socio_id"] == str(sid)][0]
    assert mia["total"] == "94000.00"     # 50 000 + 4 x 11 000
    r = client.post(f"/negocio/liquidaciones-socio/{mia['id']}/pagar", json={"referencia": "TRF-1"}, headers=duena)
    assert r.status_code == 200, r.text
    gasto = r.json()["gasto"]
    assert gasto["total"] == "94000.00"
    cats = {c["id"]: c["nombre"] for c in client.get("/negocio/categorias-gasto", headers=duena).json()}
    assert cats[gasto["categoria_id"]] == "Comisiones de socios y vendedores"
    assert client.post(f"/negocio/liquidaciones-socio/{mia['id']}/pagar", json={}, headers=duena).status_code == 409


# ------------------------------------------------------- webhook y cron -----
def test_webhook_tilopay_firma(client, dsn, mock):
    r = client.post("/webhooks/tilopay", json={"orderNumber": "X1", "code": "1"})
    assert r.status_code == 401
    r = client.post("/webhooks/tilopay?secreto=otro", json={"orderNumber": "X1"})
    assert r.status_code == 401
    r = client.post("/webhooks/tilopay", json={"orderNumber": "X1"}, headers={"X-Webhook-Secret": "secreto-webhook"})
    assert r.status_code == 200 and r.json()["aplicado"] is False
    with psycopg.connect(dsn) as c:
        n = c.execute("select count(*) filter (where not firma_valida), count(*) filter (where firma_valida) "
                      "from negocio.eventos_webhook where origen = 'tilopay' and contenido->>'orderNumber' = 'X1'").fetchone()
    assert n == (2, 1)


def test_comisiones_con_pagos_parciales(client, dsn, duena, mock):
    """Auditoría: un mes pagado en dos abonos debe dar comisión por el total (antes solo el primero)."""
    sid, cod = _socio(dsn, "contador")
    eid, _ = nueva_empresa(client, dsn, codigo=cod)
    cli = crear_cliente(client, duena, eid, alta="2026-01-01")
    a = _pagar(client, duena, cli["id"], "2026-06-01", monto="31075")
    # Medio pago no cubre el mes: sigue pendiente (antes cualquier abono lo daba por pagado).
    assert "2026-06-01" in client.get(f"/negocio/clientes/{cli['id']}", headers=duena).json()["pendientes"]
    b = _pagar(client, duena, cli["id"], "2026-06-01", monto="31075")
    assert "2026-06-01" not in client.get(f"/negocio/clientes/{cli['id']}", headers=duena).json()["pendientes"]
    assert [c["monto"] for c in a["comisiones"] + b["comisiones"]] == ["5500.00", "5500.00"]   # 20 % de 27 500 cada uno
    # Implementación en dos abonos: proporcional y con tope de ₡50 000
    i1 = _pagar(client, duena, cli["id"], None, monto="56500", concepto="implementacion", fecha="2026-06-02")
    i2 = _pagar(client, duena, cli["id"], None, monto="56500", concepto="implementacion", fecha="2026-06-20")
    i3 = _pagar(client, duena, cli["id"], None, monto="56500", concepto="implementacion", fecha="2026-06-25")
    assert [c["monto"] for c in i1["comisiones"] + i2["comisiones"]] == ["25000.00", "25000.00"] and i3["comisiones"] == []
    assert client.post("/negocio/comisiones/calcular?mes=2026-06", headers=duena).json()["creadas"] == 0


def test_cron_requiere_secreto(client, mock):
    assert client.post("/cron/cobros").status_code == 401
    assert client.post("/cron/facturas", headers={"X-Cron-Secret": "malo"}).status_code == 401


def _estado(dsn, cliente_id):
    with psycopg.connect(dsn) as c:
        return c.execute("select c.estado, e.estado_servicio from negocio.clientes c join empresas e on e.id = c.empresa_id "
                         "where c.id = %s", (cliente_id,)).fetchone()


def test_gracia_solo_lectura_suspendido_y_reactivacion(client, dsn, duena, mock):
    eid, adm = nueva_empresa(client, dsn, empleados=1)
    cli = crear_cliente(client, duena, eid, alta="2026-11-01")
    assert _estado(dsn, cli["id"]) == ("activo", "activo")
    for dia, esperado in (("2026-10-31", "activo"), ("2026-11-01", "gracia"), ("2026-11-07", "gracia"),
                          ("2026-11-08", "solo_lectura"), ("2026-11-30", "solo_lectura"), ("2026-12-01", "suspendido")):
        r = client.post(f"/cron/cobros?fecha={dia}", headers=CRON)
        assert r.status_code == 200, r.text
        assert _estado(dsn, cli["id"]) == (esperado, esperado), dia
    # En solo lectura/suspendida la empresa no puede escribir (RLS de las otras fases).
    with psycopg.connect(dsn) as c:
        assert c.execute("select public.empresa_escribible(%s)", (eid,)).fetchone()[0] is False
    _pagar(client, duena, cli["id"], "2026-11-01")
    _pagar(client, duena, cli["id"], "2026-12-01")
    client.post("/cron/cobros?fecha=2026-12-02", headers=CRON)
    assert _estado(dsn, cli["id"]) == ("activo", "activo")


def test_cobro_con_tarjeta_reintentos(client, dsn, duena, mock):
    eid, adm = nueva_empresa(client, dsn, empleados=2)
    cli = crear_cliente(client, duena, eid, alta="2026-12-01", dia_cobro=5)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("insert into negocio.metodos_pago (cliente_id, token, ultimos4, marca, correo) values (%s, 'tok-tarjeta', '4242', 'VISA', 'pagos@cliente.cr')",
                  (cli["id"],))
    mock.cobros = [{"response": "0", "description": "Fondos insuficientes"}, {"response": "0", "description": "Fondos insuficientes"}]
    agenda = {"2026-12-04": (0, "activo"), "2026-12-05": (1, "gracia"), "2026-12-06": (2, "gracia"),
              "2026-12-07": (2, "gracia"), "2026-12-08": (3, "activo"), "2026-12-09": (3, "activo")}
    for dia, (intentos, estado) in agenda.items():
        client.post(f"/cron/cobros?fecha={dia}", headers=CRON)
        cobros = client.get(f"/negocio/cobros?cliente_id={cli['id']}", headers=duena).json()
        assert len(cobros) == intentos, dia
        assert _estado(dsn, cli["id"])[0] == estado, dia
    ok = [c for c in cobros if c["resultado"] == "aprobado"][0]
    assert ok["total"] == "62150.00" and ok["comision"] == "3107.50" and ok["intento"] == 3   # 5 % de tarifa
    pago = client.get(f"/negocio/pagos?cliente_id={cli['id']}", headers=duena).json()[0]
    assert pago["medio"] == "tarjeta" and pago["periodo"] == "2026-12-01" and pago["subtotal"] == "55000.00"
    comp = client.get(f"/negocio/comprobantes/{pago['comprobante_id']}", headers=duena).json()
    assert comp["medio_pago"] == "02" and comp["estado"] == "enviado"

    # El administrador ve su suscripción con la tarjeta (sin token) y los cobros.
    s = client.get(f"/empresas/{eid}/suscripcion", headers=adm["h"])
    assert s.status_code == 200, s.text
    s = s.json()
    assert s["metodo_pago"]["ultimos4"] == "4242" and "token" not in s["metodo_pago"]
    assert len(s["cobros"]) == 3 and s["estado"] == "activo"
    # /cron/facturas consulta Hacienda y la deja aceptada.
    f = client.post("/cron/facturas?fecha=2026-12-09", headers=CRON).json()
    assert f["aceptadas"] >= 1
    assert client.get(f"/negocio/comprobantes/{pago['comprobante_id']}", headers=duena).json()["estado"] == "aceptado"


def test_webhook_confirma_con_consult(client, dsn, duena, mock):
    eid, _ = nueva_empresa(client, dsn, empleados=1)
    cli = crear_cliente(client, duena, eid, alta="2026-10-01")
    with psycopg.connect(dsn, autocommit=True) as c:
        cobro = c.execute("""insert into negocio.cobros (cliente_id, periodo, empleados_facturados, subtotal, iva, total, orden_proveedor)
                             values (%s, '2026-10-01', 1, 55000, 7150, 62150, 'AURWEBHOOK1') returning id""", (cli["id"],)).fetchone()[0]
    r = client.post("/webhooks/tilopay?secreto=secreto-webhook", json={"orderNumber": "AURWEBHOOK1", "code": "1"})
    assert r.status_code == 200 and r.json()["aplicado"] is True
    with psycopg.connect(dsn) as c:
        res, pago = c.execute("select resultado, pago_id from negocio.cobros where id = %s", (cobro,)).fetchone()
        com = c.execute("select comision from negocio.pagos where id = %s", (pago,)).fetchone()[0]
    assert res == "aprobado" and com == D("3390.00")       # comisión real informada por consult
    # Repetido: no duplica.
    assert client.post("/webhooks/tilopay?secreto=secreto-webhook", json={"orderNumber": "AURWEBHOOK1"}).json()["aplicado"] is False


# -------------------------------------------------- vista del cliente ------
def test_suscripcion_solo_de_mi_empresa_y_registro_de_tarjeta(client, dsn, duena, mock):
    eid, adm = nueva_empresa(client, dsn, empleados=55, cedula="3-101-777777")
    otra, adm2 = nueva_empresa(client, dsn)
    s = client.get(f"/empresas/{eid}/suscripcion", headers=adm["h"])
    # Con cédula, la ficha de cobro existe desde el registro (en prueba, sin cobrar).
    assert s.status_code == 200 and s.json()["cliente"]["identificacion"] == "3101777777"
    assert s.json()["precio"]["subtotal"] == "66000.00" and s.json()["estado"] == "prueba"
    assert client.get(f"/empresas/{otra}/suscripcion", headers=adm["h"]).status_code == 404
    assert client.get(f"/empresas/{eid}/suscripcion", headers=adm2["h"]).status_code == 404

    r = client.post(f"/empresas/{eid}/suscripcion/metodo-pago", headers=adm["h"])
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://secure.tilopay.com/")
    cli = client.get("/negocio/clientes", headers=duena).json()
    mio = [c for c in cli if c["empresa_id"] == eid][0]
    assert mio["identificacion"] == "3101777777" and mio["tipo_identificacion"] == "02"

    estado = tilopay.firmar_estado({"c": mio["id"], "e": eid, "m": "x@cliente.cr", "t": int(__import__("time").time())})
    malo = client.get(f"/webhooks/tilopay/retorno?estado={estado[:-2]}xx&card=tok-tarjeta&last=4111", follow_redirects=False)
    assert malo.status_code == 400
    ok = client.get(f"/webhooks/tilopay/retorno?estado={estado}&code=1&card=tok-tarjeta&last=************4111&brand=VISA",
                    follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"].endswith(f"/e/{eid}/suscripcion?tarjeta=ok")
    s = client.get(f"/empresas/{eid}/suscripcion", headers=adm["h"]).json()
    assert s["metodo_pago"]["ultimos4"] == "4111" and s["metodo_pago"]["marca"] == "VISA"
    with psycopg.connect(dsn) as c:
        ev = c.execute("select contenido::text from negocio.eventos_webhook where tipo = 'tokenizacion' order by recibido_en desc limit 1").fetchone()[0]
    assert "tok-tarjeta" not in ev                               # el token no queda en la bitácora


# ------------------------------------------------------ gastos y reportes ---
XML_43 = """<?xml version="1.0" encoding="utf-8"?>
<FacturaElectronica xmlns="https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.3/facturaElectronica">
  <Clave>50601102600310112345600100001010000000123100000001</Clave>
  <CodigoActividad>722003</CodigoActividad>
  <NumeroConsecutivo>00100001010000000123</NumeroConsecutivo>
  <FechaEmision>2026-10-03T09:15:00-06:00</FechaEmision>
  <Emisor><Nombre>Servidores CR SA</Nombre><Identificacion><Tipo>02</Tipo><Numero>3101123456</Numero></Identificacion></Emisor>
  <Receptor><Nombre>Sofía</Nombre><Identificacion><Tipo>01</Tipo><Numero>111111111</Numero></Identificacion></Receptor>
  <CondicionVenta>01</CondicionVenta>
  <DetalleServicio><LineaDetalle><NumeroLinea>1</NumeroLinea><Detalle>Hosting octubre</Detalle>
    <Impuesto><Codigo>01</Codigo><CodigoTarifa>08</CodigoTarifa><Tarifa>13</Tarifa><Monto>2600.00</Monto></Impuesto>
  </LineaDetalle></DetalleServicio>
  <ResumenFactura><CodigoTipoMoneda><CodigoMoneda>CRC</CodigoMoneda><TipoCambio>1</TipoCambio></CodigoTipoMoneda>
    <TotalVentaNeta>20000.00</TotalVentaNeta><TotalImpuesto>2600.00</TotalImpuesto><TotalComprobante>22600.00</TotalComprobante>
  </ResumenFactura>
</FacturaElectronica>"""


def test_importar_xml_gasto_y_reportes(client, dsn, duena, mock):
    cats = {c["nombre"]: c["id"] for c in client.get("/negocio/categorias-gasto", headers=duena).json()}
    r = client.post("/negocio/gastos/importar-xml", headers=duena,
                    files={"archivo": ("f.xml", XML_43.encode(), "application/xml")},
                    data={"categoria_id": cats["Infraestructura (servidores, dominios)"], "mensaje_receptor": "aceptado"})
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["version"] == "4.3" and g["proveedor"] == "Servidores CR SA" and g["subtotal"] == "20000.00"
    assert g["iva"] == "2600.00" and g["iva_acreditable"] is True and g["mensaje_receptor"] == "aceptado"
    dup = client.post("/negocio/gastos/importar-xml", headers=duena, files={"archivo": ("f.xml", XML_43.encode(), "application/xml")})
    assert dup.status_code == 409
    assert client.post("/negocio/gastos/importar-xml", headers=duena,
                       files={"archivo": ("x.xml", b"<hola/>", "application/xml")}).status_code == 422
    # Una factura propia 4.4 firmada también se lee.
    x44 = F.parsear_xml_recibido(F.construir_xml(_comp_prueba(), EMISOR, RECEPTOR, proveedor_sistemas="1"))
    assert x44["version"] == "4.4" and x44["iva"] == D("8580.00") and x44["subtotal"] == D("66000.00")

    iva = client.get("/negocio/cierres/2026/10", headers=duena).json()
    assert iva["estado"] == "abierto" and D(iva["credito_fiscal"]) >= D("2600.00")
    assert D(iva["debito_fiscal"]) >= 0 and "D-104" in iva["nota"]
    cerrado = client.post("/negocio/cierres/2026/10", headers=duena)
    assert cerrado.status_code == 200 and cerrado.json()["estado"] == "cerrado"
    assert client.post("/negocio/cierres/2026/10", headers=duena).status_code == 409
    assert client.get("/negocio/cierres/2026/10", headers=duena).json()["estado"] == "cerrado"
    client.post("/negocio/cierres/2026/10?reabrir=true", headers=duena)

    renta = client.get("/negocio/renta/2026", headers=duena).json()
    assert D(renta["utilidad"]) == D(renta["ingresos"]) - D(renta["total_gastos"])
    assert renta["impuesto_estimado"] is None and any("contador" in a for a in renta["advertencias"])

    rep = client.get("/negocio/reparto?anio=2026", headers=duena).json()
    assert rep["parametros"]["tope_salario_neto"] == "4000000"
    from app.routers.negocio import config_reparto, repartir
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        cfg = config_reparto(c)
    assert repartir(D("800000"), cfg)["salario"] == D("400000.00")            # 50 %
    assert repartir(D("2000000"), cfg)["salario"] == D("1200000.00")          # 60 %
    assert repartir(D("5000000"), cfg)["salario"] == D("3500000.00")          # 70 %
    big = repartir(D("10000000"), cfg)
    assert big["salario"] == D("4000000") and big["caja"] + big["inversion"] == D("6000000")   # 75 % con tope

    t = client.get("/negocio/tablero?anio=2026", headers=duena)
    assert t.status_code == 200, t.text
    t = t.json()
    assert D(t["mrr"]) > 0 and t["clientes"] and "reparto" in t and D(t["cobrado"]) > 0


def test_configuracion(client, duena, mock):
    assert client.put("/negocio/configuracion/cabys_suscripcion", json={"valor": "123"}, headers=duena).status_code == 422
    assert client.put("/negocio/configuracion/inventada", json={"valor": 1}, headers=duena).status_code == 422
    r = client.put("/negocio/configuracion/tarifa_tarjeta_pct", json={"valor": "5"}, headers=duena)
    assert r.status_code == 200 and r.json()["valor"] == "5"
    r = client.put("/negocio/configuracion/emisor", json={"valor": {"telefono": "2446-0000"}}, headers=duena).json()
    assert r["valor"]["telefono"] == "2446-0000" and r["valor"]["codigo_actividad"] == "722003"   # se fusiona


def test_proveedores_de_gastos(client, duena):
    base = {"fecha": "2026-08-01", "subtotal": "1000", "iva": "0"}
    for d in ({"proveedor": "Ferretería Lagos", "identificacion_proveedor": "3101999888", "fecha": "2026-07-01"},
              {"proveedor": "Ferreteria Lagos S.A.", "identificacion_proveedor": "3101999888"},
              {"proveedor": "Pulpería La Esquina"}):
        r = client.post("/negocio/gastos", json={**base, **d}, headers=duena)
        assert r.status_code == 201, r.text
    p = client.get("/negocio/gastos/proveedores", headers=duena).json()
    lagos = [x for x in p if x["identificacion_proveedor"] == "3101999888"]
    assert len(lagos) == 1 and lagos[0]["proveedor"] == "Ferreteria Lagos S.A."   # el nombre más reciente
    assert any(x["proveedor"] == "Pulpería La Esquina" for x in p)
