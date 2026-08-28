/**
 * Selector de OC en "Emitir factura" (MachParts) — SOLO funciones puras.
 *
 * Sin React acá (molde: picking/picking.ts): filtrar/ordenar las ventas que
 * devuelve GET /contabilidad/ventas/opciones y calcular el semáforo tributario
 * de las guías. SelectorOcFactura pinta sus propias filas con estos resultados.
 *
 * El PORQUÉ de cada regla:
 *  · Doble pasada (literal → colapsada): el operador escribe "78279030" y la
 *    venta guarda "78.279.030-7"; o escribe "8412" y la guía es "84-12". La
 *    pasada literal manda (permite resaltar el fragmento); la colapsada solo
 *    decide cuando la literal no pegó en esa fila (resalta el campo completo).
 *  · Facturables primero y sin-fecha AL PRINCIPIO: una guía firmada sin fecha
 *    de emisión NO se puede facturar al SII (la referencia 52 exige la fecha)
 *    — requiere acción humana en Despachos → Editar, por eso encabeza la lista.
 *  · Semáforo tributario: la factura de una guía debe emitirse a más tardar el
 *    día 10 del mes siguiente al de la guía (cierre del período IVA). Mismo
 *    período = ok; mes anterior con hoy ≤ 10 = ámbar (todavía se alcanza);
 *    después = rojo (el período ya cerró).
 *  · `hoy` SIEMPRE del backend: la TZ de Chile la manda el server — un
 *    new Date() en el navegador de otro huso cambiaría el color del semáforo.
 */

import { colapsar } from '../picking/picking'

// ── Contrato con GET /contabilidad/ventas/opciones ────────────────────────────

export interface GuiaFacturable {
  /** Folio de la guía; null = guía aún sin folio (se muestra el N° despacho). */
  numero_guia: string | null
  numero_despacho: string
  /** Fecha de emisión de la guía (ISO date) o null = falta cargarla. */
  fecha: string | null
  /** 'bloqueada' = guía electrónica trabada ante el SII: no citable en la
   *  referencia 52 — se resuelve en Despachos antes de poder facturar. */
  fuente: 'electronica' | 'papel' | 'bloqueada' | null
}

/** Espejo del contrato del backend. `guias_facturables` viaja SIEMPRE (lista
 *  vacía cuando n=0); queda opcional solo por tolerancia a un backend viejo. */
export interface OpcionVenta {
  oc_cliente_id: number
  numero_oc: string | null
  numero_cotizacion: string | null
  cliente: string
  rut_cliente: string | null
  fecha_venta: string | null
  fecha_oc: string | null
  cond_pago: string | null
  guias_facturables_n: number
  guias_facturables?: GuiaFacturable[]
}

// ── RUT canónico (contrato con el backend) ────────────────────────────────────

/**
 * Forma canónica para COMPARAR RUTs: sin puntos, sin guión, sin espacios, en
 * mayúsculas (el DV puede ser "k").
 *
 * CONTRATO: espejo exacto de `_rut_canonico` en backend/routers/contabilidad.py
 * (_rut_saneado quita puntos/espacios y sube a mayúsculas; _rut_canonico quita
 * además el guión). Así 78279030 encuentra 78.279.030-7 igual que lo haría el
 * servidor. Si el backend cambia la regla, cambiar AQUÍ también.
 */
export function rutCanonico(rut: string | null | undefined): string {
  return (rut || '').replace(/[.\s-]/g, '').toUpperCase()
}

// ── Filtro (doble pasada, molde picking.filtrarItems) ─────────────────────────

export type CampoOpcion =
  | 'cliente'
  | 'numero_oc'
  | 'numero_cotizacion'
  | 'rut_cliente'
  | 'numero_guia'

export interface OpcionMatch {
  item: OpcionVenta
  /** Campos que acertaron (en la pasada que decidió esta fila). */
  campos: CampoOpcion[]
  /** true = acierto por pasada colapsada / RUT canónico: no existe fragmento
   *  literal que subrayar — se resalta el CAMPO completo. */
  porColapsado: boolean
}

export interface ResultadoFiltroOpciones {
  matches: OpcionMatch[]
  /** true si alguna fila acertó solo en la pasada colapsada/canónica. */
  huboColapsado: boolean
}

