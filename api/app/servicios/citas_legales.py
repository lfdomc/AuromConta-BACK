"""Citas legales cortas para pies de reportes y documentos.

El detalle completo (regla, publicación, portal oficial y qué buscar) está en la tabla
public.base_legal (migración 20261013002300) y en la pantalla «Base legal». Las claves de abajo
coinciden con esa tabla; si cambia una norma, actualizar los dos lugares.
"""
from __future__ import annotations

CITAS: dict[str, str] = {
    "ccss_cuotas": "cuotas CCSS: Ley 17, Reglamento del Seguro de Salud y del IVM (Transitorio XI), Leyes 4351, 5662, 4760, 6868 y 7983",
    "ccss_bmc": "base mínima contributiva: Reglamento del Seguro de Salud art. 63 y del IVM art. 34",
    "ins_riesgos": "riesgos del trabajo: Código de Trabajo art. 193 y Título IV (Ley 6727)",
    "renta": "impuesto al salario: Ley 7092 arts. 32 a 34 y Decreto 45333-H",
    "renta_208": "declaración de retenciones: resolución MH-DGT-RES-0042-2025 (TRIBU-CR)",
    "aguinaldo": "aguinaldo: Ley 2412",
    "vacaciones": "vacaciones: Código de Trabajo arts. 153, 156 y 157",
    "preaviso": "preaviso: Código de Trabajo art. 28",
    "cesantia": "cesantía: Código de Trabajo art. 29",
    "incapacidad": "incapacidades: Código de Trabajo art. 79 y Reglamento del Seguro de Salud art. 35",
    "maternidad": "maternidad, paternidad y adopción: Código de Trabajo art. 95 (Ley 10.211)",
    "embargos": "embargos: Código de Trabajo art. 172; pensiones alimentarias: Ley 7654",
    "horas_extra": "horas extra: Código de Trabajo arts. 139 y 140",
    "salario_minimo": "salarios mínimos: Código de Trabajo art. 177 y Decreto 45303-MTSS",
}

POR_REPORTE: dict[str, list[str]] = {
    "resumen-planilla": ["ccss_cuotas", "renta"],
    "ccss": ["ccss_cuotas", "ccss_bmc"],
    "ins": ["ins_riesgos"],
    "renta": ["renta"],
    "retenciones-208": ["renta", "renta_208"],
    "costo-patronal": ["ccss_cuotas", "ins_riesgos", "aguinaldo", "vacaciones", "cesantia"],
    "costo-sucursal": ["ccss_cuotas", "ins_riesgos"],
    "detalle-empleado": ["ccss_cuotas", "renta"],
    "horas-extra": ["horas_extra"],
    "vacaciones": ["vacaciones"],
    "embargos-pensiones": ["embargos"],
    "incapacidades": ["incapacidad", "maternidad", "ins_riesgos"],
    "resumen-ejecutivo": ["ccss_cuotas", "renta", "ins_riesgos"],
}


def texto(claves: list[str]) -> str:
    """«Base legal: a; b; c.» con las claves dadas (las desconocidas se omiten)."""
    partes = [CITAS[c] for c in claves if c in CITAS]
    if not partes:
        return ""
    t = "; ".join(partes)
    return f"Base legal: {t[0].upper()}{t[1:]}. Detalle y enlaces oficiales en la sección «Base legal» del sistema."


def de_reporte(tipo: str) -> str:
    return texto(POR_REPORTE.get(tipo, []))
