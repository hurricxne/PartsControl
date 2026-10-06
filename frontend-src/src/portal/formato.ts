// Formato de lo que el cliente LEE en el portal: montos, cantidades, fechas y plazos.
// Módulo puro: no importa React y ninguna función mira el reloj del dispositivo para
// decidir algo (fmtActualizado solo convierte un instante a la hora local).
//
// TRES TRAMPAS QUE ESTE ARCHIVO EXISTE PARA EVITAR
//   · Fechas. El backend manda el DÍA de Chile como texto 'AAAA-MM-DD'. Un
//     `new Date('2026-08-12')` lo lee como medianoche UTC y en Chile (UTC−4/−3) lo pinta
//     como el 11. Acá el texto se PARTE; nunca pasa por Date para mostrarse.
//   · Plazos. «en 3 días» se calcula contra el `hoy` que trae cada respuesta (Sello.hoy
//     en types.ts), no contra el reloj del teléfono, que en faena puede estar mal puesto.
//   · Montos. null no es cero: es «esta cuenta no ve montos» o «la orden no tiene precio
//     congelado», y se pinta una raya. utils/format.ts::fmtClp (el del ERP) no sirve acá:
//     pinta $0 para null, redondea el unitario y no dice la moneda.

const SIN_DATO = '—'
// Espacio que no se corta: «$1.037.040 CLP» no puede quedar con «CLP» solo en la línea
// de abajo, tampoco fuera de una celda .portal-cifra.
const ESPACIO_FIJO = ' '
const MS_POR_DIA = 86_400_000
const MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']

/**
 * 1234567 → «1.234.567». A mano y no con toLocaleString('es-CL'): el resultado de Intl
 * depende de los datos de idioma del dispositivo (un Android antiguo sin es-CL responde
 * «1,234,567») y, en español, hay versiones que no separan los miles de un número de
 * cuatro cifras («1234»).
 */
function conMiles(entero: string): string {
  return entero.replace(/\B(?=(\d{3})+(?!\d))/g, '.')
}

/**
 * Pesos chilenos: «$1.234.567 CLP». Con `decimales` muestra los centavos solo si los hay
 * («$86.419,57 CLP», pero «$86.419 CLP» y no «$86.419,00 CLP»): es para el precio
 * unitario, que el backend manda con 2 decimales. null/undefined → «—», nunca $0.
 */
export function fmtMonto(n: number | null | undefined, opciones?: { decimales?: boolean }): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return SIN_DATO
  const [entero, centavos] = opciones?.decimales
    ? Math.abs(n).toFixed(2).split('.')
    : [String(Math.round(Math.abs(n))), '00']
  // El signo se decide DESPUÉS de redondear: −0,4 es «$0», no «-$0».
  const esCero = entero === '0' && centavos === '00'
  const signo = n < 0 && !esCero ? '-' : ''
  const fraccion = centavos === '00' ? '' : `,${centavos}`
  return `${signo}$${conMiles(entero)}${fraccion}${ESPACIO_FIJO}CLP`
}

/** Cantidad de un ítem: 4 → «4»; 2.5 → «2,5»; 1200 → «1.200». Hasta 3 decimales. */
export function fmtCantidad(n: number): string {
  if (!Number.isFinite(n)) return SIN_DATO
  const [entero, fraccion = ''] = Math.abs(n).toFixed(3).split('.')
  const decimales = fraccion.replace(/0+$/, '')
  const esCero = entero === '0' && decimales === ''
  return `${n < 0 && !esCero ? '-' : ''}${conMiles(entero)}${decimales ? `,${decimales}` : ''}`
}

/** «3 pedidos», «1 ítem»: la cantidad con su palabra en singular o plural. */
export function contar(n: number, singular: string, plural: string): string {
  return `${fmtCantidad(n)} ${n === 1 ? singular : plural}`
}

interface Dia {
  anio: number
  mes: number // 1-12
  dia: number
}

/**
 * Parte un 'AAAA-MM-DD'. Estricto a propósito: si un día llegara una fecha con hora
 * ('2026-08-12T03:00:00Z'), sus primeros 10 caracteres serían el día en UTC, no el de
 * Chile. Mejor que no calce y se vea el texto crudo a pintar un día equivocado.
 */
