-- =============================================================================
-- Planilla Costa Rica · Instalación completa en un proyecto de Supabase NUEVO
-- Contiene las 13 migraciones en orden (0001 a 0014). Pegar completo en
-- SQL Editor → New query → Run. Corre dentro de una transacción: si algo falla,
-- no queda nada a medias y se puede volver a ejecutar.
-- NO usar en un proyecto que ya tenga las migraciones aplicadas.
-- =============================================================================
begin;

-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009000100_nucleo.sql
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


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009000200_empleados_novedades.sql
-- =============================================================================
-- Planilla Costa Rica · 0002 Empleados, novedades y obligaciones (Fases 1 y 2)
-- =============================================================================

create table public.empleados (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  usuario_id uuid references auth.users(id) on delete set null, -- portal del empleado
  tipo_identificacion text not null default 'cedula'
    check (tipo_identificacion in ('cedula','dimex','pasaporte','nite','otro')),
  identificacion text not null,
  nombre text not null,
  apellidos text not null default '',
  correo text,
  telefono text,
  fecha_nacimiento date,
  fecha_ingreso date not null,
  fecha_salida date,
  estado text not null default 'activo' check (estado in ('activo','inactivo','liquidado')),
  sucursal_id uuid references public.sucursales(id) on delete set null,
  puesto text,
  calendario_id uuid references public.calendarios_pago(id) on delete set null,
  horario_id uuid references public.tipos_horario(id) on delete set null,
  frecuencia text not null default 'biweekly' check (frecuencia in ('monthly','biweekly','weekly','bimonthly')),
  salario_mensual numeric(14,2) not null default 0 check (salario_mensual >= 0),
  por_asistencia boolean not null default false,
  tarifa_hora numeric(14,2) not null default 0,
  medio_tiempo boolean not null default false,
  aplica_rop boolean not null default false,
  hijos int not null default 0 check (hijos >= 0),
  conyuge boolean not null default false,
  num_empleadores int not null default 1 check (num_empleadores >= 1),
  pensionado text not null default 'none' check (pensionado in ('none','estado','otro')),
  exento boolean not null default false,
  clase_riesgo_ins text check (clase_riesgo_ins in ('I','II','III','IV','V')),
  banco text,
  iban text,
  saldo_vacaciones numeric(8,2) not null default 0,
  creado_en timestamptz not null default now(),
  creado_por uuid default auth.uid(),
  actualizado_en timestamptz not null default now(),
  actualizado_por uuid,
  unique (empresa_id, identificacion)
);
create index on public.empleados (empresa_id, estado);
create trigger empleados_upd before update on public.empleados
  for each row execute function public.tg_actualizado();

create table public.historial_salarial (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  fecha_efectiva date not null,
  salario_anterior numeric(14,2) not null,
  salario_nuevo numeric(14,2) not null check (salario_nuevo >= 0),
  motivo text,
  estado text not null default 'pendiente' check (estado in ('pendiente','autorizado','rechazado')),
  solicitado_por uuid default auth.uid(),
  autorizado_por uuid,
  autorizado_en timestamptz,
  creado_en timestamptz not null default now()
);
create index on public.historial_salarial (empleado_id, fecha_efectiva desc);

-- Solo aprobador/admin puede autorizar o rechazar un cambio salarial.
create or replace function public.tg_historial_autorizacion() returns trigger
language plpgsql as $$
begin
  if new.estado is distinct from old.estado and new.estado <> 'pendiente' then
    if current_user = 'authenticated' and not public.puede_aprobar(new.empresa_id) then
      raise exception 'Solo un aprobador puede autorizar cambios salariales';
    end if;
    new.autorizado_por := auth.uid();
    new.autorizado_en := now();
  end if;
  return new;
end $$;
create trigger historial_autorizacion before update on public.historial_salarial
  for each row execute function public.tg_historial_autorizacion();

create table public.documentos_empleado (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  tipo text not null,
  numero text,
  vence date,
  archivo_path text,   -- ruta en Supabase Storage (bucket documentos/{empresa_id}/...)
  creado_en timestamptz not null default now()
);

create table public.movimientos_empleado (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  tipo text not null check (tipo in ('ingreso','traslado','cambio_puesto','amonestacion','salida','otro')),
  fecha date not null,
  detalle text,
  creado_en timestamptz not null default now(),
  creado_por uuid default auth.uid()
);

-- ---------------------------------------------------------------- Novedades --
create table public.marcas_asistencia (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  entrada timestamptz not null,
  salida timestamptz,
  origen text not null default 'manual' check (origen in ('reloj','manual','importado')),
  check (salida is null or salida > entrada)
);
create index on public.marcas_asistencia (empleado_id, entrada);

create table public.horas_extra (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  fecha date not null,
  horas numeric(6,2) not null check (horas > 0),
  tipo text not null default 'diurna' check (tipo in ('diurna','nocturna','mixta','feriado')),
  factor numeric(4,2) not null default 1.5,
  tarifa_hora numeric(14,2),
  monto numeric(14,2) not null check (monto >= 0),
  afecto_ccss boolean not null default true,
  origen text not null default 'manual' check (origen in ('manual','importado','automatico')),
  estado text not null default 'pendiente' check (estado in ('pendiente','aprobada','rechazada','pagada')),
  boleta_id uuid,
  creado_en timestamptz not null default now()
);
create index on public.horas_extra (empleado_id, fecha);

create table public.incapacidades (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  tipo text not null check (tipo in ('ccss','ins','maternity')),
  desde date not null,
  hasta date not null,
  porcentaje_subsidio numeric(5,2) not null default 60,
  numero_boleta text,
  es_prorroga boolean not null default false,
  medio_dia_extra boolean not null default false,
  maternidad_ccss_por_patrono boolean not null default false,
  maternidad_50_50 boolean not null default false,
  salario_diario numeric(14,2),          -- nulo = salario mensual / 30
  salario_promedio_diario numeric(14,2), -- maternidad
  estado text not null default 'confirmada' check (estado in ('borrador','confirmada','anulada')),
  creado_en timestamptz not null default now(),
  check (hasta >= desde)
);
create index on public.incapacidades (empleado_id, desde);

create table public.permisos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  tipo text not null default 'personal',
  desde date not null,
  hasta date not null,
  dias numeric(6,2) not null,
  con_goce boolean not null default false,
  monto numeric(14,2),                     -- nulo = días x salario diario
  ausencia boolean not null default false, -- ausencia injustificada (no permiso)
  fundamento text,
  estado text not null default 'aprobado' check (estado in ('pendiente','aprobado','rechazado')),
  creado_en timestamptz not null default now(),
  check (hasta >= desde)
);

create table public.vacaciones (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  tipo text not null default 'disfrute' check (tipo in ('disfrute','dinero')),
  desde date,
  hasta date,
  dias numeric(6,2) not null check (dias > 0),
  monto numeric(14,2),   -- solo pago en dinero; nulo = días x salario diario
  estado text not null default 'solicitada' check (estado in ('solicitada','aprobada','rechazada','pagada')),
  boleta_id uuid,
  solicitada_por uuid default auth.uid(),
  creado_en timestamptz not null default now()
);

create table public.bonos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  nombre text not null,
  monto numeric(14,2) not null check (monto >= 0),
  afecto_ccss boolean not null default true,
  afecto_salario_base boolean not null default true,
  recurrente boolean not null default false,
  desde date not null,
  hasta date,
  activo boolean not null default true,
  creado_en timestamptz not null default now()
);

-- Cargos y otras deducciones (sindicato, cooperativa, adelantos, uniformes...)
create table public.otras_deducciones (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  descripcion text not null,
  monto numeric(14,2) not null check (monto >= 0),
  recurrente boolean not null default false,
  desde date not null,
  hasta date,
  activo boolean not null default true,
  creado_en timestamptz not null default now()
);

create table public.rebajos_renta (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  desde date not null,
  hasta date not null,
  monto numeric(14,2) not null,
  motivo text
);

-- ------------------------------------------------------------- Obligaciones --
create table public.prestamos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  descripcion text not null default 'Préstamo',
  monto numeric(14,2) not null check (monto > 0),
  cuota numeric(14,2) not null check (cuota > 0),
  saldo numeric(14,2) not null,
  fecha_otorgado date not null default current_date,
  primera_rebaja date not null,
  estado text not null default 'activo' check (estado in ('activo','pausado','pagado','anulado')),
  creado_en timestamptz not null default now()
);

create table public.prestamo_pagos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  prestamo_id uuid not null references public.prestamos(id) on delete cascade,
  boleta_id uuid,
  fecha date not null,
  monto numeric(14,2) not null,
  creado_en timestamptz not null default now()
);

create table public.embargos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  expediente text not null,
  juzgado text,
  beneficiario text,
  cuenta text,
  tipo text not null check (tipo in ('fijo','porcentaje')),
  valor numeric(14,2) not null check (valor > 0),
  desde date not null,
  hasta date,
  estado text not null default 'activo' check (estado in ('activo','suspendido','finalizado')),
  creado_en timestamptz not null default now(),
  check (tipo = 'fijo' or valor <= 25)
);

create table public.pensiones_alimentarias (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  expediente text not null,
  juzgado text,
  beneficiario text,
  relacion text,
  cuenta text,
  tipo text not null check (tipo in ('fijo','porcentaje')),
  valor numeric(14,2) not null check (valor > 0),
  desde date not null,
  hasta date,
  estado text not null default 'activo' check (estado in ('activo','suspendido','finalizado')),
  creado_en timestamptz not null default now()
);

-- ------------------------------------------------------------------ RLS ------
select public._politicas_empresa(t::regclass) from unnest(array[
  'public.empleados','public.historial_salarial','public.documentos_empleado','public.movimientos_empleado',
  'public.marcas_asistencia','public.horas_extra','public.incapacidades','public.permisos','public.vacaciones',
  'public.bonos','public.otras_deducciones','public.rebajos_renta','public.prestamos','public.prestamo_pagos',
  'public.embargos','public.pensiones_alimentarias']) t;

-- Portal del empleado: ve su ficha y sus vacaciones, y puede solicitar vacaciones.
create policy empleado_ve_su_ficha on public.empleados for select to authenticated
  using (usuario_id = auth.uid());
create policy empleado_ve_sus_vacaciones on public.vacaciones for select to authenticated
  using (exists (select 1 from public.empleados e where e.id = empleado_id and e.usuario_id = auth.uid()));
create policy empleado_solicita_vacaciones on public.vacaciones for insert to authenticated
  with check (estado = 'solicitada' and exists (
    select 1 from public.empleados e where e.id = empleado_id and e.empresa_id = vacaciones.empresa_id
      and e.usuario_id = auth.uid()));

-- Auditoría de datos sensibles
create trigger auditar after insert or update or delete on public.empleados for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.historial_salarial for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.prestamos for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.embargos for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.pensiones_alimentarias for each row execute function public.tg_auditar();


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009000300_planillas.sql
-- =============================================================================
-- Planilla Costa Rica · 0003 Planillas, boletas, aguinaldo, liquidaciones y asientos
-- Los montos los escribe únicamente la API (que llama al motor). La base
-- protege las reglas que no pueden fallar: estados, aprobación e inmutabilidad.
-- =============================================================================

create table public.planillas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  calendario_id uuid references public.calendarios_pago(id) on delete set null,
  sucursal_id uuid references public.sucursales(id) on delete set null,
  tipo text not null default 'ordinaria' check (tipo in ('ordinaria','aguinaldo','extraordinaria','liquidacion')),
  frecuencia text not null default 'biweekly' check (frecuencia in ('monthly','biweekly','weekly','bimonthly')),
  desde date not null,
  hasta date not null,
  fecha_pago date,
  descripcion text,
  estado text not null default 'borrador' check (estado in ('borrador','calculada','aprobada','cerrada')),
  totales jsonb not null default '{}'::jsonb,
  advertencias jsonb not null default '[]'::jsonb,
  calculada_en timestamptz,
  aprobada_por uuid,
  aprobada_en timestamptz,
  cerrada_por uuid,
  cerrada_en timestamptz,
  creado_en timestamptz not null default now(),
  creado_por uuid default auth.uid(),
  actualizado_en timestamptz not null default now(),
  actualizado_por uuid,
  check (hasta >= desde)
);
create index on public.planillas (empresa_id, desde desc);
create trigger planillas_upd before update on public.planillas
  for each row execute function public.tg_actualizado();

create table public.boletas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  planilla_id uuid not null references public.planillas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete restrict,
  salario_base numeric(14,2) not null default 0,
  horas_extra numeric(14,2) not null default 0,
  vacaciones numeric(14,2) not null default 0,
  bonos numeric(14,2) not null default 0,
  bruto numeric(14,2) not null default 0,
  base_cotizable numeric(14,2) not null default 0,
  base_ccss numeric(14,2) not null default 0,
  ccss_obrero numeric(14,2) not null default 0,
  renta numeric(14,2) not null default 0,
  creditos_renta numeric(14,2) not null default 0,
  rop_obrero numeric(14,2) not null default 0,
  total_deducciones numeric(14,2) not null default 0,
  subsidio_ccss numeric(14,2) not null default 0,
  subsidio_ins numeric(14,2) not null default 0,
  costo_patrono_incapacidad numeric(14,2) not null default 0,
  neto numeric(14,2) not null default 0,
  ccss_patronal numeric(14,2) not null default 0,
  ins_patronal numeric(14,2) not null default 0,
  rop_patronal numeric(14,2) not null default 0,
  prov_aguinaldo numeric(14,2) not null default 0,
  prov_cesantia numeric(14,2) not null default 0,
  prov_vacaciones numeric(14,2) not null default 0,
  costo_total_patrono numeric(14,2) not null default 0,
  dias_incapacidad numeric(6,2) not null default 0,
  resultado jsonb not null default '{}'::jsonb,   -- salida completa del motor
  entrada jsonb not null default '{}'::jsonb,     -- datos con que se calculó
  parametros jsonb not null default '{}'::jsonb,  -- fotografía de tasas usadas
  version_motor text,
  creado_en timestamptz not null default now(),
  unique (planilla_id, empleado_id)
);
create index on public.boletas (empresa_id, empleado_id);

create table public.boleta_lineas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  boleta_id uuid not null references public.boletas(id) on delete cascade,
  tipo text not null check (tipo in ('ingreso','deduccion','patronal')),
  categoria text not null,
  codigo text,
  descripcion text not null,
  base numeric(14,2),
  tasa numeric(9,6),
  monto numeric(14,2) not null,
  referencia_id uuid,   -- préstamo, embargo, bono... que la originó
  orden int not null default 0
);
create index on public.boleta_lineas (boleta_id);

create table public.reaperturas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  planilla_id uuid not null references public.planillas(id) on delete cascade,
  motivo text not null check (length(trim(motivo)) >= 5),
  usuario_id uuid default auth.uid(),
  fecha timestamptz not null default now()
);

create table public.envios_boleta (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  boleta_id uuid not null references public.boletas(id) on delete cascade,
  canal text not null default 'correo' check (canal in ('correo','telegram','portal')),
  destino text,
  estado text not null check (estado in ('enviado','error','omitido')),
  error text,
  fecha timestamptz not null default now()
);

-- ------------------------------------------------------- Reglas de estados --
-- borrador -> calculada -> aprobada -> cerrada ; cerrada -> calculada (reabrir)
create or replace function public.tg_planilla_estados() returns trigger
language plpgsql as $$
declare
  ok boolean;
begin
  if new.estado is not distinct from old.estado then
    if old.estado in ('aprobada','cerrada') and current_user = 'authenticated'
       and (new.desde, new.hasta, new.tipo, new.frecuencia, new.totales)
           is distinct from (old.desde, old.hasta, old.tipo, old.frecuencia, old.totales) then
      raise exception 'La planilla está %; no se puede modificar', old.estado;
    end if;
    return new;
  end if;

  ok := (old.estado, new.estado) in (
    ('borrador','calculada'), ('calculada','calculada'), ('calculada','borrador'),
    ('calculada','aprobada'), ('aprobada','calculada'), ('aprobada','cerrada'),
    ('cerrada','calculada'));
  if not ok then
    raise exception 'Cambio de estado no permitido: % -> %', old.estado, new.estado;
  end if;

  if current_user = 'authenticated' and new.estado in ('aprobada','cerrada')
     and not public.puede_aprobar(new.empresa_id) then
    raise exception 'Solo un aprobador puede % la planilla',
      case new.estado when 'aprobada' then 'aprobar' else 'cerrar' end;
  end if;

  if old.estado = 'cerrada' then
    if current_user = 'authenticated' and not public.puede_aprobar(new.empresa_id) then
      raise exception 'Solo un aprobador puede reabrir una planilla cerrada';
    end if;
    if not exists (select 1 from public.reaperturas r where r.planilla_id = new.id
                   and r.fecha >= coalesce(old.cerrada_en, '-infinity')) then
      raise exception 'Para reabrir registre primero el motivo en reaperturas';
    end if;
  end if;

  if new.estado = 'aprobada' then new.aprobada_por := auth.uid(); new.aprobada_en := now(); end if;
  if new.estado = 'cerrada' then new.cerrada_por := auth.uid(); new.cerrada_en := now(); end if;
  return new;
end $$;
create trigger planilla_estados before update on public.planillas
  for each row execute function public.tg_planilla_estados();

create or replace function public.tg_planilla_no_borrar() returns trigger
language plpgsql as $$
begin
  if old.estado in ('aprobada','cerrada') then
    raise exception 'No se puede borrar una planilla %', old.estado;
  end if;
  return old;
end $$;
create trigger planilla_no_borrar before delete on public.planillas
  for each row execute function public.tg_planilla_no_borrar();

-- Boletas y líneas: solo cambian mientras la planilla está en borrador/calculada.
create or replace function public.tg_boleta_inmutable() returns trigger
language plpgsql as $$
declare
  v_planilla uuid;
  v_estado text;
begin
  if tg_table_name = 'boletas' then
    v_planilla := coalesce(new.planilla_id, old.planilla_id);
  else
    select b.planilla_id into v_planilla from public.boletas b where b.id = coalesce(new.boleta_id, old.boleta_id);
  end if;
  select estado into v_estado from public.planillas where id = v_planilla;
  if v_estado in ('aprobada','cerrada') then
    raise exception 'La planilla está %: las boletas no se pueden modificar', v_estado;
  end if;
  return coalesce(new, old);
end $$;
create trigger boleta_inmutable before insert or update or delete on public.boletas
  for each row execute function public.tg_boleta_inmutable();
create trigger linea_inmutable before insert or update or delete on public.boleta_lineas
  for each row execute function public.tg_boleta_inmutable();

-- ------------------------------------------------- Aguinaldo y liquidaciones --
create table public.aguinaldos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  anio int not null,
  empleado_id uuid not null references public.empleados(id) on delete restrict,
  detalle jsonb not null default '[]'::jsonb,   -- salarios por mes dic..nov
  total_salarios numeric(14,2) not null,
  monto numeric(14,2) not null,
  planilla_id uuid references public.planillas(id) on delete set null,
  estado text not null default 'calculado' check (estado in ('calculado','aprobado','pagado')),
  creado_en timestamptz not null default now(),
  unique (empresa_id, anio, empleado_id)
);

create table public.liquidaciones (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete restrict,
  fecha_salida date not null,
  motivo text not null check (motivo in ('renuncia','despido_sin_responsabilidad','despido_con_responsabilidad',
                                          'mutuo_acuerdo','pension','fallecimiento','fin_contrato')),
  preaviso_otorgado boolean not null default false,
  detalle jsonb not null default '{}'::jsonb,
  salario_promedio numeric(14,2) not null default 0,
  preaviso numeric(14,2) not null default 0,
  cesantia numeric(14,2) not null default 0,
  vacaciones numeric(14,2) not null default 0,
  aguinaldo numeric(14,2) not null default 0,
  otros_pagos numeric(14,2) not null default 0,
  deducciones numeric(14,2) not null default 0,
  total numeric(14,2) not null default 0,
  estado text not null default 'borrador' check (estado in ('borrador','aprobada','pagada','anulada')),
  aprobada_por uuid,
  creado_en timestamptz not null default now(),
  creado_por uuid default auth.uid()
);

