-- =============================================================================
-- Planilla Costa Rica · 0011 Seguridad avanzada
--   1. Mínimo privilegio: el rol anónimo no toca nada.
--   2. Auditoría a prueba de alteraciones (cadena de hashes SHA-256, solo agregar).
--   3. MFA (aal2) obligatoria en la base para aprobar, cerrar y reabrir planillas
--      y autorizar salarios, cuando la API la exige.
--   4. Idempotencia de operaciones (la API no repite un cierre o un envío).
-- =============================================================================

-- 1. ------------------------------------------------------- Mínimo privilegio
revoke all on all tables in schema public from anon;
revoke all on all sequences in schema public from anon;
revoke all on all functions in schema public from anon;
revoke all on schema negocio from anon;
revoke all on all tables in schema negocio from anon;
revoke all on all functions in schema negocio from anon;
alter default privileges in schema public revoke all on tables from anon;
alter default privileges in schema public revoke all on functions from anon;
alter default privileges in schema negocio revoke all on tables from anon;

-- 2. ------------------------------------------- Auditoría encadenada (append-only)
alter table public.auditoria add column if not exists hash_anterior text;
alter table public.auditoria add column if not exists hash text;

create or replace function public.tg_auditoria_cadena() returns trigger
language plpgsql security definer set search_path = public, extensions as $$
declare v_prev text;
begin
  -- Un solo escritor a la vez para que la cadena quede en orden.
  perform pg_advisory_xact_lock(hashtext('aurom.auditoria'));
  select hash into v_prev from auditoria order by id desc limit 1;
  new.fecha := clock_timestamp();
  new.hash_anterior := coalesce(v_prev, 'GENESIS');
  new.hash := encode(digest(
      new.hash_anterior || '|' || coalesce(new.empresa_id::text, '') || '|' || new.tabla || '|' ||
      coalesce(new.registro_id, '') || '|' || new.accion || '|' || coalesce(new.antes::text, '') || '|' ||
      coalesce(new.despues::text, '') || '|' || coalesce(new.usuario_id::text, '') || '|' || to_char(new.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
      'sha256'), 'hex');
  return new;
end $$;
drop trigger if exists auditoria_cadena on public.auditoria;
create trigger auditoria_cadena before insert on public.auditoria
  for each row execute function public.tg_auditoria_cadena();

create or replace function public.tg_auditoria_inmutable() returns trigger
language plpgsql as $$
begin
  raise exception 'La bitácora de auditoría no se puede modificar ni borrar';
end $$;
drop trigger if exists auditoria_inmutable on public.auditoria;
create trigger auditoria_inmutable before update or delete on public.auditoria
  for each row execute function public.tg_auditoria_inmutable();
revoke truncate on public.auditoria from authenticated, anon;

-- Verifica la cadena completa: devuelve el primer id alterado (o null si está íntegra).
create or replace function public.verificar_auditoria()
returns table (id_alterado bigint, revisados bigint)
language plpgsql stable security definer set search_path = public, extensions as $$
declare r record; v_prev text := 'GENESIS'; n bigint := 0; v_calc text;
begin
  if not public.es_admin_plataforma() and current_user = 'authenticated' then
    raise exception 'Solo la plataforma verifica la bitácora completa';
  end if;
  for r in select * from auditoria where hash is not null order by id loop
    n := n + 1;
    v_calc := encode(digest(
        r.hash_anterior || '|' || coalesce(r.empresa_id::text, '') || '|' || r.tabla || '|' ||
        coalesce(r.registro_id, '') || '|' || r.accion || '|' || coalesce(r.antes::text, '') || '|' ||
        coalesce(r.despues::text, '') || '|' || coalesce(r.usuario_id::text, '') || '|' || to_char(r.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
        'sha256'), 'hex');
    if r.hash_anterior is distinct from v_prev or r.hash is distinct from v_calc then
      id_alterado := r.id; revisados := n; return next; return;
    end if;
    v_prev := r.hash;
  end loop;
  id_alterado := null; revisados := n; return next;
end $$;
revoke all on function public.verificar_auditoria() from public;
grant execute on function public.verificar_auditoria() to authenticated;

-- 3. -------------------------------------------------------------------- MFA
-- La API fija aurom.mfa_requerida = 'si' en producción. Con esa marca, las acciones
-- críticas exigen que la sesión tenga segundo factor verificado (claim aal = aal2).
create or replace function public.mfa_ok() returns boolean
language sql stable as $$
  select coalesce(current_setting('aurom.mfa_requerida', true), '') <> 'si'
      or coalesce(auth.jwt() ->> 'aal', 'aal1') = 'aal2'
$$;

create or replace function public.tg_planilla_mfa() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and new.estado is distinct from old.estado
     and (new.estado in ('aprobada','cerrada') or old.estado = 'cerrada') and not public.mfa_ok() then
    raise exception 'Esta acción requiere verificación en dos pasos (MFA)';
  end if;
  return new;
end $$;
create trigger planilla_mfa before update on public.planillas
  for each row execute function public.tg_planilla_mfa();

create or replace function public.tg_historial_mfa() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and new.estado is distinct from old.estado and new.estado = 'autorizado'
     and not public.mfa_ok() then
    raise exception 'Autorizar salarios requiere verificación en dos pasos (MFA)';
  end if;
  return new;
end $$;
create trigger historial_mfa before update on public.historial_salarial
  for each row execute function public.tg_historial_mfa();

-- 4. ------------------------------------------------------------- Idempotencia
create table if not exists public.idempotencia (
  usuario_id uuid not null,
  clave text not null check (length(clave) between 8 and 128),
  metodo text not null,
  ruta text not null,
  huella text not null,                -- sha256 del cuerpo: la misma clave con otro cuerpo es un error
  estado text not null default 'en_proceso' check (estado in ('en_proceso','listo')),
  codigo_http int,
  respuesta jsonb,
  creado_en timestamptz not null default now(),
  primary key (usuario_id, clave)
);
create index if not exists idempotencia_vieja on public.idempotencia (creado_en);
alter table public.idempotencia enable row level security;
-- Solo la API (conexión de sistema) la usa; ningún usuario la ve directamente.
revoke all on public.idempotencia from anon, authenticated;

-- 5. ---------------------------------------- Ejecución de funciones por rol
-- Por defecto Postgres da EXECUTE a PUBLIC (incluye anon). Se quita y se concede
-- solo a usuarios autenticados y al rol de servicio.
revoke execute on all functions in schema public from public;
revoke execute on all functions in schema negocio from public;
grant execute on all functions in schema public to authenticated, service_role;
grant execute on all functions in schema negocio to authenticated, service_role;
-- Funciones internas que nadie debe llamar directamente.
revoke execute on function public.crear_cuentas_por_defecto(uuid) from authenticated;
revoke execute on function public._politicas_empresa(regclass) from authenticated;
alter default privileges in schema public revoke execute on functions from public;
alter default privileges in schema negocio revoke execute on functions from public;
