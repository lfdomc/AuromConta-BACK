-- Control de las tareas programadas (pg_cron llama a /cron/* de la API).
-- Cada ejecución queda registrada y la API la usa para:
--   * no correr la misma tarea dos veces a la vez (índice único «una en curso por tarea»);
--   * no repetirla antes de su intervalo mínimo (si pg_cron o alguien la llama de más);
--   * poner un tope diario de ejecuciones por tarea.
-- Así una llamada repetida o un secreto filtrado no pueden disparar cobros, correos o consultas sin control.
-- Se puede correr más de una vez.

create table if not exists negocio.ejecuciones_cron (
  id bigserial primary key,
  tarea text not null,
  inicio timestamptz not null default now(),
  fin timestamptz,
  estado text not null default 'en_curso' check (estado in ('en_curso', 'ok', 'error', 'abandonada')),
  duracion_ms int,
  detalle text
);
create unique index if not exists ejecuciones_cron_una_en_curso on negocio.ejecuciones_cron (tarea)
  where estado = 'en_curso';
create index if not exists ejecuciones_cron_tarea_inicio on negocio.ejecuciones_cron (tarea, inicio desc);

alter table negocio.ejecuciones_cron enable row level security;
drop policy if exists ver on negocio.ejecuciones_cron;
create policy ver on negocio.ejecuciones_cron for select to authenticated using ((select public.es_admin_plataforma()));
revoke all on negocio.ejecuciones_cron from anon;
grant select on negocio.ejecuciones_cron to authenticated;