-- --------------------------------------------------------------- Asientos ----
create table public.cuentas_contables (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  concepto text not null,          -- gasto_salarios, ccss_por_pagar, ...
  codigo text not null,
  nombre text not null,
  centro_costo text,
  unique (empresa_id, concepto)
);

create table public.asientos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  origen text not null check (origen in ('planilla','liquidacion','aguinaldo','vacaciones','manual')),
  origen_id uuid,
  fecha date not null,
  descripcion text not null,
  total_debito numeric(14,2) not null,
  total_credito numeric(14,2) not null,
  estado text not null default 'vigente' check (estado in ('vigente','reemplazado')),
  exportado_en timestamptz,
  creado_en timestamptz not null default now(),
  check (total_debito = total_credito)
);

create table public.asiento_lineas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  asiento_id uuid not null references public.asientos(id) on delete cascade,
  cuenta_codigo text not null,
  cuenta_nombre text not null,
  descripcion text,
  sucursal_id uuid,
  empleado_id uuid,
  debito numeric(14,2) not null default 0,
  credito numeric(14,2) not null default 0,
  orden int not null default 0
);

-- ------------------------------------------------------------------ RLS ------
select public._politicas_empresa(t::regclass) from unnest(array[
  'public.planillas','public.boletas','public.boleta_lineas','public.reaperturas','public.envios_boleta',
  'public.aguinaldos','public.liquidaciones','public.cuentas_contables','public.asientos','public.asiento_lineas']) t;

-- Portal del empleado: sus boletas de planillas cerradas.
create policy empleado_ve_sus_boletas on public.boletas for select to authenticated using (
  exists (select 1 from public.empleados e where e.id = empleado_id and e.usuario_id = auth.uid())
  and exists (select 1 from public.planillas p where p.id = planilla_id and p.estado = 'cerrada'));
create policy empleado_ve_sus_lineas on public.boleta_lineas for select to authenticated using (
  exists (select 1 from public.boletas b where b.id = boleta_id));

create trigger auditar after insert or update or delete on public.planillas for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.reaperturas for each row execute function public.tg_auditar();
create trigger auditar after insert or update or delete on public.liquidaciones for each row execute function public.tg_auditar();


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009000400_negocio_autogestion.sql
-- =============================================================================
-- Planilla Costa Rica · 0004 SaaS, administración de A.U.R.O.M., socios y autogestión
-- (Fases 6, 7 y 8)
-- =============================================================================

-- ------------------------------------------------------------ Fase 6: SaaS --
-- Alta de una empresa nueva: crea la empresa, deja al usuario como admin,
-- configuración por defecto, calendario y cuentas contables, y (si viene un
-- código de referido) asigna al socio como contador de soporte.
create or replace function public.crear_empresa(
  p_nombre text, p_cedula text default null, p_codigo_referido text default null,
  p_frecuencia text default 'biweekly'
) returns uuid
language plpgsql security definer set search_path = public as $$
declare
  v_empresa uuid;
  v_socio uuid;
  v_tipo text;
begin
  if auth.uid() is null then raise exception 'Debe iniciar sesión'; end if;
  if p_codigo_referido is not null then
    select id, tipo into v_socio, v_tipo from socios
     where codigo_referido = upper(trim(p_codigo_referido)) and estado = 'activo';
  end if;

  insert into empresas (nombre, cedula_juridica, estado_servicio, referido_por, creado_por)
  values (p_nombre, p_cedula, 'prueba', v_socio, auth.uid()) returning id into v_empresa;
  insert into usuarios_empresa (empresa_id, usuario_id, rol, creado_por) values (v_empresa, auth.uid(), 'admin', auth.uid());
  insert into configuracion_planilla (empresa_id) values (v_empresa);
  insert into calendarios_pago (empresa_id, nombre, frecuencia)
  values (v_empresa, case p_frecuencia when 'monthly' then 'Mensual' when 'weekly' then 'Semanal' else 'Quincenal' end, p_frecuencia);
  perform crear_cuentas_por_defecto(v_empresa);

  if v_socio is not null then
    insert into asignaciones_contador (empresa_id, socio_id, funciones, permiso, soporte_principal, asignado_por)
    values (v_empresa, v_socio,
            case v_tipo when 'contador' then array['supervision','soporte','implementacion']
                        else array['ventas','implementacion'] end,
            'operacion', v_tipo = 'contador', auth.uid());
  end if;
  return v_empresa;
end $$;
grant execute on function public.crear_empresa(text, text, text, text) to authenticated;

create or replace function public.crear_cuentas_por_defecto(p_empresa uuid) returns void
language sql security definer set search_path = public as $$
  insert into cuentas_contables (empresa_id, concepto, codigo, nombre) values
    (p_empresa, 'gasto_salarios',        '5101', 'Sueldos y salarios'),
    (p_empresa, 'gasto_cargas_sociales', '5102', 'Cargas sociales patronales'),
    (p_empresa, 'gasto_aguinaldo',       '5103', 'Aguinaldo'),
    (p_empresa, 'gasto_vacaciones',      '5104', 'Vacaciones'),
    (p_empresa, 'gasto_cesantia',        '5105', 'Cesantía'),
    (p_empresa, 'gasto_incapacidades',   '5106', 'Incapacidades a cargo del patrono'),
    (p_empresa, 'salarios_por_pagar',    '2101', 'Salarios por pagar'),
    (p_empresa, 'ccss_por_pagar',        '2102', 'CCSS por pagar'),
    (p_empresa, 'ins_por_pagar',         '2103', 'INS riesgos del trabajo por pagar'),
    (p_empresa, 'renta_por_pagar',       '2104', 'Impuesto al salario retenido'),
    (p_empresa, 'rop_por_pagar',         '2105', 'ROP por pagar'),
    (p_empresa, 'embargos_por_pagar',    '2106', 'Embargos y pensiones por pagar'),
    (p_empresa, 'otras_retenciones',     '2107', 'Otras retenciones a empleados'),
    (p_empresa, 'prov_aguinaldo',        '2111', 'Provisión aguinaldo'),
    (p_empresa, 'prov_vacaciones',       '2112', 'Provisión vacaciones'),
    (p_empresa, 'prov_cesantia',         '2113', 'Provisión cesantía'),
    (p_empresa, 'prestamos_empleados',   '1205', 'Préstamos a empleados'),
    (p_empresa, 'subsidios_por_cobrar',  '1206', 'Subsidios CCSS por cobrar')
  on conflict (empresa_id, concepto) do nothing
$$;

-- ----------------------------------------- Socios: prospectos y comisiones --
create table public.prospectos (
  id uuid primary key default gen_random_uuid(),
  socio_id uuid not null references public.socios(id) on delete cascade,
  empresa_nombre text not null,
  contacto text,
  correo text,
  telefono text,
  registrado_en timestamptz not null default now(),
  vence_atribucion date not null default (current_date + 90),
  empresa_id uuid references public.empresas(id) on delete set null,
  estado text not null default 'abierto' check (estado in ('abierto','convertido','perdido','vencido'))
);
create index on public.prospectos (lower(empresa_nombre));

create table public.comisiones (
  id uuid primary key default gen_random_uuid(),
  socio_id uuid not null references public.socios(id) on delete restrict,
  empresa_id uuid not null references public.empresas(id) on delete restrict,
  concepto text not null check (concepto in ('implementacion','mensualidad_vendedor','recurrente_contador')),
  periodo date not null,                  -- primer día del mes
  base numeric(14,2) not null,            -- pago del cliente sin IVA
  porcentaje numeric(6,3),
  monto numeric(14,2) not null,
  cobro_id uuid,                          -- pago del cliente que la origina
  liquidacion_id uuid,
  estado text not null default 'pendiente' check (estado in ('pendiente','liquidada','anulada')),
  creado_en timestamptz not null default now(),
  unique (socio_id, empresa_id, concepto, periodo)
);

create table public.liquidaciones_socio (
  id uuid primary key default gen_random_uuid(),
  socio_id uuid not null references public.socios(id) on delete restrict,
  mes date not null,
  total numeric(14,2) not null,
  factura_xml_path text,
  pagado_en timestamptz,
  referencia_pago text,
  gasto_id uuid,
  creado_en timestamptz not null default now(),
  unique (socio_id, mes)
);

alter table public.prospectos enable row level security;
create policy ver on public.prospectos for select to authenticated using (socio_id = public.mi_socio_id() or public.es_admin_plataforma());
create policy crear on public.prospectos for insert to authenticated with check (socio_id = public.mi_socio_id() or public.es_admin_plataforma());
create policy editar on public.prospectos for update to authenticated using (public.es_admin_plataforma());
grant select, insert, update on public.prospectos to authenticated;

alter table public.comisiones enable row level security;
create policy ver on public.comisiones for select to authenticated using (socio_id = public.mi_socio_id() or public.es_admin_plataforma());
create policy gestionar on public.comisiones for all to authenticated using (public.es_admin_plataforma()) with check (public.es_admin_plataforma());
grant select, insert, update, delete on public.comisiones to authenticated;

alter table public.liquidaciones_socio enable row level security;
create policy ver on public.liquidaciones_socio for select to authenticated using (socio_id = public.mi_socio_id() or public.es_admin_plataforma());
create policy gestionar on public.liquidaciones_socio for all to authenticated using (public.es_admin_plataforma()) with check (public.es_admin_plataforma());
grant select, insert, update, delete on public.liquidaciones_socio to authenticated;

-- ------------------------------------------------- Soporte (bot + tickets) --
create table public.articulos_ayuda (
  id uuid primary key default gen_random_uuid(),
  titulo text not null,
  contenido text not null,
  categoria text,
  palabras_clave text[] not null default '{}',
  publicado boolean not null default true,
  version int not null default 1,
  actualizado_en timestamptz not null default now(),
  actualizado_por uuid
);
create trigger articulos_upd before update on public.articulos_ayuda for each row execute function public.tg_actualizado();

create table public.tickets (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  usuario_id uuid default auth.uid(),
  canal text not null default 'web' check (canal in ('web','telegram','correo')),
  asunto text not null,
  estado text not null default 'abierto' check (estado in ('abierto','en_proceso','escalado','resuelto','cerrado')),
  prioridad text not null default 'normal' check (prioridad in ('baja','normal','alta','urgente')),
  -- Soporte de primer nivel: el contador asignado. Si no resuelve, se escala a la plataforma.
  asignado_socio_id uuid references public.socios(id) on delete set null,
  nivel text not null default 'contador' check (nivel in ('bot','contador','plataforma')),
  resuelto_por text check (resuelto_por in ('bot','contador','plataforma')),
  minutos_invertidos int not null default 0,
  creado_en timestamptz not null default now(),
  actualizado_en timestamptz not null default now(),
  actualizado_por uuid
);
create trigger tickets_upd before update on public.tickets for each row execute function public.tg_actualizado();

-- Al crear un ticket se asigna al contador de soporte principal de la empresa.
create or replace function public.tg_ticket_asignar() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if new.asignado_socio_id is null then
    select a.socio_id into new.asignado_socio_id
      from asignaciones_contador a
     where a.empresa_id = new.empresa_id and a.revocado_en is null
       and 'soporte' = any(a.funciones)
     order by a.soporte_principal desc, a.desde
     limit 1;
  end if;
  if new.asignado_socio_id is null and new.nivel = 'contador' then
    new.nivel := 'plataforma';
  end if;
  return new;
end $$;
create trigger ticket_asignar before insert on public.tickets for each row execute function public.tg_ticket_asignar();

create table public.mensajes_ticket (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  ticket_id uuid not null references public.tickets(id) on delete cascade,
  autor text not null check (autor in ('cliente','bot','contador','plataforma')),
  usuario_id uuid default auth.uid(),
  texto text not null,
  adjuntos jsonb not null default '[]'::jsonb,
  creado_en timestamptz not null default clock_timestamp()   -- orden estable dentro de una transacción
);

-- Tickets: el cliente no puede reasignar ni cambiar el nivel a su gusto.
-- Solo se permite escalar (contador -> plataforma); reasignar es de la plataforma o del admin de la empresa.
create or replace function public.tg_ticket_campos() returns trigger
language plpgsql as $$
begin
  if current_user <> 'authenticated' or public.es_admin_plataforma() then
    return new;
  end if;
  if new.empresa_id is distinct from old.empresa_id then
    raise exception 'No se puede mover un ticket de empresa';
  end if;
  if new.asignado_socio_id is distinct from old.asignado_socio_id and not public.es_admin_empresa(new.empresa_id) then
    raise exception 'Solo el administrador de la empresa o la plataforma reasignan tickets';
  end if;
  if new.nivel is distinct from old.nivel and not (old.nivel in ('bot','contador') and new.nivel = 'plataforma') then
    raise exception 'Un ticket solo se puede escalar a la plataforma';
  end if;
  if (new.minutos_invertidos, new.resuelto_por) is distinct from (old.minutos_invertidos, old.resuelto_por)
     and old.asignado_socio_id is distinct from public.mi_socio_id() then
    raise exception 'Solo quien atiende el ticket registra la solución';
  end if;
  return new;
end $$;
create trigger ticket_campos before update on public.tickets for each row execute function public.tg_ticket_campos();

alter table public.articulos_ayuda enable row level security;
create policy ver on public.articulos_ayuda for select to authenticated using (publicado or public.es_admin_plataforma());
create policy gestionar on public.articulos_ayuda for all to authenticated using (public.es_admin_plataforma()) with check (public.es_admin_plataforma());
grant select, insert, update, delete on public.articulos_ayuda to authenticated;

-- Tickets: los ve la empresa, su contador asignado (por la asignación) y la plataforma.
-- Cualquiera con acceso a la empresa (incluido rol lectura) puede abrir un ticket.
alter table public.tickets enable row level security;
create policy ver on public.tickets for select to authenticated using (public.puede_ver(empresa_id));
create policy crear on public.tickets for insert to authenticated with check (public.puede_ver(empresa_id));
create policy editar on public.tickets for update to authenticated using (public.puede_ver(empresa_id)) with check (public.puede_ver(empresa_id));
grant select, insert, update on public.tickets to authenticated;
alter table public.mensajes_ticket enable row level security;
create policy ver on public.mensajes_ticket for select to authenticated using (public.puede_ver(empresa_id));
create policy crear on public.mensajes_ticket for insert to authenticated with check (public.puede_ver(empresa_id));
grant select, insert on public.mensajes_ticket to authenticated;

-- ---------------------------------------------- Telegram y alertas --------
create table public.telegram_vinculos (
  id uuid primary key default gen_random_uuid(),
  usuario_id uuid not null references auth.users(id) on delete cascade,
  chat_id text,
  codigo_vinculacion text not null unique default upper(substr(md5(gen_random_uuid()::text), 1, 6)),
  rol text not null default 'cliente' check (rol in ('duena','cliente','socio')),
  avisos jsonb not null default '{"boletas": true, "recordatorios": true}'::jsonb,
  activo boolean not null default false,
  creado_en timestamptz not null default now()
);
alter table public.telegram_vinculos enable row level security;
create policy propio on public.telegram_vinculos for all to authenticated
  using (usuario_id = auth.uid() or public.es_admin_plataforma())
  with check (usuario_id = auth.uid() or public.es_admin_plataforma());
grant select, insert, update, delete on public.telegram_vinculos to authenticated;

-- =============================================================================
-- Esquema "negocio": administración interna de A.U.R.O.M. (solo la dueña)
-- =============================================================================
create schema if not exists negocio;
grant usage on schema negocio to authenticated;

create table negocio.clientes (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid unique references public.empresas(id) on delete set null,
  razon_social text not null,
  tipo_identificacion text not null default '02' check (tipo_identificacion in ('01','02','03','04')), -- física, jurídica, DIMEX, NITE
  identificacion text not null,
  correo_facturacion text not null,
  telefono text,
  actividad_receptor text,
  precio_mensual numeric(14,2) not null default 55000,   -- sin IVA
  moneda text not null default 'CRC' check (moneda in ('CRC','USD')),
  precio_fundador_hasta date,                             -- clientes Odoo: US$100 por 12 meses
  empleados_incluidos int not null default 50,
  precio_bloque_10 numeric(14,2) not null default 11000,
  implementacion numeric(14,2) not null default 100000,
  dia_cobro int not null default 1 check (dia_cobro between 1 and 28),
  estado text not null default 'activo' check (estado in ('prueba','activo','gracia','solo_lectura','suspendido','baja')),
  alta date not null default current_date,
  baja date,
  creado_en timestamptz not null default now()
);

create table negocio.comprobantes (
  id uuid primary key default gen_random_uuid(),
  tipo text not null check (tipo in ('01','02','03','04')),  -- factura, nota débito, nota crédito, tiquete
  clave char(50) unique,
  consecutivo char(20) unique,
  fecha timestamptz not null default now(),
  cliente_id uuid references negocio.clientes(id),
  periodo date,
  condicion_venta text not null default '01',
  medio_pago text not null default '04',     -- 04 transferencia/SINPE, 02 tarjeta
  moneda text not null default 'CRC',
  tipo_cambio numeric(12,5) not null default 1,
  subtotal numeric(14,2) not null,
  impuesto numeric(14,2) not null,
  total numeric(14,2) not null,
  referencia_id uuid references negocio.comprobantes(id),   -- notas de crédito
  xml_firmado text,
  respuesta_hacienda text,
  estado text not null default 'borrador'
    check (estado in ('borrador','firmado','enviado','aceptado','rechazado','error','anulado')),
  mensaje_estado text,
  enviado_por_correo_en timestamptz,
  creado_en timestamptz not null default now()
);

create table negocio.comprobante_lineas (
  id uuid primary key default gen_random_uuid(),
  comprobante_id uuid not null references negocio.comprobantes(id) on delete cascade,
  numero int not null,
  cabys char(13) not null,
  descripcion text not null,
  cantidad numeric(12,3) not null default 1,
  unidad text not null default 'Sp',
  precio_unitario numeric(14,2) not null,
  descuento numeric(14,2) not null default 0,
  tarifa_iva numeric(5,2) not null default 13,
  impuesto numeric(14,2) not null,
  total numeric(14,2) not null
);

create table negocio.pagos (
  id uuid primary key default gen_random_uuid(),
  cliente_id uuid not null references negocio.clientes(id),
  comprobante_id uuid references negocio.comprobantes(id),
  fecha date not null default current_date,
  monto numeric(14,2) not null,
  medio text not null check (medio in ('sinpe','transferencia','tarjeta','efectivo')),
  comision numeric(14,2) not null default 0,
  retencion_iva numeric(14,2) not null default 0,
  retencion_renta numeric(14,2) not null default 0,
  referencia text,
  creado_en timestamptz not null default now()
);

create table negocio.categorias_gasto (
  id uuid primary key default gen_random_uuid(),
  nombre text not null unique,
  deducible boolean not null default true,
  iva_acreditable boolean not null default true
);

create table negocio.gastos (
  id uuid primary key default gen_random_uuid(),
  fecha date not null,
  proveedor text not null,
  identificacion_proveedor text,
  categoria_id uuid references negocio.categorias_gasto(id),
  descripcion text,
  moneda text not null default 'CRC',
  tipo_cambio numeric(12,5) not null default 1,
  subtotal numeric(14,2) not null,
  iva numeric(14,2) not null default 0,
  total numeric(14,2) not null,
  iva_acreditable boolean not null default true,
  deducible boolean not null default true,
  clave_comprobante char(50),
  xml_recibido text,
  mensaje_receptor text check (mensaje_receptor in ('aceptado','aceptado_parcial','rechazado')),
  creado_en timestamptz not null default now()
);

create table negocio.periodos (
  id uuid primary key default gen_random_uuid(),
  mes date not null unique,
  estado text not null default 'abierto' check (estado in ('abierto','cerrado')),
  resumen jsonb not null default '{}'::jsonb,
  cerrado_en timestamptz
);

create table negocio.movimientos_caja (
  id uuid primary key default gen_random_uuid(),
  fecha date not null,
  tipo text not null check (tipo in ('salario_duena','caja','inversion','impuesto','comision','otro')),
  monto numeric(14,2) not null,
  nota text
);

