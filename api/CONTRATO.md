# Contrato de la API · Planilla Costa Rica

Base: `https://<api>.up.railway.app`. Autenticación: `Authorization: Bearer <JWT de Supabase>`.
Montos: texto decimal (`"1234.50"`). Fechas: `YYYY-MM-DD`. Errores: `{"detail": "mensaje en español"}`
(401 sesión, 403 rol, 404 no existe o sin acceso, 409 regla de negocio, 422 validación).

Roles efectivos por empresa (`mi_rol`): `admin`, `aprobador`, `operador`, `lectura`,
`contador_operacion`, `contador_lectura` (contador/vendedor asignado), `plataforma` (dueña A.U.R.O.M.).

## Fases 1-3 (implementado)

| Método | Ruta | Uso |
|---|---|---|
| GET | `/yo` | `{id, correo, empresas:[{id,nombre,rol,estado_servicio,via}], es_plataforma, socio, empleos}` |
| POST | `/empresas` | `{nombre, cedula_juridica?, codigo_referido?, frecuencia}` crea empresa (queda admin) |
| GET/PATCH | `/empresas/{e}` | datos de empresa (+`mi_rol`) |
| GET/PATCH | `/empresas/{e}/configuracion` | renta, BMC, cesantía, factores HE, nota de boleta |
| GET/POST/PATCH | `/empresas/{e}/usuarios[/{id}]` | usuarios (admin); POST `{correo, rol}` agrega o invita |
| GET/POST/DELETE | `/empresas/{e}/contadores[/{id}]` | contadores/vendedores asignados; POST `{socio_id | codigo_referido, funciones[], permiso, soporte_principal}`; DELETE revoca |
| GET | `/empresas/{e}/auditoria?tabla=` | bitácora |
| CRUD | `/empresas/{e}/sucursales`, `/calendarios`, `/horarios` | catálogos |
| GET/POST | `/empresas/{e}/feriados?anio=` | nacionales + propios |
| GET | `/parametros-legales` | juegos de parámetros con vigencia |
| GET/POST | `/empresas/{e}/empleados?estado=&buscar=&sucursal_id=` | lista / crear |
| GET/PATCH/DELETE | `/empresas/{e}/empleados/{id}` | ficha (+historial_salarial, prestamos_activos) |
| POST | `/empresas/{e}/empleados/{id}/salario` | solicitar cambio `{fecha_efectiva, salario_nuevo, motivo}` |
| POST | `/empresas/{e}/empleados/historial/{cambio}/decision` | `{aprobar}` (aprobador) |
| POST | `/empresas/{e}/empleados/aumento-masivo` | `{porcentaje|monto, fecha_efectiva, empleado_ids?, aplicar}` |
| GET/POST | `/empresas/{e}/empleados/{id}/documentos`, `/movimientos` | |
| CRUD | `/empresas/{e}/marcas`, `/horas-extra`, `/incapacidades`, `/permisos`, `/vacaciones`, `/bonos`, `/otras-deducciones`, `/rebajos-renta`, `/prestamos`, `/embargos`, `/pensiones` | filtros `empleado_id`, `estado`, `desde`, `hasta` |
| POST | `/empresas/{e}/horas-extra/aprobar?aprobar=true` | body: lista de ids |
| POST | `/empresas/{e}/vacaciones/{id}/decision?aprobar=true` | |
| GET | `/empresas/{e}/prestamos/{id}/pagos` | |
| GET/POST | `/empresas/{e}/planillas?estado=&tipo=&anio=` | crear `{desde, hasta, frecuencia | calendario_id, sucursal_id?, fecha_pago?, tipo}` |
| GET/DELETE | `/empresas/{e}/planillas/{id}` | detalle con `boletas[]`, `totales`, `advertencias`, `mi_rol` |
| POST | `.../planillas/{id}/calcular`, `/aprobar`, `/devolver`, `/cerrar` | flujo borrador→calculada→aprobada→cerrada |
| POST | `.../planillas/{id}/reabrir` | `{motivo}` (aprobador) |
| GET | `.../planillas/{id}/asiento` | asiento vigente |
| GET | `.../planillas/{id}/boletas/{b}` y `/pdf` | detalle con líneas / PDF |
| POST | `.../planillas/{id}/enviar` | `{boleta_ids?}` correo con PDF |
| POST | `/empresas/{e}/aguinaldo/calcular` | `{anio, crear_planilla, fecha_pago?}` |
| GET | `/empresas/{e}/aguinaldo/{anio}` | |
| POST | `/empresas/{e}/liquidaciones/simular` y `/liquidaciones` | `{empleado_id, fecha_salida, motivo, preaviso_otorgado, dias_vacaciones_pendientes?, salarios_pendientes, otros_pagos, deducciones_adicionales, incluir_saldo_prestamos}` |
| GET | `/empresas/{e}/liquidaciones`, `/{id}/pdf`, `/{id}/constancia` | |
| POST | `/empresas/{e}/liquidaciones/{id}/aprobar` | |