function partir(iso: string | null | undefined): Dia | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? '')
  if (!m) return null
  const anio = Number(m[1])
  const mes = Number(m[2])
  const dia = Number(m[3])
  if (mes < 1 || mes > 12 || dia < 1 || dia > 31) return null
  return { anio, mes, dia }
}

/** «12 ago 2026». null → «—». Un texto que no es AAAA-MM-DD se devuelve tal cual. */
export function fmtFecha(iso: string | null): string {
  if (!iso) return SIN_DATO
  const d = partir(iso)
  return d ? `${d.dia} ${MESES[d.mes - 1]} ${d.anio}` : iso
}

/** «12 ago» si la fecha es del año de `hoy`; si no, «12 ago 2025». null → «—». */
export function fmtFechaCorta(iso: string | null, hoy: string): string {
  if (!iso) return SIN_DATO
  const d = partir(iso)
  if (!d) return iso
  const mismoAnio = partir(hoy)?.anio === d.anio
  return mismoAnio ? `${d.dia} ${MESES[d.mes - 1]}` : `${d.dia} ${MESES[d.mes - 1]} ${d.anio}`
}

/**
 * Días de calendario entre dos 'AAAA-MM-DD' (hasta − desde): positivo si `hasta` es
 * posterior. Date.UTC se usa como calculadora de calendario (no hay zona ni cambio de
 * hora de por medio: los dos argumentos ya son días). Devuelve NaN si alguno no es una
 * fecha: quien pinta un plazo usa fmtPlazo, que en ese caso no escribe nada.
 */
export function diasEntre(desde: string, hasta: string): number {
  const a = partir(desde)
  const b = partir(hasta)
  if (!a || !b) return NaN
  const msDesde = Date.UTC(a.anio, a.mes - 1, a.dia)
  const msHasta = Date.UTC(b.anio, b.mes - 1, b.dia)
  return Math.round((msHasta - msDesde) / MS_POR_DIA)
}

/**
 * Cuánto falta o cuánto pasó respecto de `hoy` (el del backend): «hoy», «mañana»,
 * «en 3 días», «ayer», «hace 2 días». Sin fecha → '' (el llamador no escribe nada).
 */
export function fmtPlazo(fecha: string | null, hoy: string): string {
  if (!fecha) return ''
  const dias = diasEntre(hoy, fecha)
  if (!Number.isFinite(dias)) return ''
  if (dias === 0) return 'hoy'
  if (dias === 1) return 'mañana'
  if (dias === -1) return 'ayer'
  return dias > 0 ? `en ${dias} días` : `hace ${-dias} días`
}

/**
 * «Actualizado a las 14:32», en la hora local del dispositivo. Acá sí se usa Date: lo que
 * llega es un INSTANTE (ISO en UTC, con Z), no un día. La hora se arma a mano para que
 * salga siempre en 24 horas, diga lo que diga el idioma del teléfono. Si el texto no es
 * un instante válido devuelve '' (la pantalla muestra solo el botón Recargar).
 */
export function fmtActualizado(actualizadoAt: string): string {
  const instante = new Date(actualizadoAt)
  if (Number.isNaN(instante.getTime())) return ''
  const horas = String(instante.getHours()).padStart(2, '0')
  const minutos = String(instante.getMinutes()).padStart(2, '0')
  return `Actualizado a las ${horas}:${minutos}`
}

/**
 * Las cantidades llegan como decimales (unidades partidas, kilos) y restarlas deja
 * residuos de punto flotante: una diferencia menor que esto es cero. Es la misma
 * tolerancia del backend (portal/valor.py::TOLERANCIA).
 */
export const TOLERANCIA_DE_CANTIDAD = 0.001

/** «atrasado 3 días» / «atrasado» (si venció hoy mismo o el día no se puede contar). */
export function fmtAtraso(fecha: string, hoy: string): string {
  const dias = diasEntre(fecha, hoy)
  return dias > 0 ? `atrasado ${contar(dias, 'día', 'días')}` : 'atrasado'
}
