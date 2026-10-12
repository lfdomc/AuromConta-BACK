-- =============================================================================
-- Planilla Costa Rica · 0001 Núcleo, seguridad multiempresa y auditoría
-- Una sola base para todos los clientes. Cada fila lleva empresa_id y RLS
-- garantiza que cada usuario solo vea las empresas donde tiene acceso:
--   * usuarios de la empresa (admin / aprobador / operador / lectura)
--   * contadores o vendedores asignados (supervisión y soporte)
--   * la dueña de la plataforma (A.U.R.O.M.)
-- =============================================================================

create extension if not exists pgcrypto;

-- -----------------------------------------------------------------------------
-- Utilidades
-- -----------------------------------------------------------------------------
create or replace function public.tg_actualizado() returns trigger
language plpgsql as $$
begin
  new.actualizado_en := now();
  new.actualizado_por := auth.uid();
  return new;
end $$;

-- -----------------------------------------------------------------------------
-- Plataforma y socios (se crean antes porque las funciones de acceso los usan)
-- -----------------------------------------------------------------------------
create table public.plataforma_admins (
  usuario_id uuid primary key references auth.users(id) on delete cascade,
  creado_en timestamptz not null default now()
);

create table public.empresas (
  id uuid primary key default gen_random_uuid(),
  nombre text not null,
  cedula_juridica text,
  numero_patronal text,
  poliza_ins text,
  clase_riesgo_ins text not null default 'II' check (clase_riesgo_ins in ('I','II','III','IV','V')),
  correo text,
  telefono text,
  direccion text,
  logo_url text,
  moneda text not null default 'CRC',
  -- SaaS
  plan text not null default 'estandar',
  estado_servicio text not null default 'activo'
    check (estado_servicio in ('prueba','activo','gracia','solo_lectura','suspendido','baja')),
  referido_por uuid,                -- socio que la trajo (atribución)
  creado_en timestamptz not null default now(),
  creado_por uuid default auth.uid(),
  actualizado_en timestamptz not null default now(),
  actualizado_por uuid
);
create trigger empresas_upd before update on public.empresas
  for each row execute function public.tg_actualizado();

create table public.usuarios_empresa (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  usuario_id uuid not null references auth.users(id) on delete cascade,
  rol text not null check (rol in ('admin','aprobador','operador','lectura')),
  activo boolean not null default true,
  creado_en timestamptz not null default now(),
  creado_por uuid default auth.uid(),
  unique (empresa_id, usuario_id)
);
create index on public.usuarios_empresa (usuario_id) where activo;

-- Socios: contadores (soporte primario + 20 % recurrente) y vendedores
-- (3 primeras mensualidades). Ambos pueden quedar asignados a empresas.
create table public.socios (
  id uuid primary key default gen_random_uuid(),
  usuario_id uuid unique references auth.users(id) on delete set null,
  tipo text not null check (tipo in ('contador','vendedor')),
  nombre text not null,
  cedula text,
  correo text not null,
  telefono text,
  telegram_chat_id text,
  codigo_referido text not null unique default upper(substr(md5(gen_random_uuid()::text), 1, 8)),
  certificado boolean not null default false,
  iban text,
  estado text not null default 'activo' check (estado in ('pendiente','activo','suspendido','baja')),
  creado_en timestamptz not null default now(),
  actualizado_en timestamptz not null default now(),
  actualizado_por uuid
);
create trigger socios_upd before update on public.socios
  for each row execute function public.tg_actualizado();
alter table public.empresas
  add constraint empresas_referido_fk foreign key (referido_por) references public.socios(id);

-- Contadores / vendedores asignados a cada empresa para supervisión y soporte.
create table public.asignaciones_contador (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  socio_id uuid not null references public.socios(id) on delete cascade,
  funciones text[] not null default array['supervision','soporte']::text[],
  permiso text not null default 'lectura' check (permiso in ('lectura','operacion')),
  soporte_principal boolean not null default true,
  asignado_por uuid default auth.uid(),
  desde timestamptz not null default now(),
  revocado_en timestamptz,
  revocado_por uuid,
  check (funciones <@ array['supervision','soporte','ventas','implementacion']::text[])
);
create unique index asignacion_activa_unica on public.asignaciones_contador (empresa_id, socio_id)
  where revocado_en is null;
