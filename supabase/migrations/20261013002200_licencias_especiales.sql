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
