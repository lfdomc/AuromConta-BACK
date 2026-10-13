"""Parametros legales con vigencia.

Cada juego de parametros tiene una fecha `vigente_desde`. Para calcular una
boleta se usa el juego vigente en la fecha de inicio del periodo, de modo que
recalcular una boleta vieja da el mismo resultado aunque la ley cambie despues.

En la base de datos estos valores viven en la tabla `parametros_legales`
(Fase 1); aqui estan los valores por defecto y la logica de seleccion.

Fuentes 2026:
  - Tramos y creditos de renta: Decreto 45333-H / DGT-R-016-2026.
  - Cargas CCSS e INS, provisiones: modulo Odoo planilla_cr (planilla_const.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .dinero import D


@dataclass(frozen=True)
class Tramo:
    desde: Decimal
    hasta: Decimal | None  # None = sin tope
    tasa: Decimal          # porcentaje, ej. Decimal("10")


@dataclass(frozen=True)
class ParametrosLegales:
    vigente_desde: date
    # CCSS
    ccss_obrero: Decimal
    ccss_obrero_pensionado_estado: Decimal
    ccss_patronal: Decimal
    bmc_mensual: Decimal
    # INS riesgos del trabajo por clase
    ins_tasas: dict = field(hash=False)
    ins_clase_defecto: str = "II"
    # ROP Ley 7983
    rop_obrero: Decimal = D("0.01")
    rop_patronal: Decimal = D("0.0325")
    # Provisiones
    prov_aguinaldo: Decimal = D("0.0833")
    prov_vacaciones: Decimal = D(12) / D(288)
    prov_cesantia_defecto: Decimal = D("0.0533")
    cesantia_tabla: dict = field(default_factory=dict, hash=False)
    cesantia_max_anios: int = 8
    # Renta
    tramos_renta: tuple = ()
    credito_hijo: Decimal = D("1710")
    credito_conyuge: Decimal = D("2590")
    # Otros
    dias_mes: int = 30
    dias_incapacidad_patrono: int = 3
    max_pct_embargo: Decimal = D("25")
    # Art. 172 CT: menor salario mínimo mensual del decreto vigente (servicio doméstico,
    # Decreto 45303-MTSS para 2026). Hasta ese monto el salario es inembargable.
    salario_minimo_inembargable: Decimal = D("268731.31")
    # 0 = sin límite de edad (CCSS: desde el 1/1/2025 la BMC reducida aplica a todas las edades)
    edad_limite_bmc_reducida: int = 0
    # Base mínima contributiva por régimen (CCSS: la BMC aplica a las cuotas del SEM y del IVM,
    # no al resto: Banco Popular, FCL, ROP, IMAS, INA, Asignaciones). Si faltan, se usa el modo
    # anterior: toda la tasa sobre la BMC.
    bmc_ivm_mensual: Decimal | None = None
    ccss_sem_obrero: Decimal | None = None
    ccss_ivm_obrero: Decimal | None = None
    ccss_sem_patronal: Decimal | None = None
    ccss_ivm_patronal: Decimal | None = None


PARAMETROS_2026 = ParametrosLegales(
    vigente_desde=date(2026, 1, 1),
    ccss_obrero=D("0.1083"),
    ccss_obrero_pensionado_estado=D("0.065"),
    ccss_patronal=D("0.2683"),
    # CCSS, facturación desde enero 2026: BMC del SEM ₡346 789 y del IVM ₡324 590 (Odoo usaba
    # ₡333 328 para todo). Tasas por régimen: SEM 5,50 / 9,25 %; IVM 4,33 / 5,58 % (Transitorio XI
    # del Reglamento del IVM).
    bmc_mensual=D("346789"),
    bmc_ivm_mensual=D("324590"),
    ccss_sem_obrero=D("0.055"), ccss_ivm_obrero=D("0.0433"),
    ccss_sem_patronal=D("0.0925"), ccss_ivm_patronal=D("0.0558"),
    ins_tasas={
        "I": D("0.0087"), "II": D("0.0149"), "III": D("0.0247"),
        "IV": D("0.0413"), "V": D("0.0688"),
    },
    # Art. 29 CT: días por año según la antigüedad total. Odoo tenía año 3 = 20
    # (hallazgo H8); la ley dice 20.5.
    cesantia_tabla={
        1: D("19.5"), 2: D("20"), 3: D("20.5"), 4: D("21"), 5: D("21.24"),
        6: D("21.5"), 7: D("22"), 8: D("22"), 9: D("22"), 10: D("21.5"),
        11: D("21"), 12: D("20.5"), 13: D("20"),
    },
    tramos_renta=(
        Tramo(D("0"), D("918000"), D("0")),
        Tramo(D("918000"), D("1347000"), D("10")),
        Tramo(D("1347000"), D("2364000"), D("15")),
        Tramo(D("2364000"), D("4727000"), D("20")),
        Tramo(D("4727000"), None, D("25")),
    ),
)

def desde_json(vigente_desde: date, datos: dict) -> ParametrosLegales:
    """Construye parámetros a partir del JSON guardado en `parametros_legales`."""
    dec = {"ccss_obrero", "ccss_obrero_pensionado_estado", "ccss_patronal", "bmc_mensual",
           "rop_obrero", "rop_patronal", "prov_aguinaldo", "prov_vacaciones",
           "prov_cesantia_defecto", "credito_hijo", "credito_conyuge", "max_pct_embargo",
           "salario_minimo_inembargable", "bmc_ivm_mensual", "ccss_sem_obrero", "ccss_ivm_obrero",
           "ccss_sem_patronal", "ccss_ivm_patronal"}
    enteros = {"dias_mes", "dias_incapacidad_patrono", "cesantia_max_anios", "edad_limite_bmc_reducida"}
    kw: dict = {}
    for k, v in datos.items():
        if k in dec:
            kw[k] = D(str(v))
        elif k in enteros:
            kw[k] = int(v)
        elif k == "ins_tasas":
            kw[k] = {c: D(str(t)) for c, t in v.items()}
        elif k == "ins_clase_defecto":
            kw[k] = v
        elif k == "cesantia_tabla":
            kw[k] = {int(a): D(str(d)) for a, d in v.items()}
        elif k == "tramos_renta":
            kw[k] = tuple(Tramo(D(str(t["desde"])), None if t.get("hasta") in (None, "") else D(str(t["hasta"])),
                                D(str(t["tasa"]))) for t in v)
    return ParametrosLegales(vigente_desde=vigente_desde, **kw)


def a_json(p: ParametrosLegales) -> dict:
    """Fotografía serializable de los parámetros (se guarda en cada boleta)."""
    return {
        "vigente_desde": p.vigente_desde.isoformat(),
        "ccss_obrero": str(p.ccss_obrero), "ccss_obrero_pensionado_estado": str(p.ccss_obrero_pensionado_estado),
        "ccss_patronal": str(p.ccss_patronal), "bmc_mensual": str(p.bmc_mensual),
        "ins_tasas": {k: str(v) for k, v in p.ins_tasas.items()}, "ins_clase_defecto": p.ins_clase_defecto,
        "rop_obrero": str(p.rop_obrero), "rop_patronal": str(p.rop_patronal),
        "prov_aguinaldo": str(p.prov_aguinaldo), "prov_vacaciones": str(p.prov_vacaciones),
        "prov_cesantia_defecto": str(p.prov_cesantia_defecto),
        "cesantia_tabla": {str(k): str(v) for k, v in p.cesantia_tabla.items()},
        "cesantia_max_anios": p.cesantia_max_anios,
        "tramos_renta": [{"desde": str(t.desde), "hasta": None if t.hasta is None else str(t.hasta),
                          "tasa": str(t.tasa)} for t in p.tramos_renta],
        "credito_hijo": str(p.credito_hijo), "credito_conyuge": str(p.credito_conyuge),
        "dias_mes": p.dias_mes, "dias_incapacidad_patrono": p.dias_incapacidad_patrono,
        "max_pct_embargo": str(p.max_pct_embargo), "edad_limite_bmc_reducida": p.edad_limite_bmc_reducida,
        "salario_minimo_inembargable": str(p.salario_minimo_inembargable),
        **{k: str(getattr(p, k)) for k in ("bmc_ivm_mensual", "ccss_sem_obrero", "ccss_ivm_obrero", "ccss_sem_patronal",
                                           "ccss_ivm_patronal") if getattr(p, k) is not None},
    }


_REGISTRO: list[ParametrosLegales] = [PARAMETROS_2026]


def registrar(p: ParametrosLegales) -> None:
    """Agrega un juego de parametros (p. ej. 2027) al registro."""
    _REGISTRO.append(p)
    _REGISTRO.sort(key=lambda x: x.vigente_desde)


def parametros_para(fecha: date) -> ParametrosLegales:
    """Juego vigente en `fecha`: el de mayor vigente_desde <= fecha."""
    candidatos = [p for p in _REGISTRO if p.vigente_desde <= fecha]
    if not candidatos:
        raise ValueError(f"No hay parametros legales vigentes para {fecha}")
    return candidatos[-1]
