"""Facturación electrónica de A.U.R.O.M. ante el Ministerio de Hacienda (Costa Rica, v4.4).

Qué hace:
  * Precio mensual por cliente según colaboradores activos (`precio_cliente`).
  * Borradores de factura de suscripción / implementación y notas de crédito.
  * Clave numérica (50 dígitos) y consecutivo (20 dígitos) por tipo de comprobante.
  * XML v4.4 de FacturaElectronica, NotaCreditoElectronica y TiqueteElectronico.
  * Firma XAdES-EPES con la llave criptográfica (.p12) de ATV.
  * Token del IDP de Hacienda, envío a /recepcion y consulta de estado.
  * Correo al cliente con PDF + XML firmado + respuesta de Hacienda.
  * Lectura de comprobantes recibidos (4.3 o 4.4) para registrar gastos.

Fuentes verificadas (oct. 2026):
  * XSD oficial FacturaElectronica v4.4 — https://www.hacienda.go.cr/docs/FacturaElectronica_V4.4.xsd.xml
    (targetNamespace, orden de elementos, ProveedorSistemas, CodigoActividadEmisor/Receptor, MedioPago
    dentro de ResumenFactura con TipoMedioPago 01-07/99, TotalDesgloseImpuesto, BaseImponible,
    ImpuestoAsumidoEmisorFabrica, CodigoTipoMoneda, unidades "Sp"/"Os").
  * Anexos y estructuras v4.4 (resumen de códigos de tarifa IVA: 08 = tarifa general 13 %) —
    https://soportereal.com/factupos/ayuda/hacienda_anexos_v44/ayuda_hacienda_anexos_v44.php
  * Resolución MH-DGT-RES (disposiciones técnicas, XAdES-EPES v1.3.2 o superior, ProveedorSistemas,
    plazo de 3 horas para la respuesta) — https://www.hacienda.go.cr/docs/DGT-R-000-2024DisposicionesTecnicasDeComprobantesElectronicosCP.pdf
  * IDP (password grant, client_id api-stag) — https://www.example-code.com/phpExt/hacienda_go_cr_get_access_token.asp
  * Recepción (POST JSON clave/fecha/emisor/receptor/comprobanteXml) —
    https://example-code.com/unicodeCpp/hacienda_go_cr_recepcion_de_comprobantes_electronicos.asp
  * Base sandbox `recepcion-sandbox/v1` y realms rut-stag/rut — https://github.com/DojoCodingLabs/hacienda-cr

Supuestos (VALIDAR en el ambiente de pruebas de Hacienda antes de producción):
  * Namespaces de NotaCreditoElectronica y TiqueteElectronico v4.4: se siguen el patrón del de factura
    (…/v4.4/notaCreditoElectronica, …/v4.4/tiqueteElectronico); no se pudo leer su XSD.
  * Consulta GET /recepcion/{clave} con respuesta `ind-estado` y `respuesta-xml` (base64): es el
    comportamiento documentado por la comunidad (CRLibre), no se encontró la página oficial.
  * Política de firma: URL de la resolución publicada en el CDN de Hacienda y su SHA-256 (se descarga y
    calcula si no está configurado). Confirmar que Hacienda acepta esa URL/digest.
"""
from __future__ import annotations

import base64
import calendar
import hashlib
import json
import logging
import math
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from fastapi import HTTPException
from lxml import etree

from .. import config
from ..db import Conexion
from ..fechas import hoy_cr
from . import alertas, correo, pdf

log = logging.getLogger("aurom.facturacion")

CERO_Q = Decimal("0.00")
CR = timezone(timedelta(hours=-6))  # Costa Rica: UTC-6 todo el año

# ----------------------------------------------------------- Hacienda: URLs --
# IDP (Keycloak): https://www.example-code.com/phpExt/hacienda_go_cr_get_access_token.asp
HACIENDA_IDP = {
    "pruebas": ("https://idp.comprobanteselectronicos.go.cr/auth/realms/rut-stag/protocol/openid-connect/token", "api-stag"),
    "produccion": ("https://idp.comprobanteselectronicos.go.cr/auth/realms/rut/protocol/openid-connect/token", "api-prod"),
}
# Recepción: https://github.com/DojoCodingLabs/hacienda-cr (tabla de ambientes)
HACIENDA_RECEPCION = {
    "pruebas": "https://api.comprobanteselectronicos.go.cr/recepcion-sandbox/v1/",
    "produccion": "https://api.comprobanteselectronicos.go.cr/recepcion/v1/",
}

# ------------------------------------------------------- Esquemas v4.4 ------
NS_BASE = "https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.4/"
TIPOS = {  # tipo de comprobante -> (elemento raíz, namespace)
    "01": ("FacturaElectronica", NS_BASE + "facturaElectronica"),         # verificado en el XSD oficial
    "03": ("NotaCreditoElectronica", NS_BASE + "notaCreditoElectronica"),  # supuesto: mismo patrón
    "04": ("TiqueteElectronico", NS_BASE + "tiqueteElectronico"),          # supuesto: mismo patrón
}
TIPO_POR_RAIZ = {v[0]: k for k, v in TIPOS.items()} | {"NotaDebitoElectronica": "02",
                                                        "FacturaElectronicaCompra": "08",
                                                        "FacturaElectronicaExportacion": "09"}
DS = "http://www.w3.org/2000/09/xmldsig#"
XADES = "http://uri.etsi.org/01903/v1.3.2#"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
TIPO_SIGNED_PROPS = "http://uri.etsi.org/01903#SignedProperties"

MEDIOS_PAGO = {"01": "Efectivo", "02": "Tarjeta", "03": "Cheque", "04": "Transferencia - depósito bancario",
               "05": "Recaudado por terceros", "06": "SINPE Móvil", "07": "Plataforma digital", "99": "Otros"}
TARIFA_IVA_GENERAL = "08"   # 13 %
TIPO_NOMBRE = {"01": "Factura electrónica", "02": "Nota de débito electrónica", "03": "Nota de crédito electrónica",
               "04": "Tiquete electrónico"}

DOS = Decimal("0.01")



# XML que viene de afuera (Hacienda, facturas de proveedores): sin entidades, sin DTD,
# sin red y sin árboles gigantes (evita XXE y "billion laughs").
PARSER_SEGURO = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
                                huge_tree=False)
PARSER_SEGURO_SIN_BLANCOS = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                                            dtd_validation=False, huge_tree=False, remove_blank_text=True)

def cliente_http() -> httpx.Client:
    """Punto único de salida HTTP (las pruebas lo reemplazan con un MockTransport)."""
    return httpx.Client(timeout=40)


def _q(v) -> Decimal:
    return Decimal(str(v)).quantize(DOS, ROUND_HALF_UP)


def _f5(v) -> str:
    return f"{Decimal(str(v)):.5f}"


def _conflicto(msg: str) -> HTTPException:
    return HTTPException(409, msg)


def _cfg(conn: Conexion, clave: str, defecto: Any = None) -> Any:
    fila = conn.execute("select valor from negocio.configuracion where clave = %s", (clave,)).fetchone()
    return defecto if not fila or fila["valor"] is None else fila["valor"]


def _guardar_cfg(conn, clave: str, valor: Any) -> None:
    conn.execute("insert into negocio.configuracion (clave, valor) values (%s, %s::jsonb) "
                 "on conflict (clave) do update set valor = excluded.valor", (clave, json.dumps(valor)))


