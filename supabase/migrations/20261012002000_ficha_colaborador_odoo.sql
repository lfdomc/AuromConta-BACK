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
