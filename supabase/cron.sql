-- Tareas programadas con pg_cron + pg_net. Ejecutar una vez en el SQL Editor de Supabase.
-- El secreto del cron y la URL de la API se guardan cifrados en Supabase Vault
-- (no quedan en texto plano dentro de los trabajos de pg_cron).
create extension if not exists pg_cron;
create extension if not exists pg_net;

-- 1) Guardar los valores (reemplace y ejecute una sola vez):
-- select vault.create_secret('<CRON_SECRET>', 'aurom_cron_secret');
-- select vault.create_secret('https://<servicio>.up.railway.app', 'aurom_api_url');

-- 2) Función que llama a la API leyendo el Vault en cada ejecución.
create or replace function public.llamar_cron(p_ruta text) returns bigint
language plpgsql security definer set search_path = public, extensions as $$
declare v_url text; v_secreto text;
begin
  select decrypted_secret into v_url from vault.decrypted_secrets where name = 'aurom_api_url';
  select decrypted_secret into v_secreto from vault.decrypted_secrets where name = 'aurom_cron_secret';
  return net.http_post(url := v_url || p_ruta, headers := jsonb_build_object('X-Cron-Secret', v_secreto));
end $$;
revoke all on function public.llamar_cron(text) from public, anon, authenticated;

-- 3) Horarios (UTC; Costa Rica = UTC-6)
select cron.schedule('aurom-ancla-auditoria', '50 12 * * *',  $$ select public.llamar_cron('/cron/ancla-auditoria') $$); -- 6:50 a. m.
select cron.schedule('aurom-diario',          '0 13 * * *',   $$ select public.llamar_cron('/cron/diario') $$);          -- 7:00 a. m.
select cron.schedule('aurom-facturas',        '10 12 1 * *',  $$ select public.llamar_cron('/cron/facturas') $$);        -- día 1, 6:10 a. m.
select cron.schedule('aurom-cobros',          '30 14 * * *',  $$ select public.llamar_cron('/cron/cobros') $$);          -- 8:30 a. m.
select cron.schedule('aurom-salud',           '*/10 * * * *', $$ select public.llamar_cron('/cron/salud') $$);
