"""Guarda automática contra inyección SQL.

Regla del proyecto: el texto SQL nunca se arma con datos. Toda consulta que llega a
`execute`/`executemany` debe ser:
  - un texto literal (con marcadores %s / %(nombre)s para los valores), o
  - una composición de `psycopg.sql` (SQL, Identifier, Literal, Placeholder, Composed)
    en la que cada `sql.SQL(...)` recibe solo textos literales.
Se prohíben f-strings, concatenaciones con variables y `.format()` sobre str.

La prueba analiza el código (AST) de la API, así que falla en cuanto alguien agregue
una consulta armada con texto dinámico.
"""
import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
SQL_SEGUROS = {"SQL", "Identifier", "Literal", "Placeholder", "Composed", "DEFAULT", "NULL"}


class Ambito:
    """Funciones anidadas vistas como un solo ámbito: busca asignaciones de adentro hacia afuera."""

    def __init__(self, funciones):
        self.funciones = funciones
        self.args = funciones[0].args

    def bloques(self):
        return self.funciones


class Revisor:
    def __init__(self, arbol: ast.Module):
        self.modulo = arbol
        self.constantes = self._constantes_modulo()
        # Funciones del módulo que devuelven composiciones de psycopg.sql (anotadas así).
        self.funciones_sql = {
            n.name for n in arbol.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.returns is not None
            and any(t in ast.unparse(n.returns) for t in ("sql.Composable", "sql.Identifier", "sql.SQL", "sql.Composed"))}
        # Funciones que devuelven tuple[sql.X, ...]: posiciones con composición SQL.
        self.tuplas_sql = {}
        for n in arbol.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(n.returns, ast.Subscript) \
                    and ast.unparse(n.returns.value) == "tuple" and isinstance(n.returns.slice, ast.Tuple):
                pos = {i for i, e in enumerate(n.returns.slice.elts)
                       if any(t in ast.unparse(e) for t in ("sql.Composable", "sql.Composed", "sql.SQL", "sql.Identifier"))}
                if pos:
                    self.tuplas_sql[n.name] = pos
        # Constantes de módulo que son composiciones seguras (p. ej. listas de columnas).
        cambio = True
        while cambio:
            cambio = False
            for n in arbol.body:
                if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                    destinos = n.targets if isinstance(n, ast.Assign) else [n.target]
                    for d in destinos:
                        if isinstance(d, ast.Name) and d.id not in self.constantes and \
                                (self.consulta_segura(n.value, None) or self._dict_de_sql(n.value)):
                            self.constantes.add(d.id)
                            cambio = True

    def _dict_de_sql(self, v) -> bool:
        if isinstance(v, ast.Dict):
            return all(self.consulta_segura(x, None) for x in v.values)
        if isinstance(v, (ast.Tuple, ast.List, ast.Set)):
            return all(self.consulta_segura(x, None) for x in v.elts)
        return False

    def _constantes_modulo(self) -> set[str]:
        """Nombres de módulo cuyo valor es un literal o un contenedor de literales."""
        nombres = set()
        for n in self.modulo.body:
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                destinos = n.targets if isinstance(n, ast.Assign) else [n.target]
                if self._solo_literales(n.value):
                    nombres |= {d.id for d in destinos if isinstance(d, ast.Name)}
        return nombres

    def _solo_literales(self, v) -> bool:
        if isinstance(v, ast.Constant):
            return True
        if isinstance(v, (ast.Tuple, ast.List, ast.Set)):
            return all(self._solo_literales(x) for x in v.elts)
        if isinstance(v, ast.Dict):
            return all(k is None or self._solo_literales(k) for k in v.keys) and all(self._solo_literales(x) for x in v.values)
        if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
            return self._solo_literales(v.left) and self._solo_literales(v.right)
        return False

    def texto_literal(self, v, fn) -> bool:
        """Argumento permitido dentro de sql.SQL(...)."""
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return True
        if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
            return self.texto_literal(v.left, fn) and self.texto_literal(v.right, fn)
        if isinstance(v, ast.Name) and v.id in self.constantes:
            return True
        if isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name) and v.value.id in self.constantes:
            return True
        if isinstance(v, ast.IfExp):
            return self.texto_literal(v.body, fn) and self.texto_literal(v.orelse, fn)
        if isinstance(v, ast.Name) and fn is not None:
            valores = self._asignaciones(fn, v.id, self)
            return bool(valores) and None not in valores and all(self.texto_literal(x, fn) for x in valores)
        return False

    @staticmethod
    def _es_sql(func) -> str | None:
        """'SQL', 'Identifier'... si la llamada es sql.X(...)."""
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "sql":
            return func.attr
        return None

    def consulta_segura(self, v, fn, vistos=None) -> bool:
        vistos = vistos or set()
        if isinstance(v, ast.Constant) and isinstance(v.value, (str, bytes)):
            return True
        if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
            return self.consulta_segura(v.left, fn, vistos) and self.consulta_segura(v.right, fn, vistos)
        if isinstance(v, ast.IfExp):
            return self.consulta_segura(v.body, fn, vistos) and self.consulta_segura(v.orelse, fn, vistos)
        if isinstance(v, ast.Call):
            tipo = self._es_sql(v.func)
            if tipo == "SQL":
                return len(v.args) == 1 and not v.keywords and self.texto_literal(v.args[0], fn)
            if tipo == "Composed":
                return len(v.args) == 1 and self.iterable_seguro(v.args[0], fn, vistos)
            if tipo in SQL_SEGUROS:      # Identifier, Literal, Placeholder: escapan su contenido
                return True
            if isinstance(v.func, ast.Name) and v.func.id in getattr(self, "funciones_sql", set()):
                return True
            if isinstance(v.func, ast.Attribute) and v.func.attr in ("format", "join"):
                # Solo sobre composiciones de psycopg.sql (no sobre str), y cada pieza también segura.
                if isinstance(v.func.value, ast.Constant) or not self.consulta_segura(v.func.value, fn, vistos):
                    return False
                if v.func.attr == "join":
                    return len(v.args) == 1 and self.iterable_seguro(v.args[0], fn, vistos)
                return all(self.consulta_segura(a, fn, vistos) for a in v.args) and \
                    all(self.consulta_segura(k.value, fn, vistos) for k in v.keywords)
            return False
        if isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name) and v.value.id in self.constantes:
            return True
        if isinstance(v, ast.Name):
            if v.id in self.constantes:
                return True
            if v.id in vistos or fn is None:
                return False
            if self._parametro_tipado(fn, v.id):
                return True
            valores = self._asignaciones(fn, v.id, self)
            return bool(valores) and None not in valores and \
                all(self.consulta_segura(x, fn, vistos | {v.id}) for x in valores)
        return False

    def iterable_seguro(self, v, fn, vistos) -> bool:
        """Lista/tupla/generador de piezas seguras (para .join y sql.Composed)."""
        if isinstance(v, (ast.List, ast.Tuple)):
            return all(self.consulta_segura(x, fn, vistos) for x in v.elts)
        if isinstance(v, (ast.ListComp, ast.GeneratorExp)):
            return self.consulta_segura(v.elt, fn, vistos | self._nombres_comprension(v))
        if isinstance(v, ast.Starred):
            return self.iterable_seguro(v.value, fn, vistos)
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in ("map",) :
            return False
        if isinstance(v, ast.Name) and fn is not None and v.id not in vistos:
            valores = self._asignaciones(fn, v.id, self)
            agregados = self._agregados(fn, v.id)
            if not valores or None in valores or None in agregados:
                return False
            return all(self.iterable_seguro(x, fn, vistos | {v.id}) for x in valores) and \
                all(self.consulta_segura(x, fn, vistos | {v.id}) for x in agregados)
        return False

    @staticmethod
    def _nombres_comprension(v) -> set[str]:
        # Las variables de la comprensión no son consultas: se evalúa el elemento como pieza,
        # y una variable de iteración sola (p. ej. `x for x in ...`) no se acepta.
        return set()

    @staticmethod
    def _agregados(fn, nombre) -> list:
        """Valores agregados con lista.append(x) / lista.extend([...]) / lista += [...]."""
        out = []
        funciones = fn.bloques() if isinstance(fn, Ambito) else [fn]
        for n in (m for f in funciones for m in ast.walk(f)):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == nombre):
                if n.func.attr == "append" and len(n.args) == 1:
                    out.append(n.args[0])
                elif n.func.attr in ("extend", "insert"):
                    arg = n.args[-1] if n.args else None
                    if isinstance(arg, (ast.List, ast.Tuple)):
                        out.extend(arg.elts)
                    else:
                        out.append(None)
        return out

    @staticmethod
    def _parametro_tipado(fn, nombre) -> bool:
        funciones = fn.bloques() if isinstance(fn, Ambito) else [fn]
        for a in [a for f in funciones for a in f.args.args + f.args.kwonlyargs]:
            if a.arg == nombre and a.annotation is not None:
                anot = ast.unparse(a.annotation)
                return any(t in anot for t in ("LiteralString", "Composable", "sql.SQL", "sql.Composed"))
        return False

    @staticmethod
    def _asignaciones(fn, nombre, _rev=None) -> list:
        """Valores asignados a `nombre` en la función. Una forma de asignación que no se
        puede revisar (desempaque, for, :=, with ... as) aparece como None y hace fallar."""
        valores = []

        def nombres_en(t):
            return {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}

        funciones = fn.bloques() if isinstance(fn, Ambito) else [fn]
        for f in funciones:
            valores += Revisor._asignaciones_en(f, nombre, nombres_en, _rev)
            if valores:
                break
        return valores

    @staticmethod
    def _asignaciones_en(fn, nombre, nombres_en, _rev) -> list:
        valores = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == nombre:
                        valores.append(n.value)
                    elif not isinstance(t, ast.Name) and nombre in nombres_en(t) and not isinstance(t, (ast.Subscript, ast.Attribute)):
                        # Desempaque (a, b = ...): se acepta solo si el valor es una tupla literal alineada.
                        if isinstance(t, ast.Tuple) and isinstance(n.value, ast.Tuple) and len(t.elts) == len(n.value.elts):
                            for ti, vi in zip(t.elts, n.value.elts):
                                if isinstance(ti, ast.Name) and ti.id == nombre:
                                    valores.append(vi)
                        elif (isinstance(t, ast.Tuple) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
                              and _rev is not None and n.value.func.id in _rev.tuplas_sql):
                            idx = [i for i, ti in enumerate(t.elts) if isinstance(ti, ast.Name) and ti.id == nombre]
                            # Solo cuenta como segura la posición que la función anota como sql.*
                            valores.append(ast.Constant("") if idx and idx[0] in _rev.tuplas_sql[n.value.func.id] else None)
                        else:
                            valores.append(None)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == nombre and n.value:
                valores.append(n.value)
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == nombre:
                valores.append(n.value)
            elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and nombre in nombres_en(n.target):
                valores.append(None)
            elif isinstance(n, ast.NamedExpr) and n.target.id == nombre:
                valores.append(None)
            elif isinstance(n, ast.withitem) and n.optional_vars is not None and nombre in nombres_en(n.optional_vars):
                valores.append(None)
        return valores


