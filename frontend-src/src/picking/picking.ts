/**
 * Picking & Packing en Despachos MachParts (Grupo AM) — SOLO funciones puras.
 *
 * Sin React acá (precedente: monza-agrupacion/agrupacion.ts): filtrar las líneas
 * de la OC para el buscador de picking, contar la selección para la barra fija
 * del modal y armar el texto plano del reparto de bultos que se le manda al
 * transportista. DespachosPage pinta sus propias filas con estos resultados.
 *
 * Reglas de la marca (GA / minería):
 *  · Las cantidades aceptan DECIMALES legales (metros, kilos): las sumas se
 *    hacen SIN redondear y el texto solo omite el ".0" de los enteros.
 *  · Mínimo 1 unidad por línea marcada (regla existente del modal de crear
 *    despacho: un 0 dejaría la línea "seleccionada" para chocar con el 400).
 *  · Vocabulario: lo que no va en el despacho "queda pendiente" — las palabras
 *    "parcial" y "faltante" ya significan otra cosa en la firma de la guía.
 */

// ── Colapsado (contrato con el backend) ───────────────────────────────────────

/**
 * Forma colapsada del número de parte: sin guiones ni espacios, mayúsculas.
 *
 * CONTRATO: espejo exacto de `_colapsar` en backend/routers/despachos.py:171-175
 * (`re.sub(r"[-\s]", "", tok).upper()`). 7T1997 y 7T-1997 conviven como filas
 * distintas en la base (excel_service solo recorta), así que la pasada B compara
 * ambas formas colapsadas. Si el backend cambia la regla, cambiar AQUÍ también:
 * un colapsado distinto haría que el buscador local encuentre cosas que el
 * buscador del servidor no (o al revés) y el operador reporta "búsquedas locas".
 * Ojo: NO se eliminan puntos — el backend tampoco lo hace.
 */
export function colapsar(texto: string): string {
  return texto.replace(/[-\s]/g, '').toUpperCase()
}

// ── Cupo despachable (contrato con el backend) ────────────────────────────────

/**
 * Tolerancia de cantidad del cupo: por debajo de esto NO hay nada que despachar.
 *
 * CONTRATO: espejo exacto de `_TOL_QTY` / `_es_despachable` en
 * backend/routers/despachos.py (`disponible > 0.001`), que ese módulo declara
 * obligatorio para TODO punto de visibilidad del disponible. Si el backend
 * cambia la tolerancia, cambiar AQUÍ también.
 */
export const TOL_QTY_DESPACHABLE = 0.001

/**
 * ¿Esta cantidad disponible es cupo REAL, o ruido flotante?
 *
 * POR QUÉ EXISTE: el frontend comparaba `qty_disponible > 0` mientras el backend
 * ya usaba la tolerancia. Una línea decimal despachada en tandas (0.2 + 0.7 de
 * 0.9) deja un residuo binario de 1.1e-16: con `> 0` la pantalla ofrecía la
 * línea, precargaba «1.1102230246251565e-16» y dejaba crear un despacho zombi
 * que la guía 52 nunca puede emitir (armar_lineas descarta qty <= TOL_QTY),
 * justo mientras la insignia de la MISMA card decía «0 un. por despachar».
 *
 * El backend sigue siendo el guardián (clampa la fórmula y valida al crear);
 * esta función es el ASESOR de la pantalla, y usa el mismo umbral para que las
 * dos no puedan volver a contradecirse. Un valor no finito (NaN de un backend
 * viejo) NO es cupo.
 */
export function esDespachable(qtyDisponible: number): boolean {
  return Number.isFinite(qtyDisponible) && qtyDisponible > TOL_QTY_DESPACHABLE
}

// ── Filtro de picking (doble pasada) ──────────────────────────────────────────

/** Lo mínimo que una línea necesita para ser filtrable. */
export interface PickingFiltrable {
  numero_parte: string
  descripcion: string
}

export type CampoPicking = 'numero_parte' | 'descripcion'

