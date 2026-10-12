-- =============================================================================
-- Planilla Costa Rica · 0010 Endurecimiento (revisión independiente de seguridad)
--
-- Supabase expone el esquema public por su Data API (PostgREST). Este sistema
-- NO usa esa vía para escribir: todo cambio pasa por la API (FastAPI), que marca
-- su transacción con aurom.via_api = 'si'. Desde PostgREST un usuario no puede
-- fijar esa marca (set_config no está expuesto), así que cualquier escritura de
-- un usuario autenticado que no venga de la API se rechaza. Además se recomienda
-- desactivar la Data API o quitar public/negocio de los esquemas expuestos.
-- =============================================================================

create or replace function public.via_api() returns boolean
language sql stable as $$ select coalesce(current_setting('aurom.via_api', true), '') = 'si' $$;

create or replace function public.tg_solo_api() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and not public.via_api() then
    raise exception 'Las modificaciones se hacen desde la aplicación Planilla Costa Rica';
  end if;
  return coalesce(new, old);
end $$;

-- Aplica la regla a todas las tablas de public y negocio.
do $$
declare t record;
begin
  for t in select schemaname, tablename from pg_tables
            where schemaname in ('public','negocio') and tablename not in ('auditoria')
  loop
    execute format('drop trigger if exists solo_api on %I.%I', t.schemaname, t.tablename);
    execute format('create trigger solo_api before insert or update or delete on %I.%I '
                   'for each row execute function public.tg_solo_api()', t.schemaname, t.tablename);
  end loop;
end $$;

-- ------------------------------------------------ Empresas: columnas protegidas
create or replace function public.tg_empresa_campos() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and not public.es_admin_plataforma()
     and (new.estado_servicio, new.plan, new.referido_por, new.creado_por)
         is distinct from (old.estado_servicio, old.plan, old.referido_por, old.creado_por) then
    raise exception 'Solo la plataforma cambia el estado del servicio, el plan o el referido';
  end if;
  return new;
end $$;
create trigger empresa_campos before update on public.empresas
  for each row execute function public.tg_empresa_campos();

-- ------------------------------------------- Aprobaciones en la base de datos
-- Cambios salariales: siempre nacen pendientes.
create or replace function public.tg_historial_nuevo() returns trigger
language plpgsql as $$
begin
  -- Un aprobador puede registrar historial ya autorizado (p. ej. migración desde Odoo).
  if current_user = 'authenticated' and not public.puede_aprobar(new.empresa_id) then
    new.estado := 'pendiente'; new.autorizado_por := null; new.autorizado_en := null;
  end if;
  return new;
end $$;
create trigger historial_nuevo before insert on public.historial_salarial
  for each row execute function public.tg_historial_nuevo();

-- El salario de la ficha solo lo cambia un aprobador (vía historial autorizado).
create or replace function public.tg_empleado_salario() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and new.salario_mensual is distinct from old.salario_mensual
     and not public.puede_aprobar(new.empresa_id) then
    raise exception 'El salario se cambia con una solicitud de cambio salarial autorizada';
  end if;
  if new.empresa_id is distinct from old.empresa_id then
    raise exception 'Un empleado no se puede mover de empresa';
  end if;
  return new;
end $$;
create trigger empleado_salario before update on public.empleados
  for each row execute function public.tg_empleado_salario();

-- Planillas: nacen en borrador; salir de aprobada exige aprobador.
create or replace function public.tg_planilla_nueva() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' then
    new.estado := 'borrador'; new.aprobada_por := null; new.aprobada_en := null;
    new.cerrada_por := null; new.cerrada_en := null;
  end if;
  return new;
end $$;
create trigger planilla_nueva before insert on public.planillas
  for each row execute function public.tg_planilla_nueva();

create or replace function public.tg_planilla_salir_aprobada() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and old.estado = 'aprobada' and new.estado is distinct from old.estado
     and not public.puede_aprobar(new.empresa_id) then
    raise exception 'Solo un aprobador puede devolver o cerrar una planilla aprobada';
  end if;
  if new.empresa_id is distinct from old.empresa_id then
    raise exception 'Una planilla no se puede mover de empresa';
  end if;
  return new;
end $$;
create trigger planilla_salir_aprobada before update on public.planillas
  for each row execute function public.tg_planilla_salir_aprobada();

