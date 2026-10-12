"""Partida doble, saldos y estados financieros."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Literal

CERO = Decimal("0.00")
CENTIMO = Decimal("0.01")

Tipo = Literal["activo", "pasivo", "patrimonio", "ingreso", "costo", "gasto"]
Naturaleza = Literal["deudora", "acreedora"]
TIPOS: tuple[str, ...] = ("activo", "pasivo", "patrimonio", "ingreso", "costo", "gasto")
TIPOS_RESULTADO = frozenset({"ingreso", "costo", "gasto"})


def r2(v) -> Decimal:
    return Decimal(str(v)).quantize(CENTIMO, rounding=ROUND_HALF_UP)


def naturaleza_de(tipo: str) -> Naturaleza:
    """Naturaleza normal de cada tipo de cuenta (las cuentas correctoras la invierten)."""
    if tipo not in TIPOS:
        raise ValueError(f"Tipo de cuenta desconocido: {tipo}")
    return "deudora" if tipo in ("activo", "costo", "gasto") else "acreedora"


def saldo_natural(naturaleza: str, debito, credito) -> Decimal:
    """Saldo con el signo de la cuenta: positivo cuando está en su lado normal."""
    d, c = r2(debito), r2(credito)
    return d - c if naturaleza == "deudora" else c - d


@dataclass(frozen=True)
class Cuenta:
    id: str
    codigo: str
    nombre: str
    tipo: str
    naturaleza: str
    padre_id: str | None = None
    acepta_movimientos: bool = True
    activa: bool = True


@dataclass(frozen=True)
class Linea:
    cuenta_id: str
    debito: Decimal = CERO
    credito: Decimal = CERO
    descripcion: str = ""


@dataclass
class Totales:
    debito: Decimal = field(default_factory=lambda: CERO)
    credito: Decimal = field(default_factory=lambda: CERO)

    def sumar(self, otro: "Totales") -> "Totales":
        return Totales(self.debito + otro.debito, self.credito + otro.credito)

    @property
    def neto(self) -> Decimal:
        """Débito menos crédito (positivo = saldo deudor)."""
        return self.debito - self.credito

    @property
    def vacio(self) -> bool:
        return self.debito == 0 and self.credito == 0


# ------------------------------------------------------------------ Validación
def validar_asiento(lineas: Iterable[Linea], cuentas: dict[str, Cuenta]) -> list[str]:
    """Errores que impiden contabilizar (lista vacía = válido)."""
    errores: list[str] = []
    lineas = list(lineas)
    if len(lineas) < 2:
        errores.append("El asiento necesita al menos dos líneas")
    deb = cred = CERO
    for i, l in enumerate(lineas, 1):
        d, c = Decimal(l.debito), Decimal(l.credito)
        if d < 0 or c < 0:
            errores.append(f"Línea {i}: los montos no pueden ser negativos")
        if (d > 0) == (c > 0):
            errores.append(f"Línea {i}: debe tener débito o crédito, no ambos ni ninguno")
        if d != r2(d) or c != r2(c):
            errores.append(f"Línea {i}: los montos van con dos decimales como máximo")
        cta = cuentas.get(l.cuenta_id)
        if cta is None:
            errores.append(f"Línea {i}: la cuenta no existe en el catálogo de la empresa")
        elif not cta.acepta_movimientos:
            errores.append(f"Línea {i}: la cuenta {cta.codigo} es de agrupación y no recibe movimientos")
        elif not cta.activa:
            errores.append(f"Línea {i}: la cuenta {cta.codigo} está inactiva")
        deb += d
        cred += c
    if deb != cred:
        errores.append(f"El asiento no cuadra: débitos {deb:,.2f} y créditos {cred:,.2f}")
    elif deb == 0:
        errores.append("El asiento no tiene montos")
    return errores


def asiento_reverso(lineas: Iterable[Linea], prefijo: str = "Reversión: ") -> list[Linea]:
    """Mismas cuentas con débitos y créditos intercambiados."""
    return [Linea(l.cuenta_id, debito=r2(l.credito), credito=r2(l.debito), descripcion=(prefijo + l.descripcion)[:200])
            for l in lineas]


# ------------------------------------------------------------------- Árbol
def _hijos(cuentas: list[Cuenta]) -> tuple[list[Cuenta], dict[str, list[Cuenta]]]:
    por_id = {c.id: c for c in cuentas}
    hijos: dict[str, list[Cuenta]] = defaultdict(list)
    raices: list[Cuenta] = []
    for c in cuentas:
        if c.padre_id and c.padre_id in por_id and c.padre_id != c.id:
            hijos[c.padre_id].append(c)
        else:
            raices.append(c)
    orden = lambda x: (x.codigo, x.nombre)  # noqa: E731
    raices.sort(key=orden)
    for v in hijos.values():
        v.sort(key=orden)
    return raices, hijos


def _acumular(cuentas: list[Cuenta], valores: dict[str, Totales]) -> tuple[list[tuple[Cuenta, int, bool]], dict[str, Totales]]:
    """Recorre el catálogo en orden de código: (cuenta, nivel, es_grupo) y totales con los hijos sumados."""
    raices, hijos = _hijos(cuentas)
    orden: list[tuple[Cuenta, int, bool]] = []
    acumulado: dict[str, Totales] = {}
    visitando: set[str] = set()

    def visitar(c: Cuenta, nivel: int) -> Totales:
        if c.id in visitando:   # catálogo con ciclo: se corta (la base también lo impide)
            return Totales()
        visitando.add(c.id)
        es_grupo = bool(hijos.get(c.id))
        orden.append((c, nivel, es_grupo))
        t = valores.get(c.id, Totales())
        t = Totales(r2(t.debito), r2(t.credito))
        for h in hijos.get(c.id, []):
            t = t.sumar(visitar(h, nivel + 1))
        acumulado[c.id] = t
        return t

    for c in raices:
        visitar(c, 0)
    return orden, acumulado


# ------------------------------------------------------ Balance de comprobación
def balance_comprobacion(cuentas: list[Cuenta], iniciales: dict[str, Totales], movimientos: dict[str, Totales],
                         incluir_vacias: bool = False) -> dict:
    """Saldo inicial, débitos, créditos y saldo final por cuenta (los grupos suman a sus hijos).

    Los totales del pie se toman solo de las cuentas de detalle para no contar dos veces.
    """
    ini_ac = _acumular(cuentas, iniciales)[1]
    orden, mov_ac = _acumular(cuentas, movimientos)
    filas = []
    tot = {"debitos": CERO, "creditos": CERO, "saldo_deudor": CERO, "saldo_acreedor": CERO}
    for c, nivel, grupo in orden:
        i, m = ini_ac[c.id], mov_ac[c.id]
        if not incluir_vacias and i.vacio and m.vacio:
            continue
        final = i.sumar(m)
        neto = final.neto
        fila = {
            "id": c.id, "codigo": c.codigo, "nombre": c.nombre, "tipo": c.tipo, "naturaleza": c.naturaleza,
            "nivel": nivel, "es_grupo": grupo,
            "saldo_inicial": saldo_natural(c.naturaleza, i.debito, i.credito),
            "debitos": m.debito, "creditos": m.credito,
            "saldo_final": saldo_natural(c.naturaleza, final.debito, final.credito),
            "saldo_deudor": neto if neto > 0 else CERO, "saldo_acreedor": -neto if neto < 0 else CERO,
        }
        filas.append(fila)
        if not grupo:
            tot["debitos"] += m.debito
            tot["creditos"] += m.credito
            tot["saldo_deudor"] += fila["saldo_deudor"]
            tot["saldo_acreedor"] += fila["saldo_acreedor"]
    tot["cuadra"] = tot["debitos"] == tot["creditos"] and tot["saldo_deudor"] == tot["saldo_acreedor"]
    return {"filas": filas, "totales": tot}


# ---------------------------------------------------------- Estado de resultados
def _seccion(cuentas: list[Cuenta], valores: dict[str, Totales], tipo: str) -> dict:
    """Filas de un tipo (ingreso/costo/gasto) con el signo de la sección: una cuenta correctora resta."""
    sub = [c for c in cuentas if c.tipo == tipo]
    orden, acumulado = _acumular(sub, valores)
    signo_ingreso = tipo == "ingreso"
    filas, total = [], CERO
    for c, nivel, grupo in orden:
        t = acumulado[c.id]
        if t.vacio:
            continue
        monto = (t.credito - t.debito) if signo_ingreso else (t.debito - t.credito)
        filas.append({"id": c.id, "codigo": c.codigo, "nombre": c.nombre, "nivel": nivel, "es_grupo": grupo, "monto": monto})
        if not grupo:
            total += monto
    return {"filas": filas, "total": total}


def estado_resultados(cuentas: list[Cuenta], movimientos: dict[str, Totales]) -> dict:
    ingresos = _seccion(cuentas, movimientos, "ingreso")
    costos = _seccion(cuentas, movimientos, "costo")
    gastos = _seccion(cuentas, movimientos, "gasto")
    bruta = ingresos["total"] - costos["total"]
    return {"ingresos": ingresos, "costos": costos, "gastos": gastos,
            "utilidad_bruta": bruta, "utilidad_neta": bruta - gastos["total"]}


# ---------------------------------------------------------------- Balance general
def balance_general(cuentas: list[Cuenta], saldos: dict[str, Totales]) -> dict:
    """Situación financiera a una fecha con los saldos acumulados de toda la historia.

    El resultado de las cuentas de ingreso, costo y gasto que todavía no se cerraron se
    muestra en el patrimonio como "Utilidad (pérdida) del ejercicio".
    """
    def seccion(tipo: str) -> dict:
        sub = [c for c in cuentas if c.tipo == tipo]
        orden, acumulado = _acumular(sub, saldos)
        filas, total = [], CERO
        for c, nivel, grupo in orden:
            t = acumulado[c.id]
            if t.vacio:
                continue
            monto = t.neto if tipo == "activo" else -t.neto
            filas.append({"id": c.id, "codigo": c.codigo, "nombre": c.nombre, "nivel": nivel, "es_grupo": grupo,
                          "monto": monto})
            if not grupo:
                total += monto
        return {"filas": filas, "total": total}

    activo, pasivo, patrimonio = seccion("activo"), seccion("pasivo"), seccion("patrimonio")
    resultado = estado_resultados(cuentas, saldos)["utilidad_neta"]
    total_patrimonio = patrimonio["total"] + resultado
    return {"activo": activo, "pasivo": pasivo, "patrimonio": patrimonio, "resultado_ejercicio": resultado,
            "total_patrimonio": total_patrimonio, "total_pasivo_patrimonio": pasivo["total"] + total_patrimonio,
            "cuadra": activo["total"] == pasivo["total"] + total_patrimonio}


# ------------------------------------------------------------------ Cierre anual
def asiento_cierre(cuentas: list[Cuenta], saldos_resultado: dict[str, Totales], cuenta_destino_id: str) -> list[Linea]:
    """Lleva a cero las cuentas de ingreso, costo y gasto y pasa la diferencia a utilidades acumuladas."""
    lineas: list[Linea] = []
    neto_total = CERO
    for c in sorted(cuentas, key=lambda x: x.codigo):
        if c.tipo not in TIPOS_RESULTADO or not c.acepta_movimientos:
            continue
        t = saldos_resultado.get(c.id)
        if not t:
            continue
        neto = r2(t.debito) - r2(t.credito)
        if neto == 0:
            continue
        neto_total += neto
        lineas.append(Linea(c.id, debito=-neto if neto < 0 else CERO, credito=neto if neto > 0 else CERO,
                            descripcion=f"Cierre de {c.codigo} {c.nombre}"[:200]))
    if not lineas:
        return []
    if neto_total == 0:   # ingresos iguales a costos y gastos: no hay nada que llevar a utilidades
        return lineas
    # neto_total > 0: hubo más gasto que ingreso (pérdida) → débito a utilidades.
    lineas.append(Linea(cuenta_destino_id, debito=neto_total if neto_total > 0 else CERO,
                        credito=-neto_total if neto_total < 0 else CERO,
                        descripcion="Utilidad del ejercicio" if neto_total < 0 else "Pérdida del ejercicio"))
    return lineas