export interface PickingMatch<T extends PickingFiltrable> {
  item: T
  /** true = el acierto vino de la pasada colapsada (no hay fragmento literal que
   *  subrayar: se resalta el campo COMPLETO y se avisa "también busqué X"). */
  porColapsado: boolean
  /** Solo cuando porColapsado: qué campos acertaron en forma colapsada. */
  camposColapsados: CampoPicking[]
}

export interface ResultadoFiltro<T extends PickingFiltrable> {
  matches: PickingMatch<T>[]
  /** true si algún acierto vino de la pasada colapsada → aviso "también busqué X". */
  huboColapsado: boolean
  /** Forma colapsada de la query, para el texto del aviso. */
  queryColapsada: string
}

/**
 * Filtro local de picking sobre numero_parte y descripcion (exactamente los dos
 * campos que promete el placeholder — el placeholder es contrato), en DOS pasadas
 * POR LÍNEA:
 *  A) literal, case-insensitive (subcadena tal cual se tecleó);
 *  B) colapsada EN LOS DOS SENTIDOS: al colapsar query Y campo, 7T1997 encuentra
 *     7T-1997 y 7T-1997 encuentra 7T1997 (y "rodillo inf" pega en "Rodillo
 *     inferior" aunque el espacio no coincida).
 * La pasada B solo decide cuando la A no pegó en esa línea, para que el resaltado
 * literal (fragmento) tenga prioridad sobre el resaltado de campo completo.
 * Query vacía (o solo espacios) → todas las líneas, en su orden original.
 */
export function filtrarItems<T extends PickingFiltrable>(
  items: T[],
  query: string,
): ResultadoFiltro<T> {
  const q = query.trim()
  if (!q) {
    return {
      matches: items.map(item => ({ item, porColapsado: false, camposColapsados: [] })),
      huboColapsado: false,
      queryColapsada: '',
    }
  }

  const qLower = q.toLowerCase()
  const qColapsada = colapsar(q)
  const matches: PickingMatch<T>[] = []
  let huboColapsado = false

  for (const item of items) {
    const np = item.numero_parte || ''
    const desc = item.descripcion || ''

    // Pasada A: literal, case-insensitive.
    if (np.toLowerCase().includes(qLower) || desc.toLowerCase().includes(qLower)) {
      matches.push({ item, porColapsado: false, camposColapsados: [] })
      continue
    }

    // Pasada B: colapsada en los dos sentidos (query y campo colapsados).
    if (!qColapsada) continue // query de puros guiones/espacios: nada que comparar
    const camposColapsados: CampoPicking[] = []
    if (colapsar(np).includes(qColapsada)) camposColapsados.push('numero_parte')
    if (colapsar(desc).includes(qColapsada)) camposColapsados.push('descripcion')
    if (camposColapsados.length > 0) {
      matches.push({ item, porColapsado: true, camposColapsados })
      huboColapsado = true
    }
  }

  return { matches, huboColapsado, queryColapsada: qColapsada }
}

// ── Contadores de la selección ────────────────────────────────────────────────

export interface ContadoresSeleccion {
  lineasMarcadas: number
  lineasTotal: number
  /** Σ cantidades marcadas SIN redondear (GA acepta decimales legales). */
  unidadesTotales: number
}

/** "Marcadas X de Y líneas · Z unidades" — SIEMPRE sobre la selección COMPLETA,
 *  nunca sobre lo que el filtro deja visible (el contador no puede mentir
 *  porque el operador tenga texto en la caja de búsqueda). */
export function contarSeleccion(
  seleccion: Record<number, number>,
  lineasTotal: number,
): ContadoresSeleccion {
  const cantidades = Object.values(seleccion)
  return {
    lineasMarcadas: cantidades.length,
    lineasTotal,
    unidadesTotales: cantidades.reduce((s, q) => s + q, 0),
  }
}