-- Tilopay
create table negocio.metodos_pago (
  id uuid primary key default gen_random_uuid(),
  cliente_id uuid not null references negocio.clientes(id) on delete cascade,
  proveedor text not null default 'tilopay',
  token text not null,          -- referencia del proveedor; nunca el número de tarjeta
  ultimos4 char(4),
  vence text,
  estado text not null default 'activo' check (estado in ('activo','vencido','eliminado')),
  creado_en timestamptz not null default now()
);

create table negocio.cobros (
  id uuid primary key default gen_random_uuid(),
  cliente_id uuid not null references negocio.clientes(id),
  periodo date not null,
  empleados_facturados int not null,
  subtotal numeric(14,2) not null,
  iva numeric(14,2) not null,
  total numeric(14,2) not null,
  intento int not null default 1,
  resultado text not null default 'pendiente' check (resultado in ('pendiente','aprobado','rechazado','error')),
  codigo_respuesta text,
  orden_proveedor text,
  comision numeric(14,2) not null default 0,
  comprobante_id uuid references negocio.comprobantes(id),
  pago_id uuid references negocio.pagos(id),
  creado_en timestamptz not null default now()
);
create unique index cobro_aprobado_unico on negocio.cobros (cliente_id, periodo) where resultado = 'aprobado';

create table negocio.eventos_webhook (
  id uuid primary key default gen_random_uuid(),
  origen text not null check (origen in ('tilopay','telegram','hacienda')),
  tipo text,
  contenido jsonb not null,
  firma_valida boolean,
  procesado_en timestamptz,
  error text,
  recibido_en timestamptz not null default now()
);

create table negocio.alertas (
  id uuid primary key default gen_random_uuid(),
  tipo text not null check (tipo in ('pago','factura','error','caida','normativa','ticket','socio','resumen')),
  severidad text not null default 'info' check (severidad in ('info','aviso','critica')),
  mensaje text not null,
  enviada_en timestamptz,
  atendida boolean not null default false,
  creado_en timestamptz not null default now()
);

create table negocio.cambios_normativos (
  id uuid primary key default gen_random_uuid(),
  fuente text not null,
  enlace text,
  publicado date,
  resumen text not null,
  parametros_propuestos jsonb,
  vigente_desde date,
  estado text not null default 'detectado' check (estado in ('detectado','aprobado','rechazado','aplicado')),
  resultado_pruebas text,
  creado_en timestamptz not null default now()
);

create table negocio.chequeos_salud (
  id bigint generated always as identity primary key,
  servicio text not null,
  ok boolean not null,
  latencia_ms int,
  detalle text,
  fecha timestamptz not null default now()
);

create table negocio.configuracion (
  clave text primary key,
  valor jsonb not null
);

-- RLS: todo el esquema negocio es solo para la dueña de la plataforma.
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'negocio' loop
    execute format('alter table negocio.%I enable row level security', t);
    execute format('create policy solo_plataforma on negocio.%I for all to authenticated using (public.es_admin_plataforma()) with check (public.es_admin_plataforma())', t);
    execute format('grant select, insert, update, delete on negocio.%I to authenticated', t);
  end loop;
end $$;

-- El cliente puede ver sus propios comprobantes y cobros (portal de facturación).
create policy cliente_ve_sus_comprobantes on negocio.comprobantes for select to authenticated
  using (exists (select 1 from negocio.clientes c where c.id = cliente_id and c.empresa_id is not null and public.es_admin_empresa(c.empresa_id)));
create policy cliente_ve_su_ficha on negocio.clientes for select to authenticated
  using (empresa_id is not null and public.es_admin_empresa(empresa_id));
create policy cliente_ve_sus_cobros on negocio.cobros for select to authenticated
  using (exists (select 1 from negocio.clientes c where c.id = cliente_id and c.empresa_id is not null and public.es_admin_empresa(c.empresa_id)));

grant usage on schema negocio to service_role;
grant all on all tables in schema negocio to service_role;
grant all on all tables in schema public to service_role;


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009000500_semillas.sql
-- =============================================================================
-- Planilla Costa Rica · 0005 Datos iniciales: parámetros legales 2026, feriados,
-- categorías de gasto y configuración del negocio.
-- =============================================================================

insert into public.parametros_legales (vigente_desde, fuente, datos) values ('2026-01-01',
 'Decreto 45333-H (renta); CCSS 2026; Código de Trabajo Arts. 28-29, 153',
 '{
   "ccss_obrero": "0.1083",
   "ccss_obrero_pensionado_estado": "0.065",
   "ccss_patronal": "0.2683",
   "bmc_mensual": "333328",
   "ins_tasas": {"I": "0.0087", "II": "0.0149", "III": "0.0247", "IV": "0.0413", "V": "0.0688"},
   "ins_clase_defecto": "II",
   "rop_obrero": "0.01",
   "rop_patronal": "0.0325",
   "prov_aguinaldo": "0.0833",
   "prov_cesantia_defecto": "0.0533",
   "cesantia_tabla": {"1": "19.5", "2": "20", "3": "20.5", "4": "21", "5": "21.24", "6": "21.5", "7": "22",
                      "8": "22", "9": "22", "10": "21.5", "11": "21", "12": "20.5", "13": "20"},
   "cesantia_max_anios": 8,
   "tramos_renta": [
     {"desde": "0", "hasta": "918000", "tasa": "0"},
     {"desde": "918000", "hasta": "1347000", "tasa": "10"},
     {"desde": "1347000", "hasta": "2364000", "tasa": "15"},
     {"desde": "2364000", "hasta": "4727000", "tasa": "20"},
     {"desde": "4727000", "hasta": null, "tasa": "25"}
   ],
   "credito_hijo": "1710",
   "credito_conyuge": "2590",
   "dias_mes": 30,
   "dias_incapacidad_patrono": 3,
   "max_pct_embargo": "25",
   "salario_minimo_inembargable": "268731.31",
   "edad_limite_bmc_reducida": 35
 }'::jsonb)
on conflict (vigente_desde) do nothing;

insert into public.feriados (empresa_id, fecha, nombre, pago_obligatorio) values
  (null, '2026-01-01', 'Año Nuevo', true),
  (null, '2026-04-02', 'Jueves Santo', true),
  (null, '2026-04-03', 'Viernes Santo', true),
  (null, '2026-04-11', 'Día de Juan Santamaría', true),
  (null, '2026-05-01', 'Día Internacional del Trabajo', true),
  (null, '2026-07-25', 'Anexión del Partido de Nicoya', true),
  (null, '2026-08-02', 'Día de la Virgen de los Ángeles', false),
  (null, '2026-08-15', 'Día de la Madre', true),
  (null, '2026-08-31', 'Día de la Persona Negra y la Cultura Afrocostarricense', false),
  (null, '2026-09-15', 'Día de la Independencia', true),
  (null, '2026-12-01', 'Abolición del Ejército', false),
  (null, '2026-12-25', 'Navidad', true)
on conflict do nothing;

insert into negocio.categorias_gasto (nombre, deducible, iva_acreditable) values
  ('Infraestructura (servidores, dominios)', true, true),
  ('Comisiones de socios y vendedores', true, true),
  ('Comisiones de tarjeta', true, false),
  ('Servicios profesionales (contador, legal)', true, true),
  ('Mercadeo y publicidad', true, true),
  ('Software y suscripciones', true, true),
  ('Equipo de cómputo', true, true),
  ('Internet y telefonía', true, true),
  ('Capacitación', true, true),
  ('Otros', true, true)
on conflict (nombre) do nothing;

-- Datos del emisor para facturación electrónica. Completar antes de facturar:
-- la API rechaza emitir comprobantes mientras falte algún dato.
insert into negocio.configuracion (clave, valor) values
  ('emisor', '{"nombre": null, "tipo_identificacion": "01", "identificacion": null, "nombre_comercial": "A.U.R.O.M.",
               "correo": null, "telefono": null, "provincia": null, "canton": null, "distrito": null, "otras_senas": null,
               "codigo_actividad": null, "sucursal": "001", "terminal": "00001"}'::jsonb),
  ('cabys_suscripcion', 'null'::jsonb),
  ('cabys_implementacion', 'null'::jsonb),
  ('comisiones', '{"implementacion_contador": "50000", "recurrente_contador_pct": "20", "meses_vendedor": 3}'::jsonb),
  ('tarifa_tarjeta_pct', '"5"'::jsonb)
on conflict (clave) do nothing;


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009000600_funciones_api.sql
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


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009000800_autogestion.sql
-- =============================================================================
-- Planilla Costa Rica · 0008 Autogestión: búsqueda de ayuda, portal del empleado,
-- Telegram, acumulación de vacaciones y funciones de apoyo con permisos
-- explícitos (Fases 6 y 8).
-- =============================================================================

-- ------------------------------------------------ Búsqueda en artículos ----
-- Normaliza tildes para que "liquidacion" y "liquidación" coincidan.
create or replace function public.sin_tildes(p text) returns text
language sql immutable parallel safe as $$
  select translate(lower(coalesce(p, '')), 'áéíóúüñàèìòù', 'aeiouunaeiou')
$$;

create or replace function public.articulo_tsv(p_titulo text, p_contenido text, p_palabras text[]) returns tsvector
language sql immutable parallel safe as $$
  select setweight(to_tsvector('spanish'::regconfig, public.sin_tildes(p_titulo)), 'A')
      || setweight(to_tsvector('spanish'::regconfig, public.sin_tildes(array_to_string(p_palabras, ' '))), 'B')
      || setweight(to_tsvector('spanish'::regconfig, public.sin_tildes(p_contenido)), 'C')
$$;

alter table public.articulos_ayuda
  add column if not exists busqueda tsvector
  generated always as (public.articulo_tsv(titulo, contenido, palabras_clave)) stored;
create index if not exists articulos_ayuda_busqueda on public.articulos_ayuda using gin (busqueda);

-- Búsqueda con ranking. Corre con los permisos de quien llama (RLS: solo publicados,
-- salvo la plataforma). Usa OR entre palabras para preguntas en lenguaje natural.
create or replace function public.buscar_ayuda(p_q text, p_limite int default 5)
returns table (id uuid, titulo text, categoria text, contenido text, rango real, fragmento text)
language sql stable security invoker set search_path = public as $$
  with q as (
    select nullif(replace(plainto_tsquery('spanish'::regconfig, public.sin_tildes(p_q))::text, ' & ', ' | '), '') as txt
  ), tq as (select to_tsquery('spanish'::regconfig, txt) as consulta from q where txt is not null)
  select a.id, a.titulo, a.categoria, a.contenido,
         ts_rank_cd(a.busqueda, tq.consulta, 1)::real as rango,
         ts_headline('spanish'::regconfig, a.contenido, tq.consulta,
                     'MaxWords=45, MinWords=15, MaxFragments=2, FragmentDelimiter=" … ", StartSel="", StopSel=""') as fragmento
    from articulos_ayuda a, tq
   where a.busqueda @@ tq.consulta
   order by rango desc, a.actualizado_en desc
   limit greatest(1, least(coalesce(p_limite, 5), 20))
$$;
grant execute on function public.buscar_ayuda(text, int) to authenticated;

-- ------------------------------------------------ Portal del empleado -------
-- La política original de boletas consultaba planillas con el RLS del empleado,
-- que no ve planillas: nunca devolvía filas. Se reemplaza por una función que
-- solo responde si la planilla está cerrada.
create or replace function public.planilla_cerrada(p_planilla uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from planillas where id = p_planilla and estado = 'cerrada')
$$;

drop policy if exists empleado_ve_sus_boletas on public.boletas;
create policy empleado_ve_sus_boletas on public.boletas for select to authenticated using (
  exists (select 1 from public.empleados e where e.id = empleado_id and e.usuario_id = auth.uid())
  and public.planilla_cerrada(planilla_id));

-- Datos mínimos de las planillas cerradas donde el usuario tiene boleta (el empleado
-- no ve la tabla planillas ni la empresa: solo periodo, fecha de pago y encabezado).
create or replace function public.portal_planillas()
returns table (planilla_id uuid, empresa_id uuid, empresa_nombre text, cedula_juridica text, tipo text,
               desde date, hasta date, fecha_pago date, nota_boleta text)
language sql stable security definer set search_path = public as $$
  select distinct p.id, p.empresa_id, em.nombre, em.cedula_juridica, p.tipo, p.desde, p.hasta, p.fecha_pago, c.nota_boleta
    from boletas b
    join empleados e on e.id = b.empleado_id and e.usuario_id = auth.uid()
    join planillas p on p.id = b.planilla_id and p.estado = 'cerrada'
    join empresas em on em.id = p.empresa_id
    left join configuracion_planilla c on c.empresa_id = p.empresa_id
   where auth.uid() is not null
$$;
grant execute on function public.portal_planillas() to authenticated;

-- Nombre de la empresa de cada ficha del empleado (para el perfil del portal).
create or replace function public.portal_empleos()
returns table (empleado_id uuid, empresa_id uuid, empresa_nombre text)
language sql stable security definer set search_path = public as $$
  select e.id, e.empresa_id, em.nombre from empleados e join empresas em on em.id = e.empresa_id
   where e.usuario_id = auth.uid() and auth.uid() is not null
   order by em.nombre
$$;
grant execute on function public.portal_empleos() to authenticated;

-- Buscar una cuenta por correo: solo la plataforma o un administrador de la empresa indicada.
create or replace function public.buscar_usuario_por_correo(p_correo text, p_empresa uuid default null)
returns uuid
language plpgsql stable security definer set search_path = public as $$
begin
  if not (public.es_admin_plataforma() or (p_empresa is not null and public.es_admin_empresa(p_empresa))) then
    raise exception 'Sin permiso para buscar usuarios';
  end if;
  return (select id from auth.users where lower(email) = lower(trim(p_correo)) limit 1);
end $$;
grant execute on function public.buscar_usuario_por_correo(text, uuid) to authenticated;

-- ------------------------------------------- Acumulación de vacaciones ------
-- Bitácora de los días acreditados por la tarea diaria (Art. 153 CT). La
-- unicidad por empleado y fecha hace la tarea idempotente.
create table if not exists public.vacaciones_acumuladas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  empleado_id uuid not null references public.empleados(id) on delete cascade,
  fecha date not null,
  dias numeric(6,2) not null,
  saldo_resultante numeric(8,2) not null,
  detalle text,
  creado_en timestamptz not null default now(),
  unique (empleado_id, fecha)
);
alter table public.vacaciones_acumuladas enable row level security;
drop policy if exists ver on public.vacaciones_acumuladas;
create policy ver on public.vacaciones_acumuladas for select to authenticated using (
  public.puede_ver(empresa_id)
  or exists (select 1 from public.empleados e where e.id = empleado_id and e.usuario_id = auth.uid()));
grant select on public.vacaciones_acumuladas to authenticated;

-- ---------------------------------------------------- Telegram ------------
-- Idempotencia del webhook: un update_id de Telegram se procesa una sola vez.
create unique index if not exists eventos_telegram_update
  on negocio.eventos_webhook ((contenido ->> 'update_id')) where origen = 'telegram';

-- Chat de Telegram de un usuario de la empresa (para avisarle respuestas de soporte).
-- Vive en el esquema negocio (no expuesto por la API REST de Supabase).
create or replace function negocio.chat_usuario_empresa(p_empresa uuid, p_usuario uuid) returns text
language sql stable security definer set search_path = public as $$
  select v.chat_id from telegram_vinculos v
   where v.usuario_id = p_usuario and v.activo and v.chat_id is not null
     and public.puede_ver(p_empresa)
     and exists (select 1 from usuarios_empresa ue where ue.empresa_id = p_empresa and ue.usuario_id = p_usuario and ue.activo)
   order by v.creado_en desc limit 1
$$;
grant execute on function negocio.chat_usuario_empresa(uuid, uuid) to authenticated;

-- Alerta para la dueña cuando un ticket se escala o es urgente. Quien llama
-- debe tener acceso a la empresa del ticket y el ticket debe ameritarlo.
create or replace function negocio.alerta_soporte(p_ticket uuid, p_mensaje text, p_severidad text default 'aviso',
                                                  p_enviada boolean default false) returns uuid
language plpgsql security definer set search_path = public as $$
declare v_id uuid; v_t record;
begin
  select empresa_id, nivel, prioridad into v_t from tickets where id = p_ticket;
  if v_t is null or not public.puede_ver(v_t.empresa_id) then raise exception 'Sin permiso'; end if;
  if not (v_t.nivel = 'plataforma' or v_t.prioridad = 'urgente') then
    raise exception 'El ticket no requiere alerta a la plataforma';
  end if;
  insert into negocio.alertas (tipo, severidad, mensaje, enviada_en)
  values ('ticket', case when p_severidad in ('info','aviso','critica') then p_severidad else 'aviso' end,
          left(p_mensaje, 2000), case when p_enviada then now() end)
  returning id into v_id;
  return v_id;
end $$;
grant execute on function negocio.alerta_soporte(uuid, text, text, boolean) to authenticated;

-- ------------------------------------------------------ Prospectos --------
-- ¿Otro socio ya tiene ese prospecto con atribución vigente? Solo devuelve la
-- fecha de vencimiento (no quién lo tiene). Solo para socios activos.
create or replace function public.prospecto_ocupado(p_nombre text) returns date
language plpgsql stable security definer set search_path = public as $$
begin
  if public.mi_socio_id() is null and not public.es_admin_plataforma() then raise exception 'Solo socios'; end if;
  return (select max(vence_atribucion) from prospectos
           where public.sin_tildes(trim(empresa_nombre)) = public.sin_tildes(trim(p_nombre))
             and estado = 'abierto' and vence_atribucion >= current_date
             and socio_id is distinct from public.mi_socio_id());
end $$;
grant execute on function public.prospecto_ocupado(text) to authenticated;

