# Planilla Costa Rica · Back (Supabase + FastAPI)

| Carpeta | Qué es |
|---|---|
| `motor/` | Cálculo de planillas de Costa Rica en Python puro (boleta, renta, CCSS, incapacidades, embargos Art. 172, aguinaldo, liquidación, asiento). Sin base de datos. |
| `motor/` (contabilidad_motor) | Motor contable puro: partida doble, comprobación, estados financieros, cierre anual. |
| `api/` | API FastAPI para Railway. Toda la lógica de montos pasa por aquí. Contrato de rutas: `api/CONTRATO.md`; especificación: `api/openapi.json`. |
| `supabase/` | Migraciones SQL (tablas, RLS multiempresa, auditoría, reglas de aprobación), `cron.sql` y pruebas de seguridad. |

## 1. Supabase

1. Cree el proyecto (región más cercana: `us-east-1`).
2. **Authentication › Providers**: correo con confirmación obligatoria. **URL Configuration**: Site URL `https://planilla.auromtec.com` y redirect `https://planilla.auromtec.com/auth/callback`.
3. Aplique las migraciones en orden (SQL Editor, o desde una terminal):
   ```bash
   ./supabase/aplicar.sh "postgresql://postgres:<clave>@db.<proyecto>.supabase.co:5432/postgres"
   ```
4. **Settings › Data API**: desactívela o quite `public` y `negocio` de los esquemas expuestos. La aplicación no la usa (todo pasa por la API) y la base de todas formas rechaza escrituras que no vengan de la API.
5. Registre a la dueña de la plataforma (después de crear su usuario):
   ```sql
   insert into plataforma_admins (usuario_id) select id from auth.users where email = 'correo@auromtec.com';
   ```
6. Complete `negocio.configuracion` (emisor, códigos CABYS, reparto, renta) desde *Mi negocio › Configuración*.
7. Ejecute `supabase/cron.sql` con la URL de la API y el `CRON_SECRET`.

## 2. API en Railway

1. Nuevo servicio desde el repositorio, carpeta raíz `back/` (usa `Dockerfile` y `railway.json`; chequeo de salud en `/salud`).
2. Variables: copie `.env.example`. `DATABASE_URL` = cadena del *Session pooler* de Supabase.
3. Telegram: registre el webhook una vez
   `https://api.telegram.org/bot<TOKEN>/setWebhook?url=<API_URL>/webhooks/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>`.
4. Tilopay: URL de retorno `<API_URL>/webhooks/tilopay/retorno` y webhook `<API_URL>/webhooks/tilopay` (con el secreto propio).

## 3. Pruebas locales

Requiere Postgres 16 en `host=/tmp port=54322` (o `PG_ADMIN_DSN`).

```bash
pip install -e motor -e "api[test]"
(cd motor && pytest)      # cálculo
(cd supabase && pytest)   # RLS y reglas de la base
(cd api && pytest)        # flujos completos de la API
```

**SQL sin texto dinámico.** Ninguna consulta se arma con f-strings, `+` o `.format()` sobre `str`: se usan textos literales con `%s` o composiciones de `psycopg.sql` (nombres de tablas y columnas solo desde listas cerradas, vía `sql.Identifier`). Dos guardas:

- `api/tests/test_sql_seguro.py` revisa el código (AST) y siempre corre con `pytest`.
- `api/tests/test_sql_tipos.py` corre pyright (configuración en `api/pyrightconfig.json`) y falla si algo que no es `LiteralString` llega a `execute` o a `sql.SQL`. Se omite si pyright no está instalado:
  ```bash
  npm i -g pyright            # o: pip install pyright
  (cd api && pytest tests/test_sql_tipos.py)
  ```
  Pyright reporta además cientos de avisos ajenos a SQL; la prueba solo toma en cuenta los de SQL.

## 4. Seguridad (resumen)

- Cada petición corre en Postgres como el usuario (`role authenticated` + JWT): RLS decide qué empresa ve.
- Accesos por empresa: usuarios (admin, aprobador, operador, lectura), **contadores y vendedores asignados** (lectura u operación, revocables) y la plataforma.
- Aprobaciones, cierres, salarios y estado del servicio se validan también en la base (triggers), no solo en la API.
- Planillas aprobadas o cerradas son inmutables; reabrir exige motivo y queda en auditoría.
- La llave `service_role` solo se usa para enviar invitaciones. Webhooks y cron usan una conexión de sistema protegida por secreto.
