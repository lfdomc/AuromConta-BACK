"""Entradas y salidas del motor. Son datos puros: nada de base de datos.

La API (Fase 1) arma estos objetos a partir de Supabase, llama a
`calcular_boleta` y guarda el resultado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from .dinero import CERO


class Frecuencia(str, Enum):
    MENSUAL = "monthly"
    QUINCENAL = "biweekly"
    SEMANAL = "weekly"
    BIMENSUAL = "bimonthly"

    @property
    def factor(self) -> Decimal:
        """Fraccion del salario mensual que corresponde a un periodo."""
        return {"monthly": Decimal("1"), "biweekly": Decimal("0.5"),
                "weekly": Decimal("0.25"), "bimonthly": Decimal("2")}[self.value]

    @property
    def periodos_por_mes(self) -> Decimal:
        return {"monthly": Decimal("1"), "biweekly": Decimal("2"),
                "weekly": Decimal("4"), "bimonthly": Decimal("0.5")}[self.value]


class TipoIncapacidad(str, Enum):
    ENFERMEDAD = "ccss"      # CCSS enfermedad / accidente no laboral (Art. 79 CT)
    INS = "ins"              # Riesgo del trabajo, paga el INS fuera de planilla
    MATERNIDAD = "maternity"  # Art. 94-95 CT


class Categoria(str, Enum):
    """Categorias de lineas de novedad (ingresos y deducciones)."""
    BONO = "bonus"
    LICENCIA_CON_GOCE = "licencia_con_goce"
    LICENCIA_SIN_GOCE = "licencia_sin_goce"
    AUSENCIA = "ausencia"
    VACACIONES = "vacation"
    PENSION_ALIMENTARIA = "pension_alimentaria"
    EMBARGO = "embargo"
    PRESTAMO = "loan"
    ROP = "rop"
    OTRO = "other"


@dataclass
class Empleado:
    salario_mensual: Decimal
    frecuencia: Frecuencia = Frecuencia.QUINCENAL
    fecha_ingreso: date | None = None
    fecha_nacimiento: date | None = None
    pensionado: str = "none"           # 'none' | 'estado'
    clase_riesgo_ins: str = "II"
    hijos: int = 0
    conyuge: bool = False
    num_empleadores: int = 1           # >1 = multiempleo, creditos se reparten
    exento: bool = False               # sin CCSS ni renta (is_sp en Odoo)
    por_asistencia: bool = False
    tarifa_hora: Decimal = CERO
    medio_tiempo: bool = False
    aplica_rop: bool = False


@dataclass
class HoraExtra:
    monto: Decimal
    horas: Decimal = CERO
    afecto_ccss: bool = True
    feriado: bool = False


@dataclass
class Incapacidad:
    tipo: TipoIncapacidad
    desde: date
    hasta: date
    salario_diario: Decimal
    salario_promedio_maternidad: Decimal = CERO
    porcentaje_subsidio: Decimal = Decimal("60")
    es_prorroga: bool = False
    medio_dia_extra: bool = False
    maternidad_ccss_por_patrono: bool = False
    maternidad_50_50: bool = False


@dataclass
class Linea:
    """Ingreso o deduccion adicional de la boleta (novedad)."""
    tipo: str                  # 'ingreso' | 'deduccion'
    categoria: Categoria
    monto: Decimal
    descripcion: str = ""
    afecto_ccss: bool = False          # solo bonos: integra al bruto
    referencia: str | None = None      # id del registro de origen (préstamo, bono...)
    afecto_salario_base: bool = False  # solo bonos: integra a provisiones


@dataclass
class Embargo:
    tipo: str                  # 'fijo' | 'porcentaje'
    valor: Decimal             # monto fijo del periodo o porcentaje (20 = 20 %)
    expediente: str = ""
    referencia: str | None = None   # id del registro de origen


@dataclass
class BoletaAnterior:
    """Boleta ya confirmada del mismo mes (para el metodo mensual consolidado)."""
    base_cotizable: Decimal
    ccss_obrero: Decimal
    renta: Decimal
    creditos: Decimal


@dataclass
class ConfigEmpresa:
    aplicar_bmc: bool = True
    bmc_monto: Decimal | None = None          # None = el del parametro legal
    base_renta: str = "gross"                 # 'gross' | 'net_ccss'
    metodo_renta: str = "per_period"          # 'per_period' | 'monthly_consolidated'
    cesantia_modo: str = "legal"              # 'legal' | 'custom'
    cesantia_tasa: Decimal = Decimal("4.8")   # % si cesantia_modo == 'custom'


@dataclass
class Periodo:
    desde: date
    hasta: date
    dias_trabajados: int | None = None   # proporcional (ingreso/salida)
    horas_asistencia: Decimal = CERO
    ultima_del_mes: bool = True


@dataclass
class EntradaBoleta:
    empleado: Empleado
    periodo: Periodo
    config: ConfigEmpresa = field(default_factory=ConfigEmpresa)
    salario_cambio_en_periodo: Decimal | None = None  # historial autorizado
    horas_extra: list[HoraExtra] = field(default_factory=list)
    vacaciones_dinero: Decimal = CERO
    incapacidades: list[Incapacidad] = field(default_factory=list)
    lineas: list[Linea] = field(default_factory=list)
    embargos: list[Embargo] = field(default_factory=list)
    pensiones: list[Embargo] = field(default_factory=list)  # pensión alimentaria: prioridad sobre embargos
    otros_ingresos_salariales: Decimal = CERO
    dias_paternidad: int = 0
    rebajo_renta: Decimal = CERO
    boletas_anteriores_mes: list[BoletaAnterior] = field(default_factory=list)


@dataclass
class ResultadoBoleta:
    # Salario
    dias_periodo: int = 0
    factor_proporcional: Decimal = Decimal("1")
    salario_base: Decimal = CERO
    horas_extra: Decimal = CERO
    horas_extra_ccss: Decimal = CERO
    vacaciones: Decimal = CERO
    bono_salarial: Decimal = CERO
    bono_base_prestaciones: Decimal = CERO
    salario_cotizable: Decimal = CERO
    bruto: Decimal = CERO
    # Incapacidades
    dias_incapacidad: Decimal = CERO
    subsidio_ccss: Decimal = CERO
    subsidio_ins: Decimal = CERO
    costo_patrono_incapacidad: Decimal = CERO
    # Bases
    base_cotizable: Decimal = CERO
    base_ccss: Decimal = CERO
    bmc_aplicada: bool = False
    bmc_reducida: bool = False
    # Deducciones obrero
    ccss_obrero: Decimal = CERO
    renta: Decimal = CERO
    creditos_renta: Decimal = CERO
    credito_hijos: Decimal = CERO
    credito_conyuge: Decimal = CERO
    rop_obrero: Decimal = CERO
    embargos: Decimal = CERO
    total_deducciones: Decimal = CERO
    # Otros
    paternidad: Decimal = CERO
    otros_ingresos: Decimal = CERO
    neto: Decimal = CERO
    # Patrono
    ccss_patronal: Decimal = CERO
    ins_patronal: Decimal = CERO
    rop_patronal: Decimal = CERO
    prov_aguinaldo: Decimal = CERO
    prov_cesantia: Decimal = CERO
    prov_vacaciones: Decimal = CERO
    tasa_cesantia: Decimal = CERO
    costo_total_patrono: Decimal = CERO
    # Lineas finales (incluye embargos y ROP generados por el motor)
    lineas: list[Linea] = field(default_factory=list)
    notas: list[str] = field(default_factory=list)