/**
 * Filtro local sobre cliente / N° OC / N° cotización / RUT / números de guía —
 * exactamente los campos que promete el placeholder de la caja (el placeholder
 * es contrato). Dos pasadas POR FILA:
 *  A) literal, case-insensitive (subcadena tal cual se tecleó);
 *  B) colapsada EN LOS DOS SENTIDOS con `colapsar` (query Y campo) para
 *     OC/cotización/guías — 8412 pega en "84-12" y al revés — y con
 *     `rutCanonico` para el RUT — 78279030 pega en "78.279.030-7".
 * La pasada B solo decide cuando la A no pegó en esa fila (prioridad del
 * resaltado de fragmento). Query vacía → todas las filas en su orden original.
 */
export function filtrarOpciones(
  opciones: OpcionVenta[],
  query: string,
): ResultadoFiltroOpciones {
  const q = query.trim()
  if (!q) {
    return {
      matches: opciones.map(item => ({ item, campos: [], porColapsado: false })),
      huboColapsado: false,
    }
  }

  const qLower = q.toLowerCase()
  const qColapsada = colapsar(q)
  const qRut = rutCanonico(q)
  const matches: OpcionMatch[] = []
  let huboColapsado = false

  for (const item of opciones) {
    const oc = item.numero_oc || ''
    const cot = item.numero_cotizacion || ''
    const rut = item.rut_cliente || ''
    const guias = (item.guias_facturables || []).map(g => g.numero_guia || '')

    // Pasada A: literal, case-insensitive, campo por campo.
    const literales: CampoOpcion[] = []
    if (item.cliente.toLowerCase().includes(qLower)) literales.push('cliente')
    if (oc.toLowerCase().includes(qLower)) literales.push('numero_oc')
    if (cot.toLowerCase().includes(qLower)) literales.push('numero_cotizacion')
    if (rut.toLowerCase().includes(qLower)) literales.push('rut_cliente')
    if (guias.some(g => g && g.toLowerCase().includes(qLower))) literales.push('numero_guia')
    if (literales.length > 0) {
      matches.push({ item, campos: literales, porColapsado: false })
      continue
    }

    // Pasada B: colapsada en los dos sentidos + RUT canónico.
    const colapsados: CampoOpcion[] = []
    if (qColapsada) {
      if (oc && colapsar(oc).includes(qColapsada)) colapsados.push('numero_oc')
      if (cot && colapsar(cot).includes(qColapsada)) colapsados.push('numero_cotizacion')
      if (guias.some(g => g && colapsar(g).includes(qColapsada))) colapsados.push('numero_guia')
    }
    if (qRut && rut && rutCanonico(rut).includes(qRut)) colapsados.push('rut_cliente')
    if (colapsados.length > 0) {
      matches.push({ item, campos: colapsados, porColapsado: true })
      huboColapsado = true
    }
  }

  return { matches, huboColapsado }
}

// ── Partición y orden ─────────────────────────────────────────────────────────

export interface ParticionFacturables {
  facturables: OpcionMatch[]
  resto: OpcionMatch[]
}

/** Separa las ventas con guías firmadas pendientes de facturar (n>0) del resto.
 *  El resto CONSERVA el orden del backend (fecha_venta desc, sin-fecha al
 *  final): acá no se reordena nada. */
export function particionarFacturables(matches: OpcionMatch[]): ParticionFacturables {
  return {
    facturables: matches.filter(m => m.item.guias_facturables_n > 0),
    resto: matches.filter(m => m.item.guias_facturables_n <= 0),
  }
}

/** true si alguna guía facturable de la venta está BLOQUEADA ante el SII
 *  (fuente === 'bloqueada'): manda sobre el semáforo tributario — esa guía no se
 *  puede citar en la referencia 52 hasta resolverla en Despachos. */
export function hayGuiaBloqueada(o: OpcionVenta): boolean {
  return (o.guias_facturables || []).some(g => g.fuente === 'bloqueada')
}

/** Resumen de las guías de una venta para el chip y el orden: la fecha más
 *  antigua entre las guías CON fecha, y si alguna guía viene sin fecha. */
export function guiaMasAntigua(o: OpcionVenta): { fecha: string | null; sinFecha: boolean } {
  const guias = o.guias_facturables || []
  const fechas = guias.map(g => g.fecha).filter((f): f is string => !!f).sort()
  return { fecha: fechas[0] ?? null, sinFecha: guias.some(g => !g.fecha) }
}