-- ------------------------------------------------ Artículos de ayuda -------
insert into public.articulos_ayuda (titulo, categoria, palabras_clave, contenido)
select v.titulo, v.categoria, v.palabras, v.contenido
  from (values
  ('Cómo crear una planilla', 'planillas', array['planilla','nueva','crear','quincena','periodo','calcular'],
   'Vaya a Planillas y pulse «Nueva planilla». Elija el periodo (desde y hasta), el calendario de pago o la frecuencia (mensual, quincenal o semanal), la sucursal si aplica y la fecha de pago. La planilla queda en borrador. Antes de calcular registre las novedades del periodo: horas extra aprobadas, incapacidades, permisos, vacaciones, bonos y rebajos. Luego pulse «Calcular»: el sistema genera una boleta por cada empleado activo con CCSS, impuesto al salario, embargos, pensiones y préstamos. Puede recalcular cuantas veces necesite mientras la planilla no esté aprobada.'),
  ('Cómo aprobar y cerrar una planilla', 'planillas', array['aprobar','cerrar','reabrir','aprobador','devolver'],
   'Una planilla calculada la revisa un usuario con rol aprobador o administrador. En el detalle de la planilla revise totales y advertencias y pulse «Aprobar». Si encuentra un error use «Devolver» para que el operador la corrija y recalcule. Después del pago pulse «Cerrar»: se descuentan las cuotas de préstamos, se marcan las horas extra como pagadas, se rebaja el saldo de vacaciones y se genera el asiento contable. Si necesita corregir una planilla cerrada, un aprobador puede «Reabrir» indicando el motivo; queda registrado en la bitácora.'),
  ('Cómo enviar las boletas de pago por correo', 'boletas', array['boleta','boletas','enviar','correo','pdf','comprobante','colilla'],
   'Las boletas se envían cuando la planilla está aprobada o cerrada. En el detalle de la planilla pulse «Enviar boletas»: cada empleado recibe su boleta en PDF al correo registrado en su ficha. Los empleados sin correo aparecen como omitidos; complete el correo en la ficha y vuelva a enviar solo esas boletas. También puede descargar cada boleta en PDF o invitar al empleado al portal para que consulte sus boletas en línea.'),
  ('Cómo registrar una incapacidad', 'novedades', array['incapacidad','incapacidades','ccss','ins','maternidad','subsidio','boleta medica'],
   'En Novedades > Incapacidades pulse «Nueva». Elija el empleado, el tipo (CCSS por enfermedad, INS por riesgo del trabajo o licencia de maternidad), las fechas y el número de boleta. Para CCSS el patrono paga los primeros 3 días al 50 % y a partir del cuarto día subsidia la Caja según el porcentaje del certificado (60 % por defecto); marque «prórroga» si continúa una incapacidad anterior para no volver a cobrar los días del patrono. Las incapacidades del INS no se pagan en planilla porque el subsidio lo paga directamente el INS.'),
  ('Vacaciones: saldo, solicitudes y pago', 'vacaciones', array['vacaciones','saldo','dias','solicitud','disfrute','acumulacion'],
   'Cada empleado acumula vacaciones según el Art. 153 del Código de Trabajo (12 días hábiles por cada 50 semanas): el sistema acredita un día por cada mes cumplido desde la fecha de ingreso. Registre las vacaciones en Novedades > Vacaciones como disfrute (con fechas) o pago en dinero; un aprobador las autoriza y al cerrar la planilla se rebajan del saldo. Los empleados con acceso al portal pueden solicitar vacaciones desde su teléfono. El sistema avisa cuando hay saldos negativos o vacaciones cerca de prescribir.'),
  ('Cómo calcular el aguinaldo', 'aguinaldo', array['aguinaldo','decimotercer','diciembre','bono navideño'],
   'En Aguinaldo elija el año y pulse «Calcular». El sistema suma los salarios brutos de diciembre del año anterior a noviembre del año actual y divide entre doce. Revise el detalle por empleado; si lo desea cree la planilla de aguinaldo para aprobarla, cerrarla y enviar las boletas. El aguinaldo no lleva cargas de la CCSS ni impuesto al salario y debe pagarse dentro de los primeros veinte días de diciembre.'),
  ('Cómo hacer una liquidación laboral', 'liquidaciones', array['liquidacion','despido','renuncia','preaviso','cesantia','finiquito','prestaciones'],
   'En Liquidaciones pulse «Simular». Elija el empleado, la fecha de salida y el motivo (renuncia, despido con o sin responsabilidad patronal, mutuo acuerdo, pensión, fallecimiento o fin de contrato). El sistema calcula el salario promedio de los últimos seis meses, preaviso, cesantía, vacaciones pendientes y aguinaldo proporcional, y puede rebajar el saldo de préstamos. Si el resultado es correcto, guárdela y un aprobador la aprueba. Puede descargar el PDF de la liquidación y la constancia para el empleado.'),
  ('Cómo agregar usuarios a la empresa', 'usuarios', array['usuario','usuarios','invitar','rol','permiso','acceso','operador','aprobador'],
   'Solo un administrador de la empresa puede agregar usuarios. Vaya a Configuración > Usuarios y pulse «Agregar». Escriba el correo y elija el rol: administrador (todo), aprobador (aprueba planillas, cambios salariales y vacaciones), operador (registra datos y calcula) o lectura (solo consulta). Si la persona aún no tiene cuenta recibirá una invitación por correo. Puede cambiar el rol o desactivar el acceso en cualquier momento.'),
  ('Cómo asignar un contador a la empresa', 'contadores', array['contador','asignar','soporte','socio','codigo','supervision'],
   'Su contador puede supervisar las planillas y darle soporte de primer nivel. En Configuración > Contadores pulse «Asignar» y escriba el código de socio que le dio su contador. Elija sus funciones (supervisión, soporte) y el permiso: lectura (solo consulta) u operación (puede registrar y calcular, pero no aprobar). Los tickets de soporte llegan primero a su contador y, si él no puede resolverlos, los escala a A.U.R.O.M. Puede revocar el acceso cuando quiera.'),
  ('Cómo registrar o cambiar la tarjeta de pago', 'facturacion', array['tarjeta','pago','suscripcion','cobro','tilopay','factura','plan'],
   'En Configuración > Suscripción pulse «Registrar tarjeta». Se abre la página segura de Tilopay donde ingresa los datos de la tarjeta; Planilla Costa Rica nunca ve ni guarda el número completo. El cobro mensual se hace automáticamente según la cantidad de empleados activos y recibirá la factura electrónica por correo. Si un cobro es rechazado tendrá unos días de gracia para actualizar la tarjeta antes de que la cuenta pase a solo lectura.'),
  ('Portal del empleado', 'portal', array['portal','empleado','acceso','boletas','consultar','invitar'],
   'El administrador puede invitar a cada empleado al portal desde su ficha con «Invitar al portal»; el empleado recibe un correo para crear su contraseña. En el portal el empleado ve su ficha, descarga sus boletas de planillas cerradas, consulta su saldo de vacaciones y envía solicitudes de vacaciones para que las apruebe la empresa.'),
  ('Avisos por Telegram', 'telegram', array['telegram','avisos','notificaciones','bot','vincular','recordatorios'],
   'En su perfil pulse «Vincular Telegram» y abra el enlace: el bot de Planilla Costa Rica quedará conectado a su cuenta. Recibirá avisos de boletas disponibles y recordatorios de fechas (SICERE de la CCSS, planilla del INS, aguinaldo). En el bot puede escribir /ayuda seguido de su pregunta, /ticket para abrir una solicitud de soporte y /estado para ver la última planilla de sus empresas.')
  ) as v(titulo, categoria, palabras, contenido)
 where not exists (select 1 from public.articulos_ayuda a where a.titulo = v.titulo);


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009000900_negocio.sql
-- =============================================================================
-- Planilla Costa Rica · 0009 Negocio: consecutivos de comprobantes, columnas para
-- cobros/comisiones, sincronía de estado del servicio y configuración extra.
-- (Fase 7 y cobros de la Fase 8). Se aplica después de 0004/0005.
-- =============================================================================

-- ------------------------------------------------ Consecutivos por tipo ----
-- Hacienda exige numeración consecutiva por sucursal, terminal y tipo de
-- comprobante. Se usa una tabla (no una secuencia) para que no queden huecos:
-- el número se toma dentro de la misma transacción que firma el comprobante.
create table if not exists negocio.consecutivos (
  sucursal char(3) not null,
  terminal char(5) not null,
  tipo char(2) not null,
  ultimo bigint not null default 0 check (ultimo >= 0 and ultimo < 10000000000),
  primary key (sucursal, terminal, tipo)
);

create or replace function negocio.siguiente_consecutivo(p_sucursal text, p_terminal text, p_tipo text)
returns bigint language sql as $$
  insert into negocio.consecutivos (sucursal, terminal, tipo, ultimo)
  values (p_sucursal, p_terminal, p_tipo, 1)
  on conflict (sucursal, terminal, tipo) do update set ultimo = negocio.consecutivos.ultimo + 1
  returning ultimo
$$;
grant execute on function negocio.siguiente_consecutivo(text, text, text) to authenticated;

-- ---------------------------------------------------- Columnas nuevas ------
alter table negocio.clientes
  add column if not exists gracia_desde date,          -- primer día sin pago (vencimiento más antiguo)
  add column if not exists notas text;

alter table negocio.comprobantes
  add column if not exists concepto text not null default 'suscripcion'
    check (concepto in ('suscripcion','implementacion','otro')),
  add column if not exists situacion char(1) not null default '1',   -- 1 normal, 2 contingencia, 3 sin internet
  add column if not exists codigo_seguridad char(8),
  add column if not exists empleados_facturados int,
  add column if not exists xml_respuesta text,                        -- MensajeHacienda (decodificado)
  add column if not exists enviado_hacienda_en timestamptz,
  add column if not exists intentos_envio int not null default 0;
create unique index if not exists comprobante_suscripcion_unica
  on negocio.comprobantes (cliente_id, periodo)
  where tipo in ('01','04') and concepto = 'suscripcion' and estado <> 'anulado';
create index if not exists comprobantes_estado on negocio.comprobantes (estado, fecha);

alter table negocio.comprobante_lineas
  add column if not exists codigo_tarifa_iva char(2) not null default '08';  -- 08 = tarifa general 13 %

alter table negocio.pagos
  add column if not exists concepto text not null default 'mensualidad'
    check (concepto in ('mensualidad','implementacion','otro')),
  add column if not exists periodo date,                -- mes que paga (primer día) si es mensualidad
  add column if not exists subtotal numeric(14,2),      -- monto sin IVA (base de comisiones)
  add column if not exists moneda text not null default 'CRC',
  add column if not exists tipo_cambio numeric(12,5) not null default 1,
  add column if not exists cobro_id uuid references negocio.cobros(id);
create unique index if not exists pago_referencia_unica on negocio.pagos (medio, referencia) where referencia is not null;
create index if not exists pagos_cliente on negocio.pagos (cliente_id, periodo);

alter table negocio.cobros
  add column if not exists moneda text not null default 'CRC',
  add column if not exists descripcion text,
  add column if not exists metodo_pago_id uuid references negocio.metodos_pago(id);
create index if not exists cobros_cliente on negocio.cobros (cliente_id, periodo);

alter table negocio.metodos_pago
  add column if not exists marca text,
  add column if not exists correo text;                 -- correo asociado al token en Tilopay
create unique index if not exists metodo_pago_activo_unico on negocio.metodos_pago (cliente_id) where estado = 'activo';

alter table negocio.gastos
  add column if not exists consecutivo_comprobante char(20),
  add column if not exists comprobante_tipo char(2);
create unique index if not exists gasto_clave_unica on negocio.gastos (clave_comprobante) where clave_comprobante is not null;
create index if not exists gastos_fecha on negocio.gastos (fecha);

alter table public.comisiones add column if not exists pago_id uuid;
create index if not exists comisiones_periodo on public.comisiones (periodo, socio_id);

-- RLS para las tablas nuevas del esquema negocio (solo la dueña).
alter table negocio.consecutivos enable row level security;
drop policy if exists solo_plataforma on negocio.consecutivos;
create policy solo_plataforma on negocio.consecutivos for all to authenticated
  using (public.es_admin_plataforma()) with check (public.es_admin_plataforma());
grant select, insert, update, delete on negocio.consecutivos to authenticated;
grant all on negocio.consecutivos to service_role;

-- ------------------------------ Estado del servicio sincronizado ----------
-- negocio.clientes.estado manda; public.empresas.estado_servicio lo refleja.
create or replace function negocio.tg_cliente_estado() returns trigger
language plpgsql security definer set search_path = public, negocio as $$
begin
  if new.empresa_id is not null and (tg_op = 'INSERT' or new.estado is distinct from old.estado
                                     or new.empresa_id is distinct from old.empresa_id) then
    update public.empresas set estado_servicio = new.estado
     where id = new.empresa_id and estado_servicio is distinct from new.estado;
  end if;
  return new;
end $$;
drop trigger if exists cliente_estado on negocio.clientes;
create trigger cliente_estado after insert or update of estado, empresa_id on negocio.clientes
  for each row execute function negocio.tg_cliente_estado();

-- ------------------------------------ Vista del cliente (autogestión) -----
-- El administrador de una empresa no puede leer negocio.metodos_pago (tiene el
-- token del proveedor). Esta función le muestra solo lo seguro.
create or replace function negocio.metodo_pago_empresa(p_empresa uuid)
returns table (id uuid, proveedor text, marca text, ultimos4 text, vence text, estado text, creado_en timestamptz)
language sql stable security definer set search_path = public, negocio as $$
  select m.id, m.proveedor, m.marca, m.ultimos4::text, m.vence, m.estado, m.creado_en
    from negocio.metodos_pago m join negocio.clientes c on c.id = m.cliente_id
   where c.empresa_id = p_empresa and m.estado = 'activo' and public.es_admin_empresa(p_empresa)
   order by m.creado_en desc limit 1
$$;
grant execute on function negocio.metodo_pago_empresa(uuid) to authenticated;

-- Alta de la ficha de facturación desde la autogestión (admin de la empresa).
-- Usa los datos de la empresa; no toca precios (quedan los de la tabla).
create or replace function negocio.alta_cliente_empresa(p_empresa uuid, p_correo text)
returns uuid language plpgsql security definer set search_path = public, negocio as $$
declare
  v_id uuid;
  v_emp record;
  v_ced text;
begin
  if not public.es_admin_empresa(p_empresa) then raise exception 'Sin permiso'; end if;
  select id into v_id from negocio.clientes where empresa_id = p_empresa;
  if v_id is not null then return v_id; end if;
  select * into v_emp from public.empresas where id = p_empresa;
  v_ced := regexp_replace(coalesce(v_emp.cedula_juridica, ''), '\D', '', 'g');
  if v_ced = '' then raise exception 'La empresa no tiene cédula registrada'; end if;
  insert into negocio.clientes (empresa_id, razon_social, tipo_identificacion, identificacion, correo_facturacion,
                                telefono, estado)
  values (p_empresa, v_emp.nombre,
          case when length(v_ced) = 10 and left(v_ced, 1) = '3' then '02'
               when length(v_ced) = 9 then '01'
               when length(v_ced) in (11, 12) then '03'
               else '02' end,
          v_ced, coalesce(nullif(v_emp.correo, ''), p_correo), v_emp.telefono,
          case when v_emp.estado_servicio in ('prueba','activo','gracia','solo_lectura','suspendido')
               then v_emp.estado_servicio else 'activo' end)
  returning id into v_id;
  return v_id;
end $$;
grant execute on function negocio.alta_cliente_empresa(uuid, text) to authenticated;

-- ------------------------------------------------ Configuración extra -----
insert into negocio.configuracion (clave, valor) values
  -- Precios de lista (cada cliente guarda los suyos en negocio.clientes).
  ('precios', '{"mensual": "55000", "empleados_incluidos": 50, "bloque_10": "11000", "implementacion": "100000",
                "fundador_usd": "100", "iva_pct": "13"}'::jsonb),
  -- Política de cobro: gracia 7 días con reintentos los días 1, 3 y 5; solo lectura; suspensión a los 30 días.
  ('cobro', '{"dias_gracia": 7, "reintentos": [1, 3, 5], "dias_suspension": 30}'::jsonb),
  -- Tipo de cambio de referencia del BCCR para comprobantes en dólares (actualizar antes de facturar en USD).
  ('tipo_cambio_usd', '{"valor": null, "fecha": null, "fuente": "BCCR tipo de cambio de referencia (venta)"}'::jsonb),
  -- Política de firma XAdES-EPES. Si digest es null se descarga el documento y se calcula su SHA-256.
  ('firma_politica', '{"url": "https://cdn.comprobanteselectronicos.go.cr/xml-schemas/Resoluci%C3%B3n_General_sobre_disposiciones_t%C3%A9cnicas_comprobantes_electr%C3%B3nicos_para_efectos_tributarios.pdf",
                       "digest": null, "algoritmo": "sha256"}'::jsonb),
  -- Cédula del proveedor de sistemas (A.U.R.O.M. inscrita en el RUT). Si es null se usa la del emisor.
  ('proveedor_sistemas', 'null'::jsonb),
  -- Reparto de la utilidad mensual. Tramos por utilidad del mes (colones); ajustar con la dueña.
  ('reparto', '{"tramos": [{"hasta": "1000000", "pct_salario": "50"}, {"hasta": "3000000", "pct_salario": "60"},
                           {"hasta": "6000000", "pct_salario": "70"}, {"hasta": null, "pct_salario": "75"}],
                "tope_salario_neto": "4000000", "pct_caja_del_resto": "50", "pct_inversion_del_resto": "50"}'::jsonb),
  -- Renta y CCSS de la dueña como trabajadora independiente: se deben confirmar con el contador.
  ('renta', '{"tipo_contribuyente": "persona_fisica_actividad_lucrativa", "tramos": null,
              "ccss_independiente_pct": null, "nota": "Tramos y porcentaje CCSS pendientes de confirmar con el contador"}'::jsonb)
on conflict (clave) do nothing;


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261009001000_endurecimiento.sql
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


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261010001100_seguridad_avanzada.sql
-- =============================================================================
-- Planilla Costa Rica · 0011 Seguridad avanzada
--   1. Mínimo privilegio: el rol anónimo no toca nada.
--   2. Auditoría a prueba de alteraciones (cadena de hashes SHA-256, solo agregar).
--   3. MFA (aal2) obligatoria en la base para aprobar, cerrar y reabrir planillas
--      y autorizar salarios, cuando la API la exige.
--   4. Idempotencia de operaciones (la API no repite un cierre o un envío).
-- =============================================================================

-- 1. ------------------------------------------------------- Mínimo privilegio
revoke all on all tables in schema public from anon;
revoke all on all sequences in schema public from anon;
revoke all on all functions in schema public from anon;
revoke all on schema negocio from anon;
revoke all on all tables in schema negocio from anon;
revoke all on all functions in schema negocio from anon;
alter default privileges in schema public revoke all on tables from anon;
alter default privileges in schema public revoke all on functions from anon;
alter default privileges in schema negocio revoke all on tables from anon;

-- 2. ------------------------------------------- Auditoría encadenada (append-only)
alter table public.auditoria add column if not exists hash_anterior text;
alter table public.auditoria add column if not exists hash text;

