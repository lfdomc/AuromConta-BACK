-- Logo de cada empresa para boletas, constancias, liquidaciones y reportes en PDF.
-- Tabla aparte (no columna en empresas) para no cargar la imagen en cada consulta a empresas.
-- La API valida y normaliza la imagen (PNG, máx. 600 px) antes de guardarla.

create table if not exists public.empresa_logos (
  empresa_id uuid primary key references public.empresas(id) on delete cascade,
  tipo text not null default 'image/png' check (tipo in ('image/png', 'image/jpeg')),
  datos bytea not null check (octet_length(datos) between 1 and 524288),
  actualizado_en timestamptz not null default now()
);

alter table public.empresa_logos enable row level security;

drop policy if exists ver on public.empresa_logos;
create policy ver on public.empresa_logos for select to authenticated using (public.puede_ver(empresa_id));
drop policy if exists gestionar on public.empresa_logos;
create policy gestionar on public.empresa_logos for all to authenticated
  using (public.es_admin_empresa(empresa_id)) with check (public.es_admin_empresa(empresa_id));
grant select, insert, update, delete on public.empresa_logos to authenticated;

-- Lectura del logo para PDFs: quien ve la empresa, o un colaborador de esa empresa desde su portal
-- (el colaborador no tiene acceso RLS a la empresa, solo a sus propias boletas).
create or replace function public.logo_empresa(p_empresa uuid)
returns table (tipo text, datos bytea)
language sql stable security definer set search_path = ''
as $$
  select l.tipo, l.datos
    from public.empresa_logos l
   where l.empresa_id = p_empresa
     and (public.puede_ver(p_empresa)
          or exists (select 1 from public.empleados e
                      where e.empresa_id = p_empresa and e.usuario_id = (select auth.uid())));
$$;
revoke all on function public.logo_empresa(uuid) from public, anon;
grant execute on function public.logo_empresa(uuid) to authenticated;
