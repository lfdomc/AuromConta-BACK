# Hallazgos al portar el cálculo desde Odoo (planilla_cr FIX15)

Diferencias intencionales del motor y puntos a confirmar con el contador.

| # | Tema | Odoo hoy | Motor | Acción |
|---|------|----------|-------|--------|
| H1 | Mensaje de error de tramos de renta | Lista 918k / 1,381k / 2,423k / 4,845k | Usa 918k / 1,347k / 2,364k / 4,727k (Decreto 45333-H) | El XML de datos de Odoo ya es correcto; solo el texto del error está mal |
| H2 | Costo patronal con incapacidad CCSS | Suma el bruto y además el neto del empleado → cuenta el salario dos veces | Bruto + cargas + provisiones + días 1-3 al 50 % | Revisar el reporte de costo patronal en Odoo |
| H3 | Base de aguinaldo con incapacidad | Resta el subsidio CCSS a una base que ya excluye los días incapacitados (doble resta) | Base = salario cotizable | Confirmar con contador |
| H4 | Paternidad en quincenal | Diario = monto de la quincena / 30 → la mitad | Diario = salario mensual / 30 | Confirmar con contador |
| H12 | Paternidad y adopción (Ley 10.211, Art. 95 CT) | Suma el pago de paternidad al 100 % encima del salario normal (se pagaban dos veces esos días) | Los días de ley salen del salario y de la base CCSS; el patrono paga el 50 % del salario promedio de 3 meses (no salarial) y la CCSS el otro 50 % directo al colaborador. Se calcula en la API (servicios/licencias.py); el campo dias_paternidad del motor ya no se usa | Confirmar con el contador |
| H5 | Base Mínima Contributiva | Un monto único ₡333,328 sobre toda la tasa (10,83 % / 26,83 %) | 2026: SEM ₡346 789 e IVM ₡324 590; la BMC solo sube las cuotas del SEM y del IVM, el resto va sobre el salario real (Reglamento del Seguro de Salud art. 63; Reglamento IVM art. 34) | Corregido (octubre 2026) |
| H6 | Cesantía por antigüedad | Primer año usa 19.5 días aunque tenga menos de 1 año | Igual que Odoo | Art. 29 CT: 3-6 meses 7 días, 6-12 meses 14 días; decidir si la provisión lo refleja |
| H7 | Redondeo | `round()` de Python sobre float | Decimal, mitad hacia arriba | Diferencias de ±₡0.01 posibles en la migración paralela |

Simplificación: el costo patronal de maternidad 50/50 se toma como la mitad del subsidio del periodo.
| H8 | Tabla de cesantía año 3 | 20 días | 20.5 días (Art. 29 CT) y tabla completa hasta año 13 | Corregido en el motor y en los parámetros 2026 |
| H9 | Feriados 2026 | Incluye 12 de octubre y 8 de diciembre como feriados | Lista oficial 2026: 12 feriados (9 de pago obligatorio) | Corregido en los datos iniciales |
| H10 | Tope de embargos | 25 % plano del neto disponible | Art. 172 CT: inembargable hasta el menor salario mínimo (₡268 731,31 en 2026, servicio doméstico), 1/8 hasta 3 veces ese monto y 1/4 del exceso; la pensión alimentaria no tiene ese límite | Confirmar con el contador y actualizar el mínimo cada decreto |
| H11 | Costo patronal (FIX16 de Odoo chk88) | Odoo corrigió la duplicación del bruto con incapacidad y resta los permisos sin goce del costo | Igual que FIX16, y además suma los ingresos no salariales en todos los casos (Odoo solo con incapacidad) y no vuelve a sumar las licencias con goce, que ya están en el salario | Confirmar con el contador |
| H13 | BMC reducida de jornada parcial | Solo menores de 35 años y sin piso (cotiza sobre el salario) | Desde 1/1/2025 todas las edades: SEM sobre el 50 % de la BMC; IVM sobre la cuota parcial (25/50/75/100 % de la BMC reducida) más próxima al salario (CCSS, sesión 9275, La Gaceta 7/9/2022) | Corregido (octubre 2026) |
| H14 | Nueva incapacidad dentro de 30 días | El patrono vuelve a pagar los días 1 a 3 | La CCSS subsidia desde el primer día (Reglamento del Seguro de Salud art. 35) | Corregido (octubre 2026) |
| H15 | Salarios mínimos 2026 | 5 categorías con montos que no coinciden con el decreto | 10 categorías del Decreto 45303-MTSS (Alcance 156, La Gaceta 229, 5/12/2025) | Corregido en los datos (migración 0023) |