create or replace function public.tg_auditoria_cadena() returns trigger
language plpgsql security definer set search_path = public, extensions as $$
declare v_prev text;
begin
  -- Un solo escritor a la vez para que la cadena quede en orden.
  perform pg_advisory_xact_lock(hashtext('aurom.auditoria'));
  select hash into v_prev from auditoria order by id desc limit 1;
  new.fecha := clock_timestamp();
  new.hash_anterior := coalesce(v_prev, 'GENESIS');
  new.hash := encode(digest(
      new.hash_anterior || '|' || coalesce(new.empresa_id::text, '') || '|' || new.tabla || '|' ||
      coalesce(new.registro_id, '') || '|' || new.accion || '|' || coalesce(new.antes::text, '') || '|' ||
      coalesce(new.despues::text, '') || '|' || coalesce(new.usuario_id::text, '') || '|' || to_char(new.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
      'sha256'), 'hex');
  return new;
end $$;
drop trigger if exists auditoria_cadena on public.auditoria;
create trigger auditoria_cadena before insert on public.auditoria
  for each row execute function public.tg_auditoria_cadena();

create or replace function public.tg_auditoria_inmutable() returns trigger
language plpgsql as $$
begin
  raise exception 'La bitácora de auditoría no se puede modificar ni borrar';
end $$;
drop trigger if exists auditoria_inmutable on public.auditoria;
create trigger auditoria_inmutable before update or delete on public.auditoria
  for each row execute function public.tg_auditoria_inmutable();
revoke truncate on public.auditoria from authenticated, anon;

-- Verifica la cadena completa: devuelve el primer id alterado (o null si está íntegra).
create or replace function public.verificar_auditoria()
returns table (id_alterado bigint, revisados bigint)
language plpgsql stable security definer set search_path = public, extensions as $$
declare r record; v_prev text := 'GENESIS'; n bigint := 0; v_calc text;
begin
  if not public.es_admin_plataforma() and current_user = 'authenticated' then
    raise exception 'Solo la plataforma verifica la bitácora completa';
  end if;
  for r in select * from auditoria where hash is not null order by id loop
    n := n + 1;
    v_calc := encode(digest(
        r.hash_anterior || '|' || coalesce(r.empresa_id::text, '') || '|' || r.tabla || '|' ||
        coalesce(r.registro_id, '') || '|' || r.accion || '|' || coalesce(r.antes::text, '') || '|' ||
        coalesce(r.despues::text, '') || '|' || coalesce(r.usuario_id::text, '') || '|' || to_char(r.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
        'sha256'), 'hex');
    if r.hash_anterior is distinct from v_prev or r.hash is distinct from v_calc then
      id_alterado := r.id; revisados := n; return next; return;
    end if;
    v_prev := r.hash;
  end loop;
  id_alterado := null; revisados := n; return next;
end $$;
revoke all on function public.verificar_auditoria() from public;
grant execute on function public.verificar_auditoria() to authenticated;

-- 3. -------------------------------------------------------------------- MFA
-- La API fija aurom.mfa_requerida = 'si' en producción. Con esa marca, las acciones
-- críticas exigen que la sesión tenga segundo factor verificado (claim aal = aal2).
create or replace function public.mfa_ok() returns boolean
language sql stable as $$
  select coalesce(current_setting('aurom.mfa_requerida', true), '') <> 'si'
      or coalesce(auth.jwt() ->> 'aal', 'aal1') = 'aal2'
$$;

create or replace function public.tg_planilla_mfa() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and new.estado is distinct from old.estado
     and (new.estado in ('aprobada','cerrada') or old.estado = 'cerrada') and not public.mfa_ok() then
    raise exception 'Esta acción requiere verificación en dos pasos (MFA)';
  end if;
  return new;
end $$;
create trigger planilla_mfa before update on public.planillas
  for each row execute function public.tg_planilla_mfa();

create or replace function public.tg_historial_mfa() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and new.estado is distinct from old.estado and new.estado = 'autorizado'
     and not public.mfa_ok() then
    raise exception 'Autorizar salarios requiere verificación en dos pasos (MFA)';
  end if;
  return new;
end $$;
create trigger historial_mfa before update on public.historial_salarial
  for each row execute function public.tg_historial_mfa();

-- 4. ------------------------------------------------------------- Idempotencia
create table if not exists public.idempotencia (
  usuario_id uuid not null,
  clave text not null check (length(clave) between 8 and 128),
  metodo text not null,
  ruta text not null,
  huella text not null,                -- sha256 del cuerpo: la misma clave con otro cuerpo es un error
  estado text not null default 'en_proceso' check (estado in ('en_proceso','listo')),
  codigo_http int,
  respuesta jsonb,
  creado_en timestamptz not null default now(),
  primary key (usuario_id, clave)
);
create index if not exists idempotencia_vieja on public.idempotencia (creado_en);
alter table public.idempotencia enable row level security;
-- Solo la API (conexión de sistema) la usa; ningún usuario la ve directamente.
revoke all on public.idempotencia from anon, authenticated;

-- 5. ---------------------------------------- Ejecución de funciones por rol
-- Por defecto Postgres da EXECUTE a PUBLIC (incluye anon). Se quita y se concede
-- solo a usuarios autenticados y al rol de servicio.
revoke execute on all functions in schema public from public;
revoke execute on all functions in schema negocio from public;
grant execute on all functions in schema public to authenticated, service_role;
grant execute on all functions in schema negocio to authenticated, service_role;
-- Funciones internas que nadie debe llamar directamente.
revoke execute on function public.crear_cuentas_por_defecto(uuid) from authenticated;
revoke execute on function public._politicas_empresa(regclass) from authenticated;
alter default privileges in schema public revoke execute on functions from public;
alter default privileges in schema negocio revoke execute on functions from public;


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261010001200_seguridad_revision2.sql
-- =============================================================================
-- Planilla Costa Rica · 0012 Segunda revisión independiente de seguridad
-- =============================================================================

-- 1. ---------------------------------------- Nada se escribe desde PostgREST
-- Dentro de una función SECURITY DEFINER el usuario efectivo es el dueño, así que la
-- regla "solo desde la API" se apoya también en session_user: PostgREST siempre se
-- conecta como 'authenticator'. Cualquier escritura que llegue por esa vía (aunque sea
-- a través de una función) se rechaza si no viene marcada por la API.
create or replace function public.tg_solo_api() returns trigger
language plpgsql as $$
begin
  if (current_user = 'authenticated' or session_user = 'authenticator') and not public.via_api() then
    raise exception 'Las modificaciones se hacen desde la aplicación Planilla Costa Rica';
  end if;
  return coalesce(new, old);
end $$;

-- 2. ------------------------------------------------ MFA también fuera de la API
create table if not exists public.ajustes_seguridad (
  clave text primary key,
  valor text not null
);
insert into public.ajustes_seguridad (clave, valor) values ('mfa_requerida', 'no') on conflict do nothing;
alter table public.ajustes_seguridad enable row level security;
revoke all on public.ajustes_seguridad from anon, authenticated;

-- MFA exigida si la API lo marca en la transacción o si está activada para toda la base
-- (la API la activa al arrancar en producción).
create or replace function public.mfa_ok() returns boolean
language sql stable security definer set search_path = public as $$
  select not (coalesce(current_setting('aurom.mfa_requerida', true), '') = 'si'
              or exists (select 1 from ajustes_seguridad where clave = 'mfa_requerida' and valor = 'si'))
      or coalesce(auth.jwt() ->> 'aal', 'aal1') = 'aal2'
      or (auth.uid() is null and session_user <> 'authenticator')   -- tareas del sistema, sin usuario
$$;

-- Cambios salariales autorizados: al insertar y al actualizar.
create or replace function public.tg_historial_mfa() returns trigger
language plpgsql as $$
begin
  if (current_user = 'authenticated' or session_user = 'authenticator') and new.estado = 'autorizado'
     and (tg_op = 'INSERT' or new.estado is distinct from old.estado) and not public.mfa_ok() then
    raise exception 'Autorizar salarios requiere verificación en dos pasos (MFA)';
  end if;
  return new;
end $$;
drop trigger if exists historial_mfa on public.historial_salarial;
create trigger historial_mfa before insert or update on public.historial_salarial
  for each row execute function public.tg_historial_mfa();

create or replace function public.tg_empleado_salario() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' and new.salario_mensual is distinct from old.salario_mensual then
    if not public.puede_aprobar(new.empresa_id) then
      raise exception 'El salario se cambia con una solicitud de cambio salarial autorizada';
    end if;
    if not public.mfa_ok() then
      raise exception 'Cambiar salarios requiere verificación en dos pasos (MFA)';
    end if;
  end if;
  if new.empresa_id is distinct from old.empresa_id then
    raise exception 'Un empleado no se puede mover de empresa';
  end if;
  return new;
end $$;

create or replace function public.tg_planilla_mfa() returns trigger
language plpgsql as $$
begin
  if (current_user = 'authenticated' or session_user = 'authenticator') and new.estado is distinct from old.estado
     and (new.estado in ('aprobada','cerrada') or old.estado = 'cerrada') and not public.mfa_ok() then
    raise exception 'Esta acción requiere verificación en dos pasos (MFA)';
  end if;
  return new;
end $$;

-- Agregar usuarios a una empresa es una acción de administración: exige MFA.
create or replace function public.agregar_usuario_empresa(p_empresa uuid, p_correo text, p_rol text)
returns uuid
language plpgsql security definer set search_path = public as $$
declare v_usuario uuid; v_id uuid;
begin
  if not public.es_admin_empresa(p_empresa) then raise exception 'Sin permiso'; end if;
  if not public.mfa_ok() then raise exception 'Esta acción requiere verificación en dos pasos (MFA)'; end if;
  select id into v_usuario from auth.users
   where lower(email) = lower(trim(p_correo)) and email_confirmed_at is not null;
  if v_usuario is null then return null; end if;
  insert into usuarios_empresa (empresa_id, usuario_id, rol, creado_por)
  values (p_empresa, v_usuario, p_rol, auth.uid())
  on conflict (empresa_id, usuario_id) do update set rol = excluded.rol, activo = true
  returning id into v_id;
  return v_id;
end $$;

-- 3. --------------------------------------------- Auditoría: orden sin carreras
-- El id se asigna antes del bloqueo; el orden real de la cadena es `seq`, que se
-- toma dentro del trigger con el bloqueo ya adquirido.
create sequence if not exists public.auditoria_seq;
alter table public.auditoria add column if not exists seq bigint;
create unique index if not exists auditoria_seq_unico on public.auditoria (seq);

create or replace function public.tg_auditoria_cadena() returns trigger
language plpgsql security definer set search_path = public, extensions as $$
declare v_prev text;
begin
  perform pg_advisory_xact_lock(hashtext('aurom.auditoria'));
  select hash into v_prev from auditoria where seq is not null order by seq desc limit 1;
  new.seq := nextval('public.auditoria_seq');
  new.fecha := clock_timestamp();
  new.hash_anterior := coalesce(v_prev, 'GENESIS');
  new.hash := encode(digest(
      new.seq::text || '|' || new.hash_anterior || '|' || coalesce(new.empresa_id::text, '') || '|' || new.tabla || '|' ||
      coalesce(new.registro_id, '') || '|' || new.accion || '|' || coalesce(new.antes::text, '') || '|' ||
      coalesce(new.despues::text, '') || '|' || coalesce(new.usuario_id::text, '') || '|' ||
      to_char(new.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), 'sha256'), 'hex');
  return new;
end $$;

drop function if exists public.verificar_auditoria();
create or replace function public.verificar_auditoria()
returns table (id_alterado bigint, revisados bigint, hash_final text)
language plpgsql stable security definer set search_path = public, extensions as $$
declare r record; v_prev text := 'GENESIS'; n bigint := 0; v_calc text; v_seq bigint := 0;
begin
  if not public.es_admin_plataforma() and (session_user = 'authenticator' or auth.uid() is not null) then
    raise exception 'Solo la plataforma verifica la bitácora completa';
  end if;
  for r in select * from auditoria where seq is not null order by seq loop
    n := n + 1;
    v_calc := encode(digest(
        r.seq::text || '|' || r.hash_anterior || '|' || coalesce(r.empresa_id::text, '') || '|' || r.tabla || '|' ||
        coalesce(r.registro_id, '') || '|' || r.accion || '|' || coalesce(r.antes::text, '') || '|' ||
        coalesce(r.despues::text, '') || '|' || coalesce(r.usuario_id::text, '') || '|' ||
        to_char(r.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), 'sha256'), 'hex');
    -- Un hueco en seq significa filas borradas.
    if r.hash_anterior is distinct from v_prev or r.hash is distinct from v_calc or r.seq <> v_seq + 1 and v_seq > 0 then
      id_alterado := r.id; revisados := n; hash_final := null; return next; return;
    end if;
    v_prev := r.hash; v_seq := r.seq;
  end loop;
  id_alterado := null; revisados := n; hash_final := v_prev; return next;
end $$;
revoke all on function public.verificar_auditoria() from public, anon, authenticated;
grant execute on function public.verificar_auditoria() to authenticated, service_role;

-- 4. ------------------------------------------------------------- TRUNCATE
-- TRUNCATE no pasa por RLS ni por triggers de fila: nadie de la aplicación lo necesita.
revoke truncate on all tables in schema public from authenticated, anon;
revoke truncate on all tables in schema negocio from authenticated, anon;


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261012001300_modulos_contabilidad.sql
-- =============================================================================
-- Planilla Costa Rica · 0013 Módulos por empresa y módulo de Contabilidad
--   1. Catálogo de módulos con precio de lista y activación por empresa (historial).
--   2. Esquema `contabilidad`: catálogo de cuentas, diarios, periodos, asientos y
--      líneas, con partida doble, numeración y periodos cerrados garantizados por
--      la base (no solo por la API).
-- =============================================================================

-- 1. ----------------------------------------------------------------- Módulos
create table public.modulos (
  codigo text primary key check (codigo ~ '^[a-z_]{3,30}$'),
  nombre text not null,
  descripcion text not null,
  orden int not null default 0,
  -- Precio de lista mensual sin IVA. Cada cliente puede tener el suyo (negocio.clientes.precios_modulos).
  precio_mensual numeric(14,2) not null,
  precio_mensual_usd numeric(14,2) not null,
  unidad text not null,                 -- lo que se mide para los bloques adicionales
  incluidos int not null,               -- unidades incluidas en el precio base
  tam_bloque int not null check (tam_bloque > 0),
  precio_bloque numeric(14,2) not null,
  precio_bloque_usd numeric(14,2) not null,
  disponible boolean not null default true
);
insert into public.modulos (codigo, nombre, descripcion, orden, precio_mensual, precio_mensual_usd, unidad, incluidos,
                            tam_bloque, precio_bloque, precio_bloque_usd) values
  ('planilla', 'Planilla', 'Planillas, CCSS, INS, renta, aguinaldo, vacaciones, liquidaciones, boletas y reportes oficiales.',
   1, 55000, 100, 'colaboradores', 50, 10, 11000, 20),
  ('contabilidad', 'Contabilidad', 'Catálogo de cuentas, asientos, libros diario y mayor, balance de comprobación, '
   'estados financieros y cierres. Recibe automáticamente los asientos de planilla.',
   2, 45000, 90, 'asientos', 300, 100, 7500, 15);
alter table public.modulos enable row level security;
create policy ver on public.modulos for select to authenticated using (true);
create policy gestionar on public.modulos for all to authenticated
  using (public.es_admin_plataforma()) with check (public.es_admin_plataforma());
grant select, insert, update on public.modulos to authenticated;

-- Precio propio por cliente y módulo: {"contabilidad": {"precio_mensual": "40000", "incluidos": 500, ...}}
alter table negocio.clientes add column if not exists precios_modulos jsonb not null default '{}'::jsonb;

-- Cada activación es una fila: desde / hasta. Activa = hasta es null.
-- Un módulo se cobra en el mes si estuvo activo algún día de ese mes.
create table public.empresa_modulos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  modulo text not null references public.modulos(codigo),
  desde date not null default (now() at time zone 'America/Costa_Rica')::date,
  hasta date,
  activado_por uuid default auth.uid(),
  desactivado_por uuid,
  creado_en timestamptz not null default now(),
  check (hasta is null or hasta >= desde)
);
create unique index empresa_modulo_activo on public.empresa_modulos (empresa_id, modulo) where hasta is null;
create index on public.empresa_modulos (empresa_id);
alter table public.empresa_modulos enable row level security;
create policy ver on public.empresa_modulos for select to authenticated using (public.puede_ver(empresa_id));
-- Nadie escribe directo: solo con public.cambiar_modulo().
grant select on public.empresa_modulos to authenticated;
create trigger solo_api before insert or update or delete on public.empresa_modulos
  for each row execute function public.tg_solo_api();
create trigger auditar after insert or update or delete on public.empresa_modulos
  for each row execute function public.tg_auditar();
create trigger solo_api before insert or update or delete on public.modulos
  for each row execute function public.tg_solo_api();

-- Las empresas que ya existen usan Planilla desde que se crearon.
insert into public.empresa_modulos (empresa_id, modulo, desde, activado_por)
select id, 'planilla', creado_en::date, creado_por from public.empresas
on conflict do nothing;

-- Una empresa creada por otra vía (importación, SQL de la plataforma) nace con Planilla.
-- crear_empresa() elige sus módulos y lo indica con la marca aurom.alta_modulos.
create or replace function public.tg_empresa_modulo_inicial() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if coalesce(current_setting('aurom.alta_modulos', true), '') <> 'si' then
    insert into empresa_modulos (empresa_id, modulo, desde, activado_por)
    values (new.id, 'planilla', coalesce(new.creado_en, now())::date, new.creado_por);
  end if;
  return new;
end $$;
create trigger empresa_modulo_inicial after insert on public.empresas
  for each row execute function public.tg_empresa_modulo_inicial();
revoke execute on function public.tg_empresa_modulo_inicial() from public, anon;

create or replace function public.modulo_activo(p_empresa uuid, p_modulo text) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from empresa_modulos where empresa_id = p_empresa and modulo = p_modulo and hasta is null)
$$;

create or replace function public.modulos_activos(p_empresa uuid) returns text[]
language sql stable security definer set search_path = public as $$
  select coalesce(array_agg(m.modulo order by o.orden), '{}')
    from empresa_modulos m join modulos o on o.codigo = m.modulo
   where m.empresa_id = p_empresa and m.hasta is null and public.puede_ver(p_empresa)
$$;

-- 2. ------------------------------------------------------------ Contabilidad
create schema if not exists contabilidad;
grant usage on schema contabilidad to authenticated, service_role;
revoke all on schema contabilidad from anon;

create table contabilidad.cuentas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  codigo text not null check (codigo ~ '^[0-9A-Za-z][0-9A-Za-z._-]{0,29}$'),
  nombre text not null check (length(trim(nombre)) between 2 and 150),
  tipo text not null check (tipo in ('activo','pasivo','patrimonio','ingreso','costo','gasto')),
  naturaleza text not null check (naturaleza in ('deudora','acreedora')),
  padre_id uuid,
  acepta_movimientos boolean not null default true,
  activa boolean not null default true,
  descripcion text check (length(descripcion) <= 500),
  creado_en timestamptz not null default now(),
  creado_por uuid default auth.uid(),
  unique (empresa_id, codigo),
  unique (empresa_id, id),
  foreign key (empresa_id, padre_id) references contabilidad.cuentas(empresa_id, id)
);
create index on contabilidad.cuentas (empresa_id, padre_id);

create table contabilidad.diarios (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  codigo text not null check (codigo ~ '^[A-Z]{2,5}$'),
  nombre text not null,
  unique (empresa_id, codigo),
  unique (empresa_id, id)
);

-- Un mes sin fila está abierto.
create table contabilidad.periodos (
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  anio int not null check (anio between 2000 and 2100),
  mes int not null check (mes between 1 and 12),
  estado text not null default 'abierto' check (estado in ('abierto','cerrado')),
  cerrado_por uuid,
  cerrado_en timestamptz,
  motivo_reapertura text,
  reabierto_por uuid,
  reabierto_en timestamptz,
  primary key (empresa_id, anio, mes)
);

create table contabilidad.asientos (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  diario_id uuid not null,
  numero text,                                  -- GEN-2026-000001: se asigna al contabilizar
  fecha date not null,
  descripcion text not null check (length(trim(descripcion)) between 2 and 300),
  referencia text check (length(referencia) <= 100),
  origen text not null default 'manual'
    check (origen in ('manual','apertura','planilla','aguinaldo','liquidacion','reversion','cierre')),
  origen_id uuid,
  estado text not null default 'borrador' check (estado in ('borrador','contabilizado','revertido')),
  revierte_a uuid,                              -- en una reversión: el asiento que anula
  revertido_por uuid,                           -- en el original: su reversión
  total_debito numeric(16,2) not null default 0,
  total_credito numeric(16,2) not null default 0,
  advertencia text,
  creado_en timestamptz not null default now(),
  creado_por uuid default auth.uid(),
  contabilizado_en timestamptz,
  contabilizado_por uuid,
  unique (empresa_id, numero),
  unique (empresa_id, id),
  foreign key (empresa_id, diario_id) references contabilidad.diarios(empresa_id, id),
  foreign key (empresa_id, revierte_a) references contabilidad.asientos(empresa_id, id),
  foreign key (empresa_id, revertido_por) references contabilidad.asientos(empresa_id, id),
  check (estado = 'borrador' or (numero is not null and total_debito = total_credito and total_debito > 0))
);
create index on contabilidad.asientos (empresa_id, fecha);
create index on contabilidad.asientos (empresa_id, origen, origen_id);
create unique index asiento_una_reversion on contabilidad.asientos (revierte_a) where revierte_a is not null;

create table contabilidad.lineas (
  id uuid primary key default gen_random_uuid(),
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  asiento_id uuid not null,
  cuenta_id uuid not null,
  descripcion text check (length(descripcion) <= 200),
  debito numeric(16,2) not null default 0 check (debito >= 0),
  credito numeric(16,2) not null default 0 check (credito >= 0),
  orden int not null default 0,
  check ((debito > 0) <> (credito > 0)),
  foreign key (empresa_id, asiento_id) references contabilidad.asientos(empresa_id, id) on delete cascade,
  foreign key (empresa_id, cuenta_id) references contabilidad.cuentas(empresa_id, id)
);
create index on contabilidad.lineas (asiento_id);
create index on contabilidad.lineas (empresa_id, cuenta_id);

create table contabilidad.consecutivos (
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  diario_id uuid not null,
  anio int not null,
  ultimo int not null default 0,
  primary key (empresa_id, diario_id, anio)
);

create table contabilidad.cierres_anuales (
  empresa_id uuid not null references public.empresas(id) on delete cascade,
  anio int not null,
  asiento_id uuid not null,
  cerrado_por uuid default auth.uid(),
  cerrado_en timestamptz not null default now(),
  primary key (empresa_id, anio),
  foreign key (empresa_id, asiento_id) references contabilidad.asientos(empresa_id, id)
);

