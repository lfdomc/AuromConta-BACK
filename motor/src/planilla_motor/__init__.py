"""planilla_motor: calculo de planillas de Costa Rica en Python puro.

Sin base de datos ni framework: recibe datos, devuelve resultados. Lo usa la
API (FastAPI) y lo prueban los tests.
"""
from .boleta import calcular_boleta, tasa_cesantia, tope_embargable
from .modelos import (BoletaAnterior, Categoria, ConfigEmpresa, Embargo, Empleado,
                      EntradaBoleta, Frecuencia, HoraExtra, Incapacidad, Linea,
                      Periodo, ResultadoBoleta, TipoIncapacidad)
from .parametros import PARAMETROS_2026, ParametrosLegales, Tramo, parametros_para, registrar
from .renta import calcular_renta, impuesto_mensual, reconciliar_mes
from .parametros import a_json, desde_json
from .anuales import (EntradaLiquidacion, ResultadoAguinaldo, ResultadoLiquidacion, antiguedad,
                      calcular_aguinaldo, calcular_liquidacion, periodo_aguinaldo)
from .contabilidad import LineaAsiento, asiento_planilla, cuadra
from .serializar import a_dict, resultado_desde_dict

__version__ = "0.2.0"
__all__ = [
    "calcular_boleta", "tasa_cesantia", "tope_embargable", "calcular_renta", "impuesto_mensual", "reconciliar_mes",
    "BoletaAnterior", "Categoria", "ConfigEmpresa", "Embargo", "Empleado", "EntradaBoleta",
    "Frecuencia", "HoraExtra", "Incapacidad", "Linea", "Periodo", "ResultadoBoleta",
    "TipoIncapacidad", "PARAMETROS_2026", "ParametrosLegales", "Tramo", "parametros_para",
    "registrar", "a_json", "desde_json", "EntradaLiquidacion", "ResultadoAguinaldo",
    "ResultadoLiquidacion", "antiguedad", "calcular_aguinaldo", "calcular_liquidacion", "periodo_aguinaldo",
    "LineaAsiento", "asiento_planilla", "cuadra", "a_dict", "resultado_desde_dict",
]