-- Boletas y líneas: revisar la planilla vieja y la nueva; no se mueven.
create or replace function public.tg_boleta_inmutable() returns trigger
language plpgsql as $$
declare
  v_new jsonb := case when tg_op <> 'DELETE' then to_jsonb(new) end;
  v_old jsonb := case when tg_op <> 'INSERT' then to_jsonb(old) end;
  v_campos text[] := case when tg_table_name = 'boletas' then array['planilla_id','empresa_id','empleado_id']
                          else array['boleta_id','empresa_id'] end;
  v_c text;
  v_estado text;
  v_planillas uuid[];
begin
  if tg_op = 'UPDATE' then
    foreach v_c in array v_campos loop
      if v_new ->> v_c is distinct from v_old ->> v_c then
        raise exception 'Una boleta o línea no se puede mover de planilla, boleta, empresa o empleado';
      end if;
    end loop;
  end if;
  if tg_table_name = 'boletas' then
    v_planillas := array[coalesce(v_new, v_old) ->> 'planilla_id']::uuid[];
  else
    select array_agg(b.planilla_id) into v_planillas from public.boletas b
     where b.id = (coalesce(v_new, v_old) ->> 'boleta_id')::uuid;
  end if;
  select min(estado) into v_estado from public.planillas
   where id = any(v_planillas) and estado in ('aprobada','cerrada');
  if v_estado is not null then
    raise exception 'La planilla está %: las boletas no se pueden modificar', v_estado;
  end if;
  return coalesce(new, old);
end $$;

-- ------------------------------------- Empleados de otra empresa (FK compuesta)
alter table public.empleados add constraint empleados_empresa_id_unico unique (empresa_id, id);
do $$
declare t text;
begin
  foreach t in array array['historial_salarial','documentos_empleado','movimientos_empleado','marcas_asistencia',
    'horas_extra','incapacidades','permisos','vacaciones','bonos','otras_deducciones','rebajos_renta','prestamos',
    'embargos','pensiones_alimentarias','boletas','aguinaldos','liquidaciones']
  loop
    execute format('alter table public.%I add constraint %I foreign key (empresa_id, empleado_id) '
                   'references public.empleados (empresa_id, id) on delete cascade', t, t || '_empleado_misma_empresa');
  end loop;
end $$;
alter table public.boletas drop constraint boletas_empleado_misma_empresa;
alter table public.boletas add constraint boletas_empleado_misma_empresa foreign key (empresa_id, empleado_id)
  references public.empleados (empresa_id, id) on delete restrict;
alter table public.aguinaldos drop constraint aguinaldos_empleado_misma_empresa;
alter table public.aguinaldos add constraint aguinaldos_empleado_misma_empresa foreign key (empresa_id, empleado_id)
  references public.empleados (empresa_id, id) on delete restrict;
alter table public.liquidaciones drop constraint liquidaciones_empleado_misma_empresa;
alter table public.liquidaciones add constraint liquidaciones_empleado_misma_empresa foreign key (empresa_id, empleado_id)
  references public.empleados (empresa_id, id) on delete restrict;

-- La planilla de una boleta y el préstamo de un pago deben ser de la misma empresa.
alter table public.planillas add constraint planillas_empresa_id_unico unique (empresa_id, id);
alter table public.boletas add constraint boletas_planilla_misma_empresa foreign key (empresa_id, planilla_id)
  references public.planillas (empresa_id, id) on delete cascade;
alter table public.prestamos add constraint prestamos_empresa_id_unico unique (empresa_id, id);
alter table public.prestamo_pagos add constraint pagos_prestamo_misma_empresa foreign key (empresa_id, prestamo_id)
  references public.prestamos (empresa_id, id) on delete cascade;

-- ------------------------------------------------------------ Portal y socios
drop policy if exists empleado_solicita_vacaciones on public.vacaciones;
create policy empleado_solicita_vacaciones on public.vacaciones for insert to authenticated
  with check (estado = 'solicitada' and tipo = 'disfrute' and monto is null and boleta_id is null and exists (
    select 1 from public.empleados e where e.id = empleado_id and e.empresa_id = vacaciones.empresa_id
      and e.usuario_id = auth.uid()));

create or replace function public.tg_prospecto_campos() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and not public.es_admin_plataforma() then
    if tg_op = 'INSERT' then
      new.vence_atribucion := current_date + 90; new.estado := 'abierto'; new.empresa_id := null;
      new.registrado_en := now();
    end if;
  end if;
  return new;