/**
 * Cuántas líneas van a VIAJAR de verdad en el documento tributario.
 *
 * POR QUÉ NO SIRVE `lineasMarcadas`: ese cuenta CLAVES de la selección, y una
 * línea marcada con cantidad 0 —o con un texto a medio tipear, que vale 0— es
 * una clave más que el DTE nunca va a llevar: `armar_lineas` del backend
 * descarta toda cantidad <= TOL_QTY. Para avisar del tope de 10 ítems de la vía
 * SII gratuito hay que contar lo MISMO que cuenta el emisor, o el aviso salta de
 * más (o peor, no salta) justo en el borde de 10/11.
 *
 * POR QUÉ `idsConCupo` ES OBLIGATORIO: una línea que perdió su cupo mientras el
 * modal estaba abierto (otro despacho se lo llevó) SIGUE en la selección con su
 * cantidad intacta, pero jamás va a viajar — el envío queda bloqueado hasta
 * quitarla, y al quitarla el documento tiene una línea MENOS. Contándola, este
 * número mentía dos veces: disparaba el aviso del tope justo en el borde 10/11 y
 * no cuadraba con el «M líneas» del resumen dos bandas más abajo.
 * Se pide la lista POSITIVA (los ids que SÍ tienen cupo) y no la de excluidos,
 * por dos razones: es la misma forma que ya usa `contarMarcadasOcultas` en este
 * módulo, y un llamador que se olvide de armarla obtiene 0 —un número que salta
 * a la vista— y no un conteo inflado en silencio.
 */
export function contarLineasQueViajan(
  seleccion: Record<number, number>,
  idsConCupo: ReadonlyArray<number>,
): number {
  const conCupo = new Set(idsConCupo)
  return Object.entries(seleccion).filter(
    ([id, q]) => conCupo.has(Number(id)) && Number.isFinite(q) && q > TOL_QTY_DESPACHABLE,
  ).length
}

/** Cuántas líneas MARCADAS quedaron fuera de la vista actual (por la búsqueda o
 *  por el toggle "Ocultar marcadas") — para el "(N marcadas ocultas por el
 *  filtro)" del contador, que evita el clásico "se me borró la selección". */
export function contarMarcadasOcultas(
  seleccion: Record<number, number>,
  idsVisibles: ReadonlyArray<number>,
): number {
  const visibles = new Set(idsVisibles)
  return Object.keys(seleccion).filter(id => !visibles.has(Number(id))).length
}

// ── Cantidades en texto ───────────────────────────────────────────────────────

/** "3" y no "3.0" para los enteros; los decimales legales se conservan.
 *  El toFixed(6)→Number→String normaliza SOLO la representación: la suma en
 *  binario de decimales legales imprime ruido flotante (1.1 + 2.2 =
 *  "3.3000000000000003") y ese texto se copiaba al transportista. Las sumas y
 *  el payload NO se tocan: siguen sin redondear. */
export function fmtQty(n: number): string {
  if (Number.isInteger(n)) return n.toFixed(0)
  const r = Number(n.toFixed(6))
  // Residuo real menor que el redondeo (< 5e-7): mostrar el valor crudo antes
  // que un "0" que miente — la cantidad existe aunque sea microscópica.
  return String(r === 0 && n !== 0 ? n : r)
}

// ── Reparto de bultos (texto para el transportista) ───────────────────────────

export interface BultoItemLinea {
  numero_parte: string
  descripcion: string
  /** SIEMPRE qty_despachada: el mail al transportista se manda ANTES del viaje;
   *  qty_firmada y el faltante de entrega ocurren DESPUÉS y NO participan aquí. */
  qty: number
}

