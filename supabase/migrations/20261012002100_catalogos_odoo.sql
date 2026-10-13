-- Catálogos de Configuración como el menú del módulo Odoo, con datos predeterminados que
-- cada empresa puede cambiar: horarios, tipos de colaborador, tipos de documento,
-- tipos de cobro y tabla de bono de antigüedad. Salarios mínimos (MTSS) es global.

-- ------------------------------------------------------------ Tipos de colaborador
create table if not exists public.tipos_empleado (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  codigo text not null,
  nombre text not null,
  cotiza_ccss boolean not null default true,     -- false = sin CCSS, renta ni INS (como «no asegurado»)
  activo boolean not null default true,
  unique (empresa_id, codigo)
);
do $$ begin
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'tipos_empleado') then
    perform public._politicas_empresa('public.tipos_empleado');
  end if;
end $$;

-- El tipo del colaborador pasa a ser el código de este catálogo (cada empresa puede agregar).
alter table public.empleados drop constraint if exists empleados_tipo_empleado_check;

-- ------------------------------------------------------------ Tipos de documento
create table if not exists public.tipos_documento (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  nombre text not null,
  requiere_numero boolean not null default false,
  requiere_vencimiento boolean not null default true,
  dias_alerta int check (dias_alerta is null or dias_alerta between 1 and 365),   -- vacío = el de Configuración › Planilla
  orden int not null default 100,
  unique (empresa_id, nombre)
);
do $$ begin
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'tipos_documento') then
    perform public._politicas_empresa('public.tipos_documento');
  end if;
end $$;

-- ------------------------------------------------------------ Tipos de cobro al colaborador
create table if not exists public.tipos_cobro (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  nombre text not null,
  modo text not null default 'fijo' check (modo in ('fijo', 'por_unidad')),
  precio_unitario numeric(14,2) not null default 0 check (precio_unitario >= 0),
  unidad text,
  subsidio_pct numeric(5,2) not null default 0 check (subsidio_pct between 0 and 100),
  afecta_ccss boolean not null default false,
  activo boolean not null default true,
  orden int not null default 100,
  unique (empresa_id, nombre)
);
do $$ begin
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'tipos_cobro') then
    perform public._politicas_empresa('public.tipos_cobro');
  end if;
end $$;

-- ------------------------------------------------------------ Bono de antigüedad (tabla por tramos)
create table if not exists public.bono_antiguedad (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  nombre text not null,
  desde_anios int not null check (desde_anios >= 1),
  hasta_anios int check (hasta_anios is null or hasta_anios >= desde_anios),
  tipo text not null default 'porcentaje' check (tipo in ('porcentaje', 'fijo')),
  porcentaje numeric(6,2) check (porcentaje is null or porcentaje > 0),
  monto numeric(14,2) check (monto is null or monto > 0),
  activo boolean not null default true,
  check ((tipo = 'porcentaje' and porcentaje is not null) or (tipo = 'fijo' and monto is not null))
);
do $$ begin
  if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = 'bono_antiguedad') then
    perform public._politicas_empresa('public.bono_antiguedad');
  end if;
end $$;
create index if not exists bono_antiguedad_empresa on public.bono_antiguedad (empresa_id);

-- ------------------------------------------------------------ Salarios mínimos MTSS (global)
create table if not exists public.salarios_minimos (
  id uuid primary key default gen_random_uuid(),
  categoria text not null,
  monto numeric(14,2) not null check (monto > 0),
  vigente_desde date not null,
  decreto text,
  unique (categoria, vigente_desde)
);
alter table public.salarios_minimos enable row level security;
drop policy if exists ver on public.salarios_minimos;
create policy ver on public.salarios_minimos for select to authenticated using (true);
drop policy if exists gestionar on public.salarios_minimos;
create policy gestionar on public.salarios_minimos for all to authenticated
  using (public.es_admin_plataforma()) with check (public.es_admin_plataforma());
grant select, insert, update, delete on public.salarios_minimos to authenticated;

insert into public.salarios_minimos (categoria, monto, vigente_desde, decreto) values
  ('Trabajador en ocupación no calificada', 373092.30, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Trabajador en ocupación semicalificada', 405710.70, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Trabajador en ocupación calificada', 419755.80, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Trabajador en ocupación especializada', 487335.00, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Técnico Medio de Educación Diversificada', 496838.17, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Técnico de Educación Superior', 542096.58, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Diplomado de Educación Superior', 585484.58, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Bachiller universitario', 664078.07, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Licenciado universitario', 796921.00, '2026-01-01', 'Decreto 45303-MTSS'),
  ('Trabajo doméstico', 268731.31, '2026-01-01', 'Decreto 45303-MTSS')
on conflict do nothing;

