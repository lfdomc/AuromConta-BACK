"""Cifrado de datos sensibles guardados en la base (AES-256-GCM, autenticado).

Formato guardado: "v1:" + base64url(nonce de 12 bytes + texto cifrado + etiqueta).
La llave vive solo en la variable de entorno CLAVE_CIFRADO (32 bytes en base64),
nunca en la base. Para rotarla: agregar la nueva como CLAVE_CIFRADO y la anterior
como CLAVE_CIFRADO_ANTERIOR; lo viejo se sigue leyendo y lo nuevo se cifra con la nueva.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..config import settings

PREFIJO = "v1:"


class CifradoNoConfigurado(RuntimeError):
    pass


def _llave(b64: str) -> bytes:
    llave = base64.b64decode(b64)
    if len(llave) != 32:
        raise CifradoNoConfigurado("CLAVE_CIFRADO debe ser de 32 bytes en base64")
    return llave


def _llaves() -> list[bytes]:
    llaves = []
    if settings.clave_cifrado:
        llaves.append(_llave(settings.clave_cifrado))
    anterior = os.environ.get("CLAVE_CIFRADO_ANTERIOR", "")
    if anterior:
        llaves.append(_llave(anterior))
    return llaves


def cifrar(texto: str | None, contexto: str = "") -> str | None:
    """`contexto` (p. ej. el id del cliente) se liga al cifrado: el valor no sirve en otra fila."""
    if texto is None:
        return None
    llaves = _llaves()
    if not llaves:
        raise CifradoNoConfigurado("Configure CLAVE_CIFRADO para guardar datos sensibles")
    nonce = os.urandom(12)
    datos = AESGCM(llaves[0]).encrypt(nonce, texto.encode(), contexto.encode() or None)
    return PREFIJO + base64.urlsafe_b64encode(nonce + datos).decode()


def descifrar(valor: str | None, contexto: str = "") -> str | None:
    if valor is None or not valor.startswith(PREFIJO):
        return valor   # dato anterior a esta versión (sin cifrar)
    crudo = base64.urlsafe_b64decode(valor[len(PREFIJO):])
    nonce, datos = crudo[:12], crudo[12:]
    for llave in _llaves():
        try:
            return AESGCM(llave).decrypt(nonce, datos, contexto.encode() or None).decode()
        except Exception:  # noqa: BLE001 - se prueba la siguiente llave
            continue
    raise CifradoNoConfigurado("No se pudo descifrar: la llave no coincide")
