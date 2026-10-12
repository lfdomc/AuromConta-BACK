-- Sin duplicados de identificación. Se compara sin guiones ni espacios: «3-101-123456» = «3101123456».
--   * Empresas: una sola empresa vigente por cédula jurídica (las dadas de baja no cuentan).
--   * Colaboradores: una sola vez por empresa (la misma persona sí puede estar en otra empresa).
--   * Clientes del negocio: una sola ficha vigente por identificación.
-- Si ya hay duplicados, la migración se detiene y dice cuáles son, para corregirlos antes.

create or replace function public.ident_normal(p text) returns text
language sql immutable parallel safe set search_path = '' as $$
  select nullif(regexp_replace(upper(coalesce(p, '')), '[^0-9A-Z]', '', 'g'), '')
$$;
revoke all on function public.ident_normal(text) from public, anon;
grant execute on function public.ident_normal(text) to authenticated, service_role;

do $$
declare v text;
begin
  select string_agg(format('%s (%s)', d.ced, d.nombres), '; ') into v from (
    select public.ident_normal(cedula_juridica) as ced, string_agg(nombre, ', ') as nombres
      from public.empresas where estado_servicio <> 'baja' and public.ident_normal(cedula_juridica) is not null
     group by 1 having count(*) > 1) d;
  if v is not null then
    raise exception 'Hay empresas vigentes con la misma cédula: %. Dé de baja o borre la duplicada y vuelva a correr este archivo.', v;
  end if;

  select string_agg(format('%s en %s', d.ident, d.empresa), '; ') into v from (
    select public.ident_normal(e.identificacion) as ident, em.nombre as empresa
      from public.empleados e join public.empresas em on em.id = e.empresa_id
     group by e.empresa_id, em.nombre, 1 having count(*) > 1) d;
  if v is not null then
    raise exception 'Hay colaboradores repetidos en la misma empresa: %. Corrija uno de ellos y vuelva a correr este archivo.', v;
  end if;

  select string_agg(d.ident, ', ') into v from (
    select public.ident_normal(identificacion) as ident from negocio.clientes
     where estado <> 'baja' and public.ident_normal(identificacion) is not null
     group by 1 having count(*) > 1) d;
  if v is not null then
    raise exception 'Hay clientes vigentes con la misma identificación: %. Dé de baja el duplicado y vuelva a correr este archivo.', v;
  end if;
end $$;

create unique index if not exists empresas_cedula_unica
  on public.empresas (public.ident_normal(cedula_juridica))
  where estado_servicio <> 'baja' and public.ident_normal(cedula_juridica) is not null;

create unique index if not exists empleados_identificacion_unica
  on public.empleados (empresa_id, public.ident_normal(identificacion));

create unique index if not exists clientes_identificacion_unica
  on negocio.clientes (public.ident_normal(identificacion))
  where estado <> 'baja' and public.ident_normal(identificacion) is not null;
