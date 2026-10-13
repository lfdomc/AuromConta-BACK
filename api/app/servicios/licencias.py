"""Licencias especiales según la ley de Costa Rica (Odoo planilla.leave.cr como referencia).

Lo que dice la ley (revisado en octubre de 2026; confirmar cambios con el contador):

* Paternidad — Art. 95 CT, reformado por la Ley 10.211 (La Gaceta, 3/6/2022): 8 días, dos por
  semana durante las primeras cuatro semanas de vida del menor. La paga por partes iguales la
  CCSS y el patrono, sobre el salario promedio de los tres meses anteriores. La Sala IV la
  extendió a la madre no gestante (comaternidad). Es un piso: la empresa puede dar más.
* Adopción — Art. 95 CT (Ley 10.211): tres meses para quien adopte (si adoptan dos personas, se
  reparten). Se paga como la maternidad: 50 % CCSS y 50 % patrono.
* Duelo — Art. 69 inciso l) CT, Ley 10589 (Alcance 195, La Gaceta 227, 3/12/2024): tres días
  hábiles remunerados por la muerte de un pariente de primer grado de consanguinidad o afinidad
  (padres, hijos, cónyuge o conviviente, suegros, yernos y nueras) y un día hábil por segundo
  y tercer grado (abuelos, nietos, hermanos, cuñados, tíos, sobrinos, bisabuelos, bisnietos).
  La paga el patrono (es salario).
* Lactancia — Arts. 95, 97 y 97 bis CT, reformados por la Ley 10895 «Ley para proteger, promover y
  apoyar la lactancia materna» (Alcance 41 a La Gaceta 74, 23/4/2026, rige desde su publicación):
  el primer año de vida es período de lactancia sin certificado; después se prorroga cada tres
  meses con certificado médico (público o privado) mientras el bebé tome leche materna. Una hora
  remunerada por cada bebé lactante en la jornada ordinaria (15 min cada 2 h, dos medias horas, o
  entrar/salir una hora antes); 15 min más cada 3 h de jornada extraordinaria; y, aparte, 25 min
  cada 3 h para extracción de leche, pagados como tiempo trabajado y no acumulables. También aplica
  a la madre adoptante. Nada de esto se rebaja del salario.
* Matrimonio — el Código de Trabajo NO obliga al sector privado: depende del reglamento interno
  o la convención colectiva (Odoo la ponía como obligatoria con «Art. 37», lo cual no es correcto).

En la boleta:
* «patrono»: con goce, el salario de esos días se paga normal (no cambia la planilla).
* «50_50»: los días de ley no se pagan como salario; el patrono paga el 50 % del salario
  promedio diario como «licencia» (no salarial: sin CCSS ni renta) y la CCSS paga el otro 50 %
  directamente al colaborador. Los días que la empresa dé por encima de la ley se pagan normal.
* «sin_goce»: se rebajan del salario y de la base de la CCSS.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TipoLicencia:
    codigo: str
    nombre: str
    base_legal: str
    pago: str                 # patrono | 50_50 | sin_goce | empresa
    dias_ley: int = 0         # 0 = no hay número de días en la ley
    unidad: str = "dias"
    evento: str | None = None  # qué fecha se pide (nacimiento, fallecimiento...)
    parentesco: bool = False
    ventana_dias: int = 0     # plazo para disfrutarla desde el evento (0 = sin plazo)


TIPOS: dict[str, TipoLicencia] = {t.codigo: t for t in [
    TipoLicencia("paternidad", "Paternidad (incluye madre no gestante)", "Art. 95 CT · Ley 10.211", "50_50", 8,
                 evento="Fecha de nacimiento", ventana_dias=28),
    TipoLicencia("adopcion", "Adopción", "Art. 95 CT · Ley 10.211", "50_50", 90,
                 evento="Fecha de entrega del menor"),
    TipoLicencia("duelo_primer_grado", "Duelo — primer grado (padres, hijos, cónyuge o conviviente, suegros, yernos, nueras)",
                 "Art. 69 inc. l) CT · Ley 10589", "patrono", 3, evento="Fecha del fallecimiento", parentesco=True),
    TipoLicencia("duelo_segundo_tercer", "Duelo — segundo y tercer grado (abuelos, nietos, hermanos, cuñados, tíos, sobrinos)",
                 "Art. 69 inc. l) CT · Ley 10589", "patrono", 1, evento="Fecha del fallecimiento", parentesco=True),
    TipoLicencia("lactancia", "Lactancia — hora diaria", "Arts. 95, 97 y 97 bis CT · Ley 10895", "patrono", 0, unidad="horas",
                 evento="Fecha de nacimiento"),
    TipoLicencia("matrimonio", "Matrimonio (según política de la empresa)", "No obligatoria por ley en el sector privado",
                 "empresa"),
    TipoLicencia("con_goce", "Otro permiso con goce de salario", "Acuerdo con la empresa", "patrono"),
    TipoLicencia("sin_goce", "Permiso sin goce de salario", "Acuerdo entre las partes", "sin_goce"),
]}

PARENTESCOS = {
    "duelo_primer_grado": ["Padre", "Madre", "Hijo(a)", "Cónyuge", "Conviviente", "Suegro(a)", "Yerno", "Nuera"],
    "duelo_segundo_tercer": ["Abuelo(a)", "Nieto(a)", "Hermano(a)", "Cuñado(a)", "Tío(a)", "Sobrino(a)",
                             "Bisabuelo(a)", "Bisnieto(a)", "Tío(a) político(a)", "Sobrino(a) político(a)"],
}


def tipo(codigo: str | None) -> TipoLicencia | None:
    return TIPOS.get(codigo or "")


def con_goce(codigo: str, pedido: bool | None) -> bool:
    """Si la ley dice quién paga, eso manda; si es política de la empresa, se respeta lo elegido."""
    t = tipo(codigo)
    if t is None or t.pago == "empresa":
        return bool(pedido)
    return t.pago != "sin_goce"


def avisos(codigo: str, dias, desde, hasta, fecha_evento) -> list[str]:
    """Avisos (no errores: la ley pone un mínimo, la empresa puede dar más)."""
    t = tipo(codigo)
    if not t:
        return []
    out = []
    if t.dias_ley and dias is not None and float(dias) > t.dias_ley:
        extra = "se pagan como salario normal" if t.pago == "50_50" else "corren por cuenta de la empresa"
        out.append(f"La ley da {t.dias_ley} días; los {float(dias) - t.dias_ley:g} de más {extra}.")
    if t.ventana_dias and fecha_evento and hasta and (hasta - fecha_evento).days >= t.ventana_dias:
        out.append(f"La ley la ubica en las primeras {t.ventana_dias // 7} semanas desde el nacimiento.")
    return out


def periodo_lactancia(fecha_nacimiento, desde, hasta, certificado: str | None) -> str | None:
    """Ley 10895: el primer año no pide certificado; después, prórrogas de tres meses con certificado médico."""
    from datetime import timedelta
    try:
        un_anio = fecha_nacimiento.replace(year=fecha_nacimiento.year + 1)
    except ValueError:                                   # 29 de febrero
        un_anio = fecha_nacimiento.replace(year=fecha_nacimiento.year + 1, day=28)
    if hasta <= un_anio:
        return None
    if not certificado:
        return (f"El primer año de lactancia termina el {un_anio:%d/%m/%Y}. Para seguir después se necesita "
                "un certificado médico de prórroga (Ley 10895): anote su número.")
    inicio_prorroga = max(desde, un_anio + timedelta(days=1))
    if (hasta - inicio_prorroga).days > 92:
        return "Cada certificado de prórroga cubre tres meses: registre una licencia por cada certificado."
    return None


def catalogo() -> list[dict]:
    return [{"codigo": t.codigo, "nombre": t.nombre, "base_legal": t.base_legal, "pago": t.pago, "dias_ley": t.dias_ley,
             "unidad": t.unidad, "evento": t.evento, "parentesco": t.parentesco,
             "parentescos": PARENTESCOS.get(t.codigo, [])} for t in TIPOS.values()]