## Fase 4 · `routers/reportes.py`

Reporte genérico JSON: `{titulo, subtitulo, columnas:[{clave,titulo,tipo:"texto|dinero|numero|fecha|porcentaje"}], filas:[{...}], totales:{clave: valor}}`.
`formato=json|xlsx|pdf` (xlsx/pdf devuelven archivo).

| Método | Ruta |
|---|---|
| GET | `/empresas/{e}/dashboard?anio=&mes=` → `{empleados_activos, mes:{bruto,neto,costo_total,ccss,renta,planillas}, por_mes:[{mes,bruto,neto,costo_total}], pendientes:{horas_extra,vacaciones,cambios_salariales,planillas_por_aprobar}, vencimientos:[{tipo,mensaje,fecha}]}` |
| GET | `/empresas/{e}/reportes` → catálogo `[{tipo,titulo,descripcion,filtros[]}]` |
| GET | `/empresas/{e}/reportes/{tipo}?desde=&hasta=&planilla_id=&sucursal_id=&empleado_id=&formato=` tipos: `resumen-planilla`, `ccss`, `ins`, `renta`, `costo-patronal`, `costo-sucursal`, `detalle-empleado`, `horas-extra`, `vacaciones`, `prestamos`, `embargos-pensiones`, `historial-salarial`, `incapacidades`, `resumen-ejecutivo` |
| GET | `/empresas/{e}/exportaciones/banco?planilla_id=&formato=csv|xlsx` |
| GET | `/empresas/{e}/exportaciones/sicere?anio=&mes=` (archivo para la CCSS) |
| GET | `/empresas/{e}/exportaciones/ins?anio=&mes=` (planilla RT-Virtual) |
| GET | `/empresas/{e}/exportaciones/eddi7?anio=&mes=` |
| GET | `/empresas/{e}/asientos?desde=&hasta=&estado=` · GET `/asientos/{id}` · GET `/asientos/{id}/exportar?formato=csv|xlsx|pdf` · POST `/asientos/{id}/marcar-exportado` |
| GET/PUT | `/empresas/{e}/cuentas-contables` (PUT `[{concepto,codigo,nombre,centro_costo}]`) |

Notas de implementación (Fase 4):
- Sin `planilla_id` los reportes y exportaciones usan solo planillas `aprobada`/`cerrada` cuyo `hasta` cae en el rango; sin fechas, el mes en curso (con solo `desde`, hasta fin de ese mes). CCSS, INS, renta, SICERE y EDDI-7 usan planillas `ordinaria`/`extraordinaria` (el aguinaldo no cotiza). `sucursal_id` filtra por la sucursal del colaborador.
- Reporte JSON: montos como texto con 2 decimales; `numero`/`porcentaje` como texto (enteros como número). Puede traer `notas: [texto]` además de las claves del contrato. Filas de `ins` usan las 14 columnas del Excel de Odoo.
- `dashboard` agrega `anio` y `mes_numero`; `mes.*` y `por_mes[].*` son texto. `vencimientos` incluye RT-Virtual (día 10), SICERE y renta (día 15), aguinaldo (20/12), documentos por vencer (30 días), fin de proyectos temporales y embargos por vencer.
- `exportaciones/banco` exige planilla aprobada o cerrada (409). Parámetros extra opcionales: `banco=generico|bcr|bncr|sinpe|bac|bac_prn` (por defecto `generico`, CSV/XLSX propio de Aurom), `concepto`; BNCR (.SIN) requiere `cliente_bncr`, `transferencia`, `iban_debito` (`moneda=01|02`); BAC Formato 6 (.PRN) requiere `plan_bac` y `envio` (consecutivo de envío: Aurom no lo guarda, lo envía el cliente). `formato` aplica a `generico` y `bac`. Monto = neto de la boleta.
- Todas las exportaciones aceptan `sucursal_id`; `eddi7` acepta `incluir_maternidad=true|false`. SICERE y EDDI-7 exigen `numero_patronal` en la empresa (409 si falta).
- Los registros omitidos (sin IBAN, sin cédula, teléfono inválido...) no se escriben dentro del archivo: van en el encabezado `X-Advertencias` (texto URL-encoded, separador ` | `). Si no queda ningún registro: 409.
- Formatos portados de Odoo `planilla_cr`: SICERE ← `models/ccss_report.py` (posicional 01/02/09, CRLF, latin-1); INS RT-Virtual ← `models/ins_report.py` (xlsx 14 columnas); EDDI-7 ← `models/eddi7_export.py` (80/120/80, ASCII); banco ← `models/bank_payment.py` (BCR DAV, BNCR .SIN, SINPE Móvil, BAC CSV/XLSX, BAC .PRN). Cambios deliberados respecto a Odoo (confirmar con contador): SICERE un registro por colaborador y mes (Odoo uno por boleta) y total 09 = suma del detalle; EDDI-7 totales CCSS con cuotas reales (Odoo salario × tasa) y días de incapacidad solo dentro del mes; BNCR tipo de procesamiento con `iban[4:8]=='0152'` (Odoo comparaba una posición equivocada). La exportación de asientos (CSV con BOM/XLSX/PDF: Fecha, Cuenta, Nombre, Descripción, Centro de costo, Débito, Crédito) no existe en Odoo y es formato propio.
- `asientos?estado=` acepta `vigente|reemplazado|exportado|pendiente`; cada asiento de la lista trae `lineas` (cantidad) y datos de la planilla. `marcar-exportado` y `PUT cuentas-contables` requieren rol de operación. `GET cuentas-contables` lista también los conceptos del motor sin cuenta (`id: null`) con su `descripcion`; `PUT` valida que el concepto exista.

