-- =============================================================================
-- Planilla Costa Rica · 0014 Correcciones de la auditoría (12/10/2026)
-- =============================================================================

-- 1. Comisiones con pagos parciales.
-- La llave única (socio, empresa, concepto, periodo) hacía que, si un cliente pagaba un mes
-- en dos partes, solo la primera generara comisión: el socio cobraba de menos.
-- Ahora la idempotencia es por pago: cada abono genera su parte (y volver a calcular no duplica).
alter table public.comisiones drop constraint if exists comisiones_socio_id_empresa_id_concepto_periodo_key;
create unique index if not exists comisiones_por_pago on public.comisiones
  (socio_id, empresa_id, concepto, periodo, coalesce(pago_id, '00000000-0000-0000-0000-000000000000'::uuid));

-- 2. Inicio del módulo Planilla para clientes que pagan desde antes de existir la empresa en el sistema
-- (p. ej. los clientes de Odoo): el cobro mira los módulos activos en cada mes, así que el primer
-- periodo de Planilla debe empezar en la fecha de alta del cliente si esta es anterior.
update public.empresa_modulos m
   set desde = c.alta
  from negocio.clientes c
 where c.empresa_id = m.empresa_id and m.modulo = 'planilla' and c.alta < m.desde
   and m.desde = (select min(x.desde) from public.empresa_modulos x where x.empresa_id = m.empresa_id and x.modulo = 'planilla');

-- La misma alineación al crear o editar la ficha del cliente (solo la plataforma).
create or replace function public.alinear_inicio_planilla(p_empresa uuid, p_alta date) returns void
language plpgsql security definer set search_path = public as $$
begin
  if not public.es_admin_plataforma() then raise exception 'Solo la administración de A.U.R.O.M.'; end if;
  update empresa_modulos m set desde = p_alta
   where m.empresa_id = p_empresa and m.modulo = 'planilla' and m.desde > p_alta
     and m.desde = (select min(x.desde) from empresa_modulos x where x.empresa_id = m.empresa_id and x.modulo = 'planilla');
end $$;
revoke execute on function public.alinear_inicio_planilla(uuid, date) from public, anon;
grant execute on function public.alinear_inicio_planilla(uuid, date) to authenticated;

-- 3. Índices para todas las llaves foráneas que no tenían uno.
-- Sin índice, filtrar por empresa_id (lo hace cada política RLS), borrar en cascada o buscar por
-- empleado recorre la tabla completa; con miles de boletas y asientos se vuelve lento.
do $$
declare r record; v_nombre text;
begin
  for r in
    select c.conrelid, c.conrelid::regclass::text as tabla, n.nspname,
           string_agg(quote_ident(a.attname), ', ' order by k.ord) as cols,
           string_agg(a.attname, '_' order by k.ord) as nombres
      from pg_constraint c
      join pg_namespace n on n.oid = c.connamespace
      cross join lateral unnest(c.conkey) with ordinality as k(attnum, ord)
      join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.attnum
     where c.contype = 'f' and n.nspname in ('public', 'negocio', 'contabilidad')
       and not exists (select 1 from pg_index i
                        where i.indrelid = c.conrelid
                          and (i.indkey::int2[])[0:array_length(c.conkey, 1) - 1] @> c.conkey)
     group by c.conrelid, c.oid, n.nspname
  loop
    v_nombre := left(replace(r.tabla, '.', '_') || '_' || r.nombres, 50) || '_fk_' || substr(md5(r.tabla || r.cols), 1, 6);
    execute format('create index if not exists %I on %s (%s)', v_nombre, r.tabla, r.cols);
  end loop;
end $$;
