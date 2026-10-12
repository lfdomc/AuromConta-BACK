-- =============================================================================
-- Planilla Costa Rica · 0006 Funciones de apoyo para la API
-- =============================================================================

-- Empresas a las que el usuario tiene acceso, con su rol efectivo.
create or replace function public.mis_empresas()
returns table (id uuid, nombre text, cedula_juridica text, rol text, estado_servicio text, via text)
language sql stable security definer set search_path = public as $$
  select e.id, e.nombre, e.cedula_juridica, ue.rol, e.estado_servicio, 'usuario'
    from usuarios_empresa ue join empresas e on e.id = ue.empresa_id
   where ue.usuario_id = auth.uid() and ue.activo
  union
  select e.id, e.nombre, e.cedula_juridica,
         case when bool_or(a.permiso = 'operacion') then 'contador_operacion' else 'contador_lectura' end,
         e.estado_servicio, 'contador'
    from asignaciones_contador a join socios s on s.id = a.socio_id join empresas e on e.id = a.empresa_id
   where s.usuario_id = auth.uid() and a.revocado_en is null and s.estado = 'activo'
     and not exists (select 1 from usuarios_empresa ue where ue.empresa_id = e.id and ue.usuario_id = auth.uid() and ue.activo)
   group by e.id
  order by 2
$$;
grant execute on function public.mis_empresas() to authenticated;

-- Usuarios de una empresa con su correo (solo para administradores de la empresa).
create or replace function public.usuarios_de_empresa(p_empresa uuid)
returns table (id uuid, usuario_id uuid, correo text, rol text, activo boolean, creado_en timestamptz)
language plpgsql stable security definer set search_path = public as $$
begin
  if not public.es_admin_empresa(p_empresa) then
    raise exception 'Sin permiso';
  end if;
  return query
    select ue.id, ue.usuario_id, u.email::text, ue.rol, ue.activo, ue.creado_en
      from usuarios_empresa ue join auth.users u on u.id = ue.usuario_id
     where ue.empresa_id = p_empresa order by u.email;
end $$;
grant execute on function public.usuarios_de_empresa(uuid) to authenticated;

-- Agregar un usuario existente por correo (para administradores de la empresa).
create or replace function public.agregar_usuario_empresa(p_empresa uuid, p_correo text, p_rol text)
returns uuid
language plpgsql security definer set search_path = public as $$
declare v_usuario uuid; v_id uuid;
begin
  if not public.es_admin_empresa(p_empresa) then raise exception 'Sin permiso'; end if;
  select id into v_usuario from auth.users where lower(email) = lower(trim(p_correo));
  if v_usuario is null then return null; end if;   -- la API envía invitación
  insert into usuarios_empresa (empresa_id, usuario_id, rol, creado_por)
  values (p_empresa, v_usuario, p_rol, auth.uid())
  on conflict (empresa_id, usuario_id) do update set rol = excluded.rol, activo = true
  returning id into v_id;
  return v_id;
end $$;
grant execute on function public.agregar_usuario_empresa(uuid, text, text) to authenticated;

-- Buscar un socio activo por código (para asignarlo como contador).
create or replace function public.socio_por_codigo(p_codigo text)
returns table (id uuid, nombre text, tipo text, correo text)
language sql stable security definer set search_path = public as $$
  select id, nombre, tipo, correo from socios
   where codigo_referido = upper(trim(p_codigo)) and estado = 'activo'
$$;
grant execute on function public.socio_por_codigo(text) to authenticated;

-- Parámetros legales vigentes en una fecha.
create or replace function public.parametros_vigentes(p_fecha date)
returns table (vigente_desde date, datos jsonb)
language sql stable as $$
  select vigente_desde, datos from public.parametros_legales
   where vigente_desde <= p_fecha order by vigente_desde desc limit 1
$$;
grant execute on function public.parametros_vigentes(date) to authenticated;

-- Correo de un usuario (para mostrar quién aprobó, etc.) solo si comparte empresa.
create or replace function public.correo_usuario(p_usuario uuid) returns text
language sql stable security definer set search_path = public as $$
  select u.email::text from auth.users u
   where u.id = p_usuario and (
     p_usuario = auth.uid() or public.es_admin_plataforma()
     or exists (select 1 from usuarios_empresa a join usuarios_empresa b on a.empresa_id = b.empresa_id
                where a.usuario_id = auth.uid() and b.usuario_id = p_usuario))
$$;
grant execute on function public.correo_usuario(uuid) to authenticated;

-- Fichas de empleado del usuario actual (portal), con el nombre de la empresa.
-- El empleado no ve la tabla empresas por RLS, por eso es security definer.
create or replace function public.mis_empleos()
returns table (id uuid, empresa_id uuid, nombre text, apellidos text, empresa text)
language sql stable security definer set search_path = public as $$
  select e.id, e.empresa_id, e.nombre, e.apellidos, em.nombre
    from empleados e join empresas em on em.id = e.empresa_id
   where e.usuario_id = auth.uid()
$$;
grant execute on function public.mis_empleos() to authenticated;
