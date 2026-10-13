"""Modelos de entrada (Pydantic). Los montos son Decimal y viajan como texto."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
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
    # Pago bancario (BAC Formato 6)
    bac_plan: str | None = Field(None, max_length=10)
    bac_envio: int | None = Field(None, ge=1, le=99999)
    bac_concepto: str | None = Field(None, max_length=30)
    # Asistencia y horas extra
    he_desde_asistencia: bool | None = None
    tolerancia_tardia_min: int | None = Field(None, ge=0, le=240)
    # Asiento contable
    asiento_modo: Literal["planilla", "empleado"] | None = None
    # Correo de boletas
    correo_asunto: str | None = Field(None, max_length=200)
    correo_cuerpo: str | None = Field(None, max_length=5000)
    correo_firma: str | None = Field(None, max_length=200)
    # Firmante de constancias
    firmante_nombre: str | None = Field(None, max_length=120)
    firmante_cargo: str | None = Field(None, max_length=120)
    firmante_telefono: str | None = Field(None, max_length=40)
    firmante_correo: str | None = Field(None, max_length=120)
    firmante_lugar: str | None = Field(None, max_length=120)
    # Boleta, liquidaciones y vacaciones
    boleta_saldo_vacaciones: bool | None = None
    boleta_detalle_subsidio: bool | None = None
    liquidacion_sin_ccss: bool | None = None
    vacaciones_excluir_incapacidad: bool | None = None
    vacaciones_dias_aniversario: Decimal | None = Field(None, ge=1, le=15)
    # Alertas de documentos
    alertas_documentos_correos: str | None = Field(None, max_length=500)
    alertas_documentos_frecuencia: Literal["semanal", "mensual"] | None = None
    alertas_documentos_dias: int | None = Field(None, ge=1, le=365)


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


TipoEmpleado = Annotated[str, Field(min_length=1, max_length=40)]   # código del catálogo tipos_empleado


class TipoEmpleadoIn(Base):
    codigo: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    nombre: str = Field(min_length=1, max_length=80)
    cotiza_ccss: bool = True
    activo: bool = True


class TipoDocumentoIn(Base):
    nombre: str = Field(min_length=1, max_length=120)
    requiere_numero: bool = False
    requiere_vencimiento: bool = True
    dias_alerta: int | None = Field(None, ge=1, le=365)
    orden: int = 100


class TipoCobroIn(Base):
    nombre: str = Field(min_length=1, max_length=120)
    modo: Literal["fijo", "por_unidad"] = "fijo"
    precio_unitario: Dinero = Field(Decimal("0"), ge=0)
    unidad: str | None = Field(None, max_length=30)
    subsidio_pct: Decimal = Field(Decimal("0"), ge=0, le=100)
    afecta_ccss: bool = False
    activo: bool = True
    orden: int = 100


class BonoAntiguedadIn(Base):
    nombre: str = Field(min_length=1, max_length=120)
    desde_anios: int = Field(ge=1, le=60)
    hasta_anios: int | None = Field(None, ge=1, le=60)
    tipo: Literal["porcentaje", "fijo"] = "porcentaje"
    porcentaje: Decimal | None = Field(None, gt=0, le=100)
    monto: Dinero | None = Field(None, gt=0)
    activo: bool = True


class SalarioMinimoIn(Base):
    categoria: str = Field(min_length=1, max_length=120)
    monto: Dinero = Field(gt=0)
    vigente_desde: date
    decreto: str | None = Field(None, max_length=200)


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
    numero_ccss: str | None = Field(None, max_length=30)
    asegurado_ccss: bool = True
    tipo_empleado: TipoEmpleado = "indefinido"
    departamento: str | None = Field(None, max_length=80)
    subdepartamento: str | None = Field(None, max_length=80)
    salario_variable: bool = False
    cuenta_bancaria: str | None = Field(None, max_length=40)
    tipo_cuenta: Literal["corriente", "ahorros", "sinpe"] | None = None
    sinpe_movil: str | None = Field(None, pattern=r"^\d{8}$")
    aguinaldo_inicial: Dinero = Decimal("0")
    aguinaldo_corte: date | None = None
    vacaciones_saldo_inicial: Decimal | None = Field(None, ge=0, le=999)
    vacaciones_corte: date | None = None


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
    numero_ccss: str | None = Field(None, max_length=30)
    asegurado_ccss: bool | None = None
    tipo_empleado: TipoEmpleado | None = None
    departamento: str | None = Field(None, max_length=80)
    subdepartamento: str | None = Field(None, max_length=80)
    salario_variable: bool | None = None
    cuenta_bancaria: str | None = Field(None, max_length=40)
    tipo_cuenta: Literal["corriente", "ahorros", "sinpe"] | None = None
    sinpe_movil: str | None = Field(None, pattern=r"^\d{8}$")
    aguinaldo_inicial: Dinero | None = None
    aguinaldo_corte: date | None = None
    vacaciones_saldo_inicial: Decimal | None = Field(None, ge=0, le=999)
    vacaciones_corte: date | None = None


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


def _reglas_licencia(m):
    """Licencias de ley (servicios/licencias.py): quién paga lo dice la ley; se piden los datos que la ley exige."""
    from .servicios import licencias
    t = licencias.tipo(getattr(m, "tipo", None))
    if t is None:
        return m
    if getattr(m, "con_goce", None) is not None or "tipo" in m.model_fields_set:
        m.con_goce = licencias.con_goce(t.codigo, getattr(m, "con_goce", None))
    if t.unidad == "horas":
        m.unidad = "horas"
        if "tipo" in m.model_fields_set and not getattr(m, "horas", None):
            raise ValueError("Indique las horas de la licencia (por ejemplo 1 por día de lactancia)")
    if t.evento and "tipo" in m.model_fields_set and not getattr(m, "fecha_evento", None):
        raise ValueError(f"Indique la {t.evento.lower()}")
    if t.parentesco and "tipo" in m.model_fields_set and not getattr(m, "parentesco", None):
        raise ValueError("Indique el parentesco con la persona fallecida")
    ev, desde = getattr(m, "fecha_evento", None), getattr(m, "desde", None)
    if ev and desde and desde < ev and t.codigo != "lactancia":
        raise ValueError(f"La licencia no puede empezar antes de la {t.evento.lower()}")
    return m


class PermisoIn(Base):
    """Permisos y licencias especiales de ley (paternidad, adopción, duelo, lactancia...)."""
    empleado_id: UUID
    tipo: str = Field("con_goce", max_length=40)
    desde: date
    hasta: date
    dias: Decimal = Field(gt=0, le=366)
    con_goce: bool = False
    monto: Dinero | None = None
    ausencia: bool = False
    fundamento: str | None = None
    estado: Literal["pendiente", "aprobado", "rechazado"] = "aprobado"
    unidad: Literal["dias", "horas"] = "dias"
    horas: Decimal | None = Field(None, gt=0, le=2000)
    fecha_evento: date | None = None
    parentesco: str | None = Field(None, max_length=40)
    pariente: str | None = Field(None, max_length=120)
    numero_documento: str | None = Field(None, max_length=60)

    @model_validator(mode="after")
    def _ley(self):
        if self.hasta < self.desde:
            raise ValueError("La fecha final es anterior a la inicial")
        return _reglas_licencia(self)


class PermisoUpd(Base):
    tipo: str | None = Field(None, max_length=40)
    desde: date | None = None
    hasta: date | None = None
    dias: Decimal | None = Field(None, gt=0, le=366)
    con_goce: bool | None = None
    monto: Dinero | None = None
    ausencia: bool | None = None
    fundamento: str | None = None
    estado: Literal["pendiente", "aprobado", "rechazado"] | None = None
    unidad: Literal["dias", "horas"] | None = None
    horas: Decimal | None = Field(None, gt=0, le=2000)
    fecha_evento: date | None = None
    parentesco: str | None = Field(None, max_length=40)
    pariente: str | None = Field(None, max_length=120)
    numero_documento: str | None = Field(None, max_length=60)

    @model_validator(mode="after")
    def _ley(self):
        return _reglas_licencia(self)


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
