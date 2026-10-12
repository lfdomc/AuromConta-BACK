"""Aritmetica de dinero: todo en Decimal, redondeo comercial a 2 decimales."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CERO = Decimal("0")
CENTIMO = Decimal("0.01")


def D(valor) -> Decimal:
    """Convierte int/str/Decimal a Decimal. Los float se pasan por str para
    no arrastrar errores binarios (0.1 -> '0.1')."""
    if isinstance(valor, Decimal):
        return valor
    if isinstance(valor, float):
        return Decimal(repr(valor))
    return Decimal(valor)


def r2(valor) -> Decimal:
    """Redondeo a centimos, mitad hacia arriba (criterio contable)."""
    return D(valor).quantize(CENTIMO, rounding=ROUND_HALF_UP)


def r(valor, decimales: int) -> Decimal:
    return D(valor).quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)


def suma(valores) -> Decimal:
    total = CERO
    for v in valores:
        total += D(v)
    return total
