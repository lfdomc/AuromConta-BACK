-- Cada empresa tiene su ficha de cobro en «Mi negocio › Clientes» desde que se registra,
-- también en prueba (no se cobra mientras esté en prueba). Antes la ficha solo se creaba
-- cuando la empresa registraba su tarjeta, y las empresas en prueba no aparecían.
-- Al pasar de prueba a activo, la API mueve la fecha de alta a ese día (los meses de prueba no se cobran).

create or replace function negocio.asegurar_ficha_cliente(p_empresa uuid) returns void
language plpgsql security definer set search_path = '' as $$
declare
  v_emp record;
  v_ced text;
  v_correo text;
begin
  if exists (select 1 from negocio.clientes where empresa_id = p_empresa) then return; end if;
  select * into v_emp from public.empresas where id = p_empresa;
  if v_emp is null or v_emp.estado_servicio = 'baja' then return; end if;
  v_ced := regexp_replace(coalesce(v_emp.cedula_juridica, ''), '\D', '', 'g');
  if v_ced = '' then return; end if;                      -- sin cédula no se puede facturar todavía
  -- Si ya existe un cliente con esa cédula creado a mano (sin empresa), se vincula en vez de duplicarlo.
  update negocio.clientes set empresa_id = p_empresa
   where empresa_id is null and estado <> 'baja'
     and regexp_replace(coalesce(identificacion, ''), '\D', '', 'g') = v_ced;
  if found then return; end if;
  v_correo := nullif(trim(coalesce(v_emp.correo, '')), '');
  if v_correo is null then
    select u.email into v_correo from auth.users u where u.id = v_emp.creado_por;
  end if;
  if v_correo is null then return; end if;
  insert into negocio.clientes (empresa_id, razon_social, tipo_identificacion, identificacion, correo_facturacion,
                                telefono, estado, alta)
  values (p_empresa, v_emp.nombre,
          case when length(v_ced) = 9 then '01' when length(v_ced) in (11, 12) then '03' else '02' end,
          v_ced, v_correo, v_emp.telefono, v_emp.estado_servicio, v_emp.creado_en::date)
  on conflict do nothing;   -- si otra ficha vigente ya usa esa identificación, no se bloquea el registro
end $$;
revoke all on function negocio.asegurar_ficha_cliente(uuid) from public, anon, authenticated;

create or replace function negocio.tg_empresa_ficha_cliente() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  perform negocio.asegurar_ficha_cliente(new.id);
  -- El estado del cliente es el estado del servicio de su empresa.
  if tg_op = 'UPDATE' and new.estado_servicio is distinct from old.estado_servicio then
    update negocio.clientes set estado = new.estado_servicio
     where empresa_id = new.id and estado is distinct from new.estado_servicio;
  end if;
  return null;
end $$;

revoke all on function negocio.tg_empresa_ficha_cliente() from public, anon, authenticated;

drop trigger if exists empresa_ficha_cliente on public.empresas;
create trigger empresa_ficha_cliente
  after insert or update of cedula_juridica, correo, estado_servicio on public.empresas
  for each row execute function negocio.tg_empresa_ficha_cliente();

-- Empresas que ya existen sin ficha (p. ej. las que están en prueba).
do $$
declare r record;
begin
  for r in select e.id from public.empresas e
            where not exists (select 1 from negocio.clientes c where c.empresa_id = e.id)
  loop
    perform negocio.asegurar_ficha_cliente(r.id);
  end loop;
end $$;
