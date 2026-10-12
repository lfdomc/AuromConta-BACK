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
