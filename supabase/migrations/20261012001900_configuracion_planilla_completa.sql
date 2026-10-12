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