export interface BultoDespacho {
  numero_despacho: string
  /** null/"" = guía aún no emitida → "Guía N° PENDIENTE" + aviso ámbar. */
  numero_guia: string | null
  /** null/"" = va a la sección final "SIN BULTO ASIGNADO". */
  bulto_numero: string | null
  /**
   * Estado crudo del despacho. Es el EJE del reparto:
   *  · 'en_preparacion' = está en bodega y VA A SALIR;
   *  · 'despachado'     = ya salió (botón «Confirmar»).
   * La FIRMA no sirve como criterio: se marca DESPUÉS de la salida, a veces días
   * más tarde o nunca, así que un despacho que partió la semana pasada y cuya
   * guía firmada no volvió seguiría colándose en el correo de hoy.
   * Estado desconocido/vacío (backend viejo) → se trata como POR SALIR: este
   * texto JAMÁS esconde una caja en silencio (ver `vaASalir`).
   */
  estado: string | null
  /**
   * ¿El despacho se CERRÓ hoy? Un despacho que el operador confirmó antes de
   * redactar el mail sigue siendo la caja de hoy y no puede caer al histórico.
   *
   * Es un VEREDICTO DEL SERVIDOR, no una cuenta de fechas: el cierre se estampa
   * en UTC y sin zona, así que ningún reloj de navegador puede decidir en qué
   * día de Chile cayó (a las 20:30 de Chile el ISO ya dice «mañana»). El
   * llamador se limita a transportar el campo que manda el backend.
   *
   * `null` = el backend NO informó el campo (contrato viejo). Desconocido, y el
   * desconocido nunca esconde una caja: ver `vaASalir`.
   */
  cerradoHoy: boolean | null
  /** Fecha de salida (cierre), ya formateada por el llamador (dd-mm-aaaa) o null. */
  fechaSalida: string | null
  /** Fecha de firma del cliente, ya formateada (dd-mm-aaaa) o null. */
  fechaFirma: string | null
  /** Guía firmada por el cliente (puede estar firmada sin fecha cargada). */
  guiaFirmada: boolean
  items: BultoItemLinea[]
}

export interface ResumenBultosInput {
  numero_oc: string
  cliente: string
  direccion: string | null
  /** Ya formateada por el llamador (dd-mm-aaaa): acá no se tocan fechas ni zonas. */
  fecha: string
  /** Solo despachos NO anulados (los filtra el llamador). */
  despachos: BultoDespacho[]
}

export interface ResumenBultos {
  /** Texto plano listo para copiar: indentación con espacios, guiones ASCII,
   *  sin tabs, cantidades enteras sin ".0". */
  texto: string
  /** Rótulos de bulto distintos de lo que VA A SALIR, SIN contar la sección
   *  "SIN BULTO ASIGNADO". Los totales de cabecera son de POR SALIR: sumar lo
   *  que ya viajó hacía que el transportista fuera a buscar cajas inexistentes. */
  totalBultos: number
  /** 1 guía por despacho POR SALIR (emitida o pendiente). */
  totalGuias: number
  /** Σ qty de las líneas que VAN A SALIR, sin redondear. */
  totalUnidades: number
  /** Guías sin emitir ENTRE LAS QUE VAN A SALIR (las que ya viajaron no se
   *  arreglan desde este modal). */
  hayGuiasPendientes: boolean
  haySinBulto: boolean
  /** Grupos de rótulos que empatan al ignorar mayúsculas ( ["b2","B2"] ): casi
   *  seguro son la MISMA caja rotulada dos veces distinto. NO se unifican (ver
   *  rotuloDe) — el modal solo avisa para que se corrija en Editar despacho.
   *  Solo entre lo que va a salir: un empate contra un despacho de julio no es
   *  un error que corregir hoy. */
  colisionesRotulos: string[][]
  /** Cuántos despachos de la OC quedaron en la sección "YA DESPACHADO" (0 = el
   *  reparto es todo de hoy). El modal lo usa para avisar del histórico. */
  yaViajaron: number
  /** Σ qty de lo que ya viajó — se muestra APARTE y rotulado, nunca sumado. */
  unidadesYaViajaron: number
}

/** Rótulo normalizado del bulto: recortado; vacío = sin bulto. Se agrupa por el
 *  texto exacto que tipeó el operador (no se fuerza mayúscula: si rotuló "b2" y
 *  "B2" en dos despachos, que lo vea y lo corrija — unificar en silencio
 *  escondería el error de rotulado). */
function rotuloDe(d: BultoDespacho): string {
  return (d.bulto_numero || '').trim()
}

/** Estado del despacho que ya salió de bodega (espejo del botón «Confirmar»,
 *  backend/routers/despachos.py). */
const ESTADO_YA_SALIO = 'despachado'

/**
 * ¿Este despacho entra al correo de HOY o ya viajó?
 *
 * ÚNICA implementación del criterio (el modal no re-decide nada). El eje es el
 * ESTADO y no la firma; el cerrado de HOY sigue siendo "por salir" porque el
 * operador puede apretar «Confirmar» antes de redactar el mail y el documento
 * quedaría sin la caja de hoy. Estado desconocido → POR SALIR: preferimos
 * mostrarlo de más (y rotulado) antes que esconder una caja en silencio.
 */
