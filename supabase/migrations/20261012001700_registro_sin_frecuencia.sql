-- El registro de una empresa ya no pide la frecuencia de pago ni crea un calendario de pago.
-- La frecuencia es de cada colaborador; un calendario creado al registrar (p. ej. «Mensual») quedaba
-- asignable a colaboradores quincenales y la planilla calculaba con la frecuencia equivocada.
-- p_frecuencia se mantiene en la firma por compatibilidad y se ignora.

create or replace function public.crear_empresa(
  p_nombre text, p_cedula text default null, p_codigo_referido text default null,
  p_frecuencia text default 'biweekly', p_modulos text[] default array['planilla']
) returns uuid
language plpgsql security definer set search_path = public as $$
declare
  v_empresa uuid;
  v_socio uuid;
  v_tipo text;
  v_mod text;
begin
  if auth.uid() is null then raise exception 'Debe iniciar sesión'; end if;
  if coalesce(array_length(p_modulos, 1), 0) = 0 then raise exception 'Elija al menos un módulo'; end if;
  if exists (select 1 from unnest(p_modulos) m where m not in (select codigo from modulos where disponible)) then
    raise exception 'Módulo no disponible';
  end if;
  if p_codigo_referido is not null then
    select id, tipo into v_socio, v_tipo from socios
     where codigo_referido = upper(trim(p_codigo_referido)) and estado = 'activo';
  end if;

  perform set_config('aurom.alta_modulos', 'si', true);
  insert into empresas (nombre, cedula_juridica, estado_servicio, referido_por, creado_por)
  values (p_nombre, p_cedula, 'prueba', v_socio, auth.uid()) returning id into v_empresa;
  perform set_config('aurom.alta_modulos', '', true);
  insert into usuarios_empresa (empresa_id, usuario_id, rol, creado_por) values (v_empresa, auth.uid(), 'admin', auth.uid());
  insert into configuracion_planilla (empresa_id) values (v_empresa);
  perform crear_cuentas_por_defecto(v_empresa);
  foreach v_mod in array (select array_agg(distinct m) from unnest(p_modulos) m) loop
    insert into empresa_modulos (empresa_id, modulo, activado_por) values (v_empresa, v_mod, auth.uid());
  end loop;
  if 'contabilidad' = any(p_modulos) then
    perform contabilidad.inicializar(v_empresa);
  end if;

  if v_socio is not null then
    insert into asignaciones_contador (empresa_id, socio_id, funciones, permiso, soporte_principal, asignado_por)
    values (v_empresa, v_socio,
            case v_tipo when 'contador' then array['supervision','soporte','implementacion']
                        else array['ventas','implementacion'] end,
            'operacion', v_tipo = 'contador', auth.uid());
  end if;
  return v_empresa;
end $$;
revoke execute on function public.crear_empresa(text, text, text, text, text[]) from public, anon;
grant execute on function public.crear_empresa(text, text, text, text, text[]) to authenticated;