## Fase 5 · `routers/importaciones.py`

| Método | Ruta |
|---|---|
| GET | `/empresas/{e}/importaciones/plantillas/{tipo}` → xlsx. Tipos: `empleados`, `horas-extra`, `incapacidades`, `permisos`, `prestamos`, `saldos-vacaciones`, `historico-boletas` |
| POST | `/empresas/{e}/importaciones/{tipo}?aplicar=false` (multipart `archivo`) → `{tipo, total, validas, errores:[{fila,columna,mensaje}], vista_previa:[...], aplicado, creados, actualizados}` |
| POST | `/empresas/{e}/importaciones/odoo` `{url, base_datos, usuario, clave_api, company_id?, meses_historico=12, aplicar}` → mismo formato, por entidad |
| GET | `/empresas/{e}/diagnostico` → `[{nivel,tipo,mensaje,empleado_id?}]` |
| POST | `/empresas/{e}/paralelo/{planilla_id}` (multipart xlsx exportado de Odoo) → `{coinciden, diferencias:[{empleado, concepto, odoo, nuevo, diferencia}]}` |

Notas de implementación (Fase 5):
- Archivos `.xlsx` o `.csv` (`,` o `;`), máximo 5 MB (413) y 20 000 filas; otro formato 415. La fila de encabezados se detecta sola (primeras 15 filas de cualquier hoja) y acepta los títulos de la plantilla de Aurom y los de las plantillas de Odoo (`Cedula Empleado`, `Monto Total (CRC)`...). `fila` en los errores = número de fila en Excel.
- Importar archivo es todo o nada: con algún error, `aplicar=true` responde 200 con `aplicado:false` y no escribe. Cada elemento de `vista_previa` trae `fila`, `accion` (`crear`, `actualizar`, ...) y, si aplica, `colaborador`.
- `empleados` crea o actualiza por identificación (sin guiones); al actualizar solo cambia las columnas presentes y un salario distinto queda como cambio salarial `pendiente` (no se aplica directo). `horas-extra` creadas por quien no es aprobador quedan `pendiente`. `prestamos` acepta `cuota` o `Número de cuotas` y `Monto ya pagado` (se registra como abono inicial). `historico-boletas` con `aplicar=true` requiere rol aprobador (403): crea por periodo una planilla `ordinaria` cerrada (`descripcion` "Histórico importado · …", frecuencia según días), boletas con `version_motor='importado'` y líneas; no genera asiento; rechaza periodos que se traslapan con boletas existentes.
- `importaciones/odoo`: con `aplicar=true` requiere aprobador. URL http(s) pública (se bloquean IP privadas o locales: 422); credenciales malas 409; Odoo inalcanzable 502. Respuesta: `{tipo:"odoo", origen, desde_historico, total, validas, errores:[{entidad,fila,columna,mensaje}], aplicado, creados, actualizados, entidades:{empleados, historial_salarial, prestamos, embargos, pensiones, incapacidades, historico_boletas}}`, cada entidad con el formato de importación + `modelo_odoo`; `fila` = id del registro en Odoo. A diferencia de los archivos, en Odoo se guardan las filas válidas y se informan las demás. Las cuotas mensuales de préstamos de Odoo se convierten a cuota por planilla (÷2 quincenal, ×12/52 semanal). El mapeo de campos está en `ODOO_MAPEO` al inicio de `importaciones.py`.
- `diagnostico` ordena por nivel (`error`, `aviso`, `info`); tipos: `empresa`, `contabilidad`, `empleados`, `identificacion`, `banco`, `correo`, `salario`, `datos`, `vacaciones`, `estado`, `historico`, `prestamos`, `obligaciones`, `incapacidades`, `documentos`, `pendientes`, `planillas`.
- `paralelo` necesita columna de identificación y al menos una de montos (`Salario Bruto`, `CCSS Obrero (10.83%)`, `Salario Neto`, `Impuesto Renta`, `CCSS Patronal`, `INS`, ... o el nombre técnico de Odoo). Tolerancia ±0,01. Respuesta extra: `planilla`, `comparados`, `conceptos`, `tolerancia`, `solo_en_odoo`, `solo_en_aurom`, `errores`; `diferencias[]` trae además `identificacion`; `diferencia = nuevo − odoo`. Requiere solo acceso de lectura.

