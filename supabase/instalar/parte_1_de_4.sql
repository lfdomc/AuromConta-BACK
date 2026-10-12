-- Planilla Costa Rica · Instalación en Supabase NUEVO · PARTE 1 de 4
-- Contiene: 20261009000100_nucleo.sql, 20261009000200_empleados_novedades.sql, 20261009000300_planillas.sql, 20261009000400_negocio_autogestion.sql
-- Pegue TODO el archivo en SQL Editor (sin seleccionar nada) y presione Run.
begin;
do $guarda$ begin
  if to_regclass('public.empresas') is not null then raise exception 'La parte 1 ya está instalada: siga con la siguiente'; end if;
end $guarda$;

-- >>>>>>>>>>>>>>>> 20261009000100_nucleo.sql
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


-- >>>>>>>>>>>>>>>> 20261009000200_empleados_novedades.sql
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


-- >>>>>>>>>>>>>>>> 20261009000300_planillas.sql
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


-- >>>>>>>>>>>>>>>> 20261009000400_negocio_autogestion.sql
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

commit;
select 'Parte 1 de 4 instalada' as resultado;
