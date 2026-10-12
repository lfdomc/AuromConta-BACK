"""Segunda guarda contra SQL armado con texto: revisión de tipos con pyright.

psycopg 3 declara `execute(query: LiteralString | bytes | sql.SQL | sql.Composed, ...)` y
`sql.SQL(obj: LiteralString)`. Pyright marca cualquier `str` que no sea literal (f-strings,
concatenaciones con variables, `.format()`) que llegue ahí. Solo cubre las llamadas cuyo
`conn` tiene tipo (p. ej. `ctx.conn`, `conn: psycopg.Connection`); test_sql_seguro.py cubre
el resto.

El resto de diagnósticos de pyright (cientos, ajenos a SQL) se ignoran: aquí solo cuentan
los que tocan LiteralString o las firmas de execute/executemany.

Se omite si pyright no está instalado (`npm i -g pyright` o `pip install pyright`).
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

API = pathlib.Path(__file__).resolve().parents[1]
SQL_DIAG = re.compile(r'LiteralString|"(execute|executemany)"|parameter "query"')


@pytest.mark.skipif(shutil.which("pyright") is None, reason="pyright no está instalado")
def test_pyright_no_encuentra_sql_con_texto_dinamico():
    salida = subprocess.run(["pyright", "--outputjson", "-p", str(API / "pyrightconfig.json")],
                            cwd=API, capture_output=True, text=True, timeout=300)
    try:
        datos = json.loads(salida.stdout)
    except json.JSONDecodeError:
        pytest.skip(f"pyright no produjo JSON: {salida.stderr[:300]}")
    malos = [f"{pathlib.Path(d['file']).relative_to(API)}:{d['range']['start']['line'] + 1}: "
             + d["message"].splitlines()[0]
             for d in datos.get("generalDiagnostics", []) if SQL_DIAG.search(d["message"])]
    assert not malos, "pyright encontró SQL que no es LiteralString ni composición de psycopg.sql:\n" + "\n".join(malos)