def _digitos(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _fin_mes(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]


def nombre_mes(d: date) -> str:
    return f"{MESES[d.month - 1]} {d.year}"


# =================================================================== Precio ===
def empleados_activos(conn: Conexion, empresa_id, periodo: date) -> int:
    """Colaboradores de la empresa activos en algún momento del mes facturado."""
    if not empresa_id:
        return 0
    ini, fin = periodo.replace(day=1), _fin_mes(periodo)
    return conn.execute(
        """select count(*)::int n from public.empleados
            where empresa_id = %s and fecha_ingreso <= %s
              and (estado = 'activo' or (fecha_salida is not null and fecha_salida >= %s))""",
        (empresa_id, fin, ini)).fetchone()["n"]


def calcular_precio(empleados: int, base: Decimal, incluidos: int, bloque: Decimal, iva_pct: Decimal = Decimal(13)) -> dict:
    """Tarifa: base hasta `incluidos` colaboradores + `bloque` por cada 10 adicionales (o fracción)."""
    bloques = math.ceil(max(0, empleados - incluidos) / 10)
    subtotal = _q(base + bloque * bloques)
    iva = _q(subtotal * iva_pct / 100)
    return {"empleados": empleados, "bloques_adicionales": bloques, "base": _q(base), "adicional": _q(bloque * bloques),
            "subtotal": subtotal, "iva": iva, "total": subtotal + iva, "iva_pct": iva_pct}


def modulos_cobrables(conn: Conexion, empresa_id, periodo: date) -> list[str]:
    """Módulos que estuvieron activos algún día del mes (igual que los colaboradores)."""
    if not empresa_id:
        return ["planilla"]
    ini, fin = periodo.replace(day=1), _fin_mes(periodo)
    return [f["modulo"] for f in conn.execute(
        """select m.modulo from public.empresa_modulos m join public.modulos o on o.codigo = m.modulo
            where m.empresa_id = %s and m.desde <= %s and (m.hasta is null or m.hasta >= %s)
            group by m.modulo, o.orden order by o.orden""", (empresa_id, fin, ini)).fetchall()]


def unidades_modulo(conn: Conexion, empresa_id, unidad: str, periodo: date) -> int:
    """Volumen que define los bloques adicionales. Para contabilidad: asientos contabilizados
    el mes ANTERIOR al facturado (el mes en curso todavía no terminó cuando se cobra)."""
    if not empresa_id:
        return 0
    if unidad == "asientos":
        fin = periodo.replace(day=1)
        ini = (fin - timedelta(days=1)).replace(day=1)
        return conn.execute(
            """select count(*)::int n from contabilidad.asientos
                where empresa_id = %s and estado in ('contabilizado','revertido') and fecha >= %s and fecha < %s""",
            (empresa_id, ini, fin)).fetchone()["n"]
    return 0


def _precio_modulo(cat: dict, propio: dict, usd: bool, unidades: int) -> dict:
    sufijo = "_usd" if usd else ""
    base = _q(propio.get("precio_mensual" + sufijo, cat["precio_mensual" + sufijo]))
    incluidos = int(propio.get("incluidos", cat["incluidos"]))
    tam = max(int(propio.get("tam_bloque", cat["tam_bloque"])), 1)
    bloque = _q(propio.get("precio_bloque" + sufijo, cat["precio_bloque" + sufijo]))
    bloques = math.ceil(max(0, unidades - incluidos) / tam)
    return {"modulo": cat["codigo"], "nombre": cat["nombre"], "unidad": cat["unidad"], "unidades": unidades,
            "incluidos": incluidos, "tam_bloque": tam, "bloques_adicionales": bloques, "base": base,
            "precio_bloque": bloque, "adicional": _q(bloque * bloques), "subtotal": _q(base + bloque * bloques)}


def precio_empresa(conn: Conexion, cliente: dict | None, empresa_id, periodo: date) -> dict:
    """Precio del mes `periodo`, módulo por módulo: {modulos: [...], subtotal, iva, total, moneda, ...}.

    * Planilla: base hasta N colaboradores + bloque por cada 10 adicionales (o fracción).
      Precio fundador (clientes que venían de Odoo): mientras `precio_fundador_hasta >= periodo`,
      tarifa plana en USD; vencido, pasa a colones con la tarifa de lista.
    * Contabilidad: base hasta N asientos del mes anterior + bloque por cada 100 adicionales.
    * Cada cliente puede tener precio propio por módulo en negocio.clientes.precios_modulos.
    """
    periodo = periodo.replace(day=1)
    c = cliente
    precios = _cfg(conn, "precios", {}) or {}
    iva_pct = Decimal(str(precios.get("iva_pct", "13")))
    cobrables = modulos_cobrables(conn, empresa_id, periodo)
    catalogo = {m["codigo"]: m for m in conn.execute("select * from public.modulos").fetchall()}
    fundador = bool(c and c["precio_fundador_hasta"] and periodo <= c["precio_fundador_hasta"] and "planilla" in cobrables)
    if fundador:
        moneda = "USD"
    elif c and c["precio_fundador_hasta"] and c["moneda"] == "USD":
        moneda = "CRC"
    else:
        moneda = c["moneda"] if c else "CRC"
    n = empleados_activos(conn, empresa_id, periodo) if "planilla" in cobrables else 0
    lineas: list[dict] = []
    plan = None
    for codigo in cobrables:
        cat = catalogo[codigo]
        if codigo == "planilla":
            if fundador:
                base = Decimal(c["precio_mensual"]) if c["moneda"] == "USD" else Decimal(str(precios.get("fundador_usd", "100")))
                plan = calcular_precio(n, base, 10**9, Decimal(0), iva_pct)
            elif c and c["precio_fundador_hasta"] and c["moneda"] == "USD":
                plan = calcular_precio(n, Decimal(str(precios.get("mensual", "55000"))), c["empleados_incluidos"],
                                       Decimal(c["precio_bloque_10"]), iva_pct)
            elif c:
                plan = calcular_precio(n, Decimal(c["precio_mensual"]), c["empleados_incluidos"],
                                       Decimal(c["precio_bloque_10"]), iva_pct)
            else:
                plan = calcular_precio(n, Decimal(str(precios.get("mensual", cat["precio_mensual"]))),
                                       int(precios.get("empleados_incluidos", cat["incluidos"])),
                                       Decimal(str(precios.get("bloque_10", cat["precio_bloque"]))), iva_pct)
            incluidos = None if fundador else (c["empleados_incluidos"] if c
                                               else int(precios.get("empleados_incluidos", cat["incluidos"])))
            bloque = _q(0 if fundador else (c["precio_bloque_10"] if c else precios.get("bloque_10", cat["precio_bloque"])))
            lineas.append({"modulo": "planilla", "nombre": cat["nombre"], "unidad": "colaboradores", "unidades": n,
                           "incluidos": incluidos, "tam_bloque": 10, "bloques_adicionales": plan["bloques_adicionales"],
                           "base": plan["base"], "precio_bloque": bloque, "adicional": plan["adicional"],
                           "subtotal": plan["subtotal"], "precio_fundador": fundador})
        else:
            propio = ((c or {}).get("precios_modulos") or {}).get(codigo) or {}
            lineas.append({**_precio_modulo(cat, propio, moneda == "USD",
                                            unidades_modulo(conn, empresa_id, cat["unidad"], periodo)),
                           "precio_fundador": False})
    subtotal = _q(sum((l["subtotal"] for l in lineas), Decimal(0)))
    iva = _q(subtotal * iva_pct / 100)
    return {"modulos": lineas, "subtotal": subtotal, "iva": iva, "total": subtotal + iva, "iva_pct": iva_pct,
            "moneda": moneda, "precio_fundador": fundador, "periodo": periodo, "cliente_id": c["id"] if c else None,
            # Datos de Planilla (compatibilidad con pantallas y cobros anteriores)
            "empleados": n, "bloques_adicionales": plan["bloques_adicionales"] if plan else 0,
            "base": plan["base"] if plan else CERO_Q, "adicional": plan["adicional"] if plan else CERO_Q,
            "empleados_incluidos": None if fundador or not plan else lineas[0]["incluidos"]}


def precio_cliente(conn: Conexion, cliente_id, periodo: date) -> dict:
    """Precio del mes para un cliente de negocio.clientes (ver `precio_empresa`)."""
    c = conn.execute("select * from negocio.clientes where id = %s", (cliente_id,)).fetchone()
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    return precio_empresa(conn, c, c["empresa_id"], periodo)


# ============================================================== Borradores ===
def _cabys(conn, clave: str) -> str:
    v = _cfg(conn, clave)
    v = str(v).strip() if v is not None else ""
    if not re.fullmatch(r"\d{13}", v):
        nombre = "la suscripción" if clave == "cabys_suscripcion" else "la implementación"
        raise _conflicto(f"Falta el código CABYS de {nombre} (13 dígitos) en Configuración › {clave}. "
                         "Búsquelo en el catálogo CABYS del BCCR/Hacienda; el sistema no lo inventa.")
    return v


def _tipo_cambio(conn, moneda: str) -> Decimal:
    if moneda == "CRC":
        return Decimal(1)
    tc = _cfg(conn, "tipo_cambio_usd", {}) or {}
    if not tc.get("valor"):
        raise _conflicto("Falta el tipo de cambio del dólar (BCCR) en Configuración › tipo_cambio_usd.")
    return Decimal(str(tc["valor"]))


def detalle_comprobante(conn, comprobante_id) -> dict:
    comp = conn.execute("select * from negocio.comprobantes where id = %s", (comprobante_id,)).fetchone()
    if not comp:
        raise HTTPException(404, "Comprobante no encontrado")
    comp = dict(comp)
    comp["lineas"] = conn.execute("select * from negocio.comprobante_lineas where comprobante_id = %s order by numero",
                                  (comprobante_id,)).fetchall()
    return comp


def _insertar_comprobante(conn, cab: dict, lineas: list[dict]) -> dict:
    sub = sum((_q(l["precio_unitario"]) * Decimal(str(l.get("cantidad", 1))) - _q(l.get("descuento", 0))
               for l in lineas), Decimal(0))
    sub = _q(sub)
    iva = sum((_q((_q(l["precio_unitario"]) * Decimal(str(l.get("cantidad", 1))) - _q(l.get("descuento", 0)))
                  * Decimal(str(l.get("tarifa_iva", 13))) / 100) for l in lineas), Decimal(0))
    comp = conn.execute(
        """insert into negocio.comprobantes (tipo, cliente_id, periodo, condicion_venta, medio_pago, moneda, tipo_cambio,
                                             subtotal, impuesto, total, referencia_id, concepto, empleados_facturados,
                                             mensaje_estado)
           values (%(tipo)s, %(cliente_id)s, %(periodo)s, '01', %(medio_pago)s, %(moneda)s, %(tipo_cambio)s,
                   %(sub)s, %(iva)s, %(total)s, %(referencia_id)s, %(concepto)s, %(empleados)s, %(mensaje)s)
           returning *""",
        {"tipo": cab["tipo"], "cliente_id": cab["cliente_id"], "periodo": cab.get("periodo"),
         "medio_pago": cab.get("medio_pago", "04"), "moneda": cab.get("moneda", "CRC"),
         "tipo_cambio": cab.get("tipo_cambio", 1), "sub": sub, "iva": iva, "total": sub + iva,
         "referencia_id": cab.get("referencia_id"), "concepto": cab.get("concepto", "suscripcion"),
         "empleados": cab.get("empleados"), "mensaje": cab.get("mensaje")}).fetchone()
    for i, l in enumerate(lineas, 1):
        cant = Decimal(str(l.get("cantidad", 1)))
        neto = _q(_q(l["precio_unitario"]) * cant - _q(l.get("descuento", 0)))
        imp = _q(neto * Decimal(str(l.get("tarifa_iva", 13))) / 100)
        conn.execute(
            """insert into negocio.comprobante_lineas (comprobante_id, numero, cabys, descripcion, cantidad, unidad,
                                                       precio_unitario, descuento, tarifa_iva, impuesto, total,
                                                       codigo_tarifa_iva)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (comp["id"], i, l["cabys"], l["descripcion"][:200], cant, l.get("unidad", "Sp"), _q(l["precio_unitario"]),
             _q(l.get("descuento", 0)), Decimal(str(l.get("tarifa_iva", 13))), imp, neto + imp,
             l.get("codigo_tarifa_iva", TARIFA_IVA_GENERAL)))
    return detalle_comprobante(conn, comp["id"])


def crear_factura_suscripcion(conn: Conexion, cliente_id, periodo: date, medio_pago: str = "02") -> dict:
    """Borrador de la factura del mes (idempotente: si ya existe, la devuelve)."""
    periodo = periodo.replace(day=1)
    ya = conn.execute(
        """select id from negocio.comprobantes where cliente_id = %s and periodo = %s and concepto = 'suscripcion'
             and tipo in ('01','04') and estado <> 'anulado'""", (cliente_id, periodo)).fetchone()
    if ya:
        return detalle_comprobante(conn, ya["id"])
    if medio_pago not in MEDIOS_PAGO:
        raise HTTPException(422, f"Medio de pago {medio_pago} no válido")
    cabys = _cabys(conn, "cabys_suscripcion")
    p = precio_cliente(conn, cliente_id, periodo)
    lineas = []
    for m in p["modulos"]:
        if m["modulo"] == "planilla":
            if m["precio_fundador"]:
                lineas.append({"cabys": cabys, "precio_unitario": m["base"],
                               "descripcion": f"Suscripción Planilla Costa Rica {nombre_mes(periodo)} (precio fundador)"})
                continue
            lineas.append({"cabys": cabys, "precio_unitario": m["base"],
                           "descripcion": f"Suscripción Planilla Costa Rica {nombre_mes(periodo)} "
                                          f"(hasta {m['incluidos']} colaboradores)"})
            if m["bloques_adicionales"]:
                lineas.append({"cabys": cabys, "cantidad": m["bloques_adicionales"],
                               "precio_unitario": m["adicional"] / m["bloques_adicionales"],
                               "descripcion": f"Bloque adicional de 10 colaboradores ({m['unidades']} activos)"})
            continue
        lineas.append({"cabys": cabys, "precio_unitario": m["base"],
                       "descripcion": f"Módulo {m['nombre']} {nombre_mes(periodo)} (hasta {m['incluidos']} {m['unidad']})"})
        if m["bloques_adicionales"]:
            lineas.append({"cabys": cabys, "cantidad": m["bloques_adicionales"], "precio_unitario": m["precio_bloque"],
                           "descripcion": f"Bloque adicional de {m['tam_bloque']} {m['unidad']} de {m['nombre']} "
                                          f"({m['unidades']} el mes anterior)"})
    if not lineas:
        raise _conflicto("La empresa no tuvo módulos activos ese mes: no hay nada que facturar")
    return _insertar_comprobante(conn, {"tipo": "01", "cliente_id": cliente_id, "periodo": periodo,
                                        "medio_pago": medio_pago, "moneda": p["moneda"],
                                        "tipo_cambio": _tipo_cambio(conn, p["moneda"]), "concepto": "suscripcion",
                                        "empleados": p["empleados"]}, lineas)


def crear_factura_implementacion(conn: Conexion, cliente_id, medio_pago: str = "04") -> dict:
    c = conn.execute("select * from negocio.clientes where id = %s", (cliente_id,)).fetchone()
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    ya = conn.execute("""select id from negocio.comprobantes where cliente_id = %s and concepto = 'implementacion'
                           and tipo in ('01','04') and estado <> 'anulado'""", (cliente_id,)).fetchone()
    if ya:
        return detalle_comprobante(conn, ya["id"])
    cabys = _cabys(conn, "cabys_implementacion")
    return _insertar_comprobante(conn, {"tipo": "01", "cliente_id": cliente_id, "periodo": hoy_cr().replace(day=1),
                                        "medio_pago": medio_pago, "moneda": "CRC", "concepto": "implementacion"},
                                 [{"cabys": cabys, "precio_unitario": c["implementacion"],
                                   "descripcion": "Implementación de Planilla Costa Rica (configuración, migración y capacitación)"}])


def crear_nota_credito(conn: Conexion, comprobante_id, razon: str, monto: Decimal | None = None) -> dict:
    """Nota de crédito (tipo 03) que anula total o parcialmente una factura aceptada."""
    orig = detalle_comprobante(conn, comprobante_id)
    if orig["tipo"] not in ("01", "04") or orig["estado"] != "aceptado":
        raise _conflicto("Solo se puede hacer nota de crédito de una factura o tiquete aceptado por Hacienda")
    if monto is None or _q(monto) >= orig["subtotal"]:
        lineas = [{"cabys": l["cabys"], "descripcion": l["descripcion"], "cantidad": l["cantidad"],
                   "precio_unitario": l["precio_unitario"], "descuento": l["descuento"], "tarifa_iva": l["tarifa_iva"],
                   "unidad": l["unidad"]} for l in orig["lineas"]]
    else:
        l0 = orig["lineas"][0]
        lineas = [{"cabys": l0["cabys"], "descripcion": f"Ajuste: {razon}"[:200], "precio_unitario": _q(monto),
                   "tarifa_iva": l0["tarifa_iva"], "unidad": l0["unidad"]}]
    return _insertar_comprobante(conn, {"tipo": "03", "cliente_id": orig["cliente_id"], "periodo": orig["periodo"],
                                        "medio_pago": orig["medio_pago"], "moneda": orig["moneda"],
                                        "tipo_cambio": orig["tipo_cambio"], "referencia_id": orig["id"],
                                        "concepto": orig["concepto"], "mensaje": razon[:180]}, lineas)


# ======================================================== Emisor y claves ===
CAMPOS_EMISOR = {"nombre": "nombre o razón social", "tipo_identificacion": "tipo de identificación",
                 "identificacion": "número de identificación", "correo": "correo electrónico",
                 "provincia": "provincia", "canton": "cantón", "distrito": "distrito", "otras_senas": "otras señas",
                 "codigo_actividad": "código de actividad económica (6 dígitos)", "sucursal": "sucursal",
                 "terminal": "terminal"}


def validar_emisor(conn) -> dict:
    e = _cfg(conn, "emisor", {}) or {}
    faltan = [txt for k, txt in CAMPOS_EMISOR.items() if not str(e.get(k) or "").strip()]
    if faltan:
        raise _conflicto("No se puede emitir: faltan datos del emisor en Configuración › emisor: " + ", ".join(faltan) + ".")
    if not re.fullmatch(r"\d{6}", str(e["codigo_actividad"]).strip()):
        raise _conflicto("El código de actividad del emisor debe tener 6 dígitos (v4.4).")
    if not re.fullmatch(r"\d{3}", str(e["sucursal"])) or not re.fullmatch(r"\d{5}", str(e["terminal"])):
        raise _conflicto("La sucursal debe tener 3 dígitos y la terminal 5 (ej. 001 y 00001).")
    return e


def consecutivo(sucursal: str, terminal: str, tipo: str, numero: int) -> str:
    """20 dígitos: sucursal (3) + terminal (5) + tipo (2) + número (10)."""
    s = f"{int(sucursal):03d}{int(terminal):05d}{tipo}{int(numero):010d}"
    assert len(s) == 20
    return s


def clave_numerica(fecha: date, identificacion: str, consecutivo_20: str, situacion: str = "1",
                   codigo_seguridad: str | None = None) -> str:
    """50 dígitos: 506 + DDMMAA + identificación (12, ceros a la izquierda) + consecutivo (20)
    + situación (1) + código de seguridad (8)."""
    ident = _digitos(identificacion)
    if not ident or len(ident) > 12:
        raise ValueError("Identificación del emisor inválida para la clave")
    cod = codigo_seguridad or f"{secrets.randbelow(10**8):08d}"
    clave = f"506{fecha:%d%m%y}{ident.zfill(12)}{consecutivo_20}{situacion}{cod}"
    if not re.fullmatch(r"\d{50}", clave):
        raise ValueError("Clave numérica mal formada")
    return clave


# =================================================================== XML ====
def _sub(padre, ns: str, tag: str, texto: Any = None, **attrs):
    el = etree.SubElement(padre, f"{{{ns}}}{tag}", **attrs)
    if texto is not None:
        el.text = str(texto)
    return el


def _telefono(padre, ns, numero: str | None):
    num = _digitos(numero)
    if len(num) >= 8:
        t = _sub(padre, ns, "Telefono")
        _sub(t, ns, "CodigoPais", "506")
        _sub(t, ns, "NumTelefono", num[-8:] if len(num) == 11 and num.startswith("506") else num)


def construir_xml(comp: dict, emisor: dict, receptor: dict | None, *, proveedor_sistemas: str,
                  referencia: dict | None = None) -> bytes:
    """XML sin firmar del comprobante (v4.4), en el orden del XSD oficial."""
    raiz, ns = TIPOS[comp["tipo"]]
    doc = etree.Element(f"{{{ns}}}{raiz}", nsmap={None: ns, "ds": DS,
                                                   "xsd": "http://www.w3.org/2001/XMLSchema",
                                                   "xsi": "http://www.w3.org/2001/XMLSchema-instance"})
    fecha: datetime = comp["fecha"].astimezone(CR)
    _sub(doc, ns, "Clave", comp["clave"])
    _sub(doc, ns, "ProveedorSistemas", proveedor_sistemas[:20])
    _sub(doc, ns, "CodigoActividadEmisor", str(emisor["codigo_actividad"]).strip())
    if receptor and receptor.get("actividad_receptor") and comp["tipo"] != "04":
        _sub(doc, ns, "CodigoActividadReceptor", _digitos(receptor["actividad_receptor"])[:6].zfill(6))
    _sub(doc, ns, "NumeroConsecutivo", comp["consecutivo"])
    _sub(doc, ns, "FechaEmision", fecha.isoformat(timespec="seconds"))

    em = _sub(doc, ns, "Emisor")
    _sub(em, ns, "Nombre", emisor["nombre"][:100])
    ide = _sub(em, ns, "Identificacion")
    _sub(ide, ns, "Tipo", emisor["tipo_identificacion"])
    _sub(ide, ns, "Numero", _digitos(emisor["identificacion"]))
    if emisor.get("nombre_comercial"):
        _sub(em, ns, "NombreComercial", emisor["nombre_comercial"][:80])
    ub = _sub(em, ns, "Ubicacion")
    _sub(ub, ns, "Provincia", int(emisor["provincia"]))
    _sub(ub, ns, "Canton", f"{int(emisor['canton']):02d}")
    _sub(ub, ns, "Distrito", f"{int(emisor['distrito']):02d}")
    if emisor.get("barrio"):
        _sub(ub, ns, "Barrio", emisor["barrio"][:50])
    _sub(ub, ns, "OtrasSenas", emisor["otras_senas"][:250])
    _telefono(em, ns, emisor.get("telefono"))
    _sub(em, ns, "CorreoElectronico", emisor["correo"])

    if receptor:
        rc = _sub(doc, ns, "Receptor")
        _sub(rc, ns, "Nombre", receptor["razon_social"][:100])
        ide = _sub(rc, ns, "Identificacion")
        _sub(ide, ns, "Tipo", receptor["tipo_identificacion"])
        _sub(ide, ns, "Numero", _digitos(receptor["identificacion"]))
        _telefono(rc, ns, receptor.get("telefono"))
        if receptor.get("correo_facturacion"):
            _sub(rc, ns, "CorreoElectronico", receptor["correo_facturacion"][:160])

    _sub(doc, ns, "CondicionVenta", comp["condicion_venta"])
    det = _sub(doc, ns, "DetalleServicio")
    total_serv_grav = total_venta = total_imp = Decimal(0)
    desglose: dict[str, Decimal] = {}
    for l in comp["lineas"]:
        cant = Decimal(l["cantidad"])
        monto_total = _q(Decimal(l["precio_unitario"]) * cant)
        subtotal = monto_total - Decimal(l["descuento"])
        imp = Decimal(l["impuesto"])
        ld = _sub(det, ns, "LineaDetalle")
        _sub(ld, ns, "NumeroLinea", l["numero"])
        _sub(ld, ns, "CodigoCABYS", l["cabys"])
        _sub(ld, ns, "Cantidad", f"{cant:.3f}")
        _sub(ld, ns, "UnidadMedida", l["unidad"])
        _sub(ld, ns, "Detalle", l["descripcion"])
        _sub(ld, ns, "PrecioUnitario", _f5(l["precio_unitario"]))
        _sub(ld, ns, "MontoTotal", _f5(monto_total))
        if Decimal(l["descuento"]):
            d = _sub(ld, ns, "Descuento")
            _sub(d, ns, "MontoDescuento", _f5(l["descuento"]))
            _sub(d, ns, "CodigoDescuento", "99")
            _sub(d, ns, "CodigoDescuentoOTRO", "Descuento comercial")
        _sub(ld, ns, "SubTotal", _f5(subtotal))
        _sub(ld, ns, "BaseImponible", _f5(subtotal))
        im = _sub(ld, ns, "Impuesto")
        _sub(im, ns, "Codigo", "01")
        _sub(im, ns, "CodigoTarifaIVA", l.get("codigo_tarifa_iva") or TARIFA_IVA_GENERAL)
        _sub(im, ns, "Tarifa", f"{Decimal(l['tarifa_iva']):.2f}")
        _sub(im, ns, "Monto", _f5(imp))
        _sub(ld, ns, "ImpuestoAsumidoEmisorFabrica", _f5(0))
        _sub(ld, ns, "ImpuestoNeto", _f5(imp))
        _sub(ld, ns, "MontoTotalLinea", _f5(subtotal + imp))
        total_serv_grav += subtotal
        total_venta += monto_total
        total_imp += imp
        k = l.get("codigo_tarifa_iva") or TARIFA_IVA_GENERAL
        desglose[k] = desglose.get(k, Decimal(0)) + imp

    rs = _sub(doc, ns, "ResumenFactura")
    mon = _sub(rs, ns, "CodigoTipoMoneda")
    _sub(mon, ns, "CodigoMoneda", comp["moneda"])
    _sub(mon, ns, "TipoCambio", _f5(comp["tipo_cambio"]))
    _sub(rs, ns, "TotalServGravados", _f5(total_venta))
    _sub(rs, ns, "TotalGravado", _f5(total_venta))
    _sub(rs, ns, "TotalVenta", _f5(total_venta))
    descuentos = total_venta - total_serv_grav
    if descuentos:
        _sub(rs, ns, "TotalDescuentos", _f5(descuentos))
    _sub(rs, ns, "TotalVentaNeta", _f5(total_serv_grav))
    for cod, monto in desglose.items():
        tdi = _sub(rs, ns, "TotalDesgloseImpuesto")
        _sub(tdi, ns, "Codigo", "01")
        _sub(tdi, ns, "CodigoTarifaIVA", cod)
        _sub(tdi, ns, "TotalMontoImpuesto", _f5(monto))
    _sub(rs, ns, "TotalImpuesto", _f5(total_imp))
    if comp["tipo"] != "03":
        mp = _sub(rs, ns, "MedioPago")
        _sub(mp, ns, "TipoMedioPago", comp["medio_pago"])
        _sub(mp, ns, "TotalMedioPago", _f5(total_serv_grav + total_imp))
    _sub(rs, ns, "TotalComprobante", _f5(total_serv_grav + total_imp))

    if referencia:
        ir = _sub(doc, ns, "InformacionReferencia")
        _sub(ir, ns, "TipoDocIR", referencia["tipo"])
        _sub(ir, ns, "Numero", referencia["clave"])
        _sub(ir, ns, "FechaEmisionIR", referencia["fecha"].astimezone(CR).isoformat(timespec="seconds"))
        _sub(ir, ns, "Codigo", referencia.get("codigo", "01"))
        _sub(ir, ns, "Razon", (referencia.get("razon") or "Anulación del comprobante")[:180])
    return etree.tostring(doc, xml_declaration=True, encoding="UTF-8")


# ================================================================ Firma ======
def _c14n(el) -> bytes:
    return etree.tostring(el, method="c14n", exclusive=False, with_comments=False)


def _b64sha256(datos: bytes) -> str:
    return base64.b64encode(hashlib.sha256(datos).digest()).decode()


def _b64int(n: int) -> str:
    return base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode()


def cargar_p12(p12: bytes, pin: str):
    try:
        llave, cert, _ = pkcs12.load_key_and_certificates(p12, pin.encode() if pin else None)
    except ValueError as exc:
        raise _conflicto("No se pudo abrir la llave criptográfica (.p12): revise el PIN.") from exc
    if llave is None or cert is None or not isinstance(llave, rsa.RSAPrivateKey):
        raise _conflicto("La llave criptográfica no contiene una llave RSA y su certificado.")
    return llave, cert


def firmar_xades_epes(xml: bytes, p12: bytes, pin: str, politica: dict, ahora: datetime | None = None) -> bytes:
    """Firma enveloped XAdES-EPES (XMLDSig + QualifyingProperties v1.3.2) con RSA-SHA256.

    Estructura (la que acepta Hacienda según implementaciones en uso):
      ds:Signature
        ds:SignedInfo  (C14N 1.0 inclusivo, rsa-sha256)
          Reference URI=""                  documento, transform enveloped-signature, sha256
          Reference URI="#KeyInfo-…"        llave pública
          Reference Type=…#SignedProperties URI="#SignedProperties-…"
        ds:SignatureValue
        ds:KeyInfo (X509Certificate + RSAKeyValue)
        ds:Object/xades:QualifyingProperties/xades:SignedProperties
          SigningTime, SigningCertificate (sha256 del certificado + emisor/serie),
          SignaturePolicyIdentifier (URL de la resolución + hash)   <- lo que la hace EPES
          SignerRole, DataObjectFormat (text/xml)
    Aislada aquí para poder ajustarla tras las pruebas en el ambiente de Hacienda.
    """
    llave, cert = cargar_p12(p12, pin)
    ahora = (ahora or datetime.now(CR)).astimezone(CR).replace(microsecond=0)
    doc = etree.fromstring(xml, etree.XMLParser(remove_blank_text=True))
    digest_doc = _b64sha256(_c14n(doc))  # = transform enveloped (la firma aún no existe) + C14N

    uid = uuid.uuid4().hex
    sid, ref_id, ki_id, sp_id, sv_id = (f"Signature-{uid}", f"Reference-{uid}", f"KeyInfo-{uid}",
                                       f"SignedProperties-{uid}", f"SignatureValue-{uid}")
    d = lambda tag: f"{{{DS}}}{tag}"  # noqa: E731
    x = lambda tag: f"{{{XADES}}}{tag}"  # noqa: E731

    sig = etree.SubElement(doc, d("Signature"), Id=sid, nsmap={"ds": DS})
    si = etree.SubElement(sig, d("SignedInfo"))
    etree.SubElement(si, d("CanonicalizationMethod"), Algorithm=C14N)
    etree.SubElement(si, d("SignatureMethod"), Algorithm=RSA_SHA256)

    def referencia(uri: str, valor: str | None, *, id_=None, tipo=None, enveloped=False):
        attrs = {"URI": uri}
        if id_:
            attrs = {"Id": id_, **attrs}
        if tipo:
            attrs["Type"] = tipo
        ref = etree.SubElement(si, d("Reference"), **attrs)
        if enveloped:
            trs = etree.SubElement(ref, d("Transforms"))
            etree.SubElement(trs, d("Transform"), Algorithm=ENVELOPED)
        etree.SubElement(ref, d("DigestMethod"), Algorithm=SHA256)
        dv = etree.SubElement(ref, d("DigestValue"))
        dv.text = valor
        return dv

    referencia("", digest_doc, id_=ref_id, enveloped=True)
    dv_ki = referencia(f"#{ki_id}", None)
    dv_sp = referencia(f"#{sp_id}", None, tipo=TIPO_SIGNED_PROPS)
    sv = etree.SubElement(sig, d("SignatureValue"), Id=sv_id)

    ki = etree.SubElement(sig, d("KeyInfo"), Id=ki_id)
    xd = etree.SubElement(ki, d("X509Data"))
    etree.SubElement(xd, d("X509Certificate")).text = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    kv = etree.SubElement(etree.SubElement(ki, d("KeyValue")), d("RSAKeyValue"))
    pub = llave.public_key().public_numbers()
    etree.SubElement(kv, d("Modulus")).text = _b64int(pub.n)
    etree.SubElement(kv, d("Exponent")).text = _b64int(pub.e)

    obj = etree.SubElement(sig, d("Object"))
    qp = etree.SubElement(obj, x("QualifyingProperties"), Target=f"#{sid}", nsmap={"xades": XADES})
    sp = etree.SubElement(qp, x("SignedProperties"), Id=sp_id)
    ssp = etree.SubElement(sp, x("SignedSignatureProperties"))
    etree.SubElement(ssp, x("SigningTime")).text = ahora.isoformat()
    cer = etree.SubElement(etree.SubElement(ssp, x("SigningCertificate")), x("Cert"))
    cd = etree.SubElement(cer, x("CertDigest"))
    etree.SubElement(cd, d("DigestMethod"), Algorithm=SHA256)
    etree.SubElement(cd, d("DigestValue")).text = _b64sha256(cert.public_bytes(serialization.Encoding.DER))
    iss = etree.SubElement(cer, x("IssuerSerial"))
    etree.SubElement(iss, d("X509IssuerName")).text = cert.issuer.rfc4514_string()
    etree.SubElement(iss, d("X509SerialNumber")).text = str(cert.serial_number)
    spi = etree.SubElement(etree.SubElement(ssp, x("SignaturePolicyIdentifier")), x("SignaturePolicyId"))
    spid = etree.SubElement(spi, x("SigPolicyId"))
    etree.SubElement(spid, x("Identifier")).text = politica["url"]
    etree.SubElement(spid, x("Description"))
    sph = etree.SubElement(spi, x("SigPolicyHash"))
    etree.SubElement(sph, d("DigestMethod"), Algorithm=SHA256)
    etree.SubElement(sph, d("DigestValue")).text = politica["digest"]
    roles = etree.SubElement(etree.SubElement(ssp, x("SignerRole")), x("ClaimedRoles"))
    etree.SubElement(roles, x("ClaimedRole")).text = "Emisor"
    sdop = etree.SubElement(sp, x("SignedDataObjectProperties"))
    dof = etree.SubElement(sdop, x("DataObjectFormat"), ObjectReference=f"#{ref_id}")
    etree.SubElement(dof, x("MimeType")).text = "text/xml"
    etree.SubElement(dof, x("Encoding")).text = "UTF-8"

    dv_ki.text = _b64sha256(_c14n(ki))
    dv_sp.text = _b64sha256(_c14n(sp))
    firma = llave.sign(_c14n(si), padding.PKCS1v15(), hashes.SHA256())
    sv.text = base64.b64encode(firma).decode()
    return etree.tostring(doc, xml_declaration=True, encoding="UTF-8")


def verificar_firma(xml_firmado: bytes) -> bool:
    """Verificación local (digests + firma). No sustituye la validación de Hacienda."""
    doc = etree.fromstring(xml_firmado)
    sig = doc.find(f"{{{DS}}}Signature")
    if sig is None:
        return False
    si = sig.find(f"{{{DS}}}SignedInfo")
    cert_b64 = sig.findtext(f".//{{{DS}}}X509Certificate")
    cert = x509.load_der_x509_certificate(base64.b64decode(cert_b64))
    por_id = {el.get("Id"): el for el in doc.iter() if el.get("Id")}
    for ref in si.findall(f"{{{DS}}}Reference"):
        uri = ref.get("URI")
        if uri == "":
            copia = etree.fromstring(etree.tostring(doc))
            copia.remove(copia.find(f"{{{DS}}}Signature"))
            datos = _c14n(copia)
        else:
            datos = _c14n(por_id[uri[1:]])
        if _b64sha256(datos) != ref.findtext(f"{{{DS}}}DigestValue"):
            return False
    try:
        cert.public_key().verify(base64.b64decode(sig.findtext(f"{{{DS}}}SignatureValue")), _c14n(si),
                                 padding.PKCS1v15(), hashes.SHA256())
    except Exception:  # noqa: BLE001
        return False
    return True


def politica_firma(conn) -> dict:
    pol = dict(_cfg(conn, "firma_politica", {}) or {})
    if not pol.get("url"):
        raise _conflicto("Falta la URL de la política de firma en Configuración › firma_politica.")
    if not pol.get("digest"):
        try:
            with cliente_http() as h:
                r = h.get(pol["url"], follow_redirects=True)
                r.raise_for_status()
        except httpx.HTTPError as exc:
            raise _conflicto("No se pudo descargar la política de firma para calcular su hash; "
                             "configure firma_politica.digest (SHA-256 en base64).") from exc
        pol["digest"] = _b64sha256(r.content)
        _guardar_cfg(conn, "firma_politica", pol)
    return pol


# ============================================================ Hacienda API ===
class ErrorHacienda(RuntimeError):
    pass


_tokens: dict[tuple[str, str], tuple[str, float]] = {}


def _ambiente() -> str:
    a = config.settings.hacienda_ambiente
    return a if a in HACIENDA_IDP else "pruebas"


def credenciales() -> tuple[str, str, bytes, str]:
    s = config.settings
    if not (s.hacienda_usuario and s.hacienda_clave and s.hacienda_p12_base64):
        raise _conflicto("Faltan las credenciales de Hacienda (HACIENDA_USUARIO, HACIENDA_CLAVE, HACIENDA_P12_BASE64).")
    try:
        p12 = base64.b64decode(s.hacienda_p12_base64)
    except ValueError as exc:
        raise _conflicto("HACIENDA_P12_BASE64 no es base64 válido.") from exc
    return s.hacienda_usuario, s.hacienda_clave, p12, s.hacienda_p12_pin


def token_hacienda() -> str:
    """OAuth2 password grant contra el IDP (se reutiliza hasta 30 s antes de vencer)."""
    usuario, clave, _, _ = credenciales()
    amb = _ambiente()
    cache = _tokens.get((amb, usuario))
    if cache and cache[1] > time.time() + 30:
        return cache[0]
    url, client_id = HACIENDA_IDP[amb]
    try:
        with cliente_http() as h:
            r = h.post(url, data={"grant_type": "password", "client_id": client_id, "username": usuario,
                                  "password": clave})
    except httpx.HTTPError as exc:
        raise ErrorHacienda(f"IDP de Hacienda no disponible: {exc}") from exc
    if r.status_code != 200:
        raise ErrorHacienda(f"IDP de Hacienda rechazó las credenciales ({r.status_code}): {r.text[:200]}")
    j = r.json()
    _tokens[(amb, usuario)] = (j["access_token"], time.time() + int(j.get("expires_in", 300)))
    return j["access_token"]


def enviar_hacienda(comp: dict, xml_firmado: bytes, emisor: dict, receptor: dict | None) -> dict:
    cuerpo: dict[str, Any] = {
        "clave": comp["clave"],
        "fecha": comp["fecha"].astimezone(CR).isoformat(timespec="seconds"),
        "emisor": {"tipoIdentificacion": emisor["tipo_identificacion"], "numeroIdentificacion": _digitos(emisor["identificacion"])},
        "comprobanteXml": base64.b64encode(xml_firmado).decode(),
    }
    if receptor:
        cuerpo["receptor"] = {"tipoIdentificacion": receptor["tipo_identificacion"],
                              "numeroIdentificacion": _digitos(receptor["identificacion"])}
    if config.settings.hacienda_callback_url:
        cuerpo["callbackUrl"] = config.settings.hacienda_callback_url
    tok = token_hacienda()
    try:
        with cliente_http() as h:
            r = h.post(HACIENDA_RECEPCION[_ambiente()] + "recepcion", json=cuerpo,
                       headers={"Authorization": f"Bearer {tok}"})
    except httpx.HTTPError as exc:
        raise ErrorHacienda(f"Hacienda no disponible: {exc}") from exc
    causa = r.headers.get("X-Error-Cause", "")
    if r.status_code in (200, 201, 202):
        return {"recibido": True, "codigo": r.status_code}
    if r.status_code == 400 and "recibido" in causa.lower():
        return {"recibido": True, "codigo": r.status_code, "nota": causa}   # ya estaba en Hacienda
    raise ErrorHacienda(f"Hacienda respondió {r.status_code}: {causa or r.text[:300]}")


def estado_hacienda(clave: str) -> dict:
    tok = token_hacienda()
    try:
        with cliente_http() as h:
            r = h.get(HACIENDA_RECEPCION[_ambiente()] + f"recepcion/{clave}", headers={"Authorization": f"Bearer {tok}"})
    except httpx.HTTPError as exc:
        raise ErrorHacienda(f"Hacienda no disponible: {exc}") from exc
    if r.status_code == 404:
        return {"ind-estado": "no_encontrado"}
    if r.status_code != 200:
        raise ErrorHacienda(f"Consulta de estado {r.status_code}: {r.headers.get('X-Error-Cause') or r.text[:200]}")
    return r.json()


# ===================================================== Emitir / consultar ===
def _proveedor_sistemas(conn, emisor: dict) -> str:
    v = _cfg(conn, "proveedor_sistemas")
    return _digitos(str(v)) if v else _digitos(emisor["identificacion"])


def _receptor(conn, comp) -> dict | None:
    if not comp["cliente_id"]:
        return None
    return conn.execute("select * from negocio.clientes where id = %s", (comp["cliente_id"],)).fetchone()


def resumen_comprobante(c: dict) -> dict:
    return {k: c.get(k) for k in ("id", "tipo", "clave", "consecutivo", "fecha", "cliente_id", "periodo", "moneda",
                                  "subtotal", "impuesto", "total", "estado", "mensaje_estado", "concepto",
                                  "enviado_por_correo_en", "referencia_id", "medio_pago")}


def emitir(conn: Conexion, comprobante_id) -> dict:
    """Asigna consecutivo y clave, arma el XML 4.4, firma, envía a Hacienda y deja el estado."""
    comp = conn.execute("select * from negocio.comprobantes where id = %s for update", (comprobante_id,)).fetchone()
    if not comp:
        raise HTTPException(404, "Comprobante no encontrado")
    if comp["estado"] in ("enviado", "aceptado"):
        return resumen_comprobante(comp)
    if comp["estado"] in ("anulado", "rechazado"):
        raise _conflicto(f"El comprobante está {comp['estado']}; haga uno nuevo.")
    emisor = validar_emisor(conn)
    _, _, p12, pin = credenciales()
    cargar_p12(p12, pin)
    pol = politica_firma(conn)
    comp = detalle_comprobante(conn, comprobante_id)
    for l in comp["lineas"]:
        if not re.fullmatch(r"\d{13}", l["cabys"] or ""):
            raise _conflicto(f"La línea {l['numero']} no tiene un código CABYS válido.")
    receptor = _receptor(conn, comp)
    if comp["tipo"] == "01" and not receptor:
        raise _conflicto("La factura electrónica requiere un receptor identificado.")
    referencia = None
    if comp["tipo"] == "03":
        orig = conn.execute("select * from negocio.comprobantes where id = %s", (comp["referencia_id"],)).fetchone()
        if not orig or not orig["clave"]:
            raise _conflicto("La nota de crédito debe referirse a un comprobante emitido.")
        referencia = {"tipo": orig["tipo"], "clave": orig["clave"], "fecha": orig["fecha"], "codigo": "01",
                      "razon": comp.get("mensaje_estado") or "Anulación"}

    if not comp["clave"]:
        n = conn.execute("select negocio.siguiente_consecutivo(%s, %s, %s) n",
                         (f"{int(emisor['sucursal']):03d}", f"{int(emisor['terminal']):05d}", comp["tipo"])).fetchone()["n"]
        ahora = datetime.now(CR).replace(microsecond=0)
        cons = consecutivo(emisor["sucursal"], emisor["terminal"], comp["tipo"], n)
        cod = f"{secrets.randbelow(10**8):08d}"
        clave = clave_numerica(ahora.date(), emisor["identificacion"], cons, comp["situacion"], cod)
        conn.execute("update negocio.comprobantes set clave = %s, consecutivo = %s, codigo_seguridad = %s, fecha = %s "
                     "where id = %s", (clave, cons, cod, ahora, comprobante_id))
        comp.update(clave=clave, consecutivo=cons, codigo_seguridad=cod, fecha=ahora)

    xml = construir_xml(comp, emisor, receptor, proveedor_sistemas=_proveedor_sistemas(conn, emisor), referencia=referencia)
    firmado = firmar_xades_epes(xml, p12, pin, pol)
    conn.execute("update negocio.comprobantes set xml_firmado = %s, estado = 'firmado', mensaje_estado = null where id = %s",
                 (firmado.decode(), comprobante_id))
    try:
        res = enviar_hacienda(comp, firmado, emisor, receptor)
        conn.execute("""update negocio.comprobantes set estado = 'enviado', enviado_hacienda_en = now(),
                               intentos_envio = intentos_envio + 1, mensaje_estado = %s where id = %s""",
                     (res.get("nota") or "Recibido por Hacienda; esperando validación", comprobante_id))
    except ErrorHacienda as exc:
        conn.execute("""update negocio.comprobantes set estado = 'error', intentos_envio = intentos_envio + 1,
                               mensaje_estado = %s where id = %s""", (str(exc)[:500], comprobante_id))
        alertas.alertar(conn, "factura", f"No se pudo enviar el comprobante {comp['consecutivo']} a Hacienda: {exc}", "aviso")
    return resumen_comprobante(conn.execute("select * from negocio.comprobantes where id = %s", (comprobante_id,)).fetchone())


ESTADOS_HACIENDA = {"aceptado": "aceptado", "rechazado": "rechazado", "procesando": "enviado", "recibido": "enviado",
                    "error": "error", "no_encontrado": "error"}


def _detalle_mensaje(xml_resp: str | None) -> str | None:
    if not xml_resp:
        return None
    try:
        doc = etree.fromstring(xml_resp.encode(), PARSER_SEGURO)
        return (doc.xpath("string(//*[local-name()='DetalleMensaje'])") or None)
    except etree.XMLSyntaxError:
        return None


def consultar(conn: Conexion, comprobante_id, *, enviar_correo_si_acepta: bool = True) -> dict:
    comp = conn.execute("select * from negocio.comprobantes where id = %s for update", (comprobante_id,)).fetchone()
    if not comp:
        raise HTTPException(404, "Comprobante no encontrado")
    if not comp["clave"] or comp["estado"] in ("borrador", "firmado"):
        raise _conflicto("El comprobante aún no se ha enviado a Hacienda.")
    try:
        j = estado_hacienda(comp["clave"])
    except ErrorHacienda as exc:
        return {**resumen_comprobante(comp), "consulta": str(exc)}
    ind = str(j.get("ind-estado", "")).lower()
    nuevo = ESTADOS_HACIENDA.get(ind, comp["estado"])
    xml_resp = None
    if j.get("respuesta-xml"):
        try:
            xml_resp = base64.b64decode(j["respuesta-xml"]).decode("utf-8", "replace")
        except ValueError:
            xml_resp = None
    detalle = _detalle_mensaje(xml_resp)
    if nuevo == "error" and comp["estado"] == "enviado" and ind == "no_encontrado":
        nuevo = "enviado"  # todavía no aparece; se vuelve a consultar
    conn.execute("""update negocio.comprobantes set estado = %s, respuesta_hacienda = %s,
                           xml_respuesta = coalesce(%s, xml_respuesta), mensaje_estado = coalesce(%s, mensaje_estado)
                     where id = %s""",
                 (nuevo, json.dumps({k: v for k, v in j.items() if k != "respuesta-xml"}), xml_resp,
                  (detalle or "")[:1000] or None, comprobante_id))
    if nuevo == "rechazado" and comp["estado"] != "rechazado":
        alertas.alertar(conn, "factura", f"Hacienda RECHAZÓ el comprobante {comp['consecutivo']}: {detalle or ind}", "critica")
    if nuevo == "aceptado" and comp["tipo"] == "03" and comp["referencia_id"]:
        orig = conn.execute("select total from negocio.comprobantes where id = %s", (comp["referencia_id"],)).fetchone()
        if orig and comp["total"] >= orig["total"]:
            conn.execute("update negocio.comprobantes set estado = 'anulado' where id = %s", (comp["referencia_id"],))
    final = conn.execute("select * from negocio.comprobantes where id = %s", (comprobante_id,)).fetchone()
    if nuevo == "aceptado" and enviar_correo_si_acepta and not final["enviado_por_correo_en"]:
        try:
            enviar_correo(conn, comprobante_id)
        except Exception as exc:  # noqa: BLE001 - el correo no cambia el estado fiscal
            log.warning("Correo del comprobante %s no enviado: %s", comprobante_id, exc)
            conn.execute("update negocio.comprobantes set mensaje_estado = %s where id = %s",
                         (f"Aceptado. Correo no enviado: {exc}"[:500], comprobante_id))
        final = conn.execute("select * from negocio.comprobantes where id = %s", (comprobante_id,)).fetchone()
    return resumen_comprobante(final)


# ============================================================ PDF y correo ===
def _moneda(v, moneda: str) -> str:
    return pdf.colones(v) if moneda == "CRC" else f"US${Decimal(str(v)):,.2f}"


def pdf_comprobante(conn, comprobante_id) -> bytes:
    comp = detalle_comprobante(conn, comprobante_id)
    emisor = _cfg(conn, "emisor", {}) or {}
    cli = _receptor(conn, comp) or {}
    m = comp["moneda"]
    datos = [["Dato", "Valor"],
             ["Emisor", f"{emisor.get('nombre') or ''} · {emisor.get('identificacion') or ''}"],
             ["Receptor", f"{cli.get('razon_social', '')} · {cli.get('identificacion', '')}"],
             ["Clave", comp["clave"] or "(borrador)"], ["Consecutivo", comp["consecutivo"] or "—"],
             ["Fecha", comp["fecha"].astimezone(CR).strftime("%d/%m/%Y %H:%M")],
             ["Medio de pago", MEDIOS_PAGO.get(comp["medio_pago"], comp["medio_pago"])],
             ["Estado ante Hacienda", comp["estado"]]]
    lineas = [["Detalle", "Cantidad", "Precio", "Total"]] + [
        [l["descripcion"], f"{Decimal(l['cantidad']):g}", _moneda(l["precio_unitario"], m), _moneda(l["total"], m)]
        for l in comp["lineas"]]
    tot = [["Concepto", "Monto"], ["Subtotal", _moneda(comp["subtotal"], m)], ["IVA 13 %", _moneda(comp["impuesto"], m)],
           ["Total", _moneda(comp["total"], m)]]
    if m != "CRC":
        tot.insert(-1, ["Tipo de cambio", f"{Decimal(comp['tipo_cambio']):,.2f}"])
    return pdf.documento_simple(TIPO_NOMBRE.get(comp["tipo"], "Comprobante electrónico"),
                                f"{emisor.get('nombre_comercial') or 'A.U.R.O.M.'} · Versión 4.4",
                                [("", datos), ("Detalle", lineas), ("Totales", tot)],
                                "Comprobante electrónico autorizado mediante la resolución vigente de la DGT. "
                                "Generado con Planilla Costa Rica · auromtec.com")


def enviar_correo(conn, comprobante_id) -> str:
    comp = detalle_comprobante(conn, comprobante_id)
    cli = _receptor(conn, comp)
    if not cli or not cli["correo_facturacion"]:
        raise _conflicto("El cliente no tiene correo de facturación.")
    adj = [(f"{comp['clave'] or comp['id']}.pdf", pdf_comprobante(conn, comprobante_id))]
    if comp["xml_firmado"]:
        adj.append((f"{comp['clave']}.xml", comp["xml_firmado"].encode()))
    if comp.get("xml_respuesta"):
        adj.append((f"{comp['clave']}_respuesta.xml", comp["xml_respuesta"].encode()))
    nombre = TIPO_NOMBRE.get(comp["tipo"], "Comprobante")
    mid = correo.enviar(cli["correo_facturacion"], f"{nombre} {comp['consecutivo'] or ''} · A.U.R.O.M.",
                        f"<p>Estimado cliente {cli['razon_social']}:</p><p>Adjuntamos su {nombre.lower()} "
                        f"por {_moneda(comp['total'], comp['moneda'])}"
                        f"{' del periodo ' + nombre_mes(comp['periodo']) if comp.get('periodo') else ''}.</p>"
                        "<p>Gracias por confiar en Planilla Costa Rica.</p>", adj)
    conn.execute("update negocio.comprobantes set enviado_por_correo_en = now() where id = %s", (comprobante_id,))
    return mid


# ================================================ Comprobantes recibidos ===
def parsear_xml_recibido(xml: bytes) -> dict:
    """Extrae los datos de un comprobante recibido (v4.3 o v4.4) sin depender del namespace."""
    try:
        doc = etree.fromstring(xml, PARSER_SEGURO_SIN_BLANCOS)
    except etree.XMLSyntaxError as exc:
        raise HTTPException(422, f"El archivo no es un XML válido: {exc}") from exc
    raiz = etree.QName(doc).localname
    if raiz not in TIPO_POR_RAIZ:
        raise HTTPException(422, f"El XML no es un comprobante electrónico ({raiz}).")
    ns = etree.QName(doc).namespace or ""
    ver = re.search(r"/v(\d\.\d)/", ns)

    def t(xp: str) -> str:
        return doc.xpath(f"string({xp})").strip()

    def dec(xp: str) -> Decimal:
        v = t(xp)
        return Decimal(v) if v else Decimal(0)

    L = lambda n: f"*[local-name()='{n}']"  # noqa: E731
    rs = f"/{L(raiz)}/{L('ResumenFactura')}"
    iva = Decimal(0)
    desglose = doc.xpath(f"{rs}/{L('TotalDesgloseImpuesto')}")
    if desglose:
        for d in desglose:
            if d.xpath(f"string({L('Codigo')})") in ("01", "07", "08"):
                iva += Decimal(d.xpath(f"string({L('TotalMontoImpuesto')})") or 0)
    else:
        for im in doc.xpath(f"//{L('LineaDetalle')}/{L('Impuesto')}"):
            if im.xpath(f"string({L('Codigo')})") in ("01", "07"):
                iva += Decimal(im.xpath(f"string({L('Monto')})") or 0)
                iva -= Decimal(im.xpath(f"string({L('Exoneracion')}/{L('MontoExoneracion')})") or 0)
    total_imp = dec(f"{rs}/{L('TotalImpuesto')}")
    venta_neta = dec(f"{rs}/{L('TotalVentaNeta')}")
    fecha_txt = t(f"/{L(raiz)}/{L('FechaEmision')}")
    try:
        fecha = datetime.fromisoformat(fecha_txt).date()
    except ValueError:
        fecha = hoy_cr()
    return {
        "tipo": TIPO_POR_RAIZ[raiz], "raiz": raiz, "version": ver.group(1) if ver else None,
        "clave": t(f"/{L(raiz)}/{L('Clave')}"), "consecutivo": t(f"/{L(raiz)}/{L('NumeroConsecutivo')}"),
        "fecha": fecha,
        "emisor_nombre": t(f"/{L(raiz)}/{L('Emisor')}/{L('Nombre')}"),
        "emisor_tipo": t(f"/{L(raiz)}/{L('Emisor')}/{L('Identificacion')}/{L('Tipo')}"),
        "emisor_identificacion": t(f"/{L(raiz)}/{L('Emisor')}/{L('Identificacion')}/{L('Numero')}"),
        "receptor_identificacion": t(f"/{L(raiz)}/{L('Receptor')}/{L('Identificacion')}/{L('Numero')}"),
        "moneda": t(f"{rs}/{L('CodigoTipoMoneda')}/{L('CodigoMoneda')}") or "CRC",
        "tipo_cambio": dec(f"{rs}/{L('CodigoTipoMoneda')}/{L('TipoCambio')}") or Decimal(1),
        "subtotal": _q(venta_neta), "iva": _q(iva), "otros_impuestos": _q(total_imp - iva),
        "total": _q(dec(f"{rs}/{L('TotalComprobante')}")),
        "detalle": "; ".join(x.text or "" for x in doc.xpath(f"//{L('LineaDetalle')}/{L('Detalle')}"))[:500],
        "firmado": bool(doc.xpath("//*[local-name()='Signature']")),
    }