/**
 * Orden de la sección "LISTAS PARA FACTURAR": por fecha de guía más antigua
 * ASC, con las sin-fecha PRIMERO — una guía sin fecha de emisión no se puede
 * facturar al SII (la referencia 52 la exige) y requiere acción humana (chip
 * «sin fecha»), así que encabeza la lista en vez de perderse al final.
 */
export function ordenarFacturables(matches: OpcionMatch[]): OpcionMatch[] {
  return [...matches].sort((a, b) => {
    const ga = guiaMasAntigua(a.item)
    const gb = guiaMasAntigua(b.item)
    if (ga.sinFecha !== gb.sinFecha) return ga.sinFecha ? -1 : 1
    if (ga.fecha === gb.fecha) return 0
    if (ga.fecha === null) return -1 // sin ninguna guía fechada: también requiere acción
    if (gb.fecha === null) return 1
    return ga.fecha < gb.fecha ? -1 : 1 // ISO YYYY-MM-DD: el orden lexicográfico ES cronológico
  })
}

// ── Semáforo tributario ───────────────────────────────────────────────────────

export type EstadoTributario = 'sin_fecha' | 'ok' | 'ambar' | 'rojo'

/**
 * Semáforo de la guía frente al cierre del período IVA:
 *  · 'sin_fecha': la guía no tiene fecha de emisión (no se puede facturar al SII).
 *  · 'ok':     la guía es del MISMO período (mes) que hoy — hay tiempo.
 *  · 'ambar':  la guía es del período ANTERIOR y hoy es día ≤ 10 — todavía se
 *              alcanza a facturar dentro del plazo («facturar antes del 10»).
 *  · 'rojo':   período anterior con hoy > 10, o la guía tiene 2+ meses — el
 *              período ya cerró.
 *
 * `hoy` viene SIEMPRE del backend (campo `hoy` de /ventas/opciones): la TZ de
 * Chile la manda el server — JAMÁS usar new Date() acá, un navegador en otro
 * huso horario cambiaría de día (y de color) antes o después que el negocio.
 */
export function estadoTributario(fechaGuia: string | null, hoy: string): EstadoTributario {
  if (!fechaGuia) return 'sin_fecha'
  const g = parseIso(fechaGuia)
  const h = parseIso(hoy)
  if (!g || !h) return 'sin_fecha'
  const mesesAtras = (h.anio - g.anio) * 12 + (h.mes - g.mes)
  if (mesesAtras <= 0) return 'ok' // mismo período (o fecha futura: sin urgencia)
  if (mesesAtras === 1) return h.dia <= 10 ? 'ambar' : 'rojo'
  return 'rojo'
}

// ── Fechas ────────────────────────────────────────────────────────────────────

function parseIso(s: string): { anio: number; mes: number; dia: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s)
  if (!m) return null
  return { anio: Number(m[1]), mes: Number(m[2]), dia: Number(m[3]) }
}

/** "2026-08-04" → "04-08" (dd-mm, para chips angostos). Se parsea el TEXTO ISO
 *  directamente — nada de new Date(): en Chile (UTC-4/-3) una fecha pura
 *  interpretada en UTC retrocede un día. null/no parseable → "—". */
export function fmtFechaCorta(s: string | null): string {
  if (!s) return '—'
  const p = parseIso(s)
  if (!p) return s
  return `${String(p.dia).padStart(2, '0')}-${String(p.mes).padStart(2, '0')}`
}

/**
 * fmtFechaCorta + el AÑO en dos dígitos cuando la guía NO es del año de `hoy`
 * ("12-06" → "12-06-25").
 *
 * POR QUÉ existe: el chip rojo (período IVA cerrado) NO se limita al mes
 * anterior como el ámbar — abarca guías de varios meses y de AÑOS anteriores.
 * "12-06" a secas no distingue junio de 2025 de junio de 2026, y es justo el
 * caso donde el dato más importa (mientras más vieja la guía, más grave el
 * atraso). El formato corto se conserva para el resto del semáforo: el chip es
 * angosto y el año solo se paga cuando aporta.
 *
 * `hoy` viene del backend, igual que en estadoTributario: nada de new Date().
 */
export function fmtFechaCortaConAnio(s: string | null, hoy: string): string {
  const corta = fmtFechaCorta(s)
  const p = s ? parseIso(s) : null
  const h = parseIso(hoy)
  if (!p || !h || p.anio === h.anio) return corta
  return `${corta}-${String(p.anio % 100).padStart(2, '0')}`
}
