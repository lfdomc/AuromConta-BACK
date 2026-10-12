"""Integración con Tilopay (cobro con tarjeta guardada).

Documentación consultada (oct. 2026, "API v1", verificada por Tilopay el 2026-08-29):
  * Portal: https://tilopay.com/en/developers
  * Autenticación: https://tilopay.com/en/developers/api/autenticacion
  * Tokenizar tarjeta: https://tilopay.com/en/developers/api/cof/process-tokenize
  * Cobrar tarjeta guardada: https://tilopay.com/en/developers/api/cof/realizar-cobro
  * Eliminar tarjeta: https://tilopay.com/en/developers/api/cof/remove-card
  * Página de pago: https://tilopay.com/en/developers/api/hosted-payment-page/process-payment
  * Webhooks: https://tilopay.com/en/developers/api/webhooks
  * OpenAPI: https://tilopay.com/developers/openapi.json (consult, processRecurrentPayment, login)

Flujo implementado
  1. `url_tokenizacion` -> POST processTokenize: Tilopay devuelve la URL de su formulario seguro; el
     cliente registra la tarjeta allá (nunca pasa por nuestros servidores).
  2. Tilopay redirige a `redirect` (GET /webhooks/tilopay/retorno en esta API) con el resultado.
  3. Cada mes: `cobrar_token` -> POST processRecurrentPayment con el token v2.
  4. Antes de dar por pagado algo que llegó por webhook, se confirma con POST consult (los webhooks no
     tienen firma pública; la doc pide no tomar decisiones irreversibles solo con su contenido).

Lo que la documentación NO dice (pendiente de confirmar con soporte@tilopay.com):
  * Nombres de los parámetros con que el retorno de processTokenize entrega el token, los últimos 4
    dígitos y la marca. Se leen candidatos razonables (PARAMS_TOKEN, PARAMS_ULTIMOS4, PARAMS_MARCA).
  * Algoritmo de `orderHash`/`OrderHash` ("its algorithm is not public"). No se valida.
  * Firma de webhooks: no existe un HMAC documentado. Se protege el endpoint con un secreto propio en
    la URL registrada en Tilopay (`?secreto=`) o en el encabezado X-Webhook-Secret, y se confirma
    cada pago con `consult`.
  * Ambiente de pruebas: la doc dice que no hay un host aparte ("no separate host for testing"); se usan
    credenciales de pruebas del comercio.
  * Formato de montos: los ejemplos usan texto con 2 decimales ("10.00"); se envía así.

Nunca se guarda el número de tarjeta: solo el token del proveedor, los últimos 4 dígitos y la marca.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any

import httpx

from .. import config

# Host único de API y SDK — https://tilopay.com/en/developers/api/autenticacion
TILOPAY_HOST = "https://app.tilopay.com"
EP_LOGIN = "/api/v1/login"                                # body {apiuser, password} -> {access_token, token_type, expires_in}
EP_TOKENIZAR = "/api/v1/processTokenize"                  # body {key, redirect, email, language, firstName, lastName, token_version:"v2"} -> {type, url}
EP_COBRO_TOKEN = "/api/v1/processRecurrentPayment"        # body {key, amount, currency, orderNumber, capture, email, card} -> {type, response, description, auth, tpt, order_id, tilopayTransaction, orderHash}
EP_ELIMINAR_TARJETA = "/api/v1/user/card-remove"          # body {key, email, token}
EP_CONSULTAR = "/api/v1/consult"                          # body {key, orderNumber, merchantId?} -> {type, message, response:[{code, response, auth, commission, iva_commission, retention_iva, retention_rent, net_to_liquidate, card, last, ...}]}
EP_PAGINA_PAGO = "/api/v1/processPayment"                 # hosted payment page (subscription=1 obliga a guardar la tarjeta)

# Parámetros del retorno de tokenización: NO documentados. Se prueban en este orden.
PARAMS_TOKEN = ("card", "token", "card_token", "cardToken", "crd")
PARAMS_ULTIMOS4 = ("last", "last4", "lastDigits", "last_digits")
PARAMS_MARCA = ("brand", "cardBrand", "card_type", "type_card")
PARAMS_VENCE = ("expiration", "exp", "expire")
# En el retorno de una transacción "code = 1" es aprobada (process-payment). En processRecurrentPayment
# el ejemplo empareja response "1" con "Transacción aprobada".
CODIGO_APROBADO = "1"


class ErrorTilopay(RuntimeError):
    pass


def cliente_http() -> httpx.Client:
    """Punto único de salida HTTP (las pruebas lo reemplazan con un MockTransport)."""
    return httpx.Client(base_url=TILOPAY_HOST, timeout=40)


_token: dict[str, Any] = {}


def configurado() -> bool:
    s = config.settings
    return bool(s.tilopay_api_user and s.tilopay_api_password and s.tilopay_api_key)


def _exigir_config() -> None:
    if not configurado():
        raise ErrorTilopay("Tilopay no está configurado (TILOPAY_API_USER, TILOPAY_API_PASSWORD, TILOPAY_API_KEY).")


def token() -> str:
    _exigir_config()
    s = config.settings
    if _token.get("valor") and _token.get("vence", 0) > time.time() + 30 and _token.get("usuario") == s.tilopay_api_user:
        return _token["valor"]
    try:
        with cliente_http() as h:
            r = h.post(EP_LOGIN, json={"apiuser": s.tilopay_api_user, "password": s.tilopay_api_password})
    except httpx.HTTPError as exc:
        raise ErrorTilopay(f"Tilopay no disponible: {exc}") from exc
    if r.status_code != 200 or "access_token" not in r.text:
        raise ErrorTilopay(f"Tilopay rechazó el inicio de sesión ({r.status_code})")
    j = r.json()
    _token.update(valor=j["access_token"], vence=time.time() + int(j.get("expires_in") or 300), usuario=s.tilopay_api_user)
    return j["access_token"]


def _post(ruta: str, cuerpo: dict) -> dict:
    tok = token()
    try:
        with cliente_http() as h:
            r = h.post(ruta, json=cuerpo, headers={"Authorization": f"bearer {tok}", "Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise ErrorTilopay(f"Tilopay no disponible: {exc}") from exc
    try:
        j = r.json()
    except ValueError:
        j = {"raw": r.text[:500]}
    if r.status_code >= 500:
        raise ErrorTilopay(f"Tilopay respondió {r.status_code}")
    j["_http"] = r.status_code
    return j


def monto(v) -> str:
    return f"{Decimal(str(v)):.2f}"


# ------------------------------------------------------------- Estado firmado --
# El parámetro `estado` del retorno lo firmamos nosotros (HMAC-SHA256 con TILOPAY_WEBHOOK_SECRET) para
# saber a qué cliente pertenece la tarjeta sin confiar en datos del navegador.
def firmar_estado(datos: dict) -> str:
    crudo = base64.urlsafe_b64encode(json.dumps(datos, separators=(",", ":")).encode()).decode().rstrip("=")
    firma = hmac.new(_secreto(), crudo.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{crudo}.{firma}"


def leer_estado(estado: str, max_edad_s: int = 3600) -> dict | None:
    try:
        crudo, firma = estado.rsplit(".", 1)
    except (ValueError, AttributeError):
        return None
    esperada = hmac.new(_secreto(), crudo.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(firma, esperada):
        return None
    datos = json.loads(base64.urlsafe_b64decode(crudo + "=" * (-len(crudo) % 4)))
    if time.time() - datos.get("t", 0) > max_edad_s:
        return None
    return datos


def _secreto() -> bytes:
    s = config.settings.tilopay_webhook_secret or config.settings.cron_secret
    if not s:
        raise ErrorTilopay("Falta TILOPAY_WEBHOOK_SECRET")
    return s.encode()


def secreto_webhook_valido(recibido: str | None) -> bool:
    s = config.settings.tilopay_webhook_secret
    return bool(s and recibido and hmac.compare_digest(s.encode(), recibido.encode()))


# ---------------------------------------------------------------- Operaciones --
def url_tokenizacion(*, correo: str, nombre: str, apellido: str, redirect: str, idioma: str = "es") -> str:
    """URL del formulario seguro de Tilopay para registrar la tarjeta."""
    _exigir_config()
    j = _post(EP_TOKENIZAR, {"key": config.settings.tilopay_api_key, "redirect": redirect, "email": correo,
                             "language": idioma, "firstName": nombre[:50] or "Cliente", "lastName": apellido[:50] or "Aurom",
                             "token_version": "v2"})
    if not j.get("url"):
        raise ErrorTilopay(f"Tilopay no devolvió la URL de registro: {j.get('message') or j.get('description') or j}")
    return j["url"]


def datos_tarjeta(params: dict[str, str]) -> dict:
    """Saca token/últimos 4/marca de los parámetros del retorno (nombres sin documentar)."""
    def primero(nombres):
        for n in nombres:
            if params.get(n):
                return str(params[n])
        return None
    ultimos = primero(PARAMS_ULTIMOS4)
    return {"token": primero(PARAMS_TOKEN), "ultimos4": "".join(ch for ch in (ultimos or "") if ch.isdigit())[-4:] or None,
            "marca": primero(PARAMS_MARCA), "vence": primero(PARAMS_VENCE)}


def cobrar_token(*, token_tarjeta: str, correo: str, monto_total, moneda: str, orden: str) -> dict:
    """Cobra la tarjeta guardada. Devuelve {aprobado, codigo, descripcion, autorizacion, transaccion, crudo}."""
    _exigir_config()
    j = _post(EP_COBRO_TOKEN, {"key": config.settings.tilopay_api_key, "amount": monto(monto_total), "currency": moneda,
                               "orderNumber": orden, "capture": "1", "email": correo, "card": token_tarjeta})
    cod = str(j.get("response", j.get("code", "")))
    return {"aprobado": cod == CODIGO_APROBADO, "codigo": cod, "descripcion": j.get("description") or j.get("message"),
            "autorizacion": j.get("auth"), "transaccion": str(j.get("tilopayTransaction") or j.get("tpt") or ""),
            "crudo": {k: v for k, v in j.items() if k not in ("card",)}}


def consultar_orden(orden: str) -> dict | None:
    """Confirma una transacción en Tilopay. Devuelve el primer resultado o None."""
    _exigir_config()
    j = _post(EP_CONSULTAR, {"key": config.settings.tilopay_api_key, "orderNumber": orden})
    filas = j.get("response") or []
    if isinstance(filas, dict):
        filas = [filas]
    if not filas:
        return None
    f = filas[0]
    cod = str(f.get("code", f.get("response", "")))
    return {"aprobado": cod == CODIGO_APROBADO, "codigo": cod, "monto": f.get("amount"), "moneda": f.get("currency"),
            "comision": Decimal(str(f.get("commission") or 0)) + Decimal(str(f.get("iva_commission") or 0)),
            "retencion_iva": Decimal(str(f.get("retention_iva") or 0)),
            "retencion_renta": Decimal(str(f.get("retention_rent") or 0)),
            "autorizacion": f.get("auth"), "transaccion": str(f.get("id_tilopay") or ""), "ultimos4": f.get("last")}


def eliminar_tarjeta(*, token_tarjeta: str, correo: str) -> bool:
    _exigir_config()
    j = _post(EP_ELIMINAR_TARJETA, {"key": config.settings.tilopay_api_key, "email": correo, "token": token_tarjeta})
    return j.get("_http", 500) < 300
