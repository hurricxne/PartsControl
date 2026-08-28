// Selector de OC sin desplegables para "Emitir factura" / "Factura de anticipo"
// (MachParts): caja de búsqueda + listbox INLINE con dos secciones (las ventas
// con guías firmadas listas para facturar primero, con semáforo tributario).
// Lógica pura en facturas/selectorOc.ts; resaltado compartido en Resaltado.tsx.
import { useEffect, useMemo, useRef, useState, useId } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search, X, Loader2, AlertCircle } from 'lucide-react'
import { contabilidadAPI } from '../services/api'
import { fmtDate } from '../utils/format'
import {
  OpcionVenta, OpcionMatch, CampoOpcion, filtrarOpciones, particionarFacturables,
  ordenarFacturables, guiaMasAntigua, estadoTributario, fmtFechaCorta, fmtFechaCortaConAnio,
  hayGuiaBloqueada,
} from '../facturas/selectorOc'
import { CampoResaltado } from './Resaltado'

interface VentasOpcionesResp { hoy: string; opciones: OpcionVenta[] }

/** Cap de "OTRAS VENTAS" sin búsqueda: las recientes a la vista, el resto se
 *  encuentra buscando (el pie lo dice). La búsqueda LEVANTA el cap. */
const CAP_OTRAS = 8
const MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']

const CHIP_CLS: Record<string, string> = {
  ok: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20',
  ambar: 'bg-amber-500/10 text-amber-500 border-amber-500/30',
  rojo: 'bg-red-500/10 text-red-500 border-red-500/30',
  // slate (no gray): mismo tono que los estados neutros de Despachos.
  sin_fecha: 'bg-slate-500/10 text-slate-500 border-slate-500/20',
}

