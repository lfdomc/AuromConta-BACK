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
