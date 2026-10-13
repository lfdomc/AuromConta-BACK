"""Caché en memoria para datos GLOBALES que cambian muy poco (parámetros legales, base legal, salarios mínimos).

Solo se guardan aquí datos iguales para todos los usuarios: nunca datos de una empresa o de una persona.
Cada proceso de la API tiene su propia copia; al editar desde la plataforma se borra la del proceso que
atendió el cambio y las demás vencen solas (TTL). Ahorra consultas repetidas a Supabase cuando muchos
usuarios abren las mismas pantallas.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")
_datos: dict[str, tuple[float, Any]] = {}
_candado = threading.Lock()
MAX_ENTRADAS = 256


def obtener(clave: str, ttl_seg: float, cargar: Callable[[], T]) -> T:
    ahora = time.monotonic()
    with _candado:
        v = _datos.get(clave)
        if v is not None and v[0] > ahora:
            return v[1]
    valor = cargar()
    with _candado:
        if len(_datos) >= MAX_ENTRADAS:
            _datos.clear()
        _datos[clave] = (ahora + ttl_seg, valor)
    return valor


def borrar(prefijo: str = "") -> None:
    with _candado:
        for k in [k for k in _datos if k.startswith(prefijo)]:
            del _datos[k]
