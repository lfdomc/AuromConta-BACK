-- Planilla Costa Rica · Instalación en Supabase NUEVO · PARTE 2 de 4
-- Contiene: 20261009000500_semillas.sql, 20261009000600_funciones_api.sql, 20261009000800_autogestion.sql, 20261009000900_negocio.sql, 20261009001000_endurecimiento.sql
-- Pegue TODO el archivo en SQL Editor (sin seleccionar nada) y presione Run.
begin;
do $guarda$ begin
  if to_regclass('negocio.clientes') is null then raise exception 'Primero ejecute la parte 1'; end if;
  if to_regprocedure('public.via_api()') is not null then raise exception 'La parte 2 ya está instalada: siga con la siguiente'; end if;
end $guarda$;

-- >>>>>>>>>>>>>>>> 20261009000500_semillas.sql
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


-- >>>>>>>>>>>>>>>> 20261009000600_funciones_api.sql
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


-- >>>>>>>>>>>>>>>> 20261009000800_autogestion.sql
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


-- >>>>>>>>>>>>>>>> 20261009000900_negocio.sql
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


-- >>>>>>>>>>>>>>>> 20261009001000_endurecimiento.sql
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

commit;
select 'Parte 2 de 4 instalada' as resultado;
