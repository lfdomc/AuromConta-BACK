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
  -- 60 s de espera (pg_net usa 5 s por defecto). Si la API tarda más, la tarea igual termina allá;
  -- la API además impide que la misma tarea corra dos veces a la vez o antes de su intervalo.
  return net.http_post(url := v_url || p_ruta, headers := jsonb_build_object('X-Cron-Secret', v_secreto),
                       timeout_milliseconds := 60000);
end $$;
revoke all on function public.llamar_cron(text) from public, anon, authenticated;

-- 3) Horarios (UTC; Costa Rica = UTC-6). Volver a correr este archivo actualiza los horarios (mismo nombre).
--    Pensado para Railway con «Serverless» (la API se duerme sin uso): el chequeo de salud va una vez por
--    hora para dejarla dormir, y cada tarea diaria tiene un «reintento» 5 minutos después, porque la primera
--    llamada a una API dormida puede fallar mientras despierta. Si la primera sí corrió, el reintento no
--    hace nada (la API no repite una tarea antes de su intervalo mínimo).
select cron.schedule('aurom-ancla-auditoria',           '50 12 * * *', $$ select public.llamar_cron('/cron/ancla-auditoria') $$); -- 6:50 a. m.
select cron.schedule('aurom-ancla-auditoria-reintento', '55 12 * * *', $$ select public.llamar_cron('/cron/ancla-auditoria') $$);
select cron.schedule('aurom-facturas',                  '10 12 * * *', $$ select public.llamar_cron('/cron/facturas') $$);        -- 6:10 a. m.
select cron.schedule('aurom-facturas-reintento',        '15 12 * * *', $$ select public.llamar_cron('/cron/facturas') $$);
select cron.schedule('aurom-diario',                    '0 13 * * *',  $$ select public.llamar_cron('/cron/diario') $$);          -- 7:00 a. m.
select cron.schedule('aurom-diario-reintento',          '5 13 * * *',  $$ select public.llamar_cron('/cron/diario') $$);
select cron.schedule('aurom-cobros',                    '30 14 * * *', $$ select public.llamar_cron('/cron/cobros') $$);          -- 8:30 a. m.
select cron.schedule('aurom-cobros-reintento',          '35 14 * * *', $$ select public.llamar_cron('/cron/cobros') $$);
select cron.schedule('aurom-salud',                     '0 * * * *',   $$ select public.llamar_cron('/cron/salud') $$);           -- cada hora en punto

-- 4) Limpieza: pg_cron guarda cada ejecución en cron.job_run_details y no la borra solo.
--    Domingos 3:00 a. m.: se conservan 14 días.
select cron.schedule('aurom-limpiar-historial-cron', '0 9 * * 0',
  $$ delete from cron.job_run_details where end_time < now() - interval '14 days' $$);

-- 5) Revisar lo programado y las últimas corridas:
-- select jobname, schedule, active from cron.job order by jobname;
-- select tarea, inicio, estado, duracion_ms, detalle from negocio.ejecuciones_cron order by inicio desc limit 20;
