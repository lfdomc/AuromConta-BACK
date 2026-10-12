"""Conversión de entradas y resultados del motor a JSON y de vuelta.

Los montos viajan como texto ("1234.50") para no perder exactitud.
"""
from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal
from enum import Enum
from uuid import UUID

from .modelos import Categoria, Linea, ResultadoBoleta


def a_dict(obj):
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: a_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (list, tuple)):
        return [a_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): a_dict(v) for k, v in obj.items()}
    return obj


def resultado_desde_dict(d: dict) -> ResultadoBoleta:
    res = ResultadoBoleta()
    for f in dataclasses.fields(ResultadoBoleta):
        if f.name not in d:
            continue
        v = d[f.name]
        if f.name == "lineas":
            v = [Linea(tipo=l["tipo"], categoria=Categoria(l["categoria"]), monto=Decimal(l["monto"]),
                       descripcion=l.get("descripcion", ""), afecto_ccss=l.get("afecto_ccss", False),
                       afecto_salario_base=l.get("afecto_salario_base", False), referencia=l.get("referencia"))
                 for l in v]
        elif f.name == "notas":
            v = list(v)
        elif f.name in ("dias_periodo",):
            v = int(v)
        elif isinstance(getattr(res, f.name), bool):
            v = bool(v)
        elif isinstance(getattr(res, f.name), Decimal):
            v = Decimal(str(v))
        setattr(res, f.name, v)
    return res