-- ---------------------------------------------------------------- Permisos
-- Igual que el resto: ver con acceso a la empresa; escribir si puede operar, la empresa
-- está al día y el módulo está activo.
create or replace function contabilidad._politicas(p_tabla regclass) returns void
language plpgsql as $$
begin
  execute format('alter table %s enable row level security', p_tabla);
  execute format('create policy ver on %s for select to authenticated using (public.puede_ver(empresa_id))', p_tabla);
  execute format('create policy crear on %s for insert to authenticated with check (public.puede_operar(empresa_id) '
                 'and public.empresa_escribible(empresa_id) and public.modulo_activo(empresa_id, ''contabilidad''))', p_tabla);
  execute format('create policy editar on %s for update to authenticated using (public.puede_operar(empresa_id)) '
                 'with check (public.puede_operar(empresa_id) and public.empresa_escribible(empresa_id) '
                 'and public.modulo_activo(empresa_id, ''contabilidad''))', p_tabla);
  execute format('create policy borrar on %s for delete to authenticated using (public.puede_operar(empresa_id) '
                 'and public.empresa_escribible(empresa_id) and public.modulo_activo(empresa_id, ''contabilidad''))', p_tabla);
  execute format('grant select, insert, update, delete on %s to authenticated', p_tabla);
  execute format('create trigger solo_api before insert or update or delete on %s '
                 'for each row execute function public.tg_solo_api()', p_tabla);
end $$;
select contabilidad._politicas(t::regclass) from unnest(array[
  'contabilidad.cuentas', 'contabilidad.diarios', 'contabilidad.periodos', 'contabilidad.asientos',
  'contabilidad.lineas', 'contabilidad.cierres_anuales']) t;
-- Los consecutivos solo los toca el trigger de contabilizar.
alter table contabilidad.consecutivos enable row level security;
revoke all on contabilidad.consecutivos from authenticated, anon;
revoke truncate on all tables in schema contabilidad from authenticated, anon;
revoke execute on function contabilidad._politicas(regclass) from public, authenticated;

-- Bitácora con el nombre de esquema (hay "asientos" de planilla y de contabilidad).
create or replace function public.tg_auditar_esquema() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_row jsonb := to_jsonb(coalesce(new, old));
begin
  insert into auditoria (empresa_id, tabla, registro_id, accion, antes, despues, usuario_id)
  values (nullif(v_row ->> 'empresa_id', '')::uuid, tg_table_schema || '.' || tg_table_name,
          coalesce(v_row ->> 'id', (v_row ->> 'anio') || '-' || coalesce(v_row ->> 'mes', '')), tg_op,
          case when tg_op <> 'INSERT' then to_jsonb(old) end,
          case when tg_op <> 'DELETE' then to_jsonb(new) end,
          auth.uid());
  return coalesce(new, old);
end $$;
create trigger auditar after insert or update or delete on contabilidad.cuentas
  for each row execute function public.tg_auditar_esquema();
create trigger auditar after insert or update or delete on contabilidad.asientos
  for each row execute function public.tg_auditar_esquema();
create trigger auditar after insert or update or delete on contabilidad.periodos
  for each row execute function public.tg_auditar_esquema();
create trigger auditar after insert or update or delete on contabilidad.cierres_anuales
  for each row execute function public.tg_auditar_esquema();

-- ------------------------------------------------------------- Integridad
create or replace function contabilidad.periodo_abierto(p_empresa uuid, p_fecha date) returns boolean
language sql stable security definer set search_path = contabilidad, public as $$
  select not exists (select 1 from periodos where empresa_id = p_empresa and estado = 'cerrado'
                       and anio = extract(year from p_fecha)::int and mes = extract(month from p_fecha)::int)
$$;

-- Cuentas: jerarquía sin ciclos, mismo tipo que el grupo, y nada que rompa los saldos.
create or replace function contabilidad.tg_cuenta() returns trigger
language plpgsql security definer set search_path = contabilidad, public as $$
declare v_padre record; v_tiene boolean; v_cursor uuid; n int := 0;
begin
  if tg_op = 'UPDATE' and new.empresa_id is distinct from old.empresa_id then
    raise exception 'Una cuenta no se puede mover de empresa';
  end if;
  v_tiene := tg_op = 'UPDATE' and exists (select 1 from lineas where cuenta_id = old.id);
  if v_tiene and (new.tipo is distinct from old.tipo or new.naturaleza is distinct from old.naturaleza) then
    raise exception 'La cuenta % ya tiene movimientos: no se puede cambiar su tipo ni su naturaleza', old.codigo;
  end if;
  if v_tiene and old.acepta_movimientos and not new.acepta_movimientos then
    raise exception 'La cuenta % ya tiene movimientos: no se puede convertir en cuenta de agrupación', old.codigo;
  end if;
  if new.acepta_movimientos and tg_op = 'UPDATE' and not old.acepta_movimientos
     and exists (select 1 from cuentas where padre_id = new.id) then
    raise exception 'La cuenta % tiene subcuentas: no puede recibir movimientos', new.codigo;
  end if;
  if new.padre_id is not null then
    select * into v_padre from cuentas where id = new.padre_id and empresa_id = new.empresa_id;
    if not found then raise exception 'La cuenta de agrupación no existe'; end if;
    if v_padre.tipo <> new.tipo then
      raise exception 'La subcuenta debe ser del mismo tipo que su grupo % (%)', v_padre.codigo, v_padre.tipo;
    end if;
    -- Sin ciclos: subir por los padres no debe volver a esta cuenta.
    v_cursor := new.padre_id;
    while v_cursor is not null loop
      if v_cursor = new.id then raise exception 'La jerarquía de cuentas no puede formar un ciclo'; end if;
      n := n + 1;
      if n > 20 then raise exception 'La jerarquía de cuentas es demasiado profunda'; end if;
      select padre_id into v_cursor from cuentas where id = v_cursor;
    end loop;
    -- El grupo deja de recibir movimientos (si todavía no tiene ninguno).
    if v_padre.acepta_movimientos then
      if exists (select 1 from lineas where cuenta_id = v_padre.id) then
        raise exception 'La cuenta % ya tiene movimientos: no puede tener subcuentas', v_padre.codigo;
      end if;
      update cuentas set acepta_movimientos = false where id = v_padre.id;
    end if;
  end if;
  return new;
end $$;
create trigger cuenta_reglas before insert or update on contabilidad.cuentas
  for each row execute function contabilidad.tg_cuenta();

-- Asientos: borrador editable; contabilizado inmutable (se corrige con reversión).
create or replace function contabilidad.tg_asiento() returns trigger
language plpgsql security definer set search_path = contabilidad, public as $$
declare v_deb numeric; v_cre numeric; v_n int; v_malas text; v_diario text; v_num int;
begin
  if tg_op = 'INSERT' then
    if new.estado <> 'borrador' then raise exception 'Un asiento nace como borrador y luego se contabiliza'; end if;
    new.numero := null; new.revertido_por := null; new.contabilizado_en := null; new.contabilizado_por := null;
    return new;
  end if;
  if tg_op = 'DELETE' then
    -- Al borrar la empresa completa (derecho de eliminación) se va todo.
    if old.estado <> 'borrador' and exists (select 1 from public.empresas where id = old.empresa_id) then
      raise exception 'Un asiento contabilizado no se borra: haga una reversión';
    end if;
    return old;
  end if;
  -- UPDATE
  if new.empresa_id is distinct from old.empresa_id then raise exception 'Un asiento no se puede mover de empresa'; end if;
  if old.estado = 'revertido' then raise exception 'El asiento ya fue revertido'; end if;
  if old.estado = 'contabilizado' then
    if new.estado = 'revertido' and new.revertido_por is not null
       and (new.fecha, new.diario_id, new.descripcion, new.numero, new.total_debito, new.origen, new.origen_id)
           is not distinct from (old.fecha, old.diario_id, old.descripcion, old.numero, old.total_debito, old.origen, old.origen_id) then
      return new;
    end if;
    raise exception 'Un asiento contabilizado no se modifica: haga una reversión';
  end if;
  if new.estado = 'revertido' then raise exception 'Solo un asiento contabilizado se puede revertir'; end if;
  if new.estado = 'contabilizado' then
    if not contabilidad.periodo_abierto(new.empresa_id, new.fecha) then
      raise exception 'El periodo % de % está cerrado', extract(month from new.fecha)::int, extract(year from new.fecha)::int;
    end if;
    select coalesce(sum(debito), 0), coalesce(sum(credito), 0), count(*) into v_deb, v_cre, v_n
      from lineas where asiento_id = new.id;
    if v_n < 2 then raise exception 'El asiento necesita al menos dos líneas'; end if;
    if v_deb <> v_cre then raise exception 'El asiento no cuadra: débitos % y créditos %', v_deb, v_cre; end if;
    if v_deb = 0 then raise exception 'El asiento no tiene montos'; end if;
    select string_agg(distinct c.codigo, ', ') into v_malas
      from lineas l join cuentas c on c.id = l.cuenta_id
     where l.asiento_id = new.id and (not c.acepta_movimientos or not c.activa);
    if v_malas is not null then
      raise exception 'Estas cuentas no reciben movimientos (agrupación o inactivas): %', v_malas;
    end if;
    select codigo into v_diario from diarios where id = new.diario_id;
    insert into consecutivos (empresa_id, diario_id, anio, ultimo)
    values (new.empresa_id, new.diario_id, extract(year from new.fecha)::int, 1)
    on conflict (empresa_id, diario_id, anio) do update set ultimo = consecutivos.ultimo + 1
    returning ultimo into v_num;
    new.numero := v_diario || '-' || extract(year from new.fecha)::int || '-' || lpad(v_num::text, 6, '0');
    new.total_debito := v_deb; new.total_credito := v_cre;
    new.contabilizado_en := now(); new.contabilizado_por := auth.uid();
  else
    new.numero := null;
  end if;
  return new;
end $$;
create trigger asiento_reglas before insert or update or delete on contabilidad.asientos
  for each row execute function contabilidad.tg_asiento();

create or replace function contabilidad.tg_linea() returns trigger
language plpgsql security definer set search_path = contabilidad, public as $$
declare v_estado text;
begin
  select estado into v_estado from asientos where id = coalesce(new.asiento_id, old.asiento_id);
  if v_estado is not null and v_estado <> 'borrador'
     and exists (select 1 from public.empresas where id = coalesce(new.empresa_id, old.empresa_id)) then
    raise exception 'Las líneas de un asiento contabilizado no se modifican';
  end if;
  if tg_op = 'UPDATE' and (new.asiento_id is distinct from old.asiento_id or new.empresa_id is distinct from old.empresa_id) then
    raise exception 'Una línea no se mueve a otro asiento';
  end if;
  return coalesce(new, old);
end $$;
create trigger linea_reglas before insert or update or delete on contabilidad.lineas
  for each row execute function contabilidad.tg_linea();

-- Periodos: cerrar y reabrir es de aprobadores, con segundo factor.
create or replace function contabilidad.tg_periodo() returns trigger
language plpgsql as $$
begin
  if (current_user = 'authenticated' or session_user = 'authenticator') then
    if not public.puede_aprobar(new.empresa_id) then
      raise exception 'Solo un aprobador o administrador cierra y reabre periodos contables';
    end if;
    if not public.mfa_ok() then raise exception 'Cerrar o reabrir periodos requiere verificación en dos pasos (MFA)'; end if;
  end if;
  if tg_op = 'UPDATE' and (new.empresa_id, new.anio, new.mes) is distinct from (old.empresa_id, old.anio, old.mes) then
    raise exception 'El periodo no se puede cambiar de fecha';
  end if;
  if tg_op = 'UPDATE' and old.estado = 'cerrado' and new.estado = 'abierto'
     and exists (select 1 from contabilidad.cierres_anuales where empresa_id = new.empresa_id and anio = new.anio) then
    raise exception 'El año % tiene cierre anual: reabra primero el año', new.anio;
  end if;
  return new;
end $$;
create trigger periodo_reglas before insert or update on contabilidad.periodos
  for each row execute function contabilidad.tg_periodo();

-- ------------------------------------------------- Catálogo inicial (NIIF PYMES)
-- Códigos compatibles con las cuentas por defecto de planilla (5101..., 2101..., 1205, 1206).
create or replace function contabilidad.inicializar(p_empresa uuid) returns void
language plpgsql security definer set search_path = contabilidad, public as $$
begin
  if not public.es_admin_empresa(p_empresa) and auth.uid() is not null then
    raise exception 'Sin permiso';
  end if;
  insert into diarios (empresa_id, codigo, nombre) values
    (p_empresa, 'GEN', 'General'), (p_empresa, 'PLA', 'Planillas'), (p_empresa, 'VEN', 'Ventas'),
    (p_empresa, 'COM', 'Compras y gastos'), (p_empresa, 'BAN', 'Bancos'), (p_empresa, 'AJU', 'Ajustes'),
    (p_empresa, 'APE', 'Apertura'), (p_empresa, 'CIE', 'Cierre')
  on conflict (empresa_id, codigo) do nothing;
  if exists (select 1 from cuentas where empresa_id = p_empresa) then
    return;
  end if;
  -- (código, nombre, tipo, naturaleza, recibe movimientos)
  insert into cuentas (empresa_id, codigo, nombre, tipo, naturaleza, acepta_movimientos)
  select p_empresa, c.codigo, c.nombre, c.tipo, c.naturaleza, c.mov from (values
    ('1','ACTIVO','activo','deudora',false),
    ('11','Activo corriente','activo','deudora',false),
    ('1101','Caja general','activo','deudora',true),
    ('1102','Caja chica','activo','deudora',true),
    ('1103','Bancos','activo','deudora',true),
    ('12','Cuentas por cobrar','activo','deudora',false),
    ('1201','Cuentas por cobrar a clientes','activo','deudora',true),
    ('1202','Estimación para incobrables','activo','acreedora',true),
    ('1203','IVA soportado (crédito fiscal)','activo','deudora',true),
    ('1204','Pagos parciales de impuesto sobre la renta','activo','deudora',true),
    ('1205','Préstamos a empleados','activo','deudora',true),
    ('1206','Subsidios CCSS por cobrar','activo','deudora',true),
    ('1207','Otras cuentas por cobrar','activo','deudora',true),
    ('13','Inventarios','activo','deudora',false),
    ('1301','Inventario de mercadería','activo','deudora',true),
    ('14','Gastos pagados por anticipado','activo','deudora',false),
    ('1401','Seguros pagados por anticipado','activo','deudora',true),
    ('1402','Alquileres pagados por anticipado','activo','deudora',true),
    ('15','Propiedad, planta y equipo','activo','deudora',false),
    ('1501','Terrenos','activo','deudora',true),
    ('1502','Edificios','activo','deudora',true),
    ('1503','Mobiliario y equipo de oficina','activo','deudora',true),
    ('1504','Equipo de cómputo','activo','deudora',true),
    ('1505','Vehículos','activo','deudora',true),
    ('1509','Depreciación acumulada','activo','acreedora',true),
    ('16','Otros activos','activo','deudora',false),
    ('1601','Depósitos en garantía','activo','deudora',true),
    ('2','PASIVO','pasivo','acreedora',false),
    ('21','Pasivo corriente','pasivo','acreedora',false),
    ('2101','Salarios por pagar','pasivo','acreedora',true),
    ('2102','CCSS por pagar','pasivo','acreedora',true),
    ('2103','INS riesgos del trabajo por pagar','pasivo','acreedora',true),
    ('2104','Impuesto al salario retenido','pasivo','acreedora',true),
    ('2105','ROP por pagar','pasivo','acreedora',true),
    ('2106','Embargos y pensiones por pagar','pasivo','acreedora',true),
    ('2107','Otras retenciones a empleados','pasivo','acreedora',true),
    ('2111','Provisión aguinaldo','pasivo','acreedora',true),
    ('2112','Provisión vacaciones','pasivo','acreedora',true),
    ('2113','Provisión cesantía','pasivo','acreedora',true),
    ('2120','Cuentas por pagar a proveedores','pasivo','acreedora',true),
    ('2121','IVA por pagar (débito fiscal)','pasivo','acreedora',true),
    ('2122','Impuesto sobre la renta por pagar','pasivo','acreedora',true),
    ('2123','Retenciones por pagar','pasivo','acreedora',true),
    ('2124','Gastos acumulados por pagar','pasivo','acreedora',true),
    ('2125','Préstamos bancarios a corto plazo','pasivo','acreedora',true),
    ('2126','Anticipos de clientes','pasivo','acreedora',true),
    ('22','Pasivo no corriente','pasivo','acreedora',false),
    ('2201','Préstamos bancarios a largo plazo','pasivo','acreedora',true),
    ('3','PATRIMONIO','patrimonio','acreedora',false),
    ('31','Capital','patrimonio','acreedora',false),
    ('3101','Capital social','patrimonio','acreedora',true),
    ('32','Reservas','patrimonio','acreedora',false),
    ('3201','Reserva legal','patrimonio','acreedora',true),
    ('33','Resultados','patrimonio','acreedora',false),
    ('3301','Utilidades acumuladas','patrimonio','acreedora',true),
    ('4','INGRESOS','ingreso','acreedora',false),
    ('41','Ingresos de operación','ingreso','acreedora',false),
    ('4101','Ventas de mercadería','ingreso','acreedora',true),
    ('4102','Ventas de servicios','ingreso','acreedora',true),
    ('4103','Devoluciones y descuentos sobre ventas','ingreso','deudora',true),
    ('42','Otros ingresos','ingreso','acreedora',false),
    ('4201','Ingresos financieros','ingreso','acreedora',true),
    ('4202','Diferencial cambiario ganado','ingreso','acreedora',true),
    ('4203','Otros ingresos','ingreso','acreedora',true),
    ('5','GASTOS','gasto','deudora',false),
    ('51','Gastos de personal','gasto','deudora',false),
    ('5101','Sueldos y salarios','gasto','deudora',true),
    ('5102','Cargas sociales patronales','gasto','deudora',true),
    ('5103','Aguinaldo','gasto','deudora',true),
    ('5104','Vacaciones','gasto','deudora',true),
    ('5105','Cesantía','gasto','deudora',true),
    ('5106','Incapacidades a cargo del patrono','gasto','deudora',true),
    ('52','Gastos generales y administrativos','gasto','deudora',false),
    ('5201','Alquileres','gasto','deudora',true),
    ('5202','Electricidad y agua','gasto','deudora',true),
    ('5203','Teléfono e internet','gasto','deudora',true),
    ('5204','Honorarios profesionales','gasto','deudora',true),
    ('5205','Papelería y útiles','gasto','deudora',true),
    ('5206','Mantenimiento y reparaciones','gasto','deudora',true),
    ('5207','Seguros','gasto','deudora',true),
    ('5208','Publicidad y mercadeo','gasto','deudora',true),
    ('5209','Combustible y transporte','gasto','deudora',true),
    ('5210','Depreciación','gasto','deudora',true),
    ('5211','Patentes e impuestos municipales','gasto','deudora',true),
    ('5212','Software y suscripciones','gasto','deudora',true),
    ('5299','Otros gastos','gasto','deudora',true),
    ('53','Gastos financieros','gasto','deudora',false),
    ('5301','Intereses','gasto','deudora',true),
    ('5302','Comisiones bancarias','gasto','deudora',true),
    ('5303','Diferencial cambiario perdido','gasto','deudora',true),
    ('54','Impuesto sobre la renta','gasto','deudora',false),
    ('5401','Gasto por impuesto sobre la renta','gasto','deudora',true),
    ('6','COSTOS','costo','deudora',false),
    ('61','Costo de ventas','costo','deudora',false),
    ('6101','Costo de mercadería vendida','costo','deudora',true),
    ('6102','Costo de servicios prestados','costo','deudora',true)
  ) as c(codigo, nombre, tipo, naturaleza, mov);
  -- Padre = el grupo con el código más largo que sea prefijo.
  update cuentas c set padre_id = (
      select p.id from cuentas p
       where p.empresa_id = c.empresa_id and p.codigo <> c.codigo and not p.acepta_movimientos
         and c.codigo like p.codigo || '%'
       order by length(p.codigo) desc limit 1)
   where c.empresa_id = p_empresa;