end $$;
create trigger prospecto_campos before insert on public.prospectos
  for each row execute function public.tg_prospecto_campos();

-- Telegram: códigos largos; el vínculo (chat, activo, rol) solo lo cambia el sistema.
alter table public.telegram_vinculos alter column codigo_vinculacion
  set default upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 16));
create or replace function public.tg_telegram_campos() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and not public.es_admin_plataforma() then
    if tg_op = 'INSERT' then
      new.chat_id := null; new.activo := false; new.creado_en := now();
      new.codigo_vinculacion := upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 16));
    elsif (new.chat_id, new.activo, new.rol, new.creado_en, new.codigo_vinculacion, new.usuario_id)
          is distinct from (old.chat_id, old.activo, old.rol, old.creado_en, old.codigo_vinculacion, old.usuario_id) then
      raise exception 'Solo se pueden cambiar las preferencias de avisos';
    end if;
  end if;
  return new;
end $$;
create trigger telegram_campos before insert or update on public.telegram_vinculos
  for each row execute function public.tg_telegram_campos();

-- ------------------------------------------- Funciones SECURITY DEFINER
revoke execute on function public.crear_cuentas_por_defecto(uuid) from public, anon, authenticated;

create or replace function public.socio_por_codigo(p_codigo text)
returns table (id uuid, nombre text, tipo text, correo text)
language sql stable security definer set search_path = public as $$
  select id, nombre, tipo, null::text from socios
   where codigo_referido = upper(trim(p_codigo)) and estado = 'activo' and auth.uid() is not null
$$;

create or replace function public.planilla_cerrada(p_planilla uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from planillas p where p.id = p_planilla and p.estado = 'cerrada'
                 and exists (select 1 from boletas b join empleados e on e.id = b.empleado_id
                             where b.planilla_id = p.id and e.usuario_id = auth.uid()))
      or exists (select 1 from planillas p where p.id = p_planilla and p.estado = 'cerrada'
                 and public.puede_ver(p.empresa_id))
$$;

-- Vincular cuentas por correo solo si el correo está confirmado.
create or replace function public.agregar_usuario_empresa(p_empresa uuid, p_correo text, p_rol text)
returns uuid
language plpgsql security definer set search_path = public as $$
declare v_usuario uuid; v_id uuid;
begin
  if not public.es_admin_empresa(p_empresa) then raise exception 'Sin permiso'; end if;
  select id into v_usuario from auth.users
   where lower(email) = lower(trim(p_correo)) and email_confirmed_at is not null;
  if v_usuario is null then return null; end if;
  insert into usuarios_empresa (empresa_id, usuario_id, rol, creado_por)
  values (p_empresa, v_usuario, p_rol, auth.uid())
  on conflict (empresa_id, usuario_id) do update set rol = excluded.rol, activo = true
  returning id into v_id;
  return v_id;
end $$;

create or replace function public.buscar_usuario_por_correo(p_correo text, p_empresa uuid default null)
returns uuid
language plpgsql stable security definer set search_path = public as $$
declare v_id uuid;
begin
  if not (public.es_admin_plataforma() or (p_empresa is not null and public.es_admin_empresa(p_empresa))) then
    raise exception 'Sin permiso para buscar usuarios';
  end if;
  select id into v_id from auth.users
   where lower(email) = lower(trim(p_correo)) and email_confirmed_at is not null limit 1;
  -- Un administrador de empresa solo obtiene el id si la persona ya trabaja en esa empresa
  -- o se va a vincular a ella; para no exponer cuentas ajenas se devuelve únicamente a la
  -- plataforma o cuando la cuenta ya está ligada a esa empresa (como usuario o empleado).
  if v_id is not null and not public.es_admin_plataforma() and not exists (
       select 1 from usuarios_empresa where empresa_id = p_empresa and usuario_id = v_id)
     and not exists (select 1 from empleados where empresa_id = p_empresa and lower(correo) = lower(trim(p_correo))) then
    return null;
  end if;
  return v_id;
end $$;

-- Ninguna función nueva queda ejecutable por anon de forma implícita.
alter default privileges in schema public revoke execute on functions from public;
alter default privileges in schema negocio revoke execute on functions from public;