-- ------------------------------------------------------------ Datos predeterminados por empresa
-- Igual que default_data.xml del módulo Odoo. Solo agrega lo que falta: no pisa lo que la empresa cambió.
create or replace function public.sembrar_catalogos_empresa(p_empresa uuid) returns void
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.tipos_horario (empresa_id, nombre, horas_diarias, dias_semana, jornada, medio_tiempo)
  select p_empresa, h.nombre, h.horas, h.dias, h.jornada, h.medio
    from (values
      ('Jornada Completa (8 horas - Lun a Vie)', 8.0, 5, 'diurna', false),
      ('Jornada Completa (8 horas - Lun a Sáb)', 8.0, 6, 'diurna', false),
      ('Jornada Mixta (7 horas)', 7.0, 6, 'mixta', false),
      ('Jornada Nocturna (6 horas)', 6.0, 6, 'nocturna', false),
      ('Jornada Acumulada 4x10 (10 horas - 4 días)', 10.0, 4, 'diurna', false),
      ('Jornada Acumulada 3x12 (12 horas - 3 días)', 12.0, 3, 'diurna', false),
      ('Medio Tiempo (4 horas)', 4.0, 5, 'diurna', true),
      ('Tres Cuartos (6 horas)', 6.0, 5, 'diurna', true),
      ('Horario de Confianza', 8.0, 5, 'diurna', false),
      ('Guardias / Turnos 24 horas', 12.0, 3, 'mixta', false),
      ('Turno Rotativo (Mañana / Tarde / Noche)', 8.0, 5, 'mixta', false),
      ('Fines de Semana (Sáb y Dom)', 8.0, 2, 'diurna', false)
    ) as h(nombre, horas, dias, jornada, medio)
   where not exists (select 1 from public.tipos_horario t where t.empresa_id = p_empresa and t.nombre = h.nombre);

  insert into public.tipos_empleado (empresa_id, codigo, nombre, cotiza_ccss)
  values (p_empresa, 'indefinido', 'Empleado Indefinido', true),
         (p_empresa, 'plazo_fijo', 'Empleado Plazo Fijo', true),
         (p_empresa, 'periodo_prueba', 'Período de prueba', true),
         (p_empresa, 'tiempo_parcial', 'Tiempo Parcial', true),
         (p_empresa, 'practicante', 'Practicante', true),
         (p_empresa, 'ocasional', 'Ocasional', true),
         (p_empresa, 'servicios_profesionales', 'Servicios Profesionales', false)
  on conflict (empresa_id, codigo) do nothing;

  insert into public.tipos_documento (empresa_id, nombre, requiere_numero, requiere_vencimiento, dias_alerta, orden)
  values (p_empresa, 'Cédula de Identidad / Residencia', true, true, 60, 10),
         (p_empresa, 'Licencia de Conducir', true, true, 30, 20),
         (p_empresa, 'Carné de Manipulación de Alimentos', true, true, 30, 30),
         (p_empresa, 'Permiso de Trabajo (extranjeros)', true, true, 60, 40),
         (p_empresa, 'Certificado Médico Ocupacional', false, true, 30, 50),
         (p_empresa, 'Contrato Laboral', false, false, null, 60),
         (p_empresa, 'Copia de Cédula', false, false, null, 70),
         (p_empresa, 'Currículum', false, false, null, 80),
         (p_empresa, 'Carta de Recomendación', false, false, null, 90)
  on conflict (empresa_id, nombre) do nothing;

  insert into public.tipos_cobro (empresa_id, nombre, modo, unidad, subsidio_pct, orden)
  values (p_empresa, 'Almuerzo Comedor (Fijo Mensual)', 'fijo', 'período', 0, 10),
         (p_empresa, 'Almuerzo Comedor (Por Días)', 'por_unidad', 'días', 0, 20),
         (p_empresa, 'Almuerzo Subsidiado (Empresa y Empleado)', 'por_unidad', 'días', 50, 30),
         (p_empresa, 'Productos / Mercadería', 'por_unidad', 'unidades', 0, 40),
         (p_empresa, 'Uniforme / Equipo de Trabajo', 'fijo', 'kit', 0, 50),
         (p_empresa, 'Parqueo / Estacionamiento', 'fijo', 'período', 0, 60),
         (p_empresa, 'Seguro Colectivo Voluntario', 'fijo', 'período', 0, 70),
         (p_empresa, 'Otro Cobro al Empleado', 'fijo', 'unidad', 0, 99)
  on conflict (empresa_id, nombre) do nothing;
end $$;
revoke all on function public.sembrar_catalogos_empresa(uuid) from public, anon, authenticated;

create or replace function public.tg_sembrar_catalogos() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  perform public.sembrar_catalogos_empresa(new.id);
  return new;
end $$;
revoke all on function public.tg_sembrar_catalogos() from public, anon, authenticated;

drop trigger if exists empresa_catalogos on public.empresas;
create trigger empresa_catalogos after insert on public.empresas
  for each row execute function public.tg_sembrar_catalogos();

-- Empresas que ya existen
select public.sembrar_catalogos_empresa(id) from public.empresas;

-- Restaurar los predeterminados que una empresa borró (botón en Configuración).
create or replace function public.restaurar_catalogos(p_empresa uuid) returns void
language plpgsql security definer set search_path = '' as $$
begin
  if not public.es_admin_empresa(p_empresa) then raise exception 'Sin permiso'; end if;
  perform public.sembrar_catalogos_empresa(p_empresa);
end $$;
revoke all on function public.restaurar_catalogos(uuid) from public, anon;
grant execute on function public.restaurar_catalogos(uuid) to authenticated;
