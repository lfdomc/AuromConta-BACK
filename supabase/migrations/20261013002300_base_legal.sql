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
