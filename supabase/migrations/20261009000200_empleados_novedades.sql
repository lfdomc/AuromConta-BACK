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
