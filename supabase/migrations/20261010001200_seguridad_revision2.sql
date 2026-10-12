-- =============================================================================
-- Planilla Costa Rica · 0012 Segunda revisión independiente de seguridad
-- =============================================================================

-- 1. ---------------------------------------- Nada se escribe desde PostgREST
-- Dentro de una función SECURITY DEFINER el usuario efectivo es el dueño, así que la
-- regla "solo desde la API" se apoya también en session_user: PostgREST siempre se
-- conecta como 'authenticator'. Cualquier escritura que llegue por esa vía (aunque sea
-- a través de una función) se rechaza si no viene marcada por la API.
create or replace function public.tg_solo_api() returns trigger
language plpgsql as $$
begin
  if (current_user = 'authenticated' or session_user = 'authenticator') and not public.via_api() then
    raise exception 'Las modificaciones se hacen desde la aplicación Planilla Costa Rica';
  end if;
  return coalesce(new, old);
end $$;

-- 2. ------------------------------------------------ MFA también fuera de la API
create table if not exists public.ajustes_seguridad (
  clave text primary key,
  valor text not null
);
insert into public.ajustes_seguridad (clave, valor) values ('mfa_requerida', 'no') on conflict do nothing;
alter table public.ajustes_seguridad enable row level security;
revoke all on public.ajustes_seguridad from anon, authenticated;

-- MFA exigida si la API lo marca en la transacción o si está activada para toda la base
-- (la API la activa al arrancar en producción).
create or replace function public.mfa_ok() returns boolean
language sql stable security definer set search_path = public as $$
  select not (coalesce(current_setting('aurom.mfa_requerida', true), '') = 'si'
              or exists (select 1 from ajustes_seguridad where clave = 'mfa_requerida' and valor = 'si'))
      or coalesce(auth.jwt() ->> 'aal', 'aal1') = 'aal2'
      or (auth.uid() is null and session_user <> 'authenticator')   -- tareas del sistema, sin usuario
$$;

-- Cambios salariales autorizados: al insertar y al actualizar.
create or replace function public.tg_historial_mfa() returns trigger
language plpgsql as $$
begin
  if (current_user = 'authenticated' or session_user = 'authenticator') and new.estado = 'autorizado'
     and (tg_op = 'INSERT' or new.estado is distinct from old.estado) and not public.mfa_ok() then
    raise exception 'Autorizar salarios requiere verificación en dos pasos (MFA)';
  end if;
  return new;
end $$;
drop trigger if exists historial_mfa on public.historial_salarial;
create trigger historial_mfa before insert or update on public.historial_salarial
  for each row execute function public.tg_historial_mfa();

create or replace function public.tg_empleado_salario() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and new.salario_mensual is distinct from old.salario_mensual then
    if not public.puede_aprobar(new.empresa_id) then
      raise exception 'El salario se cambia con una solicitud de cambio salarial autorizada';
    end if;
    if not public.mfa_ok() then
      raise exception 'Cambiar salarios requiere verificación en dos pasos (MFA)';
    end if;
  end if;
  if new.empresa_id is distinct from old.empresa_id then
    raise exception 'Un empleado no se puede mover de empresa';
  end if;
  return new;
end $$;

create or replace function public.tg_planilla_mfa() returns trigger
language plpgsql as $$
begin
  if (current_user = 'authenticated' or session_user = 'authenticator') and new.estado is distinct from old.estado
     and (new.estado in ('aprobada','cerrada') or old.estado = 'cerrada') and not public.mfa_ok() then
    raise exception 'Esta acción requiere verificación en dos pasos (MFA)';
  end if;
  return new;
end $$;