function vaASalir(d: BultoDespacho): boolean {
  if (d.estado !== ESTADO_YA_SALIO) return true
  // `!== false`: al histórico solo se va con un "no" EXPLÍCITO del servidor.
  // `null` (el backend no informó `cerradoHoy`) es una DUDA, y la duda se
  // resuelve mostrando la caja — la misma regla que ya rige para el estado
  // desconocido. Esconder una caja por una duda es el error caro.
  return d.cerradoHoy !== false
}

interface GrupoBultos {
  /** Rótulos distintos ordenados naturalmente ("1" < "B2" < "B10"), sin el "". */
  rotulos: string[]
  porBulto: Map<string, BultoDespacho[]>
  haySinBulto: boolean
}

/** Agrupa por rótulo de bulto, con orden alfanumérico NATURAL; el sin-bulto ("")
 *  se saca del sort y se anexa al final. */
function agruparPorBulto(despachos: BultoDespacho[]): GrupoBultos {
  const porBulto = new Map<string, BultoDespacho[]>()
  for (const d of despachos) {
    const rotulo = rotuloDe(d)
    const grupo = porBulto.get(rotulo)
    if (grupo) porBulto.set(rotulo, [...grupo, d])
    else porBulto.set(rotulo, [d])
  }
  const rotulos = [...porBulto.keys()]
    .filter(r => r !== '')
    .sort((a, b) => a.localeCompare(b, 'es', { numeric: true, sensitivity: 'base' }))
  return { rotulos, porBulto, haySinBulto: porBulto.has('') }
}

function sumarUnidades(despachos: BultoDespacho[]): number {
  return despachos.reduce((s, d) => s + d.items.reduce((si, it) => si + it.qty, 0), 0)
}

/** "1 bulto" / "3 bultos" — el plural del castellano de estas tres palabras es
 *  siempre + "s" (bulto, guía, unidad no se usa en singular acá). */
function plural(n: number, singular: string): string {
  return `${n} ${singular}${n === 1 ? '' : 's'}`
}

/**
 * Sello de estado de UN despacho, para que dos guías con el mismo formato no
 * parezcan la misma cosa: el transportista tiene que poder distinguir la caja
 * que va a cargar de la que entregó en julio.
 */
function selloDe(d: BultoDespacho): string {
  const partes: string[] = []
  if (d.estado === ESTADO_YA_SALIO) {
    if (d.cerradoHoy === true) partes.push(d.fechaSalida ? `cerrado hoy ${d.fechaSalida}` : 'cerrado hoy')
    else if (d.cerradoHoy === false) partes.push(d.fechaSalida ? `salió ${d.fechaSalida}` : 'ya salió de bodega')
    // Desconocido (backend viejo): se dice el HECHO —el despacho está cerrado—
    // sin afirmar "hoy" ni "salió antes", que es justo lo que no se sabe. La
    // caja igual aparece en POR SALIR (vaASalir), rotulada con su fecha para
    // que el transportista distinga la de julio de la que va a cargar.
    else partes.push(d.fechaSalida ? `cerrado ${d.fechaSalida}` : 'cerrado')
  } else if (d.estado === 'en_preparacion') {
    partes.push('en preparación')
  } else if (d.estado) {
    // Estado que este texto no conoce: se ROTULA tal cual (nunca se esconde ni
    // se traduce a un estado que no es).
    partes.push(d.estado)
  }
  if (d.fechaFirma) partes.push(`firmada ${d.fechaFirma}`)
  else if (d.guiaFirmada) partes.push('firmada')
  return partes.length > 0 ? ` - ${partes.join(' - ')}` : ''
}

/**
 * Arma el texto plano del reparto, PARTIDO EN DOS SECCIONES rotuladas:
 * "POR SALIR" (lo que el transportista va a cargar) y "YA DESPACHADO"
 * (histórico de la OC, que se muestra pero NO suma a los totales).
 *
 * POR QUÉ: antes salían todos juntos con el mismo formato y la fecha de HOY en
 * la cabecera, así que una OC con tres entregas de julio y una caja nueva le
 * decía al transportista «4 bultos»: iba a buscar cuatro y encontraba una.
 * Nada se excluye en silencio (el operador puede haber cerrado el despacho
 * antes de escribir el mail, y el histórico es información legítima de la OC).
 */
