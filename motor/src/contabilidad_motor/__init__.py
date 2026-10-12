"""Motor contable de Planilla Costa Rica (Python puro, sin base de datos).

Partida doble con Decimal y redondeo ROUND_HALF_UP a céntimos. Recibe totales ya
sumados (débitos y créditos por cuenta) y devuelve reportes listos para mostrar:
balance de comprobación, estado de resultados, balance general, asiento de
cierre anual y reversiones. Toda la lógica de montos contables vive aquí para
probarla sin base de datos, igual que planilla_motor.
"""
from .motor import (CERO, Cuenta, Linea, Totales, asiento_cierre, asiento_reverso, balance_comprobacion,
                    balance_general, estado_resultados, naturaleza_de, r2, saldo_natural, validar_asiento)

__version__ = "1.0.0"

__all__ = [
    "CERO", "Cuenta", "Linea", "Totales", "asiento_cierre", "asiento_reverso", "balance_comprobacion",
    "balance_general", "estado_resultados", "naturaleza_de", "r2", "saldo_natural", "validar_asiento",
]
