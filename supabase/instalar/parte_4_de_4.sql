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
  v_correo := nullif(trim(coalesce(v_emp.correo, '')), '');
  if v_correo is null then
    select u.email into v_correo from auth.users u where u.id = v_emp.creado_por;
  end if;
  if v_correo is null then return; end if;
  insert into negocio.clientes (empresa_id, razon_social, tipo_identificacion, identificacion, correo_facturacion,
                                telefono, estado, alta)
  values (p_empresa, v_emp.nombre,
          case when length(v_ced) = 9 then '01' when length(v_ced) in (11, 12) then '03' else '02' end,
          v_ced, v_correo, v_emp.telefono, v_emp.estado_servicio, v_emp.creado_en::date);
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

commit;
select 'Parte 4 de 4 instalada' as resultado;
