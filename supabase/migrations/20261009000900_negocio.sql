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
