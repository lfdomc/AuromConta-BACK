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