end $$;
revoke execute on function contabilidad.inicializar(uuid) from public;

-- ---------------------------------------- Activar y desactivar módulos
-- Lo hace el administrador de la empresa (con MFA) o la plataforma. El historial queda
-- en empresa_modulos y en la bitácora. Siempre debe quedar al menos un módulo activo.
create or replace function public.cambiar_modulo(p_empresa uuid, p_modulo text, p_activo boolean) returns text[]
language plpgsql security definer set search_path = public as $$
declare v_hoy date := (now() at time zone 'America/Costa_Rica')::date;
begin
  if not public.es_admin_empresa(p_empresa) then raise exception 'Solo el administrador de la empresa cambia los módulos'; end if;
  if not public.mfa_ok() then raise exception 'Cambiar los módulos requiere verificación en dos pasos (MFA)'; end if;
  if not exists (select 1 from modulos where codigo = p_modulo and (disponible or not p_activo)) then
    raise exception 'Módulo no disponible: %', p_modulo;
  end if;
  if p_activo then
    if not public.modulo_activo(p_empresa, p_modulo) then
      if exists (select 1 from empresas where id = p_empresa and estado_servicio in ('solo_lectura','suspendido','baja'))
         and not public.es_admin_plataforma() then
        raise exception 'Regularice la suscripción antes de activar otro módulo';
      end if;
      insert into empresa_modulos (empresa_id, modulo, desde, activado_por) values (p_empresa, p_modulo, v_hoy, auth.uid());
    end if;
    if p_modulo = 'contabilidad' then
      perform contabilidad.inicializar(p_empresa);
    end if;
  else
    if not exists (select 1 from empresa_modulos where empresa_id = p_empresa and hasta is null and modulo <> p_modulo) then
      raise exception 'La empresa debe tener al menos un módulo activo';
    end if;
    update empresa_modulos set hasta = v_hoy, desactivado_por = auth.uid()
     where empresa_id = p_empresa and modulo = p_modulo and hasta is null;
  end if;
  return public.modulos_activos(p_empresa);
end $$;
revoke execute on function public.cambiar_modulo(uuid, text, boolean) from public, anon;
grant execute on function public.cambiar_modulo(uuid, text, boolean) to authenticated;

-- Alta de empresa eligiendo los módulos (por defecto, Planilla).
drop function if exists public.crear_empresa(text, text, text, text);
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
  insert into calendarios_pago (empresa_id, nombre, frecuencia)
  values (v_empresa, case p_frecuencia when 'monthly' then 'Mensual' when 'weekly' then 'Semanal' else 'Quincenal' end, p_frecuencia);
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

-- Planilla también se respeta en la base: sin el módulo no se crean planillas.
create or replace function public.tg_planilla_nueva() returns trigger
language plpgsql as $$
begin
  if current_user = 'authenticated' or session_user = 'authenticator' then
    if not public.modulo_activo(new.empresa_id, 'planilla') then
      raise exception 'El módulo Planilla no está activo para esta empresa';
    end if;
    new.estado := 'borrador'; new.aprobada_por := null; new.aprobada_en := null;
    new.cerrada_por := null; new.cerrada_en := null;
  end if;
  return new;
end $$;

-- Ejecución de funciones: igual que en public (0011), solo usuarios autenticados.
revoke execute on all functions in schema contabilidad from public, anon;
grant execute on function contabilidad.periodo_abierto(uuid, date) to authenticated, service_role;
grant execute on function contabilidad.tg_cuenta() to authenticated;
grant execute on function contabilidad.tg_asiento() to authenticated;
grant execute on function contabilidad.tg_linea() to authenticated;
grant execute on function contabilidad.tg_periodo() to authenticated;
grant execute on function contabilidad.inicializar(uuid) to authenticated;
revoke execute on function public.modulo_activo(uuid, text) from public, anon;
grant execute on function public.modulo_activo(uuid, text) to authenticated, service_role;
revoke execute on function public.modulos_activos(uuid) from public, anon;
grant execute on function public.modulos_activos(uuid) to authenticated, service_role;
revoke execute on function public.tg_auditar_esquema() from public, anon;
alter default privileges in schema contabilidad revoke execute on functions from public;

-- ------------------------------------------- Bitácora: secuencia sin huecos
-- Con nextval(), una transacción que se deshace (un error de validación cualquiera)
-- dejaba un hueco en `seq` y la verificación lo reportaba como filas borradas.
-- El número se toma ahora del último registro confirmado, con el bloqueo ya adquirido.
create or replace function public.tg_auditoria_cadena() returns trigger
language plpgsql security definer set search_path = public, extensions as $$
declare v_prev text; v_seq bigint;
begin
  perform pg_advisory_xact_lock(hashtext('aurom.auditoria'));
  select hash, seq into v_prev, v_seq from auditoria where seq is not null order by seq desc limit 1;
  new.seq := coalesce(v_seq, 0) + 1;
  new.fecha := clock_timestamp();
  new.hash_anterior := coalesce(v_prev, 'GENESIS');
  new.hash := encode(digest(
      new.seq::text || '|' || new.hash_anterior || '|' || coalesce(new.empresa_id::text, '') || '|' || new.tabla || '|' ||
      coalesce(new.registro_id, '') || '|' || new.accion || '|' || coalesce(new.antes::text, '') || '|' ||
      coalesce(new.despues::text, '') || '|' || coalesce(new.usuario_id::text, '') || '|' ||
      to_char(new.fecha at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), 'sha256'), 'hex');
  return new;
end $$;


-- >>>>>>>>>>>>>>>>>>>>>>>> 20261012001400_auditoria.sql
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



-- ===== 20261012001500_logo_empresa.sql =====
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


-- ===== 20261012001600_ficha_cliente_automatica.sql =====
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


-- ===== 20261012001700_registro_sin_frecuencia.sql =====
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


-- ===== 20261012001800_identificaciones_unicas.sql =====
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

-- ===== 20261012001900_configuracion_planilla_completa.sql =====
-- Opciones de planilla por empresa, equivalentes a la «Configuración Contable de Planilla CR» del módulo Odoo.

alter table public.configuracion_planilla
  -- Pago bancario: BAC Macro Pago Planilla Formato 6
  add column if not exists bac_plan text,
  add column if not exists bac_envio int not null default 1 check (bac_envio between 1 and 99999),
  add column if not exists bac_concepto text not null default 'Pago de Planilla',
  -- Asistencia y horas extra
  add column if not exists he_desde_asistencia boolean not null default false,
  add column if not exists tolerancia_tardia_min int not null default 15 check (tolerancia_tardia_min between 0 and 240),
  -- Asiento contable: uno por planilla (consolidado) o uno por boleta
  add column if not exists asiento_modo text not null default 'planilla' check (asiento_modo in ('planilla','empleado')),
  -- Correo de boletas ({periodo}, {empresa}, {colaborador})
  add column if not exists correo_asunto text,
  add column if not exists correo_cuerpo text,
  add column if not exists correo_firma text,
  -- Firmante de constancias laborales
  add column if not exists firmante_nombre text,
  add column if not exists firmante_cargo text,
  add column if not exists firmante_telefono text,
  add column if not exists firmante_correo text,
  add column if not exists firmante_lugar text,
  -- Boleta en PDF
  add column if not exists boleta_saldo_vacaciones boolean not null default false,
  add column if not exists boleta_detalle_subsidio boolean not null default false,
  -- Liquidaciones
  add column if not exists liquidacion_sin_ccss boolean not null default false,
  -- Vacaciones
  add column if not exists vacaciones_excluir_incapacidad boolean not null default false,
  add column if not exists vacaciones_dias_aniversario numeric(5,2) check (vacaciones_dias_aniversario is null or vacaciones_dias_aniversario between 1 and 15),
  -- Alertas de documentos de colaboradores por vencer
  add column if not exists alertas_documentos_correos text,
  add column if not exists alertas_documentos_frecuencia text not null default 'mensual'
    check (alertas_documentos_frecuencia in ('semanal','mensual')),
  add column if not exists alertas_documentos_dias int not null default 30 check (alertas_documentos_dias between 1 and 365),
  add column if not exists alertas_documentos_ultima date;

-- Horas extra detectadas desde las marcas: una por colaborador y día (no se duplican al recalcular).
create unique index if not exists horas_extra_automatica_dia
  on public.horas_extra (empleado_id, fecha) where origen = 'automatico';

-- El número de envío BAC avanza al exportar, aunque quien exporte no sea administrador.
create or replace function public.avanzar_envio_bac(p_empresa uuid, p_usado int) returns void
language plpgsql security definer set search_path = '' as $$
begin
  if not public.puede_operar(p_empresa) then raise exception 'Sin permiso'; end if;
  update public.configuracion_planilla set bac_envio = greatest(bac_envio, p_usado) % 99999 + 1
   where empresa_id = p_empresa;
end $$;
revoke all on function public.avanzar_envio_bac(uuid, int) from public, anon;
grant execute on function public.avanzar_envio_bac(uuid, int) to authenticated;

-- El portal del colaborador también respeta las opciones de la boleta (saldo de vacaciones, subsidio).
drop function if exists public.portal_planillas();
create function public.portal_planillas()
returns table (planilla_id uuid, empresa_id uuid, empresa_nombre text, cedula_juridica text, tipo text,
               desde date, hasta date, fecha_pago date, nota_boleta text,
               boleta_saldo_vacaciones boolean, boleta_detalle_subsidio boolean)
language sql stable security definer set search_path = public as $$
  select distinct p.id, p.empresa_id, em.nombre, em.cedula_juridica, p.tipo, p.desde, p.hasta, p.fecha_pago, c.nota_boleta,
         coalesce(c.boleta_saldo_vacaciones, false), coalesce(c.boleta_detalle_subsidio, false)
    from boletas b
    join empleados e on e.id = b.empleado_id and e.usuario_id = auth.uid()
    join planillas p on p.id = b.planilla_id and p.estado = 'cerrada'
    join empresas em on em.id = p.empresa_id
    left join configuracion_planilla c on c.empresa_id = p.empresa_id
   where auth.uid() is not null
$$;
revoke all on function public.portal_planillas() from public, anon;
grant execute on function public.portal_planillas() to authenticated;


-- ===== 20261012002000_ficha_colaborador_odoo.sql =====
-- Ficha del colaborador con los datos de la pestaña «Planilla CR» del módulo Odoo.

alter table public.empleados
  -- CCSS
  add column if not exists numero_ccss text,
  add column if not exists asegurado_ccss boolean not null default true,      -- no asegurado = sin CCSS, renta ni INS (planilla SP de Odoo)
  -- Clasificación
  add column if not exists tipo_empleado text not null default 'indefinido'
    check (tipo_empleado in ('indefinido','plazo_fijo','periodo_prueba','practicante','servicios_profesionales','ocasional','tiempo_parcial')),
  add column if not exists departamento text,
  add column if not exists subdepartamento text,
  add column if not exists salario_variable boolean not null default false,   -- comisiones / HE recurrentes
  -- Banco
  add column if not exists cuenta_bancaria text,
  add column if not exists tipo_cuenta text check (tipo_cuenta in ('corriente','ahorros','sinpe')),
  add column if not exists sinpe_movil text,
  -- Saldos al implementar el sistema (pre-implementación)
  add column if not exists aguinaldo_inicial numeric(14,2) not null default 0 check (aguinaldo_inicial >= 0),
  add column if not exists aguinaldo_corte date,
  add column if not exists vacaciones_saldo_inicial numeric(8,2),
  add column if not exists vacaciones_corte date;

comment on column public.empleados.aguinaldo_inicial is
  'Aguinaldo acumulado antes de usar el sistema (Art. 228 CT). Se suma al aguinaldo calculado desde el día siguiente a aguinaldo_corte.';
comment on column public.empleados.vacaciones_saldo_inicial is
  'Saldo de vacaciones a la fecha vacaciones_corte. Desde ahí se acumula 1 día por mes cumplido y se restan los días tomados.';

-- Al guardar el saldo inicial de vacaciones en la ficha, quien opera la planilla recalcula
-- las acreditaciones posteriores al corte.
drop policy if exists operar_insertar on public.vacaciones_acumuladas;
create policy operar_insertar on public.vacaciones_acumuladas for insert to authenticated
  with check (public.puede_operar(empresa_id));
drop policy if exists operar_borrar on public.vacaciones_acumuladas;
create policy operar_borrar on public.vacaciones_acumuladas for delete to authenticated
  using (public.puede_operar(empresa_id));
grant insert, delete on public.vacaciones_acumuladas to authenticated;


-- ===== 20261012002100_catalogos_odoo.sql =====
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

-- ===== 20261013002200_licencias_especiales.sql =====
-- Licencias especiales de ley (Costa Rica), con Odoo planilla.leave.cr como referencia.
-- Se guardan en permisos: el tipo dice la base legal, los días de ley y quién paga
-- (ver app/servicios/licencias.py). Se puede correr más de una vez.

alter table public.permisos
  add column if not exists unidad text not null default 'dias' check (unidad in ('dias', 'horas')),
  add column if not exists horas numeric(6,2) check (horas is null or horas > 0),
  add column if not exists fecha_evento date,          -- nacimiento, entrega en adopción o fallecimiento
  add column if not exists parentesco text,            -- duelo: parentesco con la persona fallecida
  add column if not exists pariente text,              -- nombre del familiar (expediente)
  add column if not exists numero_documento text;      -- certificado de la CCSS, acta, resolución

comment on column public.permisos.tipo is
  'paternidad, adopcion, duelo_primer_grado, duelo_segundo_tercer, lactancia, matrimonio, con_goce, sin_goce o un texto libre anterior';


-- ===== 20261013002300_base_legal.sql =====
-- Base legal: cada regla que aplica el sistema con su ley, decreto o artículo, para que la persona
-- usuaria pueda revisar de dónde sale cada cálculo. Es un catálogo global (lo mantiene la
-- plataforma) y se muestra en «Base legal», en las pantallas donde se usa y en los documentos.
-- También corrige datos 2026 que venían del módulo Odoo y no coinciden con lo publicado:
--   * salarios mínimos 2026 (Decreto 45303-MTSS),
--   * base mínima contributiva 2026 de la CCSS (SEM ₡346 789 e IVM ₡324 590),
--   * BMC reducida de jornada parcial: desde 2025 sin límite de edad.
-- Se puede correr más de una vez.

create table if not exists public.base_legal (
  clave text primary key,
  tema text not null,
  titulo text not null,
  regla text not null,                 -- qué hace el sistema, en palabras sencillas
  norma text not null,                 -- ley, decreto, reglamento y artículos
  publicacion text,                    -- dónde y cuándo se publicó
  vigente_desde date,
  fuente text,                         -- institución que la emite o publica
  enlace text,                         -- portal oficial
  como_buscar text,                    -- qué escribir en el buscador del portal
  donde text[] not null default '{}',  -- pantallas y documentos donde se aplica
  nota text,                           -- advertencias (criterios, cosas por confirmar)
  verificado_en date,
  orden int not null default 0,
  actualizado_en timestamptz not null default now()
);
alter table public.base_legal enable row level security;
drop policy if exists ver on public.base_legal;
create policy ver on public.base_legal for select to authenticated using (true);
drop policy if exists gestionar on public.base_legal;
create policy gestionar on public.base_legal for all to authenticated
  using (public.es_admin_plataforma()) with check (public.es_admin_plataforma());
grant select, insert, update, delete on public.base_legal to authenticated;

insert into public.base_legal (orden, clave, tema, titulo, regla, norma, publicacion, vigente_desde, fuente, enlace,
                               como_buscar, donde, nota, verificado_en) values
-- ------------------------------------------------------------------ CCSS
(10, 'ccss_cuotas', 'CCSS', 'Cuotas obrero-patronales',
 'Se rebaja al colaborador 10,83 % (SEM 5,50 + IVM 4,33 + Banco Popular 1,00) y la empresa paga 26,83 % (SEM 9,25, IVM 5,58, Asignaciones Familiares 5,00, IMAS 0,50, INA 1,50, Banco Popular 0,50, FCL 1,50, ROP 2,00, INS 1,00).',
 'Ley Constitutiva de la CCSS (Ley 17); Reglamento del Seguro de Salud; Reglamento del Seguro de Invalidez, Vejez y Muerte; Ley de Protección al Trabajador (Ley 7983); Ley Orgánica del Banco Popular (Ley 4351); Ley 5662 (Asignaciones Familiares); Ley 4760 (IMAS); Ley 6868 (INA)',
 'Tabla de cuotas publicada por la CCSS para 2026', '2026-01-01', 'CCSS', 'https://www.ccss.sa.cr',
 'En ccss.sa.cr: «cuotas obrero patronales»', '{Boleta,Reporte CCSS,SICERE,Costo de planilla}', null, '2026-10-13'),
(11, 'ccss_ivm_2026', 'CCSS', 'Aumento de la cuota del IVM en 2026',
 'Desde enero 2026 el IVM sube a 4,33 % obrero y 5,58 % patronal (aumento escalonado).',
 'Reglamento del Seguro de Invalidez, Vejez y Muerte, Transitorio XI', 'Junta Directiva CCSS, sesión 9038 (2019)', '2026-01-01',
 'CCSS', 'https://www.ccss.sa.cr', 'En ccss.sa.cr: «Reglamento IVM transitorio XI»', '{Boleta,Parámetros legales}', null, '2026-10-13'),
(12, 'ccss_bmc', 'CCSS', 'Base mínima contributiva (BMC)',
 'Si el salario del mes es menor que la BMC, la cuota del SEM se calcula sobre ₡346 789 y la del IVM sobre ₡324 590. El resto de las cargas (Banco Popular, FCL, ROP, IMAS, INA, Asignaciones, INS) se calcula sobre el salario real. No se aplica en meses con incapacidad o licencia sin goce.',
 'Reglamento del Seguro de Salud, art. 63; Reglamento del IVM, art. 34', 'Montos de la CCSS para la facturación desde enero 2026', '2026-01-01',
 'CCSS', 'https://www.ccss.sa.cr', 'En ccss.sa.cr: «base mínima contributiva 2026»', '{Boleta,Reporte CCSS,Parámetros legales}',
 'Odoo usaba ₡333 328 para todo; se corrigió con los montos publicados por la CCSS.', '2026-10-13'),
(13, 'ccss_bmc_reducida', 'CCSS', 'BMC reducida para jornada parcial',
 'Colaboradores marcados como medio tiempo con salario menor a la BMC: el SEM se calcula sobre la mitad de la BMC y el IVM sobre la cuota parcial más próxima al salario (25, 50, 75 o 100 % de la BMC reducida). Desde el 1/1/2025 aplica a todas las edades.',
 'Reglamento del Seguro de Salud, art. 63; Reglamento del IVM, arts. 2 y 34 (reforma de la Junta Directiva de la CCSS, sesión 9275)',
 'La Gaceta, 7/9/2022; gradual por edad: menores de 35 (2023), menores de 50 (2024), todas las edades (2025)', '2025-01-01',
 'CCSS', 'https://www.ccss.sa.cr', 'En ccss.sa.cr: «base mínima contributiva reducida jornada parcial»', '{Boleta,Ficha del colaborador}',
 'Antes el sistema la limitaba a menores de 35 años y no cobraba piso; se actualizó.', '2026-10-13'),
(14, 'ccss_pensionado', 'CCSS', 'Pensionados que siguen trabajando',
 'A quien ya está pensionado y sigue trabajando se le rebaja 6,50 % (SEM 5,50 + Banco Popular 1,00): no cotiza de nuevo al IVM.',
 'Reglamento del Seguro de Salud; Reglamento del IVM', null, null, 'CCSS', 'https://www.ccss.sa.cr',
 'En ccss.sa.cr: «pensionado que labora cotización»', '{Boleta,Ficha del colaborador}',
 'Confirme con la CCSS el régimen de cada pensionado (IVM, Hacienda, Magisterio).', '2026-10-13'),