export function armarResumenBultos(input: ResumenBultosInput): ResumenBultos {
  const porSalir = input.despachos.filter(vaASalir)
  const yaViajaron = input.despachos.filter(d => !vaASalir(d))

  const gSalir = agruparPorBulto(porSalir)
  const gViejos = agruparPorBulto(yaViajaron)

  // Colisión b2/B2: mismo rótulo al bajar a minúsculas, distinto tal cual. Se
  // DETECTA sin unificar (la decisión de rotuloDe se conserva: unificar en
  // silencio escondería el error de rotulado) y el modal pinta el aviso.
  const porMinuscula = new Map<string, string[]>()
  for (const r of gSalir.rotulos) {
    const clave = r.toLocaleLowerCase('es')
    porMinuscula.set(clave, [...(porMinuscula.get(clave) ?? []), r])
  }
  const colisionesRotulos = [...porMinuscula.values()].filter(g => g.length > 1)

  const totalGuias = porSalir.length
  const totalUnidades = sumarUnidades(porSalir)
  const unidadesYaViajaron = sumarUnidades(yaViajaron)
  const hayGuiasPendientes = porSalir.some(d => !(d.numero_guia || '').trim())

  const lineas: string[] = []
  lineas.push(`OC ${input.numero_oc} - ${input.cliente}`)
  lineas.push(`Entrega: ${input.direccion || 'Sin dirección registrada'}`)
  // "Fecha del reparto" y no "Fecha" a secas: el transportista la leía como
  // fecha de ENTREGA comprometida.
  lineas.push(`Fecha del reparto: ${input.fecha}`)
  lineas.push(
    `Total por salir: ${plural(gSalir.rotulos.length, 'bulto')} / ` +
    `${plural(totalGuias, 'guía')} / ${fmtQty(totalUnidades)} unidades`,
  )

  const pintarGrupo = (titulo: string, despachos: BultoDespacho[]) => {
    lineas.push('')
    lineas.push(titulo)
    for (const d of despachos) {
      const guia = (d.numero_guia || '').trim()
      lineas.push(`  Guía N° ${guia || 'PENDIENTE'} (${d.numero_despacho})${selloDe(d)}`)
      for (const it of d.items) {
        lineas.push(`    ${fmtQty(it.qty)} x ${it.numero_parte} - ${it.descripcion}`)
      }
    }
  }

  const pintarGrupos = (g: GrupoBultos) => {
    for (const rotulo of g.rotulos) pintarGrupo(`BULTO ${rotulo}`, g.porBulto.get(rotulo)!)
    if (g.haySinBulto) pintarGrupo('SIN BULTO ASIGNADO', g.porBulto.get('')!)
  }

  lineas.push('')
  lineas.push('== POR SALIR ==')
  if (porSalir.length === 0) {
    // Vacío EXPLÍCITO: un correo sin cajas es una señal, no un texto truncado.
    lineas.push('  (nada por salir: todos los despachos de esta OC ya viajaron)')
  } else {
    pintarGrupos(gSalir)
  }

  if (yaViajaron.length > 0) {
    lineas.push('')
    lineas.push('== YA DESPACHADO - NO VIAJA EN ESTE REPARTO ==')
    lineas.push(
      `  Histórico de la OC: ${plural(gViejos.rotulos.length, 'bulto')} / ` +
      `${plural(yaViajaron.length, 'guía')} / ${fmtQty(unidadesYaViajaron)} unidades`,
    )
    pintarGrupos(gViejos)
  }

  return {
    texto: lineas.join('\n'),
    totalBultos: gSalir.rotulos.length,
    totalGuias,
    totalUnidades,
    hayGuiasPendientes,
    haySinBulto: gSalir.haySinBulto,
    colisionesRotulos,
    yaViajaron: yaViajaron.length,
    unidadesYaViajaron,
  }
}