## Fase 6 · `routers/saas.py`

| Método | Ruta |
|---|---|
| GET | `/plataforma/resumen` → `{clientes:{total, por_estado}, empleados_activos, mrr, tickets:{abiertos, plataforma, urgentes}, socios:{contador, vendedor}, alertas_pendientes}` |
| GET/PATCH | `/plataforma/empresas[/{id}]` (estado_servicio, plan). GET `?estado=&buscar=`; cada fila trae `empleados, tickets_abiertos, ultima_planilla, socios[], referido_por_nombre`; el detalle agrega `cliente` (negocio.clientes). PATCH de estado también sincroniza negocio.clientes |
| GET/POST/PATCH | `/plataforma/socios[/{id}]` · GET `?tipo=&estado=` · POST `{tipo, nombre, correo, cedula?, telefono?, iban?, certificado?, estado?, codigo_referido?, invitar=true}` vincula la cuenta si el correo ya existe o la invita (Supabase Admin); responde además `{cuenta_vinculada, invitado, enlace_registro}` · GET `/{id}` agrega `asignaciones[]`, `comisiones[]` |
| POST | `/plataforma/socios/{id}/asignar` `{empresa_id, funciones, permiso, soporte_principal}` (409 si ya está asignado; `soporte_principal` solo aplica si `funciones` incluye `soporte`) |
| POST | `/plataforma/parametros` `{vigente_desde, datos, fuente}`. `datos` puede ser parcial: se combina con el juego vigente y se valida con el motor (rangos, tramos contiguos, batería de boletas). 422 con los errores; 409 si ya existe esa vigencia; responde la fila + `validacion` |
| POST | `/empresas/{e}/empleados/{id}/invitar-portal` (admin) body opcional `{correo}` (si la ficha no tiene) → `{estado: "vinculado" \| "invitado", usuario_id, correo, enlace}` |
| GET | `/portal/perfil`, `/portal/boletas?anio=`, `/portal/boletas/{id}/pdf`, `/portal/vacaciones`; POST `/portal/vacaciones` `{dias, desde, hasta}`. Todas aceptan `empleado_id` opcional (si la persona trabaja en varias empresas; por defecto la primera ficha). Perfil: datos de la ficha + `empresa_nombre`, `empleos[]`. Boletas: solo planillas cerradas, con `desde, hasta, fecha_pago, tipo, empresa_nombre`. Vacaciones GET → `{empleado_id, saldo, solicitudes[], acumulaciones[]}`; POST 409 si pide más días que su saldo. 404 si el usuario no es empleado |

## Fase 7 · `routers/negocio.py` (solo dueña)

| Método | Ruta |
|---|---|
| GET | `/negocio/tablero?anio=` → MRR, clientes, cobrado, por cobrar, gastos, utilidad, IVA, reparto |
| CRUD | `/negocio/clientes` |
| POST | `/negocio/facturas/generar?periodo=YYYY-MM-01` → borradores del mes según empleados activos |
| GET | `/negocio/comprobantes?estado=&desde=&hasta=` · GET `/negocio/comprobantes/{id}` |
| POST | `/negocio/comprobantes/{id}/emitir` (clave, consecutivo, XML 4.4, firma, envío a Hacienda, correo) |
| POST | `/negocio/comprobantes/{id}/consultar` · GET `/{id}/xml` · GET `/{id}/pdf` · POST `/{id}/nota-credito` |
| GET/POST | `/negocio/pagos` |
| GET/POST/PATCH | `/negocio/gastos` · POST `/negocio/gastos/importar-xml` (multipart) |
| GET | `/negocio/categorias-gasto` |
| POST/GET | `/negocio/cierres/{anio}/{mes}` → resumen IVA (formulario D-104) |
| GET | `/negocio/renta/{anio}` → estado de resultados, base imponible, CCSS independiente |
| GET | `/negocio/reparto?anio=` |
| GET/PUT | `/negocio/configuracion/{clave}` |
| POST | `/negocio/comisiones/calcular?mes=` · GET `/negocio/comisiones?mes=&socio_id=` · POST `/negocio/liquidaciones-socio/generar?mes=` · POST `/negocio/liquidaciones-socio/{id}/pagar` |