(15, 'ccss_sicere', 'CCSS', 'Planilla mensual a la CCSS (SICERE)',
 'Cada mes se reporta a la CCSS a cada asegurado con su salario. Los colaboradores no asegurados o marcados como exentos no van en el archivo porque no se les rebaja nada.',
 'Ley Constitutiva de la CCSS (Ley 17); Reglamento del Seguro de Salud; Reglamento del IVM', null, null, 'CCSS',
 'https://www.ccss.sa.cr', 'En ccss.sa.cr: «Planilla en Línea SICERE»', '{Exportaciones SICERE,Reporte CCSS}', null, '2026-10-13'),
-- ------------------------------------------------------------------ INS
(20, 'ins_riesgos', 'INS', 'Seguro de Riesgos del Trabajo',
 'Todo patrono debe asegurar a sus trabajadores contra riesgos del trabajo. La prima se calcula sobre el salario según la clase de riesgo; cuando hay un accidente o enfermedad laboral, el INS paga el subsidio directamente y la planilla no paga esos días.',
 'Código de Trabajo, Título IV (reformado por la Ley 6727 sobre Riesgos del Trabajo), art. 193', 'Ley 6727 de 1982', null,
 'INS / SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 193', '{Boleta,Reporte INS,Incapacidades}',
 'Las tasas por clase del sistema son de referencia: use la tarifa de su póliza del INS.', '2026-10-13'),
-- ------------------------------------------------------------------ Renta
(30, 'renta_tramos', 'Impuesto al salario', 'Tramos del impuesto al salario 2026',
 'Exento hasta ₡918 000; 10 % de ₡918 000 a ₡1 347 000; 15 % hasta ₡2 364 000; 20 % hasta ₡4 727 000; 25 % sobre el exceso (montos mensuales).',
 'Ley del Impuesto sobre la Renta (Ley 7092), arts. 32 y 33; Decreto 45333-H', 'La Gaceta 229, 5/12/2025', '2026-01-01',
 'Ministerio de Hacienda', 'https://www.hacienda.go.cr', 'En SCIJ o La Gaceta: «Decreto 45333-H»', '{Boleta,Reporte de renta,Reporte 208}', null, '2026-10-13'),
(31, 'renta_creditos', 'Impuesto al salario', 'Créditos fiscales por hijos y cónyuge',
 'Se rebaja del impuesto ₡1 710 por cada hijo y ₡2 590 por el cónyuge, por mes. Si la persona trabaja con varios patronos, el crédito lo aplica uno solo.',
 'Ley 7092, art. 34; Decreto 45333-H', 'La Gaceta 229, 5/12/2025', '2026-01-01', 'Ministerio de Hacienda',
 'https://www.hacienda.go.cr', 'En SCIJ: «Ley del Impuesto sobre la Renta» artículo 34', '{Boleta,Ficha del colaborador}', null, '2026-10-13'),
(32, 'renta_retencion', 'Impuesto al salario', 'El patrono como agente retenedor',
 'La empresa retiene el impuesto del salario en cada pago y lo entrega a Hacienda.',
 'Ley 7092, arts. 23 y 32 y siguientes; Reglamento de la Ley del Impuesto sobre la Renta', null, null, 'Ministerio de Hacienda',
 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Ley del Impuesto sobre la Renta» artículo 23', '{Boleta,Reporte 208}', null, '2026-10-13'),
(33, 'renta_208', 'Impuesto al salario', 'Declaración de retenciones (formularios 208/138 en TRIBU-CR)',
 'Cada mes se declara lo retenido a los colaboradores. El reporte 208 del sistema da los montos por persona; la declaración vence el día 15 del mes siguiente.',
 'Resolución MH-DGT-RES-0042-2025 de la Dirección General de Tributación', 'La Gaceta, 17/9/2025; rige desde el 6/10/2025', '2025-10-06',
 'Ministerio de Hacienda', 'https://www.hacienda.go.cr', 'En hacienda.go.cr: «TRIBU-CR retenciones salario 208»', '{Reporte 208}', null, '2026-10-13'),
-- ------------------------------------------------------------------ Salario y jornada
(40, 'salario_minimo', 'Salario y jornada', 'Salarios mínimos 2026',
 'El salario no puede ser menor que el mínimo de su categoría. El diagnóstico avisa si alguien de tiempo completo gana menos que el mínimo más bajo (sin contar el doméstico, que tiene su propio mínimo).',
 'Código de Trabajo, art. 177; Decreto 45303-MTSS', 'Alcance 156 a La Gaceta 229, 5/12/2025', '2026-01-01', 'MTSS',
 'https://www.mtss.go.cr', 'En mtss.go.cr: «salarios mínimos 2026»', '{Salarios mínimos,Diagnóstico,Ficha del colaborador}',
 'Odoo traía montos distintos; se reemplazaron por los del decreto.', '2026-10-13'),
(41, 'salario_diario', 'Salario y jornada', 'Salario diario = salario mensual / 30',
 'Para salarios mensuales el día vale la treintava parte: el salario mensual ya incluye los días de descanso y los feriados.',
 'Código de Trabajo, arts. 152 (descanso semanal) y 148 (feriados); criterio del MTSS para salarios mensuales', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/',
 'En SCIJ: «Código de Trabajo» artículo 152', '{Boleta,Liquidación,Licencias}', null, '2026-10-13'),
(42, 'jornadas', 'Salario y jornada', 'Jornadas ordinarias',
 'Diurna: hasta 8 horas diarias y 48 semanales. Nocturna: hasta 6 y 36. Mixta: hasta 7 y 42. La diurna puede ampliarse a 10 horas si no pasa de 48 por semana (jornada acumulativa).',
 'Código de Trabajo, arts. 135 a 138', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículos 135 a 138',
 '{Horarios,Asistencia}', null, '2026-10-13'),
(43, 'horas_extra', 'Salario y jornada', 'Horas extra',
 'Cada hora extra se paga con 50 % más que la hora ordinaria. Jornada ordinaria más extra: máximo 12 horas al día.',
 'Código de Trabajo, arts. 139 y 140', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 139',
 '{Horas extra,Boleta,Asistencia}', null, '2026-10-13'),
(44, 'feriados', 'Salario y jornada', 'Feriados',
 'Los feriados de pago obligatorio se pagan aunque no se trabajen; si se trabajan, se pagan dobles.',
 'Código de Trabajo, arts. 148 a 152', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 148',
 '{Feriados,Boleta}', 'Revise cada año si la Asamblea trasladó algún feriado.', '2026-10-13'),
(45, 'constancia', 'Salario y jornada', 'Constancias de trabajo',
 'Al terminar la relación, el patrono debe dar un certificado con la fecha de inicio y fin, la clase de trabajo y el salario.',
 'Código de Trabajo, art. 35', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 35',
 '{Constancias}', null, '2026-10-13'),
-- ------------------------------------------------------------------ Aguinaldo y vacaciones
(50, 'aguinaldo', 'Aguinaldo y vacaciones', 'Aguinaldo',
 'Es la doceava parte de todo lo ganado del 1 de diciembre al 30 de noviembre; se paga dentro de los primeros 20 días de diciembre. Al terminar la relación se paga lo proporcional. No paga CCSS ni renta.',
 'Ley 2412 de Aguinaldo para la empresa privada; Ley 7092, art. 35 (exención)', 'Ley 2412 de 1959', null, 'SCIJ',
 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Ley 2412 aguinaldo»', '{Aguinaldo,Liquidación,Provisiones}', null, '2026-10-13'),
(51, 'vacaciones', 'Aguinaldo y vacaciones', 'Vacaciones',
 'Dos semanas pagadas por cada 50 semanas trabajadas. Si la relación termina antes, un día por cada mes trabajado. Se pagan con el promedio de las últimas 50 semanas (o del tiempo trabajado si es menos). Solo se pagan en dinero al terminar la relación.',
 'Código de Trabajo, arts. 153, 156 y 157', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 153',
 '{Vacaciones,Liquidación,Provisiones}', null, '2026-10-13'),
-- ------------------------------------------------------------------ Terminación
(60, 'preaviso', 'Terminación', 'Preaviso',
 'Al despedir sin justa causa: 1 semana si trabajó de 3 a 6 meses, 15 días de 6 meses a 1 año y 1 mes después de un año. Si no se da, se paga.',
 'Código de Trabajo, art. 28', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 28',
 '{Liquidación}', null, '2026-10-13'),
(61, 'cesantia', 'Terminación', 'Cesantía',
 'Al despedir sin justa causa: 7 días (3 a 6 meses), 14 días (6 a 12 meses) y, desde un año, los días por año de la tabla legal (19,5 a 22), con tope de 8 años. Se calcula con el promedio de los últimos 6 meses.',
 'Código de Trabajo, art. 29 (reformado por la Ley de Protección al Trabajador, Ley 7983)', null, null, 'SCIJ',
 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 29', '{Liquidación,Parámetros legales}',
 'Odoo tenía 20 días en el tercer año; la ley dice 20,5.', '2026-10-13'),
-- ------------------------------------------------------------------ Incapacidades
(70, 'incapacidad_ccss', 'Incapacidades', 'Incapacidad por enfermedad (CCSS)',
 'Los primeros 3 días los paga el patrono al 50 % del salario; desde el cuarto día la CCSS paga el subsidio (60 %) directamente al colaborador.',
 'Código de Trabajo, art. 79; Reglamento del Seguro de Salud, art. 35', null, null, 'CCSS / MTSS', 'https://www.ccss.sa.cr',
 'En SCIJ: «Código de Trabajo» artículo 79; en ccss.sa.cr: «Reglamento del Seguro de Salud»', '{Incapacidades,Boleta}',
 'El 50 % de los días 1 a 3 es el criterio del MTSS (DAJ-AE-829-06) y de la jurisprudencia.', '2026-10-13'),
(71, 'incapacidad_recaida', 'Incapacidades', 'Nueva incapacidad dentro de 30 días',
 'Si la nueva incapacidad empieza dentro de los 30 días siguientes al fin de la anterior, la CCSS la subsidia desde el primer día y el patrono no paga los días 1 a 3.',
 'Reglamento del Seguro de Salud, art. 35', null, null, 'CCSS', 'https://www.ccss.sa.cr', 'En ccss.sa.cr: «Reglamento del Seguro de Salud»',
 '{Incapacidades,Boleta}', null, '2026-10-13'),
(72, 'maternidad', 'Incapacidades', 'Licencia de maternidad',
 'Un mes antes y tres meses después del parto. La paga 50 % la CCSS y 50 % el patrono; la parte del patrono no paga CCSS. La trabajadora embarazada o en lactancia no puede ser despedida sin autorización del MTSS.',
 'Código de Trabajo, arts. 94, 94 bis y 95', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 95',
 '{Incapacidades,Boleta}', null, '2026-10-13'),
-- ------------------------------------------------------------------ Licencias
(80, 'paternidad', 'Licencias', 'Licencia de paternidad',
 '8 días: dos por semana durante las primeras cuatro semanas desde el nacimiento. La pagan por partes iguales la CCSS y el patrono, sobre el salario promedio de los 3 meses anteriores. También aplica a la madre no gestante.',
 'Código de Trabajo, art. 95, reformado por la Ley 10.211', 'La Gaceta, 3/6/2022', '2022-06-03', 'SCIJ', 'https://pgrweb.go.cr/scij/',
 'En SCIJ: «Ley 10211»', '{Licencias,Boleta}', null, '2026-10-13'),
(81, 'adopcion', 'Licencias', 'Licencia por adopción',
 'Tres meses para quien adopta (si adoptan dos personas, se reparten). Se paga como la maternidad: 50 % CCSS y 50 % patrono.',
 'Código de Trabajo, art. 95, reformado por la Ley 10.211', 'La Gaceta, 3/6/2022', '2022-06-03', 'SCIJ', 'https://pgrweb.go.cr/scij/',
 'En SCIJ: «Ley 10211»', '{Licencias,Boleta}', null, '2026-10-13'),
(82, 'duelo', 'Licencias', 'Licencia por duelo',
 '3 días hábiles pagados por la muerte de un pariente de primer grado (padres, hijos, cónyuge o conviviente, suegros, yernos y nueras) y 1 día hábil por segundo y tercer grado. La paga el patrono.',
 'Código de Trabajo, art. 69 inciso l), Ley 10589', 'Alcance 195 a La Gaceta 227, 3/12/2024', '2024-12-03', 'SCIJ', 'https://pgrweb.go.cr/scij/',
 'En SCIJ: «Ley 10589»', '{Licencias}', null, '2026-10-13'),
(83, 'lactancia', 'Licencias', 'Lactancia',
 'Una hora pagada por día por cada bebé lactante. El primer año no pide certificado; después se prorroga cada tres meses con certificado médico. Además, 25 minutos cada 3 horas para extracción de leche. Nada se rebaja del salario.',
 'Código de Trabajo, arts. 95, 97 y 97 bis, reformados por la Ley 10895 (Ley para proteger, promover y apoyar la lactancia materna); salas de lactancia: Decreto 44943-MTSS-S',
 'Alcance 41 a La Gaceta 74, 23/4/2026', '2026-04-23', 'SCIJ / La Gaceta', 'https://www.imprentanacional.go.cr/gaceta/',
 'En SCIJ: «Ley 10895»', '{Licencias,Boleta}', null, '2026-10-13'),
(84, 'matrimonio', 'Licencias', 'Permiso por matrimonio',
 'No es obligatorio por ley en el sector privado: depende del reglamento interno o de la convención colectiva. El sistema deja que la empresa elija si es con goce.',
 'Código de Trabajo (no lo establece para el sector privado)', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', null, '{Licencias}',
 'Odoo lo ponía como obligatorio citando el «Art. 37»; no es correcto.', '2026-10-13'),
-- ------------------------------------------------------------------ Deducciones
(90, 'embargos', 'Deducciones', 'Embargos de salario',
 'Es inembargable el salario líquido (después de CCSS y renta) hasta el menor salario mínimo del decreto (doméstico: ₡268 731,31 en 2026). Del exceso se puede embargar hasta un octavo de la parte que llegue al triple de ese monto y hasta un cuarto del resto.',
 'Código de Trabajo, art. 172; Decreto 45303-MTSS', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Código de Trabajo» artículo 172',
 '{Embargos,Boleta}', 'Las pensiones alimentarias pueden llegar hasta el 50 % del salario. Varios embargos hacen fila: el siguiente espera a que se libere la parte embargable.', '2026-10-13'),
(91, 'pension_alimentaria', 'Deducciones', 'Pensión alimentaria',
 'Se rebaja primero y según lo que ordena el juzgado; no le aplica el mínimo inembargable del art. 172 y puede llegar hasta el 50 % del salario.',
 'Ley de Pensiones Alimentarias (Ley 7654)', null, null, 'SCIJ', 'https://pgrweb.go.cr/scij/', 'En SCIJ: «Ley 7654»', '{Embargos,Boleta}', null, '2026-10-13'),
(92, 'rop_voluntario', 'Deducciones', 'Aporte voluntario adicional de pensión',
 'Solo para quien lo pidió: se rebaja el porcentaje acordado del salario además de las cuotas de la CCSS.',
 'Ley de Protección al Trabajador (Ley 7983), régimen voluntario', null, null, 'SUPEN', 'https://www.supen.fi.cr',
 'En SCIJ: «Ley 7983»', '{Ficha del colaborador,Boleta}', 'No es obligatorio.', '2026-10-13'),
-- ------------------------------------------------------------------ Configuración de la empresa
(100, 'bono_antiguedad', 'Políticas de la empresa', 'Bono por antigüedad',
 'No lo exige la ley en el sector privado: lo define la empresa en su catálogo.', 'No es obligación legal (política interna)', null, null,
 null, null, null, '{Configuración}', null, '2026-10-13'),
(101, 'exentos', 'Políticas de la empresa', 'Colaboradores exentos o no asegurados',
 'Quien se marca como exento o no asegurado no paga CCSS ni renta y no va en SICERE, INS ni CCSS. Úselo solo cuando haya un motivo legal (por ejemplo, servicios profesionales sin relación laboral).',
 'Ley 17 y Ley 7092: la empresa responde por la clasificación', null, null, null, null, null,
 '{Ficha del colaborador,SICERE,Reporte INS}', 'Si existe relación laboral, la CCSS puede cobrar las cuotas no reportadas.', '2026-10-13')
on conflict (clave) do update set
  tema = excluded.tema, titulo = excluded.titulo, regla = excluded.regla, norma = excluded.norma,
  publicacion = excluded.publicacion, vigente_desde = excluded.vigente_desde, fuente = excluded.fuente,
  enlace = excluded.enlace, como_buscar = excluded.como_buscar, donde = excluded.donde, nota = excluded.nota,
  verificado_en = excluded.verificado_en, orden = excluded.orden, actualizado_en = now();

-- ------------------------------------------------------------------ Salarios mínimos 2026 (Decreto 45303-MTSS)
delete from public.salarios_minimos
 where vigente_desde = '2026-01-01' and decreto = 'Decreto MTSS salarios mínimos 2026';
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
on conflict (categoria, vigente_desde) do update set monto = excluded.monto, decreto = excluded.decreto;

-- ------------------------------------------------------------------ Parámetros legales 2026
-- BMC por régimen (solo si sigue el valor de Odoo) y tasas del SEM/IVM para calcularla.
update public.parametros_legales
   set datos = datos || '{"bmc_mensual": "346789"}'::jsonb
 where vigente_desde = '2026-01-01' and datos->>'bmc_mensual' = '333328';
update public.parametros_legales
   set datos = '{"bmc_ivm_mensual": "324590", "ccss_sem_obrero": "0.055", "ccss_ivm_obrero": "0.0433",
                 "ccss_sem_patronal": "0.0925", "ccss_ivm_patronal": "0.0558"}'::jsonb || datos
 where vigente_desde = '2026-01-01';
update public.parametros_legales
   set datos = datos || '{"edad_limite_bmc_reducida": 0}'::jsonb
 where vigente_desde = '2026-01-01' and (datos->>'edad_limite_bmc_reducida')::int = 35;
update public.parametros_legales
   set fuente = 'Decreto 45333-H (renta); CCSS: cuotas y BMC 2026, Transitorio XI Reglamento IVM; Código de Trabajo arts. 28, 29, 153 y 172; Decreto 45303-MTSS'
 where vigente_desde = '2026-01-01' and fuente = 'Decreto 45333-H (renta); CCSS 2026; Código de Trabajo Arts. 28-29, 153';

-- ===== 20261013002400_rls_rapido.sql =====
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

-- ===== 20261013002500_tareas_programadas.sql =====
-- Control de las tareas programadas (pg_cron llama a /cron/* de la API).
-- Cada ejecución queda registrada y la API la usa para:
--   * no correr la misma tarea dos veces a la vez (índice único «una en curso por tarea»);
--   * no repetirla antes de su intervalo mínimo (si pg_cron o alguien la llama de más);
--   * poner un tope diario de ejecuciones por tarea.
-- Así una llamada repetida o un secreto filtrado no pueden disparar cobros, correos o consultas sin control.
-- Se puede correr más de una vez.

create table if not exists negocio.ejecuciones_cron (
  id bigserial primary key,
  tarea text not null,
  inicio timestamptz not null default now(),
  fin timestamptz,
  estado text not null default 'en_curso' check (estado in ('en_curso', 'ok', 'error', 'abandonada')),
  duracion_ms int,
  detalle text
);
create unique index if not exists ejecuciones_cron_una_en_curso on negocio.ejecuciones_cron (tarea)
  where estado = 'en_curso';
create index if not exists ejecuciones_cron_tarea_inicio on negocio.ejecuciones_cron (tarea, inicio desc);

alter table negocio.ejecuciones_cron enable row level security;
drop policy if exists ver on negocio.ejecuciones_cron;
create policy ver on negocio.ejecuciones_cron for select to authenticated using ((select public.es_admin_plataforma()));
revoke all on negocio.ejecuciones_cron from anon;
grant select on negocio.ejecuciones_cron to authenticated;

commit;

-- Verificación: debe decir "Instalación completa" con 4 valores en true.
select 'Instalación completa' as resultado,
       to_regclass('public.empresas') is not null as base,
       to_regclass('public.idempotencia') is not null as seguridad,
       to_regclass('public.modulos') is not null as contabilidad,
       to_regclass('public.comisiones_por_pago') is not null as auditoria;
