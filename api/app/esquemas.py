"""Modelos de entrada (Pydantic). Los montos son Decimal y viajan como texto."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Frecuencia = Literal["monthly", "biweekly", "weekly", "bimonthly"]
Dinero = Decimal


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------- Empresa ------
class EmpresaIn(Base):
    nombre: str | None = None
    cedula_juridica: str | None = None
    numero_patronal: str | None = None
    poliza_ins: str | None = None
    clase_riesgo_ins: Literal["I", "II", "III", "IV", "V"] | None = None
    correo: str | None = None
    telefono: str | None = None
    direccion: str | None = None
    logo_url: str | None = None


ModuloCodigo = Literal["planilla", "contabilidad"]


class NuevaEmpresa(Base):
    nombre: str = Field(min_length=2)
    cedula_juridica: str | None = None
    codigo_referido: str | None = None
    frecuencia: Frecuencia = "biweekly"
    modulos: list[ModuloCodigo] = Field(default_factory=lambda: ["planilla"], min_length=1, max_length=5)


class CambioModuloIn(Base):
    activo: bool


class LogoIn(Base):
    """Imagen PNG o JPG en base64 (máx. 2 MB). La API la reduce a 600 px y la guarda como PNG."""
    datos_base64: str = Field(min_length=8, max_length=2_800_000)


class ConfiguracionIn(Base):
    metodo_renta: Literal["per_period", "monthly_consolidated"] | None = None
    base_renta: Literal["gross", "net_ccss"] | None = None
    aplicar_bmc: bool | None = None
    bmc_monto: Dinero | None = None
    cesantia_modo: Literal["legal", "custom"] | None = None
    cesantia_tasa: Decimal | None = None
    factor_he_diurna: Decimal | None = None
    factor_he_doble: Decimal | None = None
    correo_remitente: str | None = None
    nota_boleta: str | None = None


class UsuarioEmpresaIn(Base):
    correo: str
    rol: Literal["admin", "aprobador", "operador", "lectura"]


class UsuarioEmpresaUpd(Base):
    rol: Literal["admin", "aprobador", "operador", "lectura"] | None = None
    activo: bool | None = None


class AsignacionIn(Base):
    socio_id: UUID | None = None
    codigo_referido: str | None = None
    funciones: list[Literal["supervision", "soporte", "ventas", "implementacion"]] = ["supervision", "soporte"]
    permiso: Literal["lectura", "operacion"] = "lectura"
    soporte_principal: bool = True

    @model_validator(mode="after")
    def _uno(self):
        if not self.socio_id and not self.codigo_referido:
            raise ValueError("Indique socio_id o codigo_referido")
        return self


class SucursalIn(Base):
    codigo: str | None = None
    nombre: str
    direccion: str | None = None
    telefono: str | None = None
    correo: str | None = None
    es_temporal: bool = False
    fecha_fin_proyecto: date | None = None
    activa: bool = True


class CalendarioIn(Base):
    nombre: str
    frecuencia: Frecuencia
    activo: bool = True


class HorarioIn(Base):
    nombre: str
    horas_diarias: Decimal = Decimal("8")
    dias_semana: int = 6
    jornada: Literal["diurna", "mixta", "nocturna"] = "diurna"
    medio_tiempo: bool = False


class FeriadoIn(Base):
    fecha: date
    nombre: str
    pago_obligatorio: bool = True


# ------------------------------------------------------------- Empleados ----
class EmpleadoIn(Base):
    tipo_identificacion: Literal["cedula", "dimex", "pasaporte", "nite", "otro"] = "cedula"
    identificacion: str
    nombre: str
    apellidos: str = ""
    correo: str | None = None
    telefono: str | None = None
    fecha_nacimiento: date | None = None
    fecha_ingreso: date
    fecha_salida: date | None = None
    estado: Literal["activo", "inactivo", "liquidado"] = "activo"
    sucursal_id: UUID | None = None
    puesto: str | None = None
    calendario_id: UUID | None = None
    horario_id: UUID | None = None
    frecuencia: Frecuencia = "biweekly"
    salario_mensual: Dinero = Decimal("0")
    por_asistencia: bool = False
    tarifa_hora: Dinero = Decimal("0")
    medio_tiempo: bool = False
    aplica_rop: bool = False
    hijos: int = 0
    conyuge: bool = False
    num_empleadores: int = 1
    pensionado: Literal["none", "estado", "otro"] = "none"
    exento: bool = False
    clase_riesgo_ins: Literal["I", "II", "III", "IV", "V"] | None = None
    banco: str | None = None
    iban: str | None = None
    saldo_vacaciones: Decimal = Decimal("0")


class EmpleadoUpd(Base):
    """Edición parcial. El salario no se cambia aquí: use historial salarial."""
    tipo_identificacion: Literal["cedula", "dimex", "pasaporte", "nite", "otro"] | None = None
    identificacion: str | None = None
    nombre: str | None = None
    apellidos: str | None = None
    correo: str | None = None
    telefono: str | None = None
    fecha_nacimiento: date | None = None
    fecha_ingreso: date | None = None
    fecha_salida: date | None = None
    estado: Literal["activo", "inactivo", "liquidado"] | None = None
    sucursal_id: UUID | None = None
    puesto: str | None = None
    calendario_id: UUID | None = None
    horario_id: UUID | None = None
    frecuencia: Frecuencia | None = None
    por_asistencia: bool | None = None
    tarifa_hora: Dinero | None = None
    medio_tiempo: bool | None = None
    aplica_rop: bool | None = None
    hijos: int | None = None
    conyuge: bool | None = None
    num_empleadores: int | None = None
    pensionado: Literal["none", "estado", "otro"] | None = None
    exento: bool | None = None
    clase_riesgo_ins: Literal["I", "II", "III", "IV", "V"] | None = None
    banco: str | None = None
    iban: str | None = None
    saldo_vacaciones: Decimal | None = None


class CambioSalarialIn(Base):
    fecha_efectiva: date
    salario_nuevo: Dinero = Field(ge=0)
    motivo: str | None = None


class DecisionIn(Base):
    aprobar: bool
    comentario: str | None = None


class DocumentoIn(Base):
    tipo: str
    numero: str | None = None
    vence: date | None = None
    archivo_path: str | None = None


class MovimientoIn(Base):
    tipo: Literal["ingreso", "traslado", "cambio_puesto", "amonestacion", "salida", "otro"]
    fecha: date
    detalle: str | None = None


# ------------------------------------------------------------- Novedades ----
class MarcaIn(Base):
    empleado_id: UUID
    entrada: datetime
    salida: datetime | None = None
    origen: Literal["reloj", "manual", "importado"] = "manual"


class HoraExtraIn(Base):
    empleado_id: UUID
    fecha: date
    horas: Decimal = Field(gt=0)
    tipo: Literal["diurna", "nocturna", "mixta", "feriado"] = "diurna"
    factor: Decimal | None = None       # nulo = según tipo y configuración
    monto: Dinero | None = None         # nulo = se calcula con la tarifa del empleado
    afecto_ccss: bool = True
    estado: Literal["pendiente", "aprobada", "rechazada"] = "pendiente"


class IncapacidadIn(Base):
    empleado_id: UUID
    tipo: Literal["ccss", "ins", "maternity"]
    desde: date
    hasta: date
    porcentaje_subsidio: Decimal = Decimal("60")
    numero_boleta: str | None = None
    es_prorroga: bool = False
    medio_dia_extra: bool = False
    maternidad_ccss_por_patrono: bool = False
    maternidad_50_50: bool = False
    salario_diario: Dinero | None = None
    salario_promedio_diario: Dinero | None = None
    estado: Literal["borrador", "confirmada", "anulada"] = "confirmada"


class PermisoIn(Base):
    empleado_id: UUID
    tipo: str = "personal"
    desde: date
    hasta: date
    dias: Decimal
    con_goce: bool = False
    monto: Dinero | None = None
    ausencia: bool = False
    fundamento: str | None = None
    estado: Literal["pendiente", "aprobado", "rechazado"] = "aprobado"


class VacacionIn(Base):
    empleado_id: UUID
    tipo: Literal["disfrute", "dinero"] = "disfrute"
    desde: date | None = None
    hasta: date | None = None
    dias: Decimal = Field(gt=0)
    monto: Dinero | None = None
    estado: Literal["solicitada", "aprobada", "rechazada"] = "solicitada"


class BonoIn(Base):
    empleado_id: UUID
    nombre: str
    monto: Dinero = Field(ge=0)
    afecto_ccss: bool = True
    afecto_salario_base: bool = True
    recurrente: bool = False
    desde: date
    hasta: date | None = None
    activo: bool = True


class OtraDeduccionIn(Base):
    empleado_id: UUID
    descripcion: str
    monto: Dinero = Field(ge=0)
    recurrente: bool = False
    desde: date
    hasta: date | None = None
    activo: bool = True


class RebajoRentaIn(Base):
    empleado_id: UUID
    desde: date
    hasta: date
    monto: Dinero
    motivo: str | None = None


class PrestamoIn(Base):
    empleado_id: UUID
    descripcion: str = "Préstamo"
    monto: Dinero = Field(gt=0)
    cuota: Dinero = Field(gt=0)
    fecha_otorgado: date | None = None
    primera_rebaja: date
    estado: Literal["activo", "pausado", "pagado", "anulado"] = "activo"


class EmbargoIn(Base):
    empleado_id: UUID
    expediente: str
    juzgado: str | None = None
    beneficiario: str | None = None
    cuenta: str | None = None
    tipo: Literal["fijo", "porcentaje"]
    valor: Dinero = Field(gt=0)
    desde: date
    hasta: date | None = None
    estado: Literal["activo", "suspendido", "finalizado"] = "activo"

    @model_validator(mode="after")
    def _tope(self):
        if self.tipo == "porcentaje" and self.valor > 25:
            raise ValueError("Un embargo no puede superar el 25 % (Art. 172 CT)")
        return self


class PensionIn(Base):
    empleado_id: UUID
    expediente: str
    juzgado: str | None = None
    beneficiario: str | None = None
    relacion: str | None = None
    cuenta: str | None = None
    tipo: Literal["fijo", "porcentaje"]
    valor: Dinero = Field(gt=0)
    desde: date
    hasta: date | None = None
    estado: Literal["activo", "suspendido", "finalizado"] = "activo"


# ------------------------------------------------------------- Planillas ----
class PlanillaIn(Base):
    tipo: Literal["ordinaria", "extraordinaria"] = "ordinaria"
    calendario_id: UUID | None = None
    sucursal_id: UUID | None = None
    frecuencia: Frecuencia = "biweekly"
    desde: date
    hasta: date
    fecha_pago: date | None = None
    descripcion: str | None = None

    @model_validator(mode="after")
    def _fechas(self):
        if self.hasta < self.desde:
            raise ValueError("La fecha final es anterior a la inicial")
        return self


class ReabrirIn(Base):
    motivo: str = Field(min_length=5)


class EnviarIn(Base):
    boleta_ids: list[UUID] | None = None   # nulo = todas


class AguinaldoIn(Base):
    anio: int
    crear_planilla: bool = True
    fecha_pago: date | None = None


class LiquidacionIn(Base):
    empleado_id: UUID
    fecha_salida: date
    motivo: Literal["renuncia", "despido_sin_responsabilidad", "despido_con_responsabilidad",
                    "mutuo_acuerdo", "pension", "fallecimiento", "fin_contrato"]
    preaviso_otorgado: bool = False
    dias_vacaciones_pendientes: Decimal | None = None   # nulo = saldo del empleado
    salarios_pendientes: Dinero = Decimal("0")
    otros_pagos: Dinero = Decimal("0")
    deducciones_adicionales: Dinero = Decimal("0")
    incluir_saldo_prestamos: bool = True


class AumentoMasivoIn(Base):
    empleado_ids: list[UUID] | None = None   # nulo = todos los activos
    porcentaje: Decimal | None = None
    monto: Dinero | None = None
    fecha_efectiva: date
    motivo: str = "Aumento salarial"
    aplicar: bool = False                     # False = solo vista previa

    @model_validator(mode="after")
    def _uno(self):
        if (self.porcentaje is None) == (self.monto is None):
            raise ValueError("Indique porcentaje o monto")
        return self


class CuentaIn(Base):
    codigo: str
    nombre: str
    centro_costo: str | None = None
