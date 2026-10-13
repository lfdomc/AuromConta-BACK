-- Planilla Costa Rica · Instalación en Supabase NUEVO · PARTE 4 de 4
-- Contiene: 20261012001300_modulos_contabilidad.sql, 20261012001400_auditoria.sql
-- Pegue TODO el archivo en SQL Editor (sin seleccionar nada) y presione Run.
begin;
do $guarda$ begin
  if to_regclass('public.ajustes_seguridad') is null then raise exception 'Primero ejecute la parte 3'; end if;
  if to_regclass('public.comisiones_por_pago') is not null then raise exception 'La parte 4 ya está instalada: siga con la siguiente'; end if;
end $guarda$;

-- >>>>>>>>>>>>>>>> 20261012001300_modulos_contabilidad.sql
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


-- >>>>>>>>>>>>>>>> 20261012001400_auditoria.sql
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
select 'Parte 4 de 4 instalada' as resultado;
