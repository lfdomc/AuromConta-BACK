-- RLS más rápido: los permisos del usuario se calculan UNA vez por consulta y no una vez por fila.
--
-- Antes, cada política llamaba public.puede_ver(empresa_id) / puede_operar / empresa_escribible por cada fila
-- leída o escrita (3 subconsultas por fila). Un reporte de 3 000 líneas hacía ~9 000 subconsultas extra
-- (medido: 72 ms contra 2 ms sin RLS). Ahora la política compara empresa_id con el conjunto de empresas del
-- usuario, que Postgres calcula una sola vez por consulta (subplan con hash).
--
-- Las reglas de acceso NO cambian: public.mis_roles() replica exactamente public.rol_en_empresa()
-- (misma prioridad: usuario de la empresa > contador asignado > administración de la plataforma), y
-- la prueba tests/test_rls_rapido.py compara las dos para todos los usuarios y empresas.
-- Se puede correr más de una vez.

create or replace function public.mis_roles() returns table (empresa_id uuid, rol text)
language sql stable security definer set search_path = public as $$
  with ue as (
    select u.empresa_id, u.rol from usuarios_empresa u
     where u.usuario_id = auth.uid() and u.activo and u.rol is not null
  ), co as (
    select a.empresa_id,
           case when bool_or(a.permiso = 'operacion') then 'contador_operacion' else 'contador_lectura' end as rol
      from asignaciones_contador a join socios s on s.id = a.socio_id
     where a.revocado_en is null and s.usuario_id = auth.uid() and s.estado = 'activo'
     group by a.empresa_id
  )
  select ue.empresa_id, ue.rol from ue
  union all
  select co.empresa_id, co.rol from co where not exists (select 1 from ue where ue.empresa_id = co.empresa_id)
  union all
  select e.id, 'plataforma' from empresas e
   where exists (select 1 from plataforma_admins p where p.usuario_id = auth.uid())
     and not exists (select 1 from ue where ue.empresa_id = e.id)
     and not exists (select 1 from co where co.empresa_id = e.id)
$$;

create or replace function public.empresas_visibles() returns setof uuid
language sql stable security definer set search_path = public as $$
  select r.empresa_id from public.mis_roles() r
$$;

create or replace function public.empresas_operables() returns setof uuid
language sql stable security definer set search_path = public as $$
  select r.empresa_id from public.mis_roles() r
   where r.rol in ('admin', 'aprobador', 'operador', 'contador_operacion', 'plataforma')
$$;

create or replace function public.empresas_escribibles() returns setof uuid
language sql stable security definer set search_path = public as $$
  select e.id from empresas e
   where public.es_admin_plataforma() or e.estado_servicio in ('prueba', 'activo', 'gracia')
$$;

revoke execute on function public.mis_roles(), public.empresas_visibles(), public.empresas_operables(),
  public.empresas_escribibles() from public, anon;
grant execute on function public.mis_roles(), public.empresas_visibles(), public.empresas_operables(),
  public.empresas_escribibles() to authenticated;

-- ------------------------------------------------------------------ Reescribir las políticas existentes
create or replace function public._rls_rapida(expr text) returns text language sql immutable as $$
  select regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(expr,
    '(?<![\w.])(public\.)?puede_ver\(empresa_id\)', '(empresa_id IN (SELECT public.empresas_visibles()))', 'g'),
    '(?<![\w.])(public\.)?puede_operar\(empresa_id\)', '(empresa_id IN (SELECT public.empresas_operables()))', 'g'),
    '(?<![\w.])(public\.)?empresa_escribible\(empresa_id\)', '(empresa_id IN (SELECT public.empresas_escribibles()))', 'g'),
    '(?<!SELECT )(?<![\w.(])(public\.)?es_admin_plataforma\(\)', '(SELECT public.es_admin_plataforma())', 'g'),
    '(?<!SELECT )(?<![\w.(])auth\.uid\(\)', '(SELECT auth.uid())', 'g')
$$;

do $$
declare
  p record;
  q text;
  c text;
  sentencia text;
begin
  for p in select schemaname, tablename, policyname, qual, with_check from pg_policies
            where schemaname in ('public', 'contabilidad', 'negocio') loop
    q := case when p.qual is null then null else public._rls_rapida(p.qual) end;
    c := case when p.with_check is null then null else public._rls_rapida(p.with_check) end;
    if q is distinct from p.qual or c is distinct from p.with_check then
      sentencia := format('alter policy %I on %I.%I', p.policyname, p.schemaname, p.tablename);
      if q is not null then sentencia := sentencia || ' using (' || q || ')'; end if;
      if c is not null then sentencia := sentencia || ' with check (' || c || ')'; end if;
      execute sentencia;
    end if;
  end loop;
end $$;
drop function public._rls_rapida(text);

-- ------------------------------------------------------------------ Tablas nuevas: ya con la forma rápida
create or replace function public._politicas_empresa(p_tabla regclass) returns void language plpgsql as $$
begin
  execute format('alter table %s enable row level security', p_tabla);
  execute format('create policy ver on %s for select to authenticated using (empresa_id in (select public.empresas_visibles()))', p_tabla);
  execute format('create policy crear on %s for insert to authenticated with check (empresa_id in (select public.empresas_operables()) and empresa_id in (select public.empresas_escribibles()))', p_tabla);
  execute format('create policy editar on %s for update to authenticated using (empresa_id in (select public.empresas_operables())) with check (empresa_id in (select public.empresas_operables()) and empresa_id in (select public.empresas_escribibles()))', p_tabla);
  execute format('create policy borrar on %s for delete to authenticated using (empresa_id in (select public.empresas_operables()) and empresa_id in (select public.empresas_escribibles()))', p_tabla);
  execute format('grant select, insert, update, delete on %s to authenticated', p_tabla);
end $$;

create or replace function contabilidad._politicas(p_tabla regclass) returns void language plpgsql as $$
begin
  execute format('alter table %s enable row level security', p_tabla);
  execute format('create policy ver on %s for select to authenticated using (empresa_id in (select public.empresas_visibles()))', p_tabla);
  execute format('create policy crear on %s for insert to authenticated with check (empresa_id in (select public.empresas_operables()) '
                 'and empresa_id in (select public.empresas_escribibles()) and public.modulo_activo(empresa_id, ''contabilidad''))', p_tabla);
  execute format('create policy editar on %s for update to authenticated using (empresa_id in (select public.empresas_operables())) '
                 'with check (empresa_id in (select public.empresas_operables()) and empresa_id in (select public.empresas_escribibles()) '
                 'and public.modulo_activo(empresa_id, ''contabilidad''))', p_tabla);
  execute format('create policy borrar on %s for delete to authenticated using (empresa_id in (select public.empresas_operables()) '
                 'and empresa_id in (select public.empresas_escribibles()) and public.modulo_activo(empresa_id, ''contabilidad''))', p_tabla);
  execute format('grant select, insert, update, delete on %s to authenticated', p_tabla);
  execute format('create trigger solo_api before insert or update or delete on %s '
                 'for each row execute function public.tg_solo_api()', p_tabla);
end $$;

-- ------------------------------------------------------------------ Trigger de boletas inmutables
-- Solo revisa el estado de la planilla: con SECURITY DEFINER sus dos búsquedas no vuelven a evaluar la RLS
-- en cada línea insertada (era la mayor parte del tiempo al calcular una planilla). La RLS de la propia
-- fila insertada se sigue aplicando igual.
alter function public.tg_boleta_inmutable() security definer;
alter function public.tg_boleta_inmutable() set search_path = public;