-- Agregar usuarios a una empresa es una acción de administración: exige MFA.
create or replace function public.agregar_usuario_empresa(p_empresa uuid, p_correo text, p_rol text)
returns uuid
language plpgsql security definer set search_path = public as $$
declare v_usuario uuid; v_id uuid;
begin
  if not public.es_admin_empresa(p_empresa) then raise exception 'Sin permiso'; end if;
  if not public.mfa_ok() then raise exception 'Esta acción requiere verificación en dos pasos (MFA)'; end if;
  select id into v_usuario from auth.users
   where lower(email) = lower(trim(p_correo)) and email_confirmed_at is not null;
  if v_usuario is null then return null; end if;
  insert into usuarios_empresa (empresa_id, usuario_id, rol, creado_por)
  values (p_empresa, v_usuario, p_rol, auth.uid())
  on conflict (empresa_id, usuario_id) do update set rol = excluded.rol, activo = true
  returning id into v_id;
  return v_id;
end $$;

-- 3. --------------------------------------------- Auditoría: orden sin carreras
-- El id se asigna antes del bloqueo; el orden real de la cadena es `seq`, que se
-- toma dentro del trigger con el bloqueo ya adquirido.
create sequence if not exists public.auditoria_seq;
alter table public.auditoria add column if not exists seq bigint;
create unique index if not exists auditoria_seq_unico on public.auditoria (seq);

create or replace function public.tg_auditoria_cadena() returns trigger
language plpgsql security definer set search_path = public, extensions as $$
declare v_prev text;
begin
  perform pg_advisory_xact_lock(hashtext('aurom.auditoria'));
  select hash into v_prev from auditoria where seq is not null order by seq desc limit 1;
  new.seq := nextval('public.auditoria_seq');
  new.fecha := clock_timestamp();
  new.hash_anterior := coalesce(v_prev, 'GENESIS');
  new.hash := encode(digest(
      new.seq::text || '|' || new.hash_anterior || '|' || coalesce(new.empresa_id::text, '') || '|' || new.tabla || '|' ||
      coalesce(new.registro_id, '') || '|' || new.accion || '|' || coalesce(new.antes::text, '') || '|' ||
      coalesce(new.despues::text, '') || '|' || coalesce(new.usuario_id::text, '') || '|' ||
      to_char(new.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), 'sha256'), 'hex');
  return new;
end $$;

drop function if exists public.verificar_auditoria();
create or replace function public.verificar_auditoria()
returns table (id_alterado bigint, revisados bigint, hash_final text)
language plpgsql stable security definer set search_path = public, extensions as $$
declare r record; v_prev text := 'GENESIS'; n bigint := 0; v_calc text; v_seq bigint := 0;
begin
  if not public.es_admin_plataforma() and (session_user = 'authenticator' or auth.uid() is not null) then
    raise exception 'Solo la plataforma verifica la bitácora completa';
  end if;
  for r in select * from auditoria where seq is not null order by seq loop
    n := n + 1;
    v_calc := encode(digest(
        r.seq::text || '|' || r.hash_anterior || '|' || coalesce(r.empresa_id::text, '') || '|' || r.tabla || '|' ||
        coalesce(r.registro_id, '') || '|' || r.accion || '|' || coalesce(r.antes::text, '') || '|' ||
        coalesce(r.despues::text, '') || '|' || coalesce(r.usuario_id::text, '') || '|' ||
        to_char(r.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), 'sha256'), 'hex');
    -- Un hueco en seq significa filas borradas.
    if r.hash_anterior is distinct from v_prev or r.hash is distinct from v_calc or r.seq <> v_seq + 1 and v_seq > 0 then
      id_alterado := r.id; revisados := n; hash_final := null; return next; return;
    end if;
    v_prev := r.hash; v_seq := r.seq;
  end loop;
  id_alterado := null; revisados := n; hash_final := v_prev; return next;
end $$;
revoke all on function public.verificar_auditoria() from public, anon, authenticated;
grant execute on function public.verificar_auditoria() to authenticated, service_role;

-- 4. ------------------------------------------------------------- TRUNCATE
-- TRUNCATE no pasa por RLS ni por triggers de fila: nadie de la aplicación lo necesita.
revoke truncate on all tables in schema public from authenticated, anon;
revoke truncate on all tables in schema negocio from authenticated, anon;