def _infracciones():
    malas = []
    for archivo in sorted(APP.rglob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        rev = Revisor(arbol)
        padres = {}
        for n in ast.walk(arbol):
            for h in ast.iter_child_nodes(n):
                padres[h] = n
        for n in ast.walk(arbol):
            es_execute = (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                          and n.func.attr in ("execute", "executemany", "copy") and n.args)
            es_ayudante = (isinstance(n, ast.Call) and len(n.args) >= 2 and (
                (isinstance(n.func, ast.Attribute) and n.func.attr in ("uno", "todos")) or
                (isinstance(n.func, ast.Name) and n.func.id in ("uno", "todos"))))
            if not (es_execute or es_ayudante):
                continue
            # Funciones que encierran la llamada (de la más interna a la más externa): una
            # variable puede venir de la función de afuera (clausura).
            cadena, x = [], n
            while x in padres:
                x = padres[x]
                if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cadena.append(x)
            fn = Ambito(cadena) if cadena else None
            arg = n.args[1] if es_ayudante else n.args[0]
            if not rev.consulta_segura(arg, fn):
                malas.append(f"{archivo.relative_to(APP.parent)}:{n.lineno}: {ast.unparse(arg)[:90]}")
    return malas


def test_ninguna_consulta_se_arma_con_texto_dinamico():
    malas = _infracciones()
    assert not malas, "Consultas SQL armadas con texto dinámico:\n" + "\n".join(malas)


def test_el_revisor_detecta_inyeccion():
    codigo = ast.parse('''
def mala(conn, nombre):
    conn.execute(f"select * from t where n = '{nombre}'")
def mala2(conn, orden):
    q = "select * from t order by " + orden
    conn.execute(q)
def mala3(conn, x):
    conn.execute(sql.SQL(x))
def mala4(conn, x):
    conn.execute(sql.SQL("select {}").format(sql.SQL(x)))
def mala5(conn, x):
    conn.execute(sql.Composed([sql.SQL(x)]))
def mala6(conn, x):
    q, p = f"select {x}", []
    conn.execute(q, p)
def mala7(conn, xs):
    for q in xs:
        conn.execute(q)
def buena(conn, n):
    q = sql.SQL("select * from {} where n = %s").format(sql.Identifier("t"))
    q += sql.SQL(" limit 5")
    conn.execute(q, (n,))
''')
    rev = Revisor(codigo)
    res = {}
    for fn in codigo.body:
        call = next(c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "execute")
        res[fn.name] = rev.consulta_segura(call.args[0], fn)
    assert res == {"mala": False, "mala2": False, "mala3": False, "mala4": False, "mala5": False,
                   "mala6": False, "mala7": False, "buena": True}