Notas de implementación (Fase 7):

- Todo `/negocio/*` exige ser dueña de la plataforma: 403 para cualquier otro usuario, 401 sin sesión.
- Clientes: POST `{empresa_id?, razon_social, tipo_identificacion:"01|02|03|04", identificacion, correo_facturacion, telefono?, actividad_receptor?, precio_mensual="55000", moneda:"CRC|USD", precio_fundador_hasta?, empleados_incluidos=50, precio_bloque_10="11000", implementacion="100000", dia_cobro=1..28, estado, alta?, notas?}`. Las respuestas traen `precio_actual {empleados, bloques_adicionales, subtotal, iva, total, moneda, precio_fundador}` y `metodo_pago {marca, ultimos4, vence}`. GET `/{id}` agrega `cobros[]`, `pagos[]`, `comprobantes[]` y `pendientes[]` (meses vencidos sin pago). DELETE no borra: pasa el cliente a `baja`. Cambiar `estado` actualiza `empresas.estado_servicio` (trigger).
- Rutas agregadas: GET `/negocio/clientes/{id}/precio?periodo=`, GET `/negocio/cobros?cliente_id=&resultado=`, POST `/negocio/facturas/implementacion/{cliente_id}` (borrador de ₡100 000 + IVA), POST `/negocio/comprobantes/{id}/reenviar` (correo), POST `/negocio/comprobantes/{id}/anular-borrador`, GET `/negocio/configuracion` (todas), GET `/negocio/liquidaciones-socio?mes=`.
- Precio: base hasta `empleados_incluidos` + `precio_bloque_10` por cada 10 colaboradores adicionales o fracción (50 → ₡55 000; 51 → ₡66 000; 75 → ₡88 000) + IVA 13 %. Se cuentan los colaboradores activos durante el mes. Precio fundador: mientras `periodo <= precio_fundador_hasta` se cobra una tarifa plana en USD (el `precio_mensual` del cliente si su moneda es USD; si no, `precios.fundador_usd`). Al vencer pasa a colones con la tarifa de lista. Para USD hace falta `tipo_cambio_usd.valor`; si falta, responde 409.
- `facturas/generar?periodo=&incluir_tarjeta=false` → `{periodo, creadas[], existentes, errores[]}`. Es idempotente (un comprobante de suscripción por cliente y mes). Por defecto omite a los clientes con tarjeta, porque a ellos se les factura cuando el cobro sale aprobado.
- Emitir: responde 409 con un mensaje en español si falta algún dato del emisor (`configuracion.emisor`: nombre, tipo e identificación, correo, provincia, cantón, distrito, otras señas, código de actividad de 6 dígitos, sucursal de 3 dígitos y terminal de 5), un CABYS (13 dígitos; nunca se inventa) o las credenciales/.p12 de Hacienda. Después del envío el comprobante queda en `enviado`. Si Hacienda no responde queda en `error`, con `mensaje_estado`, y `/cron/facturas` lo reintenta hasta 5 veces. `consultar` lo pasa a `aceptado`/`rechazado`. Al quedar aceptado se envía por correo el PDF, el XML firmado y la respuesta de Hacienda. Un rechazo genera una alerta crítica. GET `/{id}/xml?respuesta=true` devuelve el MensajeHacienda.
- Nota de crédito: body `{razon, monto?(sin IVA; vacío = total), emitir=false}`. Solo procede sobre un comprobante `aceptado`. Si la nota es total y Hacienda la acepta, la factura original queda `anulada`.
- Pagos: POST `{cliente_id, comprobante_id?, fecha?, monto(con IVA), medio:"sinpe|transferencia|tarjeta|efectivo", concepto:"mensualidad|implementacion|otro", periodo?(obligatorio en mensualidad sin comprobante), subtotal?, referencia?, comision, retencion_iva, retencion_renta}`. Responde con el pago más `comisiones[]` y `estado_cliente`; si el cliente queda al día, se reactiva. Una referencia repetida da 409.
- Gastos: POST/PATCH `{fecha, proveedor, identificacion_proveedor?, categoria_id?, descripcion?, moneda, tipo_cambio, subtotal, iva, total?, iva_acreditable?, deducible?, clave_comprobante?}`. Si no se indican `iva_acreditable` ni `deducible`, se toman de la categoría. `importar-xml`: multipart con `archivo`, más `categoria_id?` y `mensaje_receptor?` (`aceptado|aceptado_parcial|rechazado`). Acepta 4.3 y 4.4 y responde con el gasto más `version`, `otros_impuestos` y `advertencias[]`. Una clave repetida da 409. La decisión del mensaje receptor se guarda, pero el MensajeReceptor **no** se envía a Hacienda.
- Cierres: GET devuelve el resumen guardado si el mes está cerrado y, si no, el cálculo en vivo (`estado: abierto`). POST cierra el mes y responde 409 si ya estaba cerrado (`?reabrir=true` recalcula). Formato: `{ventas:{gravadas_13, notas_credito, netas}, debito_fiscal, compras:{con_credito, total}, credito_fiscal, iva_no_acreditable, retenciones_iva_tarjeta, impuesto_por_pagar, saldo_a_favor, nota}`. Solo cuentan los comprobantes aceptados por Hacienda; los montos van en colones.
- Renta: `{ingresos, gastos_deducibles, comisiones_tarjeta, gastos_por_categoria[], utilidad, base_imponible, impuesto_estimado|null, retenciones_renta_tarjeta, impuesto_neto_estimado, ccss_independiente_estimada|null, advertencias[]}`. Los tramos de renta de persona física y el porcentaje de CCSS de trabajador independiente **no** vienen fijos en el código: se leen de `configuracion.renta` y deben confirmarse con el contador.
- Reparto: `{parametros, meses:[{mes, ingresos, gastos, utilidad, pct_salario, salario, caja, inversion, registrado}], totales}`. Los tramos van en `configuracion.reparto`: 50/60/70/75 % según la utilidad del mes, con un tope de ₡4 000 000 netos; el resto se divide entre caja e inversión (por defecto 50/50).
- Configuración PUT: body `{"valor": <json>}`. Claves permitidas: `emisor` (se fusiona con lo existente), `cabys_suscripcion`, `cabys_implementacion`, `comisiones`, `tarifa_tarjeta_pct`, `precios`, `cobro`, `tipo_cambio_usd`, `firma_politica`, `proveedor_sistemas`, `reparto` y `renta`.
- Comisiones: el vendedor recibe el 100 % del subtotal de las mensualidades 1 a 3 que el cliente pagó; el contador, el 20 % de cada mensualidad pagada mientras siga asignado, más ₡50 000 por la implementación. Las comisiones se generan al registrar cada pago (son idempotentes). `pagar` body `{referencia?, fecha?}` registra un gasto en «Comisiones de socios y vendedores» y responde 409 si ya estaba pagada.

