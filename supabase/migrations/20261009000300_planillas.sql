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
