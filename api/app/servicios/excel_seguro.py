"""Protección contra inyección de fórmulas en los Excel y CSV que genera el sistema.

Un nombre de colaborador, sucursal o descripción que empiece con «=» lo escribe la empresa
(o llega en un archivo importado). openpyxl convierte ese texto en fórmula y Excel la ejecuta
al abrir el archivo, por ejemplo en la computadora del contador. El sistema nunca escribe
fórmulas a propósito, así que todo texto se guarda como texto.
"""
from typing import Any

from openpyxl.cell.cell import Cell

_original = Cell._bind_value
PELIGROSOS = ("=", "+", "-", "@", "\t", "\r")


def _bind_seguro(self: Cell, value: Any) -> None:
    _original(self, value)
    if self.data_type == "f":
        self.data_type = "s"


def activar() -> None:
    if Cell._bind_value is not _bind_seguro:
        Cell._bind_value = _bind_seguro


def texto_csv(v: Any) -> Any:
    """Texto para CSV: si empieza como fórmula se le antepone un apóstrofo (Excel no lo ejecuta).
    Los números (incluidos los negativos) se dejan igual."""
    if not isinstance(v, str) or not v.startswith(PELIGROSOS):
        return v
    try:
        float(v)
        return v
    except ValueError:
        return "'" + v


activar()