export default function SelectorOcFactura({ value, onSelect, contexto, autoFocus, inputId }: {
  value: number | ''
  onSelect: (o: OpcionVenta | null) => void
  contexto: 'factura' | 'anticipo'
  autoFocus?: boolean
  /** id de la caja de búsqueda, para que el rótulo del formulario padre la
   *  apunte con <label htmlFor>. El padre NO puede envolver este componente en
   *  un <label>: un label sin `for` se asocia a su PRIMER descendiente
   *  etiquetable, y un clic en el rótulo (o en cualquier texto del widget)
   *  disparaba «cambiar», que borra el formulario a medio llenar. */
  inputId?: string
}) {
  const [q, setQ] = useState('')
  const [hi, setHi] = useState(-1)  // índice resaltado sobre la lista visible APLANADA
  // SNAPSHOT del OpcionVenta elegido: la barra colapsada se pinta desde acá,
  // JAMÁS desde opciones.find sobre datos frescos — al facturarse su última guía
  // la OC SALE de la lista en el próximo refetch y la barra quedaría en blanco
  // con el formulario aún abierto.
  const [snapshot, setSnapshot] = useState<OpcionVenta | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const cambiarRef = useRef<HTMLButtonElement>(null)
  const uid = useId()

  // staleTime 0 + refetchOnMount 'always' A PROPÓSITO: el staleTime global es
  // 30s (main.tsx) y el gesto más común es firmar la guía → abrir este modal:
  // con caché de 30s la guía recién firmada NO aparecería justo cuando se la busca.
  const { data, isLoading, isError, refetch } = useQuery<VentasOpcionesResp>({
    queryKey: ['contabilidad', 'ventas-opciones'],
    queryFn: async () => (await contabilidadAPI.ventasOpciones()).data,
    staleTime: 0,
    refetchOnMount: 'always',
  })
  const hoy = data?.hoy || ''

  // value seteado = SIEMPRE colapsado, tenga snapshot o no: al volver del flujo
  // SII (FacturasPage cede el modal a EmisionFacturaSII y este selector se
  // desmonta) el remonte llega con value y snapshot === null — mostrar la caja
  // abierta con autoFocus robaba el foco y escondía QUÉ OC se estaba facturando.
  const colapsado = value !== ''
  const snapshotVigente = colapsado && snapshot?.oc_cliente_id === value ? snapshot : null
  // Relleno del snapshot perdido en el remonte: se deriva de los datos frescos
  // SOLO cuando falta — jamás pisa un snapshot vivo (ver comentario de `snapshot`:
  // la OC puede SALIR de la lista en el refetch con el formulario aún abierto).
  useEffect(() => {
    if (value === '' || snapshot?.oc_cliente_id === value) return
    const o = data?.opciones.find(op => op.oc_cliente_id === value)
    if (o) setSnapshot(o)
  }, [value, data, snapshot])
  // Foco programático en las transiciones: al colapsar → botón [cambiar]; al
  // reabrir con [cambiar] → caja de búsqueda. (El montaje inicial lo gobierna autoFocus.)
  const prevColapsado = useRef(colapsado)
  useEffect(() => {
    if (colapsado && !prevColapsado.current) cambiarRef.current?.focus()
    if (!colapsado && prevColapsado.current) inputRef.current?.focus()
    prevColapsado.current = colapsado
  }, [colapsado])

  const buscando = q.trim().length > 0
  const { matches } = useMemo(() => filtrarOpciones(data?.opciones ?? [], q), [data, q])

  // Secciones + lista visible aplanada (para el teclado, saltándose encabezados)
  const { secciones, visibles, ocultas } = useMemo(() => {
    if (contexto === 'anticipo') {
      // Anticipo: UNA lista plana en el orden del backend (fecha_venta desc).
      // Sin sección de facturables ni semáforo: el anticipo se emite ANTES del
      // despacho — un semáforo de guías acá sería un dato falso.
      // MISMO cap de 8 que "OTRAS VENTAS": sin búsqueda, solo las recientes a la
      // vista y el pie dice cuántas quedan; la búsqueda LEVANTA el cap.
      const filas = buscando ? matches : matches.slice(0, CAP_OTRAS)
      return {
        secciones: [{ titulo: null as string | null, filas, vacia: null as string | null }],
        visibles: filas,
        ocultas: matches.length - filas.length,
      }
    }
    const { facturables, resto } = particionarFacturables(matches)
    const listas = ordenarFacturables(facturables)
    const otras = buscando ? resto : resto.slice(0, CAP_OTRAS)
    const secciones = [
      // Con búsqueda activa el vacío es NEUTRO: un ✓ verde global diría "no hay
      // nada pendiente" cuando lo único cierto es que EL TÉRMINO no coincidió.
      {
        titulo: `LISTAS PARA FACTURAR (${listas.length})`,
        filas: listas,
        vacia: buscando
          ? 'Ninguna lista para facturar coincide con la búsqueda'
          : '✓ No hay guías firmadas pendientes de facturar',
      },
      { titulo: 'OTRAS VENTAS', filas: otras, vacia: null },
    ]
    return { secciones, visibles: [...listas, ...otras], ocultas: resto.length - otras.length }
  }, [contexto, matches, buscando])

  useEffect(() => { if (hi >= visibles.length) setHi(-1) }, [visibles.length, hi])

  const optId = (o: OpcionVenta) => `oc-opt-${uid}-${o.oc_cliente_id}`
  const elegir = (o: OpcionVenta) => { setSnapshot(o); setQ(''); setHi(-1); onSelect(o) }
  const limpiar = () => { setQ(''); setHi(-1); inputRef.current?.focus() }

  // Teclado: el option activo se mantiene A LA VISTA dentro del listbox (max-h-64
  // scrolleable). block:'nearest' no salta si ya está visible, así el hover (que
  // también mueve `hi`) no provoca brincos de scroll.
  useEffect(() => {
    if (hi < 0 || !visibles[hi]) return
    document.getElementById(optId(visibles[hi].item))?.scrollIntoView({ block: 'nearest' })
  }, [hi, visibles])  // eslint-disable-line react-hooks/exhaustive-deps -- optId es estable (uid)

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setHi(h => Math.min(h + 1, visibles.length - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi(h => Math.max(h - 1, -1)) }
    else if (e.key === 'Enter') {
      e.preventDefault()
      if (hi >= 0 && visibles[hi]) elegir(visibles[hi].item)
      else if (visibles.length === 1) elegir(visibles[0].item)  // 1 sola coincidencia: Enter directo
    } else if (e.key === 'Escape' && q) {
      // Esc PRIMERA capa: limpiar el texto y NADA más (no cierra el modal)
      e.preventDefault(); e.stopPropagation(); limpiar()
    }
  }

  // ── Barra colapsada (desde el snapshot; con value y sin snapshot, fallback
  //    «OC #id» mientras el efecto de arriba lo deriva de los datos frescos) ──
  if (colapsado) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 rounded-lg border text-sm"
        style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}>
        {snapshotVigente ? (
          <>
            <span className="font-mono font-bold shrink-0">OC {snapshotVigente.numero_oc || `#${snapshotVigente.oc_cliente_id}`}</span>
            <span className="truncate" title={snapshotVigente.cliente} style={{ color: 'var(--text-muted)' }}>— {snapshotVigente.cliente}</span>
            {contexto === 'factura' && (
              <span className="shrink-0 text-xs" style={{ color: 'var(--text-faint)' }}>
                · {snapshotVigente.guias_facturables_n} guía{snapshotVigente.guias_facturables_n === 1 ? '' : 's'}
              </span>
            )}
          </>
        ) : (
          <span className="font-mono font-bold shrink-0">OC #{value}</span>
        )}
        <button ref={cambiarRef} type="button" onClick={() => { setSnapshot(null); onSelect(null) }}
          className="ml-auto shrink-0 text-xs underline underline-offset-2 focus:outline-none focus:ring-2 focus:ring-brand-500/40 rounded"
          style={{ color: 'var(--text-muted)' }}>
          cambiar
        </button>
      </div>
    )
  }

  const marca = (m: OpcionMatch, campo: CampoOpcion) => m.campos.includes(campo)
  const resalta = (m: OpcionMatch, campo: CampoOpcion, texto: string) => (
    marca(m, campo) ? <CampoResaltado texto={texto} query={q} colapsado={m.porColapsado} /> : <>{texto}</>
  )

  const fila = (m: OpcionMatch, idx: number) => {
    const o = m.item
    const g = guiaMasAntigua(o)
    const est = g.sinFecha ? 'sin_fecha' : estadoTributario(g.fecha, hoy)
    const bloqueada = hayGuiaBloqueada(o)
    const nGuias = `${o.guias_facturables_n} guía${o.guias_facturables_n === 1 ? '' : 's'}`
    const mesGuia = g.fecha ? MESES[Number(g.fecha.slice(5, 7)) - 1] : ''
    return (
      <button key={o.oc_cliente_id} type="button" role="option" id={optId(o)} aria-selected={hi === idx}
        onClick={() => elegir(o)} onMouseEnter={() => setHi(idx)}
        className={`w-full text-left px-3 py-2 border-b last:border-b-0 focus:outline-none ${hi === idx ? 'bg-brand-500/10' : ''}`}
        style={{ borderColor: 'var(--border)' }}>
        <div className="flex items-center gap-2">
          <span className="font-mono font-bold text-sm shrink-0" style={{ color: 'var(--text-primary)' }}>
            OC {resalta(m, 'numero_oc', o.numero_oc || `#${o.oc_cliente_id}`)}
          </span>
          <span className="text-sm truncate" title={o.cliente} style={{ color: 'var(--text-muted)' }}>
            {resalta(m, 'cliente', o.cliente)}
          </span>
          {contexto === 'factura' && o.guias_facturables_n > 0 && (
            // Guía bloqueada en el SII: manda sobre el semáforo (no se puede
            // facturar hasta resolverla en Despachos, cualquiera sea el período).
            bloqueada ? (
              <span className={`ml-auto shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium border ${CHIP_CLS.ambar}`}
                title="Guía bloqueada en SII — revísala en Despachos antes de facturar">
                {nGuias} · guía bloqueada en SII
              </span>
            ) : (
              <span className={`ml-auto shrink-0 inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium border ${CHIP_CLS[est]}`}
                title={est === 'rojo' ? 'Período IVA vencido — regularizar con el contador' : undefined}>
                {est === 'sin_fecha' && `${nGuias} · sin fecha`}
                {est === 'ambar' && `${nGuias} · Guía de ${mesGuia} — facturar antes del 10`}
                {est === 'ok' && `${nGuias} · ${fmtFechaCorta(g.fecha)}`}
                {/* El rojo es el estado MÁS grave del semáforo y era el único
                    que hablaba solo por COLOR: su texto era idéntico al del ok
                    («1 guía · 12-06») y la explicación vivía únicamente en el
                    title, que exige dejar el mouse quieto encima. Ahora dice
                    con palabras qué pasó y qué hacer — y lleva el AÑO cuando la
                    guía es de otro año, porque el rojo abarca años anteriores
                    (ver fmtFechaCortaConAnio). El title queda como detalle. */}
                {est === 'rojo' && `${nGuias} · Guía ${fmtFechaCortaConAnio(g.fecha, hoy)} — período cerrado, avisa al contador`}
              </span>
            )
          )}
        </div>
        <div className="text-[11px] mt-0.5 truncate" style={{ color: 'var(--text-muted)' }}>
          {fmtDate(o.fecha_venta)}
          {o.numero_cotizacion && <> · Cot {resalta(m, 'numero_cotizacion', o.numero_cotizacion)}</>}
          {o.rut_cliente && <> · {resalta(m, 'rut_cliente', o.rut_cliente)}</>}
          {(o.guias_facturables ?? []).length > 0 && (
            <> · {marca(m, 'numero_guia')
              ? <CampoResaltado colapsado={m.porColapsado} query={q}
                  texto={(o.guias_facturables ?? []).map(gg => gg.numero_guia ? `Guía ${gg.numero_guia}` : gg.numero_despacho).join(' · ')} />
              : (o.guias_facturables ?? []).map(gg => gg.numero_guia ? `Guía ${gg.numero_guia}` : gg.numero_despacho).join(' · ')}
            </>
          )}
        </div>
      </button>
    )
  }

  // ── Búsqueda + listbox INLINE en el flujo del modal ────────────────────────
  // INLINE a propósito: el panel del Modal scrollea (max-h + overflow-y-auto) y
  // su overlay cierra al click (FacturasPage · Modal): un panel en portal o con
  // position absolute quedaría FUERA del div que hace stopPropagation, y el
  // click en una opción burbujearía al overlay cerrando el modal con el
  // formulario a medio llenar.
  let idxGlobal = -1
  // El listbox solo existe cuando hay datos que listar: los estados de carga /
  // error / cero-resultados lo reemplazan — el ARIA debe decir la verdad.
  const listboxAbierto = !isLoading && !isError && !(buscando && matches.length === 0)
  return (
    <div>
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input ref={inputRef} id={inputId} autoFocus={autoFocus} role="combobox" aria-expanded={listboxAbierto} aria-autocomplete="list"
          aria-controls={listboxAbierto ? `oc-listbox-${uid}` : undefined}
          aria-activedescendant={hi >= 0 && visibles[hi] ? optId(visibles[hi].item) : undefined}
          className="w-full pl-9 pr-8 py-2 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40"
          style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
          placeholder="Buscar por N° OC, cotización, cliente, RUT o N° de guía"
          value={q} onChange={e => { setQ(e.target.value); setHi(-1) }} onKeyDown={onKeyDown} />
        {q && (
          <button type="button" onClick={limpiar} aria-label="Limpiar búsqueda"
            className="absolute right-2 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-white/10" style={{ color: 'var(--text-muted)' }}>
            <X className="w-4 h-4" />
          </button>
        )}
      </div>
      <p aria-live="polite" className="sr-only">{visibles.length} resultado{visibles.length === 1 ? '' : 's'}</p>

      <div className="mt-2 rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
        {isLoading && (
          <div className="p-3 space-y-2">
            {[0, 1, 2].map(i => <div key={i} className="h-9 rounded-lg animate-pulse" style={{ backgroundColor: 'var(--surface-300)' }} />)}
            <p className="text-xs text-center" style={{ color: 'var(--text-muted)' }}><Loader2 className="w-3.5 h-3.5 animate-spin inline mr-1" /> Cargando ventas…</p>
          </div>
        )}
        {isError && (
          <div className="p-4 text-center space-y-2">
            {/* Error VISIBLE con reintento — nada de .catch(() => {}) mudo */}
            <p className="text-xs text-red-500 flex items-center justify-center gap-1.5"><AlertCircle className="w-4 h-4" /> No se pudieron cargar las ventas</p>
            <button type="button" onClick={() => refetch()} className="btn-secondary text-xs">Reintentar</button>
          </div>
        )}
        {!isLoading && !isError && buscando && matches.length === 0 && (
          <div className="p-4 text-center space-y-2">
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              Ninguna venta coincide con “{q.trim()}” · Busqué N° OC, cotización, cliente, RUT y N° de guía
            </p>
            <button type="button" onClick={limpiar} className="btn-secondary text-xs">Limpiar</button>
          </div>
        )}
        {!isLoading && !isError && !(buscando && matches.length === 0) && (
          <div role="listbox" id={`oc-listbox-${uid}`} aria-label="Ventas" className="max-h-64 overflow-y-auto">
            {secciones.map((s, si) => (
              <div key={si} role="presentation">
                {s.titulo && (
                  // role="presentation": el encabezado no es un option — que el
                  // lector de pantalla no lo cuente entre los resultados.
                  <div role="presentation" className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider sticky top-0"
                    style={{ color: 'var(--text-faint)', backgroundColor: 'var(--surface-200)' }}>
                    {s.titulo}
                  </div>
                )}
                {s.filas.map(m => { idxGlobal += 1; return fila(m, idxGlobal) })}
                {s.filas.length === 0 && s.vacia && (
                  // Verde SOLO para el vacío positivo real (sin búsqueda); bajo
                  // búsqueda el texto es neutro (ver armado de secciones).
                  <p className={`px-3 py-2 text-xs ${buscando ? '' : 'text-emerald-500'}`}
                    style={buscando ? { color: 'var(--text-muted)' } : undefined}>
                    {s.vacia}
                  </p>
                )}
                {s.filas.length === 0 && !s.vacia && (
                  <p className="px-3 py-2 text-xs" style={{ color: 'var(--text-faint)' }}>Sin ventas</p>
                )}
              </div>
            ))}
            {ocultas > 0 && (
              <p className="px-3 py-2 text-[11px] border-t" style={{ color: 'var(--text-faint)', borderColor: 'var(--border)' }}>
                {ocultas} venta{ocultas === 1 ? ' más — búscala' : 's más — búscalas'} por cliente, N° OC, cotización, RUT o guía
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