## Fase 8 · `routers/autogestion.py` y `routers/socios.py`

| Método | Ruta |
|---|---|
| GET | `/empresas/{e}/suscripcion` → plan, precio, estado, método de pago, cobros, comprobantes |
| POST | `/empresas/{e}/suscripcion/metodo-pago` → `{url}` para registrar tarjeta en Tilopay |
| POST | `/webhooks/tilopay` · `/webhooks/telegram` (Telegram: header `X-Telegram-Bot-Api-Secret-Token`, 401 si no coincide; idempotente por `update_id` → `{ok, duplicado?}`) |
| POST | `/telegram/vincular` body opcional `{avisos:{boletas, recordatorios}}` → `{codigo, enlace, rol, vence_en}` (el código vence en 24 h; enlace `https://t.me/<TELEGRAM_BOT_USERNAME>?start=<codigo>`) |
| GET/POST | `/empresas/{e}/tickets` · GET/PATCH `/empresas/{e}/tickets/{id}` · POST `/{id}/mensajes` · POST `/{id}/escalar` — GET `?estado=abiertos\|<estado>`; POST `{asunto, mensaje?, prioridad?}` → ticket + `conversacion[]` + `ayuda` (el bot agrega artículos sugeridos); PATCH `{estado?, prioridad?, minutos_invertidos?, asunto?}` (el cliente solo cierra/reabre y cambia prioridad; `escalado` solo por /escalar); mensajes `{texto}`; escalar `{motivo?}` (contador asignado o admin; 409 si ya está en plataforma) |
| GET | `/ayuda/buscar?q=&limite=` → `{consulta, articulos:[{id,titulo,categoria,rango,fragmento}], respuesta_sugerida:{articulo_id,titulo,texto,resumen} \| null, confianza}` |
| GET | `/soporte/tickets?estado=&nivel=` (contador: los de sus clientes; dueña: escalados y todos). `estado` por defecto `abiertos`; `todos` o un estado. Orden: urgentes, escalados, plataforma, recientes. 403 si no es socio ni plataforma |
| CRUD | `/plataforma/articulos` `{titulo, contenido, categoria?, palabras_clave[], publicado}` (`?categoria=&buscar=`; PATCH sube `version`) |
| GET | `/plataforma/alertas?atendida=false&tipo=&limite=` · POST `/plataforma/alertas/{id}/atender` |
| GET/POST | `/plataforma/normativa` · POST `/plataforma/normativa/{id}/aprobar` · POST `/plataforma/normativa/{id}/rechazar` — POST `{fuente, enlace?, publicado?, resumen, parametros_propuestos?, vigente_desde?}`; aprobar body opcional `{parametros_propuestos?, vigente_desde?}`: valida con el motor, guarda `resultado_pruebas` (JSON; en GET también `resultado_pruebas_json`) y crea `parametros_legales` → estado `aplicado`; si no pasa: 409 `{detail, resultado_pruebas}` y queda `detectado`; rechazar `{motivo?}` |
| GET | `/plataforma/salud?horas=24` → `{ok, servicios (último chequeo por servicio), disponibilidad[], historial[]}` |
| POST | `/cron/diario`, `/cron/cobros`, `/cron/salud`, `/cron/facturas` (header `X-Cron-Secret`; 401 si falta o no coincide). `/cron/diario?fecha=&forzar_avisos=` (los avisos se envían una vez por día). `/cron/salud` → `{ok, servicios:[{servicio, ok, latencia_ms, detalle}]}` |
| GET/PATCH | `/socios/yo` → perfil, código de referido, enlace de registro, resumen de comisiones (`comisiones:{pendiente, liquidado, mes_actual, anio_actual}`, `clientes`, `prospectos_abiertos`, `telegram_vinculado`). PATCH `{telefono?, iban?, telegram_chat_id?, nombre?, cedula?}` (IBAN CR validado). 403 si no es socio activo |
| GET | `/socios/clientes` → empresas asignadas `[{empresa_id, nombre, estado_servicio, empleados, tickets_abiertos, funciones, permiso}]` (+ `plan, soporte_principal, desde, ultima_planilla`) |
| GET | `/socios/comisiones?mes=AAAA-MM&estado=` · `/socios/liquidaciones` · `/socios/tickets?estado=` (mismo formato que /soporte/tickets) |
| GET/POST | `/socios/prospectos` POST `{empresa_nombre, contacto?, correo?, telefono?}` → atribución 90 días; 409 si otro socio tiene ese prospecto abierto y vigente (comparación sin tildes ni mayúsculas) o si ya lo tiene registrado |