create index on public.asignaciones_contador (socio_id) where revocado_en is null;

-- -----------------------------------------------------------------------------
-- Funciones de acceso (security definer: leen tablas protegidas sin recursión)
-- -----------------------------------------------------------------------------
create or replace function public.es_admin_plataforma() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from plataforma_admins where usuario_id = auth.uid())
$$;

create or replace function public.mi_socio_id() returns uuid
language sql stable security definer set search_path = public as $$
  select id from socios where usuario_id = auth.uid() and estado = 'activo'
$$;

-- Rol efectivo del usuario actual en una empresa.
--   admin | aprobador | operador | lectura           (usuarios de la empresa)
--   contador_operacion | contador_lectura           (socio asignado)
--   plataforma                                       (dueña de A.U.R.O.M.)
create or replace function public.rol_en_empresa(p_empresa uuid) returns text
language sql stable security definer set search_path = public as $$
  select coalesce(
    (select rol from usuarios_empresa
      where empresa_id = p_empresa and usuario_id = auth.uid() and activo),
    (select case when bool_or(a.permiso = 'operacion') then 'contador_operacion'
                 else 'contador_lectura' end
       from asignaciones_contador a join socios s on s.id = a.socio_id
      where a.empresa_id = p_empresa and a.revocado_en is null
        and s.usuario_id = auth.uid() and s.estado = 'activo'
      having count(*) > 0),
    (select 'plataforma' where exists (select 1 from plataforma_admins where usuario_id = auth.uid()))
  )
$$;

create or replace function public.puede_ver(p_empresa uuid) returns boolean
language sql stable as $$ select public.rol_en_empresa(p_empresa) is not null $$;

create or replace function public.puede_operar(p_empresa uuid) returns boolean
language sql stable as $$
  select public.rol_en_empresa(p_empresa) in ('admin','aprobador','operador','contador_operacion','plataforma')
$$;

create or replace function public.puede_aprobar(p_empresa uuid) returns boolean
language sql stable as $$
  select public.rol_en_empresa(p_empresa) in ('admin','aprobador','plataforma')
$$;

create or replace function public.es_admin_empresa(p_empresa uuid) returns boolean
language sql stable as $$
  select public.rol_en_empresa(p_empresa) in ('admin','plataforma')
$$;

-- Empresa en solo lectura o suspendida: nadie (salvo plataforma) escribe.
create or replace function public.empresa_escribible(p_empresa uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select public.es_admin_plataforma()
      or exists (select 1 from empresas where id = p_empresa
                 and estado_servicio in ('prueba','activo','gracia'))
$$;

-- Crea las 4 políticas estándar de una tabla con empresa_id.
create or replace function public._politicas_empresa(p_tabla regclass) returns void
language plpgsql as $$
begin
  execute format('alter table %s enable row level security', p_tabla);
  execute format('create policy ver on %s for select to authenticated using (public.puede_ver(empresa_id))', p_tabla);
  execute format('create policy crear on %s for insert to authenticated with check (public.puede_operar(empresa_id) and public.empresa_escribible(empresa_id))', p_tabla);
  execute format('create policy editar on %s for update to authenticated using (public.puede_operar(empresa_id)) with check (public.puede_operar(empresa_id) and public.empresa_escribible(empresa_id))', p_tabla);
  execute format('create policy borrar on %s for delete to authenticated using (public.puede_operar(empresa_id) and public.empresa_escribible(empresa_id))', p_tabla);
  execute format('grant select, insert, update, delete on %s to authenticated', p_tabla);
end $$;

-- -----------------------------------------------------------------------------
-- Auditoría
-- -----------------------------------------------------------------------------
create table public.auditoria (
  id bigint generated always as identity primary key,
  empresa_id uuid,
  tabla text not null,
  registro_id text,
  accion text not null,
  antes jsonb,
  despues jsonb,
  usuario_id uuid,
  fecha timestamptz not null default now()
);
create index on public.auditoria (empresa_id, fecha desc);

create or replace function public.tg_auditar() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_row jsonb := to_jsonb(coalesce(new, old));
begin
  insert into auditoria (empresa_id, tabla, registro_id, accion, antes, despues, usuario_id)
  values (nullif(v_row ->> 'empresa_id', '')::uuid, tg_table_name, v_row ->> 'id', tg_op,
          case when tg_op <> 'INSERT' then to_jsonb(old) end,
          case when tg_op <> 'DELETE' then to_jsonb(new) end,
          auth.uid());
  return coalesce(new, old);
end $$;

-- -----------------------------------------------------------------------------
-- Configuración y catálogos
-- -----------------------------------------------------------------------------
create table public.configuracion_planilla (
  empresa_id uuid primary key references public.empresas(id) on delete cascade,
  metodo_renta text not null default 'per_period' check (metodo_renta in ('per_period','monthly_consolidated')),
  base_renta text not null default 'gross' check (base_renta in ('gross','net_ccss')),
  aplicar_bmc boolean not null default true,
  bmc_monto numeric(14,2),
  cesantia_modo text not null default 'legal' check (cesantia_modo in ('legal','custom')),
  cesantia_tasa numeric(6,3) not null default 4.8,
  factor_he_diurna numeric(4,2) not null default 1.5,
  factor_he_doble numeric(4,2) not null default 2.0,
  correo_remitente text,
  nota_boleta text,
  actualizado_en timestamptz not null default now(),
  actualizado_por uuid
);
create trigger configuracion_upd before update on public.configuracion_planilla
  for each row execute function public.tg_actualizado();

create table public.sucursales (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  codigo text,
  nombre text not null,
  direccion text,
  telefono text,
  correo text,
  es_temporal boolean not null default false,
  fecha_fin_proyecto date,
  activa boolean not null default true,
  creado_en timestamptz not null default now()
);

create table public.calendarios_pago (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  nombre text not null,
  frecuencia text not null check (frecuencia in ('monthly','biweekly','weekly','bimonthly')),
  activo boolean not null default true,
  creado_en timestamptz not null default now()
);

create table public.tipos_horario (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  nombre text not null,
  horas_diarias numeric(5,2) not null default 8,
  dias_semana int not null default 6,
  jornada text not null default 'diurna' check (jornada in ('diurna','mixta','nocturna')),
  medio_tiempo boolean not null default false
);

-- Feriados: empresa_id nulo = feriado nacional (visible para todos).
create table public.feriados (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid references public.empresas(id) on delete cascade,
  fecha date not null,
  nombre text not null,
  pago_obligatorio boolean not null default true
);
create unique index feriados_unicos on public.feriados (coalesce(empresa_id, '00000000-0000-0000-0000-000000000000'::uuid), fecha);

-- Parámetros legales con vigencia (globales). El motor los recibe como JSON.
create table public.parametros_legales (
  id uuid primary key default gen_random_uuid(),
  vigente_desde date not null unique,
  datos jsonb not null,
  fuente text,
  aprobado_por uuid,
  creado_en timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- RLS del núcleo
-- -----------------------------------------------------------------------------
alter table public.plataforma_admins enable row level security;
create policy ver on public.plataforma_admins for select to authenticated using (usuario_id = auth.uid());
grant select on public.plataforma_admins to authenticated;

alter table public.empresas enable row level security;
create policy ver on public.empresas for select to authenticated using (public.puede_ver(id));
create policy editar on public.empresas for update to authenticated
  using (public.es_admin_empresa(id)) with check (public.es_admin_empresa(id));
-- Alta de empresas: solo por la función crear_empresa() (Fase 6) o la plataforma.
create policy crear on public.empresas for insert to authenticated with check (public.es_admin_plataforma());
grant select, insert, update on public.empresas to authenticated;

alter table public.usuarios_empresa enable row level security;
create policy ver on public.usuarios_empresa for select to authenticated
  using (usuario_id = auth.uid() or public.es_admin_empresa(empresa_id));
create policy gestionar on public.usuarios_empresa for all to authenticated
  using (public.es_admin_empresa(empresa_id)) with check (public.es_admin_empresa(empresa_id));
grant select, insert, update, delete on public.usuarios_empresa to authenticated;

alter table public.socios enable row level security;
create policy ver on public.socios for select to authenticated using (
  usuario_id = auth.uid() or public.es_admin_plataforma()
  or exists (select 1 from public.asignaciones_contador a
             where a.socio_id = socios.id and a.revocado_en is null and public.puede_ver(a.empresa_id)));
create policy editar_propio on public.socios for update to authenticated
  using (usuario_id = auth.uid() or public.es_admin_plataforma())
  with check (usuario_id = auth.uid() or public.es_admin_plataforma());
create policy gestionar on public.socios for insert to authenticated with check (public.es_admin_plataforma());
create policy borrar on public.socios for delete to authenticated using (public.es_admin_plataforma());
grant select, insert, update, delete on public.socios to authenticated;
-- Un socio no puede cambiarse a sí mismo el tipo, estado ni la certificación.
create or replace function public.tg_socio_campos_protegidos() returns trigger
language plpgsql as $$
begin
  if not public.es_admin_plataforma() and current_user = 'authenticated'
     and (new.tipo, new.estado, new.certificado, new.codigo_referido, new.usuario_id)
         is distinct from (old.tipo, old.estado, old.certificado, old.codigo_referido, old.usuario_id) then
    raise exception 'Solo la plataforma puede cambiar tipo, estado o certificación del socio';
  end if;
  return new;
end $$;
create trigger socios_protegidos before update on public.socios
  for each row execute function public.tg_socio_campos_protegidos();

alter table public.asignaciones_contador enable row level security;
create policy ver on public.asignaciones_contador for select to authenticated
  using (public.puede_ver(empresa_id) or socio_id = public.mi_socio_id());
create policy gestionar on public.asignaciones_contador for all to authenticated
  using (public.es_admin_empresa(empresa_id)) with check (public.es_admin_empresa(empresa_id));
grant select, insert, update, delete on public.asignaciones_contador to authenticated;

alter table public.auditoria enable row level security;
create policy ver on public.auditoria for select to authenticated
  using (public.es_admin_empresa(empresa_id) or (empresa_id is null and public.es_admin_plataforma()));
grant select on public.auditoria to authenticated;

select public._politicas_empresa('public.sucursales');
select public._politicas_empresa('public.calendarios_pago');
select public._politicas_empresa('public.tipos_horario');

alter table public.configuracion_planilla enable row level security;
create policy ver on public.configuracion_planilla for select to authenticated using (public.puede_ver(empresa_id));
create policy gestionar on public.configuracion_planilla for all to authenticated
  using (public.es_admin_empresa(empresa_id)) with check (public.es_admin_empresa(empresa_id));
grant select, insert, update on public.configuracion_planilla to authenticated;

alter table public.feriados enable row level security;
create policy ver on public.feriados for select to authenticated using (empresa_id is null or public.puede_ver(empresa_id));
create policy gestionar on public.feriados for all to authenticated
  using ((empresa_id is null and public.es_admin_plataforma()) or (empresa_id is not null and public.es_admin_empresa(empresa_id)))
  with check ((empresa_id is null and public.es_admin_plataforma()) or (empresa_id is not null and public.es_admin_empresa(empresa_id)));
grant select, insert, update, delete on public.feriados to authenticated;

alter table public.parametros_legales enable row level security;
create policy ver on public.parametros_legales for select to authenticated using (true);
create policy gestionar on public.parametros_legales for all to authenticated
  using (public.es_admin_plataforma()) with check (public.es_admin_plataforma());
grant select, insert, update, delete on public.parametros_legales to authenticated;

create trigger auditar after insert or update or delete on public.usuarios_empresa
  for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.asignaciones_contador
  for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.configuracion_planilla
  for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.parametros_legales
  for each row execute function public.tg_auditar();