Notas de cobros (implementados en `routers/negocio.py`):

- GET `/empresas/{e}/suscripcion` (solo admin de **su** empresa; 404 para las demás) → `{empresa_id, empresa, plan, estado, mensaje, cliente:{razon_social, identificacion, correo_facturacion, dia_cobro, alta, moneda, precio_fundador_hasta, gracia_desde}|null, precio:{periodo, empleados, empleados_incluidos, bloques_adicionales, subtotal, iva, total, moneda, precio_fundador}, proximo_cobro, metodo_pago:{id, proveedor, marca, ultimos4, vence, estado, creado_en}|null, cobros[], comprobantes[]}`. Nunca expone el token de la tarjeta. Si la empresa todavía no tiene ficha de cliente, el precio se estima con la tarifa de lista. Rutas agregadas: GET `/empresas/{e}/suscripcion/comprobantes/{id}/pdf` y `/xml`.
- POST `/empresas/{e}/suscripcion/metodo-pago` (admin) → `{url}` del formulario de Tilopay. Si falta, crea la ficha de cliente con los datos de la empresa; sin cédula registrada responde 409. Si Tilopay no está configurado, 409; si no responde, 502.
- GET `/webhooks/tilopay/retorno?estado=…` (ruta agregada; es el `redirect` que recibe Tilopay): guarda el token, los últimos 4 dígitos y la marca, y redirige con 303 a `APP_URL/empresas/{e}/suscripcion?tarjeta=ok|error`. Si el cliente debe meses, cobra de inmediato. El `estado` va firmado con HMAC y vence en 1 h (400 si es inválido). **Pendiente con Tilopay:** sus docs no dicen con qué nombre de parámetro llega el token en este retorno. Se aceptan `card`, `token`, `card_token`, `cardToken` y `crd`, y para los últimos 4 dígitos `last` o `last4`.
- POST `/webhooks/tilopay`: Tilopay no publica una firma de webhook (`orderHash` "not public"), así que se exige un secreto propio en `?secreto=` o en el header `X-Webhook-Secret` (= `TILOPAY_WEBHOOK_SECRET`); si no coincide, 401. Cada pago se confirma con `POST /api/v1/consult` antes de aplicarlo. Responde `{ok, evento_id, aplicado}` y es idempotente.
- POST `/cron/cobros?fecha=` → `{fecha, clientes:[{cliente_id, razon_social, intentos[], estado, anterior, cambio, dias_atraso, pendientes[]}], resumen, aprobados, rechazados, errores[]}`. Una vez al día cobra con la tarjeta guardada desde `dia_cobro` y reintenta los días +1, +3 y +5. Estados por días de atraso: 0–6 `gracia`, 7–29 `solo_lectura`, 30 o más `suspendido`; vuelve a `activo` al pagar. Todo se configura en `configuracion.cobro`. Cada cobro aprobado genera el pago (comisión de tarjeta según `tarifa_tarjeta_pct`, 5 %), la factura con medio 02, su envío a Hacienda y las comisiones de socios.
- POST `/cron/facturas?fecha=` → `{creadas, emitidas, consultadas, aceptadas, rechazadas, errores[]}`. Hace borradores del mes para los clientes sin tarjeta (excepto los suspendidos), emite los borradores y los que quedaron en `error` (hasta 5 intentos) y consulta los que están `enviado`.

## Módulos por empresa · `routers/empresas.py`

Cada empresa activa los módulos que usa (Planilla, Contabilidad). Con un módulo apagado se
puede consultar y exportar lo que existe, pero no registrar (403 con `X-Aurom-Error: modulo_inactivo`;
la base aplica la misma regla con RLS). Siempre queda al menos un módulo activo.

| Método | Ruta | Quién |
|---|---|---|
| GET | `/modulos` | cualquiera con sesión: catálogo y precio de lista |
| GET | `/empresas/{id}/modulos` | rol de la empresa: activos e historial |
| PUT | `/empresas/{id}/modulos/{planilla\|contabilidad}` `{activo}` | administrador (MFA) o plataforma |
| POST | `/empresas` `{..., modulos: [...]}` | alta eligiendo módulos (por defecto Planilla) |

Cobro: un módulo se factura completo el mes en que estuvo activo algún día. Planilla por colaboradores
(₡55 000 hasta 50 + ₡11 000 por cada 10); Contabilidad por asientos del mes anterior (₡45 000 hasta 300
+ ₡7 500 por cada 100). Precio propio por cliente en `negocio.clientes.precios_modulos`.
`precio_cliente()` devuelve `modulos: [...]` con el detalle de cada uno y la factura lleva una línea por módulo.

## Módulo Contabilidad · `routers/contabilidad.py` (prefijo `/empresas/{id}/contabilidad`)

| Método | Ruta | Notas |
|---|---|---|
| GET | `/resumen` | activos, pasivos, patrimonio, caja, resultado del mes y del año, pendientes |
| GET/POST | `/cuentas` | catálogo en árbol con saldo; subcuentas (`padre_id`) |
| PATCH/DELETE | `/cuentas/{id}` | con movimientos no cambia tipo ni se borra |
| GET | `/diarios` | GEN, PLA, VEN, COM, BAN, AJU, APE, CIE |
| GET/POST | `/asientos` | filtros: estado, diario, origen, fechas, buscar, cuenta; `contabilizar: true` |
| GET/PUT/DELETE | `/asientos/{id}` | PUT y DELETE solo en borrador |
| POST | `/asientos/{id}/contabilizar` | valida partida doble, cuentas de detalle y periodo abierto; asigna número |
| POST | `/asientos/{id}/revertir` `{fecha?, motivo}` | crea el asiento contrario; nada se borra |
| GET | `/periodos?anio=` | 12 meses con estado |
| POST | `/periodos/{anio}/{mes}/cerrar` · `/reabrir` `{motivo}` | aprobador con MFA |
| POST | `/cierre-anual/{anio}` · `/cierre-anual/{anio}/reabrir` | resultados a 3301 y año bloqueado |
| GET | `/reportes/{diario\|mayor\|comprobacion\|resultados\|balance}` | JSON |
| GET | `/reportes/{tipo}/exportar?formato=xlsx\|pdf` | Excel o PDF |

Integración: al cerrar una planilla su asiento entra solo al diario PLA (cuentas por código; si falta
alguna se crea en su grupo y queda una advertencia). Al reabrirla se revierte.
