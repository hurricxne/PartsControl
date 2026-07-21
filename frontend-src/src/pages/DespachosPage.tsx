import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { despachosAPI, cotizacionesAPI, cotizadorAPI, comprasAPI, wasabilAPI, abrirDocumento } from '../services/api'
import {
  Truck, Package, CheckCircle2, AlertCircle, Search, X,
  ChevronRight, ChevronDown, Plus, Trash2, Send,
  FileSpreadsheet, FileText, FileDown, Loader2,
  Clock, AlertTriangle, Upload, Pencil, Eye, Receipt,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { hoyLocal } from '../utils/format'

interface DocumentosOc {
  excel_oc: boolean
  cot_formal_excel: boolean
  cot_pdf: boolean
  cot_original_excel: boolean
}

interface DocValue {
  value: string
  is_file: boolean
  filename: string | null
}

interface EmbarqueDocs {
  awb: DocValue | null
  factura_comercial: DocValue | null
  packing_list: DocValue | null
  certificado_origen: DocValue | null
  doc_adicional: DocValue | null
}

interface EmbarqueResumen {
  id: number
  numero: string
  estado?: string
  forwarder?: string
  fecha_despacho?: string
  fecha_llegada_est?: string | null
  items_de_esta_oc: number
  documentos: EmbarqueDocs
}

async function downloadBlob(
  fetcher: () => Promise<any>,
  filename: string,
  mime: string,
) {
  try {
    const resp = await fetcher()
    const blob = new Blob([resp.data], { type: mime })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e: any) {
    toast.error(e?.response?.data?.detail || 'Error al descargar documento')
  }
}

type Tab = 'listas' | 'en_curso' | 'historial'

interface ProgresoEstados {
  pendiente: number
  en_compras: number
  en_transito: number
  en_bodega: number
  despachado: number
  reclamo: number
}

interface OcCard {
  id: number
  cotizacion_id?: number
  numero_oc: string
  numero_cotizacion?: string
  cliente: string
  rut_cliente?: string
  fecha_oc?: string
  fecha_entrega?: string
  cond_pago?: string
  direccion?: string
  contacto?: string
  telefono?: string
  email?: string
  total_items: number
  items_en_bodega: number
  items_despachados: number
  items_no_disponibles: number
  progreso_pct: number
  progreso_estados?: ProgresoEstados
  dias_restantes_oc?: number | null
  dias_restantes_critico?: number | null
  estado: 'listo' | 'parcial' | 'completado' | 'pendiente'
  documentos?: DocumentosOc
}

interface ItemRow {
  id: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  qty_despachada: number
  qty_disponible: number
  estado_item: string
  en_reclamo: boolean
  plazo_entrega_max?: number | null
  plazo_entrega_min?: number | null
  deadline_item?: string | null
  dias_restantes?: number | null
}

interface DespachoRow {
  id: number
  numero_despacho: string
  numero_guia?: string
  transportista?: string
  estado: string
  numero_expedicion?: string
  guia_firmada?: boolean
  fecha_firma?: string
  guia_firmada_archivo?: string
  fecha_creacion?: string
  fecha_despacho?: string
  items_count: number
}

interface OcDetail extends OcCard {
  items: ItemRow[]
  despachos: DespachoRow[]
  embarques?: EmbarqueResumen[]
}

const estadoLabel: Record<string, { label: string; color: string }> = {
  listo: { label: 'Listo para despacho', color: 'bg-emerald-500/15 text-emerald-500 dark:text-emerald-400' },
  parcial: { label: 'Parcial', color: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' },
  completado: { label: 'Completado', color: 'bg-blue-500/15 text-blue-600 dark:text-blue-400' },
  pendiente: { label: 'Pendiente bodega', color: 'bg-slate-500/15 text-slate-500' },
}

export default function DespachosPage() {
  const [tab, setTab] = useState<Tab>('listas')
  const [search, setSearch] = useState('')
  const [expandedOc, setExpandedOc] = useState<number | null>(null)
  const [modalOc, setModalOc] = useState<OcDetail | null>(null)
  const [firmarId, setFirmarId] = useState<number | null>(null)
  const [firmarDteFolio, setFirmarDteFolio] = useState<string | null>(null)
  const [editDespacho, setEditDespacho] = useState<DespachoRow | null>(null)
  const [editDespachoDteFolio, setEditDespachoDteFolio] = useState<string | null>(null)
  const [emitirGuia, setEmitirGuia] = useState<DespachoRow | null>(null)
  const qc = useQueryClient()

  const { data: counts } = useQuery({
    queryKey: ['despachos', 'counts'],
    queryFn: despachosAPI.getCounts,
    refetchInterval: 60000,
  })

  const { data: ocs = [], isLoading } = useQuery({
    queryKey: ['despachos', 'oc-clientes', tab, search],
    queryFn: () => despachosAPI.listOcClientes(tab, search),
  })

  const { data: ocDetail } = useQuery({
    queryKey: ['despachos', 'oc-detail', expandedOc],
    queryFn: () => (expandedOc ? despachosAPI.getOcDetail(expandedOc) : null),
    enabled: expandedOc !== null,
  })

  const cerrarMut = useMutation({
    mutationFn: (despachoId: number) => despachosAPI.cerrar(despachoId),
    onSuccess: () => {
      toast.success('Despacho cerrado y notificado a Ventas')
      qc.invalidateQueries({ queryKey: ['despachos'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Error al cerrar'),
  })

  const anularMut = useMutation({
    mutationFn: (despachoId: number) => despachosAPI.anular(despachoId),
    onSuccess: () => {
      toast.success('Despacho anulado')
      qc.invalidateQueries({ queryKey: ['despachos'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Error al anular'),
  })

  return (
    <div className="space-y-4">
      {/* KPI cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <KpiCard
          label="OCs Listas"
          value={counts?.ocs_listas ?? 0}
          icon={<Package className="w-5 h-5" />}
          color="text-brand-500"
          sub="Con ítems en bodega"
        />
        <KpiCard
          label="Items Disponibles"
          value={counts?.items_listos ?? 0}
          icon={<CheckCircle2 className="w-5 h-5" />}
          color="text-emerald-500"
          sub="Listos para despacho"
        />
        <KpiCard
          label="Items Despachados"
          value={counts?.items_despachados ?? 0}
          icon={<Truck className="w-5 h-5" />}
          color="text-purple-500"
          sub="Total histórico"
        />
        <KpiCard
          label="OCs En Curso"
          value={ocs.filter((o: OcCard) => o.estado === 'parcial').length}
          icon={<AlertCircle className="w-5 h-5" />}
          color="text-amber-500"
          sub="Con despacho abierto"
        />
      </div>

      {/* Tabs */}
      <div className="flex gap-2">
        <TabBtn active={tab === 'listas'} onClick={() => setTab('listas')}>
          OC-Clientes Listas
        </TabBtn>
        <TabBtn active={tab === 'en_curso'} onClick={() => setTab('en_curso')}>
          Despachos en Curso
        </TabBtn>
        <TabBtn active={tab === 'historial'} onClick={() => setTab('historial')}>
          Historial
        </TabBtn>
      </div>

      {/* Search */}
      <div className="relative">
        <Search
          className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2"
          style={{ color: 'var(--text-faint)' }}
        />
        <input
          type="text"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Buscar por cliente, N° COT u OC..."
          className="input pl-10 pr-10"
        />
        {search && (
          <button
            onClick={() => setSearch('')}
            className="absolute right-3 top-1/2 -translate-y-1/2 hover:opacity-100 opacity-60"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* List */}
      {isLoading ? (
        <div className="text-center py-12" style={{ color: 'var(--text-faint)' }}>
          Cargando...
        </div>
      ) : ocs.length === 0 ? (
        <div className="text-center py-12" style={{ color: 'var(--text-faint)' }}>
          No hay OCs {tab === 'listas' ? 'listas para despacho' : tab === 'en_curso' ? 'con despachos en curso' : 'en historial'}
        </div>
      ) : (
        <div className="space-y-2">
          {ocs.map((oc: OcCard) => (
            <OcRow
              key={oc.id}
              oc={oc}
              expanded={expandedOc === oc.id}
              onExpand={() => setExpandedOc(expandedOc === oc.id ? null : oc.id)}
              detail={expandedOc === oc.id ? (ocDetail as OcDetail | undefined) : undefined}
              onCrearDespacho={() => setModalOc(ocDetail as OcDetail)}
              onCerrarDespacho={(id: number) => {
                if (confirm('¿Confirmar cierre del despacho? Los ítems pasarán a estado "despachado".')) {
                  cerrarMut.mutate(id)
                }
              }}
              onAnularDespacho={(id: number) => {
                if (confirm('¿Anular despacho? Los ítems volverán a estar disponibles.')) {
                  anularMut.mutate(id)
                }
              }}
              onFirmarDespacho={(id: number, dteFolio: string | null) => {
                setFirmarId(id)
                setFirmarDteFolio(dteFolio)
              }}
              onEditDespacho={(d: DespachoRow, dteFolio: string | null) => {
                setEditDespacho(d)
                setEditDespachoDteFolio(dteFolio)
              }}
              onEmitirGuia={(d: DespachoRow) => setEmitirGuia(d)}
            />
          ))}
        </div>
      )}

      {/* Modal crear despacho */}
      {modalOc && (
        <CrearDespachoModal
          oc={modalOc}
          onClose={() => setModalOc(null)}
          onCreated={() => {
            setModalOc(null)
            qc.invalidateQueries({ queryKey: ['despachos'] })
          }}
        />
      )}

      {/* Modal firmar guía (subir foto firmada) */}
      {firmarId !== null && (
        <FirmarGuiaModal
          despachoId={firmarId}
          dteFolio={firmarDteFolio}
          onClose={() => setFirmarId(null)}
          onDone={() => {
            setFirmarId(null)
            qc.invalidateQueries({ queryKey: ['despachos'] })
          }}
        />
      )}

      {/* Modal editar transportista / N° expedición */}
      {editDespacho && (
        <EditarDespachoModal
          despacho={editDespacho}
          dteFolio={editDespachoDteFolio}
          onClose={() => setEditDespacho(null)}
          onSaved={() => {
            setEditDespacho(null)
            qc.invalidateQueries({ queryKey: ['despachos'] })
          }}
        />
      )}

      {/* Modal emitir guía de despacho electrónica (SII) vía Wasabil */}
      {emitirGuia && (
        <EmitirGuiaSIIModal
          despacho={emitirGuia}
          onClose={() => {
            setEmitirGuia(null)
            // Refrescar SIEMPRE al cerrar: la emisión pudo avanzar aunque el
            // usuario cierre el modal a mitad de camino (folio/badges al día)
            qc.invalidateQueries({ queryKey: ['despachos'] })
            qc.invalidateQueries({ queryKey: ['wasabil'] })
          }}
          onDone={() => {
            qc.invalidateQueries({ queryKey: ['despachos'] })
            qc.invalidateQueries({ queryKey: ['wasabil'] })
          }}
        />
      )}
    </div>
  )
}

function KpiCard({ label, value, icon, color, sub }: any) {
  return (
    <div className="card p-4">
      <div className="flex items-center justify-between mb-2">
        <span
          className="text-xs uppercase tracking-wider"
          style={{ color: 'var(--text-faint)' }}
        >
          {label}
        </span>
        <span className={color}>{icon}</span>
      </div>
      <div className={`text-3xl font-bold ${color}`}>{value}</div>
      <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>{sub}</div>
    </div>
  )
}

function TabBtn({ active, onClick, children }: any) {
  return (
    <button
      onClick={onClick}
      className={`px-4 py-2 rounded-xl text-sm font-semibold border transition-colors ${
        active
          ? 'bg-brand-500/15 text-brand-500 border-brand-500/40'
          : 'hover:bg-[var(--surface-200)]'
      }`}
      style={
        active
          ? undefined
          : { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' }
      }
    >
      {children}
    </button>
  )
}

// Estados de un DTE realmente EN PROCESO (espejo del guard _guia_electronica_activa
// del backend): claim en vuelo o documento vivo en Wasabil sin resultado final.
// OJO: 'no_enviado' (limbo: claim expirado sin respuesta) NO está — el backend
// permite anular/editar en ese caso y el frontend no debe sobre-bloquear.
const DTE_EN_PROCESO = ['enviando', 'procesando', 'pendiente']

// Qué pasarle a los modales que pueden tocar el N° de guía (editar / firmar):
//   'verificando' = la consulta de DTEs no resolvió con éxito (bloquear por precaución)
//   folio SII     = guía electrónica emitida (bloqueado: el folio no se edita a mano)
//   'en_emision'  = guía en proceso en el SII, aún sin folio (bloqueado: llegará solo)
//   null          = sin guía electrónica (o fallida): el N° manual se puede editar
function folioParaModal(dtesListos: boolean, dte: any): string | null {
  if (!dtesListos) return 'verificando'
  if (dte?.folio) return dte.folio
  if (dte && DTE_EN_PROCESO.includes(dte.estado)) return 'en_emision'
  return null
}

function OcRow({
  oc,
  expanded,
  onExpand,
  detail,
  onCrearDespacho,
  onCerrarDespacho,
  onAnularDespacho,
  onFirmarDespacho,
  onEditDespacho,
  onEmitirGuia,
}: any) {
  const badge = estadoLabel[oc.estado] ?? estadoLabel.pendiente
  // Estado de las guías electrónicas (Wasabil) de los despachos de esta OC —
  // solo BD, en lote, para pintar folio/PDF/fallida sin N llamadas.
  // `dtesListos` importa: mientras la consulta no resuelva CON ÉXITO, el folio SII
  // se trata como DESCONOCIDO (se bloquea la edición manual por precaución, no al
  // revés). isSuccess y no isFetched: una consulta FALLIDA también debe bloquear.
  const despachoIds = (detail?.despachos ?? []).map((d: DespachoRow) => d.id)
  const { data: dtes = {}, isSuccess: dtesListos } = useQuery({
    queryKey: ['wasabil', 'estado-batch', despachoIds],
    queryFn: () => wasabilAPI.estadoBatch(despachoIds).then(r => r.data),
    enabled: expanded && despachoIds.length > 0,
  })
  return (
    <div className="card overflow-hidden">
      <button
        onClick={onExpand}
        className="w-full p-4 flex items-center gap-4 hover:bg-[var(--surface-200)] transition text-left"
      >
        <div className="w-10 h-10 rounded-lg bg-brand-500/10 flex items-center justify-center text-brand-500 shrink-0">
          <Package className="w-5 h-5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <span className="text-xs text-brand-500 font-mono font-semibold">OC-{oc.numero_oc}</span>
            <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ${badge.color}`}>
              {badge.label}
            </span>
            <DiasRestantesBadge
              dias={oc.dias_restantes_critico ?? oc.dias_restantes_oc ?? null}
              label="entrega"
            />
          </div>
          <div className="font-semibold truncate" style={{ color: 'var(--text-primary)' }}>{oc.cliente}</div>
          <div className="text-xs truncate" style={{ color: 'var(--text-muted)' }}>
            {oc.numero_cotizacion} · OC #{oc.numero_oc}
            {oc.fecha_oc && ` · ${oc.fecha_oc}`}
            {oc.cond_pago && ` · ${oc.cond_pago}`}
            {oc.fecha_entrega && ` · Entrega: ${oc.fecha_entrega}`}
          </div>
          {/* Progress bar multi-segment */}
          <PipelineProgress oc={oc} />
        </div>
        <div className="shrink-0" style={{ color: 'var(--text-faint)' }}>
          {expanded ? <ChevronDown className="w-5 h-5" /> : <ChevronRight className="w-5 h-5" />}
        </div>
      </button>

      {expanded && detail && (
        <div className="border-t p-4 space-y-4" style={{ borderColor: 'var(--border)' }}>
          {/* Documentos */}
          {detail.cotizacion_id && detail.documentos && (
            <DocumentosSection
              cotizacionId={detail.cotizacion_id}
              ocId={detail.id}
              docs={detail.documentos}
              numeroOc={detail.numero_oc}
              numeroCot={detail.numero_cotizacion}
              embarques={detail.embarques}
            />
          )}

          {/* Datos destinatario */}
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                Destinatario
              </div>
              <div style={{ color: 'var(--text-primary)' }}>{detail.contacto || '—'}</div>
              <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{detail.telefono || ''}</div>
              <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{detail.email || ''}</div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                Dirección de Entrega
              </div>
              <div className="text-xs" style={{ color: 'var(--text-primary)' }}>
                {detail.direccion || 'Sin dirección'}
              </div>
            </div>
          </div>

          {/* Items */}
          <div>
            <div className="text-xs uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
              Items ({detail.items.length})
            </div>
            <div className="border rounded-xl overflow-x-auto" style={{ borderColor: 'var(--border)' }}>
              <table className="w-full text-sm">
                <thead style={{ backgroundColor: 'var(--surface-200)' }}>
                  <tr className="text-xs uppercase" style={{ color: 'var(--text-muted)' }}>
                    <th className="text-left p-2">N° Parte</th>
                    <th className="text-left p-2">Descripción</th>
                    <th className="text-left p-2">Marca</th>
                    <th className="text-right p-2">Cant.</th>
                    <th className="text-right p-2">Desp.</th>
                    <th className="text-right p-2">Disp.</th>
                    <th className="text-right p-2">Plazo</th>
                    <th className="text-center p-2">Días Rest.</th>
                    <th className="text-center p-2">Estado</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.items.map((it: ItemRow) => {
                    const yaDespachado = it.estado_item === 'despachado'
                    return (
                      <tr key={it.id} className="border-t" style={{ borderColor: 'var(--border)' }}>
                        <td className="p-2 font-mono text-xs text-brand-500">{it.numero_parte}</td>
                        <td className="p-2" style={{ color: 'var(--text-primary)' }}>{it.descripcion}</td>
                        <td className="p-2 text-xs" style={{ color: 'var(--text-muted)' }}>{it.marca}</td>
                        <td className="p-2 text-right" style={{ color: 'var(--text-primary)' }}>{it.cantidad}</td>
                        <td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{it.qty_despachada}</td>
                        <td className="p-2 text-right">
                          <span
                            className={it.qty_disponible > 0 ? 'text-emerald-500 font-semibold' : ''}
                            style={it.qty_disponible > 0 ? undefined : { color: 'var(--text-faint)' }}
                          >
                            {it.qty_disponible}
                          </span>
                        </td>
                        <td className="p-2 text-right text-xs" style={{ color: 'var(--text-muted)' }}>
                          {it.plazo_entrega_max != null ? `${it.plazo_entrega_max}d` : '—'}
                        </td>
                        <td className="p-2 text-center">
                          {yaDespachado ? (
                            <span className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
                              —
                            </span>
                          ) : (
                            <DiasRestantesBadge dias={it.dias_restantes} compact />
                          )}
                        </td>
                        <td className="p-2 text-center">
                          <ItemEstadoBadge estado={it.estado_item} reclamo={it.en_reclamo} />
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Despachos */}
          {detail.despachos.length > 0 && (
            <div>
              <div className="text-xs uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
                Despachos ({detail.despachos.length})
              </div>
              <div className="space-y-2">
                {detail.despachos.map((d: DespachoRow) => {
                  const dte = (dtes as any)[d.id]
                  return (
                  <div
                    key={d.id}
                    className="flex items-center justify-between p-3 border rounded-xl"
                    style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
                          {d.numero_despacho}
                        </span>
                        <DespachoEstadoBadge estado={d.estado} />
                        {dte?.estado === 'emitido' && (
                          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-blue-500/15 text-blue-600 dark:text-blue-400 inline-flex items-center gap-1">
                            <Receipt className="w-3 h-3" /> Guía SII {dte.folio}
                          </span>
                        )}
                        {dte && dte.estado !== 'emitido' && (
                          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 inline-flex items-center gap-1">
                            <AlertTriangle className="w-3 h-3" />
                            {dte.puede_reintentar ? 'Emisión SII fallida' : 'Emisión SII en proceso'}
                          </span>
                        )}
                        {d.guia_firmada && (
                          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1">
                            <CheckCircle2 className="w-3 h-3" /> Guía firmada
                          </span>
                        )}
                      </div>
                      <div className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
                        {d.items_count} ítems
                        {d.transportista && ` · ${d.transportista}`}
                        {d.numero_guia && ` · Guía: ${d.numero_guia}`}
                        {d.numero_expedicion && ` · Exp: ${d.numero_expedicion}`}
                        {d.fecha_despacho && ` · ${new Date(d.fecha_despacho).toLocaleDateString('es-CL')}`}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2 shrink-0 items-center justify-end">
                      {d.estado === 'en_preparacion' && (
                        <>
                          {(!dte || dte.puede_reintentar) && (
                            <button
                              onClick={() => onEmitirGuia(d)}
                              className="px-3 py-1.5 text-xs bg-blue-500/15 text-blue-600 dark:text-blue-400 rounded-lg hover:bg-blue-500/25 flex items-center gap-1 font-semibold"
                              title="Emitir la guía de despacho electrónica al SII vía Wasabil"
                            >
                              <Receipt className="w-3 h-3" /> {dte ? 'Reintentar guía SII' : 'Emitir guía SII'}
                            </button>
                          )}
                          {/* Paso 3 del flujo: el transportista se agrega DESPUÉS de emitir
                              la guía (no viaja al SII). Reusa el modal de edición, que ya
                              blinda el folio del SII para que no se pise a mano. */}
                          <button
                            onClick={() => onEditDespacho(d, folioParaModal(dtesListos, dte))}
                            className="px-3 py-1.5 text-xs rounded-lg hover:bg-[var(--surface-300)] flex items-center gap-1 font-semibold border"
                            style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}
                            title="Agregar o editar el transportista y el N° de expedición"
                          >
                            <Truck className="w-3 h-3" /> {d.transportista ? 'Transportista' : 'Agregar transportista'}
                          </button>
                          <button
                            onClick={() => onCerrarDespacho(d.id)}
                            className="px-3 py-1.5 text-xs bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 rounded-lg hover:bg-emerald-500/25 flex items-center gap-1 font-semibold"
                          >
                            <Send className="w-3 h-3" /> Confirmar
                          </button>
                          <button
                            onClick={() => {
                              // El backend rechaza (409) anular con guía SII viva; acá se
                              // explica de inmediato en vez de dejar intentar algo que fallará.
                              if (dte?.estado === 'emitido') {
                                toast.error(
                                  `Este despacho tiene guía SII EMITIDA (folio ${dte.folio}): ` +
                                  'anúlala primero en Wasabil y luego anula el despacho.'
                                )
                                return
                              }
                              if (dte && DTE_EN_PROCESO.includes(dte.estado)) {
                                toast.error('Este despacho tiene una emisión de guía SII en curso: espera el resultado antes de anular.')
                                return
                              }
                              onAnularDespacho(d.id)
                            }}
                            className="px-3 py-1.5 text-xs bg-red-500/10 text-red-500 rounded-lg hover:bg-red-500/20"
                          >
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </>
                      )}
                      {d.estado === 'despachado' && !d.guia_firmada && (
                        <button
                          onClick={() => onFirmarDespacho(d.id, folioParaModal(dtesListos, dte))}
                          className="px-3 py-1.5 text-xs bg-blue-500/15 text-blue-600 dark:text-blue-400 rounded-lg hover:bg-blue-500/25 flex items-center gap-1 font-semibold"
                        >
                          <CheckCircle2 className="w-3 h-3" /> Marcar guía firmada
                        </button>
                      )}
                      {dte && dte.estado !== 'emitido' && !dte.puede_reintentar && (
                        <button
                          onClick={() => onEmitirGuia(d)}
                          className="px-3 py-1.5 text-xs bg-amber-500/15 text-amber-600 dark:text-amber-400 rounded-lg hover:bg-amber-500/25 flex items-center gap-1 font-semibold"
                          title="La emisión sigue en proceso: consultar el estado real en el SII"
                        >
                          <Clock className="w-3 h-3" /> Estado guía SII
                        </button>
                      )}
                      {dte?.pdf_url && (
                        <button
                          onClick={() => window.open(dte.pdf_url, '_blank', 'noopener,noreferrer')}
                          className="px-2.5 py-1.5 text-xs bg-blue-500/10 text-blue-600 dark:text-blue-400 rounded-lg hover:bg-blue-500/20 flex items-center gap-1"
                          title="Ver el PDF de la guía electrónica (SII)"
                        >
                          <FileText className="w-3 h-3" /> PDF SII
                        </button>
                      )}
                      {d.guia_firmada_archivo && (
                        <button
                          onClick={() => abrirDocumento(d.guia_firmada_archivo!)}
                          className="px-2.5 py-1.5 text-xs bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 rounded-lg hover:bg-emerald-500/20 flex items-center gap-1"
                          title="Ver guía firmada"
                        >
                          <Eye className="w-3 h-3" /> Guía
                        </button>
                      )}
                      {d.estado !== 'en_preparacion' && d.estado !== 'anulado' && (
                        <button
                          onClick={() => onEditDespacho(d, folioParaModal(dtesListos, dte))}
                          className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]"
                          style={{ color: 'var(--text-muted)' }}
                          title="Editar transportista / N° expedición"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </div>
                  </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Action */}
          {detail.items.some((it: ItemRow) => it.qty_disponible > 0) && (
            <div className="space-y-1.5">
              <button
                onClick={onCrearDespacho}
                className="btn-primary w-full justify-center"
              >
                <Plus className="w-4 h-4" /> Crear Despacho
              </button>
              <p className="text-[11px] text-center" style={{ color: 'var(--text-faint)' }}>
                Eliges qué se despacha. La guía SII, el transportista y la confirmación van después, en la fila del despacho.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function DocumentosSection({
  cotizacionId,
  ocId,
  docs,
  numeroOc,
  numeroCot,
  embarques,
}: {
  cotizacionId: number
  ocId: number
  docs: DocumentosOc
  numeroOc?: string
  numeroCot?: string
  embarques?: EmbarqueResumen[]
}) {
  const MIME_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  const MIME_PDF = 'application/pdf'
  const tag = numeroOc || `OC-${ocId}`
  const cotTag = numeroCot || `COT-${cotizacionId}`

  const buttons = [
    {
      key: 'excel_oc',
      label: 'Excel OC',
      hint: 'Listado de items para picking',
      icon: <FileSpreadsheet className="w-4 h-4" />,
      enabled: docs.excel_oc,
      run: () =>
        downloadBlob(() => comprasAPI.downloadOcClienteExcel(ocId), `${tag}.xlsx`, MIME_XLSX),
    },
    {
      key: 'cot_formal',
      label: 'Cotización Formal',
      hint: 'Excel formal enviado al cliente',
      icon: <FileSpreadsheet className="w-4 h-4" />,
      enabled: docs.cot_formal_excel,
      run: () =>
        downloadBlob(
          () => cotizadorAPI.downloadFormal(cotizacionId),
          `${cotTag}-formal.xlsx`,
          MIME_XLSX,
        ),
    },
    {
      key: 'cot_pdf',
      label: 'Cotización PDF',
      hint: 'PDF de la cotización formal',
      icon: <FileText className="w-4 h-4" />,
      enabled: docs.cot_pdf,
      run: () =>
        downloadBlob(
          () => cotizadorAPI.downloadPdf(cotizacionId),
          `${cotTag}.pdf`,
          MIME_PDF,
        ),
    },
    {
      key: 'cot_original',
      label: 'Excel Original',
      hint: 'Archivo Excel cargado al iniciar',
      icon: <FileDown className="w-4 h-4" />,
      enabled: docs.cot_original_excel,
      run: () =>
        downloadBlob(
          () => cotizacionesAPI.download(cotizacionId),
          `${cotTag}-original.xlsx`,
          MIME_XLSX,
        ),
    },
  ]

  return (
    <div className="space-y-4">
      <div>
        <div
          className="text-xs uppercase tracking-wider mb-2 font-semibold"
          style={{ color: 'var(--text-faint)' }}
        >
          Documentos de la Cotización
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {buttons.map(({ key, ...rest }) => (
            <DocButton key={key} {...rest} />
          ))}
        </div>
      </div>
      {embarques && embarques.length > 0 && (
        <div>
          <div
            className="text-xs uppercase tracking-wider mb-2 font-semibold"
            style={{ color: 'var(--text-faint)' }}
          >
            Embarques de Importación ({embarques.length})
          </div>
          <div className="space-y-2">
            {embarques.map(emb => (
              <EmbarqueDocsCard key={emb.id} emb={emb} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function EmbarqueDocsCard({ emb }: { emb: EmbarqueResumen }) {
  const labels: { key: keyof EmbarqueDocs; label: string }[] = [
    { key: 'awb', label: 'AWB / BL' },
    { key: 'factura_comercial', label: 'Factura Comercial' },
    { key: 'packing_list', label: 'Packing List' },
    { key: 'certificado_origen', label: 'Cert. Origen' },
    { key: 'doc_adicional', label: 'Otros' },
  ]
  return (
    <div
      className="border rounded-xl p-3"
      style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
    >
      <div className="flex flex-wrap items-center gap-2 mb-2 pb-2 border-b" style={{ borderColor: 'var(--border)' }}>
        <span className="font-mono text-sm font-semibold text-brand-500">{emb.numero}</span>
        {emb.estado && (
          <span className="text-xs px-2 py-0.5 rounded-full font-semibold bg-amber-500/15 text-amber-600 dark:text-amber-400">
            {emb.estado}
          </span>
        )}
        <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
          {emb.items_de_esta_oc} ítem{emb.items_de_esta_oc === 1 ? '' : 's'} de esta OC
        </span>
        {emb.forwarder && (
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
            · {emb.forwarder}
          </span>
        )}
        {emb.fecha_llegada_est && (
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
            · ETA: {emb.fecha_llegada_est}
          </span>
        )}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-2">
        {labels.map(({ key, label }) => (
          <EmbDocField key={key} label={label} doc={emb.documentos[key]} />
        ))}
      </div>
    </div>
  )
}

function EmbDocField({ label, doc }: { label: string; doc: DocValue | null }) {
  const [loading, setLoading] = useState(false)
  const [showTextModal, setShowTextModal] = useState(false)

  const handleClick = async () => {
    if (!doc) return
    if (doc.is_file && doc.filename) {
      // Real file: download via authenticated endpoint
      setLoading(true)
      try {
        const token = (() => {
          try {
            return JSON.parse(localStorage.getItem('machparts-auth') || '{}')?.state?.token || ''
          } catch {
            return ''
          }
        })()
        const res = await fetch(`/api/despachos/docs/${encodeURIComponent(doc.filename)}`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok) {
          const err = await res.json().catch(() => ({}))
          throw new Error(err.detail || 'Error al descargar')
        }
        const blob = await res.blob()
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = doc.filename
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        setTimeout(() => URL.revokeObjectURL(url), 1000)
      } catch (e: any) {
        toast.error(e?.message || 'Error al descargar')
      } finally {
        setLoading(false)
      }
    } else {
      // Text value: open modal with content + copy
      setShowTextModal(true)
    }
  }

  const isFile = doc?.is_file

  return (
    <>
      <div>
        <div
          className="text-[10px] uppercase tracking-wider mb-0.5"
          style={{ color: 'var(--text-faint)' }}
        >
          {label}
        </div>
        {!doc ? (
          <div
            className="text-xs px-2 py-1.5 rounded-lg border border-dashed"
            style={{ borderColor: 'var(--border)', color: 'var(--text-faint)' }}
          >
            —
          </div>
        ) : (
          <button
            onClick={handleClick}
            disabled={loading}
            title={isFile ? 'Descargar archivo' : 'Ver contenido'}
            className="w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg border transition-colors hover:bg-[var(--surface-200)] disabled:opacity-50 text-left"
            style={{
              borderColor: 'var(--border)',
              backgroundColor: 'var(--surface)',
              color: 'var(--text-primary)',
            }}
          >
            {loading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin text-emerald-500 shrink-0" />
            ) : (
              <FileText
                className={`w-3.5 h-3.5 shrink-0 ${isFile ? 'text-emerald-500' : 'text-brand-500'}`}
              />
            )}
            <span className="text-[11px] truncate flex-1" title={doc.value}>
              {doc.value}
            </span>
            <FileDown className="w-3 h-3 shrink-0 opacity-60" />
          </button>
        )}
      </div>

      {showTextModal && doc && (
        <TextDocModal
          label={label}
          value={doc.value}
          onClose={() => setShowTextModal(false)}
        />
      )}
    </>
  )
}

function TextDocModal({
  label,
  value,
  onClose,
}: {
  label: string
  value: string
  onClose: () => void
}) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('No se pudo copiar')
    }
  }

  const downloadTxt = () => {
    const blob = new Blob([value], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${label.replace(/[^a-z0-9]+/gi, '_').toLowerCase()}.txt`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-2xl border shadow-2xl overflow-hidden"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div>
            <h3 className="font-bold text-sm" style={{ color: 'var(--text-primary)' }}>
              {label}
            </h3>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              Contenido registrado (referencia / tracking)
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4">
          <pre
            className="text-sm whitespace-pre-wrap break-all p-3 rounded-lg border"
            style={{
              backgroundColor: 'var(--surface)',
              borderColor: 'var(--border)',
              color: 'var(--text-primary)',
              fontFamily: 'inherit',
            }}
          >
            {value}
          </pre>
        </div>
        <div
          className="p-3 border-t flex items-center justify-end gap-2"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <button onClick={downloadTxt} className="btn-secondary text-sm">
            <FileDown className="w-4 h-4" />
            Descargar .txt
          </button>
          <button onClick={copy} className="btn-primary text-sm">
            {copied ? (
              <>
                <CheckCircle2 className="w-4 h-4" />
                Copiado
              </>
            ) : (
              <>
                <FileText className="w-4 h-4" />
                Copiar
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}

function DocButton({
  label,
  hint,
  icon,
  enabled,
  run,
}: {
  label: string
  hint: string
  icon: React.ReactNode
  enabled: boolean
  run: () => Promise<void>
}) {
  const [loading, setLoading] = useState(false)
  const handle = async () => {
    if (!enabled || loading) return
    setLoading(true)
    try {
      await run()
    } finally {
      setLoading(false)
    }
  }
  return (
    <button
      onClick={handle}
      disabled={!enabled || loading}
      title={enabled ? hint : 'No disponible'}
      className="flex items-center gap-2 px-3 py-2.5 rounded-xl border transition-colors text-left disabled:opacity-40 disabled:cursor-not-allowed hover:bg-[var(--surface-200)]"
      style={{
        borderColor: 'var(--border)',
        backgroundColor: 'var(--surface-100)',
        color: enabled ? 'var(--text-primary)' : 'var(--text-faint)',
      }}
    >
      <span className={enabled ? 'text-emerald-500' : ''} style={enabled ? undefined : { color: 'var(--text-faint)' }}>
        {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : icon}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-xs font-semibold truncate">{label}</div>
        <div className="text-[10px] truncate" style={{ color: 'var(--text-muted)' }}>
          {enabled ? hint : 'No disponible'}
        </div>
      </div>
    </button>
  )
}

function DiasRestantesBadge({
  dias,
  label,
  compact = false,
}: {
  dias: number | null | undefined
  label?: string
  compact?: boolean
}) {
  if (dias === null || dias === undefined) {
    return compact ? (
      <span style={{ color: 'var(--text-faint)' }}>—</span>
    ) : null
  }
  const sufix = label ? ` · ${label}` : ''
  if (dias < 0) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-red-500/15 text-red-500">
        <AlertTriangle className="w-3 h-3" />
        Vencido {Math.abs(dias)}d
      </span>
    )
  }
  if (dias === 0) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-red-500/15 text-red-500">
        <Clock className="w-3 h-3" />
        Hoy{sufix}
      </span>
    )
  }
  let style = 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
  if (dias <= 3) style = 'bg-red-500/15 text-red-500'
  else if (dias <= 7) style = 'bg-amber-500/15 text-amber-600 dark:text-amber-400'
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold ${style}`}>
      <Clock className="w-3 h-3" />
      {dias}d{sufix}
    </span>
  )
}

const PIPELINE_SEGMENTS: { key: keyof ProgresoEstados; label: string; color: string }[] = [
  { key: 'pendiente',   label: 'Pendiente',    color: '#94a3b8' },
  { key: 'en_compras',  label: 'En Compras',   color: '#f59e0b' },
  { key: 'en_transito', label: 'En Tránsito',  color: '#8b5cf6' },
  { key: 'en_bodega',   label: 'En Bodega',    color: '#10b981' },
  { key: 'despachado',  label: 'Despachado',   color: '#1a5cf0' },
  { key: 'reclamo',     label: 'Reclamo',      color: '#ef4444' },
]

function PipelineProgress({ oc }: { oc: OcCard }) {
  const total = oc.total_items
  const estados = oc.progreso_estados
  if (!total || total === 0) return null

  const segments = PIPELINE_SEGMENTS.map(s => ({
    ...s,
    count: estados?.[s.key] ?? 0,
  })).filter(s => s.count > 0)

  return (
    <div className="mt-2 space-y-1.5">
      <div className="flex items-center justify-between text-[11px]">
        <span className="uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
          Estado de Avance
        </span>
        <span
          className={oc.progreso_pct === 100 ? 'text-emerald-500 font-semibold' : ''}
          style={oc.progreso_pct === 100 ? undefined : { color: 'var(--text-muted)' }}
        >
          {oc.items_despachados}/{total} despachados · {oc.progreso_pct}%
        </span>
      </div>
      <div
        className="h-2 rounded-full overflow-hidden flex"
        style={{ backgroundColor: 'var(--surface-300)' }}
      >
        {segments.map(s => (
          <div
            key={s.key}
            title={`${s.label}: ${s.count}`}
            className="h-full transition-all"
            style={{
              width: `${(s.count / total) * 100}%`,
              backgroundColor: s.color,
            }}
          />
        ))}
      </div>
      {/* Legend */}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px]" style={{ color: 'var(--text-muted)' }}>
        {segments.map(s => (
          <span key={s.key} className="inline-flex items-center gap-1">
            <span
              className="w-2 h-2 rounded-sm shrink-0"
              style={{ backgroundColor: s.color }}
            />
            {s.label} ({s.count})
          </span>
        ))}
      </div>
    </div>
  )
}

function ItemEstadoBadge({ estado, reclamo }: { estado: string; reclamo: boolean }) {
  if (reclamo) {
    return <span className="text-xs px-2 py-0.5 rounded-full font-semibold bg-red-500/15 text-red-500">Reclamo</span>
  }
  const map: Record<string, { label: string; color: string }> = {
    en_bodega: { label: 'En Bodega', color: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' },
    despachado: { label: 'Despachado', color: 'bg-blue-500/15 text-blue-600 dark:text-blue-400' },
    embarcado: { label: 'En Tránsito', color: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' },
    reclamo_proveedor: { label: 'Reclamo', color: 'bg-red-500/15 text-red-500' },
  }
  const cfg = map[estado] ?? { label: estado, color: 'bg-slate-500/15 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ${cfg.color}`}>{cfg.label}</span>
}

function DespachoEstadoBadge({ estado }: { estado: string }) {
  const map: Record<string, { label: string; color: string }> = {
    en_preparacion: { label: 'En Preparación', color: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' },
    despachado: { label: 'Despachado', color: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' },
    anulado: { label: 'Anulado', color: 'bg-slate-500/15 text-slate-500' },
  }
  const cfg = map[estado] ?? { label: estado, color: 'bg-slate-500/15 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ${cfg.color}`}>{cfg.label}</span>
}

function CrearDespachoModal({ oc, onClose, onCreated }: any) {
  // Este modal es el PASO 1 del despacho: definir QUÉ se despacha (ítems + a quién).
  // Los datos de transporte (transportista, N° de expedición) y el N° de guía NO se
  // piden aquí a propósito: la guía de despacho SII se emite en el paso 2 y su folio
  // lo asigna el SII; el transportista se agrega en el paso 3, ya con la guía emitida.
  // Ver EditarDespachoModal (paso 3) y EmitirGuiaSIIModal (paso 2).
  const [contacto, setContacto] = useState(oc.contacto || '')
  const [direccion, setDireccion] = useState(oc.direccion || '')
  const [observaciones, setObservaciones] = useState('')
  const [selectedItems, setSelectedItems] = useState<Record<number, number>>({})

  const disponibles = useMemo(
    () => oc.items.filter((it: ItemRow) => it.qty_disponible > 0),
    [oc.items]
  )

  const toggleItem = (it: ItemRow) => {
    setSelectedItems(prev => {
      const copy = { ...prev }
      if (copy[it.id] !== undefined) {
        delete copy[it.id]
      } else {
        copy[it.id] = it.qty_disponible
      }
      return copy
    })
  }

  const updateQty = (id: number, value: number) => {
    // Nunca menos de 1: un 0 o negativo dejaría el ítem "seleccionado" y el
    // botón Crear habilitado, para chocar recién con el 400 del backend.
    setSelectedItems(prev => ({ ...prev, [id]: Math.max(1, value) }))
  }

  const seleccionTotal = Object.keys(selectedItems).length

  const createMut = useMutation({
    mutationFn: () =>
      despachosAPI.create({
        oc_cliente_id: oc.id,
        // transportista / numero_guia / numero_expedicion se omiten a propósito:
        // el backend los acepta nulos y se completan en los pasos 2 y 3 del flujo.
        contacto_destinatario: contacto || null,
        direccion_entrega: direccion || null,
        observaciones: observaciones || null,
        items: Object.entries(selectedItems).map(([id, qty]) => ({
          item_cotizacion_id: Number(id),
          qty_despachada: qty,
        })),
      }),
    onSuccess: (data: any) => {
      toast.success(`Despacho ${data.numero_despacho} creado. Ahora emite la guía SII, agrega el transportista y confirma el despacho.`)
      onCreated()
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Error al crear'),
  })

  return (
    <div
      className="fixed inset-0 flex items-center justify-center z-50 p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="max-w-3xl w-full max-h-[90vh] overflow-hidden flex flex-col rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div>
            <h2 className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>
              Crear despacho
            </h2>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              OC {oc.numero_oc} · {oc.cliente}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 space-y-4 overflow-y-auto flex-1">
          {/* Guía del flujo: este modal es solo el paso 1 de 4. */}
          <div
            className="flex items-start gap-2 p-3 rounded-xl border text-xs"
            style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' }}
          >
            <Receipt className="w-4 h-4 shrink-0 mt-0.5 text-blue-500" />
            <span>
              <b style={{ color: 'var(--text-primary)' }}>Paso 1 de 4:</b> elige qué se despacha.
              Luego, en la fila del despacho: <b style={{ color: 'var(--text-primary)' }}>emites la guía SII</b> (el
              folio lo asigna el SII), <b style={{ color: 'var(--text-primary)' }}>agregas el transportista</b> y
              <b style={{ color: 'var(--text-primary)' }}> confirmas el despacho</b>.
            </span>
          </div>

          {/* Destinatario (la guía SII usa la ficha del cliente en Wasabil; estos
              datos son de referencia interna del despacho y vienen desde la OC). */}
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Contacto Destinatario"
              value={contacto}
              onChange={setContacto}
            />
            <Input
              label="Dirección de Entrega"
              value={direccion}
              onChange={setDireccion}
            />
          </div>
          <div>
            <label
              className="block text-xs font-semibold uppercase tracking-wider mb-1.5"
              style={{ color: 'var(--text-faint)' }}
            >
              Observaciones
            </label>
            <textarea
              value={observaciones}
              onChange={e => setObservaciones(e.target.value)}
              rows={2}
              className="input resize-none"
              placeholder="Opcional..."
            />
          </div>

          {/* Items disponibles */}
          <div>
            <div
              className="text-xs uppercase tracking-wider mb-2 font-semibold"
              style={{ color: 'var(--text-faint)' }}
            >
              Items disponibles ({disponibles.length})
            </div>
            {disponibles.length === 0 ? (
              <div className="text-center py-4" style={{ color: 'var(--text-faint)' }}>
                No hay items disponibles para despacho
              </div>
            ) : (
              <div className="border rounded-xl overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                {disponibles.map((it: ItemRow) => {
                  const selected = selectedItems[it.id] !== undefined
                  return (
                    <div
                      key={it.id}
                      className="p-3 flex items-center gap-3 border-b last:border-0 transition-colors"
                      style={{
                        borderColor: 'var(--border)',
                        backgroundColor: selected ? 'rgba(26, 92, 240, 0.06)' : 'transparent',
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => toggleItem(it)}
                        className="w-4 h-4 accent-brand-500"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="font-mono text-xs text-brand-500 font-semibold">{it.numero_parte}</div>
                        <div className="text-sm truncate" style={{ color: 'var(--text-primary)' }}>
                          {it.descripcion}
                        </div>
                        <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{it.marca}</div>
                      </div>
                      {selected ? (
                        <input
                          type="number"
                          min={1}
                          max={it.qty_disponible}
                          value={selectedItems[it.id]}
                          onChange={e =>
                            updateQty(it.id, Math.min(it.qty_disponible, Number(e.target.value) || 0))
                          }
                          className="input w-20 text-right py-1.5 px-2"
                        />
                      ) : (
                        <span
                          className="text-sm w-20 text-right"
                          style={{ color: 'var(--text-muted)' }}
                        >
                          /{it.qty_disponible} disp.
                        </span>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </div>

        <div
          className="p-4 border-t flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div className="text-sm" style={{ color: 'var(--text-muted)' }}>
            {seleccionTotal} ítem{seleccionTotal === 1 ? '' : 's'} seleccionado{seleccionTotal === 1 ? '' : 's'}
          </div>
          <div className="flex gap-2">
            <button onClick={onClose} className="btn-secondary text-sm">
              Cancelar
            </button>
            <button
              onClick={() => createMut.mutate()}
              disabled={seleccionTotal === 0 || createMut.isPending}
              className="btn-primary text-sm"
            >
              <Plus className="w-4 h-4" />
              {createMut.isPending ? 'Creando...' : 'Crear Despacho'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function Input({ label, value, onChange, placeholder, disabled, hint }: any) {
  return (
    <div>
      <label
        className="block text-xs font-semibold uppercase tracking-wider mb-1.5"
        style={{ color: 'var(--text-faint)' }}
      >
        {label}
      </label>
      <input
        type="text"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className={`input ${disabled ? 'opacity-60 cursor-not-allowed' : ''}`}
      />
      {hint && (
        <p className="text-[11px] mt-1" style={{ color: 'var(--text-faint)' }}>{hint}</p>
      )}
    </div>
  )
}

// ─── Modal: marcar guía firmada (subir foto/PDF de la guía firmada) ───────────
function FirmarGuiaModal({
  despachoId,
  dteFolio,
  onClose,
  onDone,
}: {
  despachoId: number
  dteFolio?: string | null
  onClose: () => void
  onDone: () => void
}) {
  const [file, setFile] = useState<File | null>(null)
  const [numeroGuia, setNumeroGuia] = useState('')
  // hoyLocal() y no toISOString(): en Chile (UTC-3/-4) la fecha UTC ya es
  // "mañana" pasadas las ~21:00 y la firma quedaría con fecha futura.
  const [fecha, setFecha] = useState(hoyLocal())
  const [saving, setSaving] = useState(false)

  // Mismo candado que EditarDespachoModal (ver folioParaModal): con guía
  // electrónica emitida/en proceso, el N° es (o será) el folio del SII.
  const folioBloqueado = dteFolio !== null && dteFolio !== undefined

  const submit = async () => {
    if (!file) {
      toast.error('Sube la foto o PDF de la guía firmada')
      return
    }
    setSaving(true)
    try {
      const up = await despachosAPI.uploadDoc(file)
      await despachosAPI.firmar(despachoId, {
        fecha_firma: fecha,
        // Con folio SII, el N° de guía no viaja (el backend además lo rechaza)
        numero_guia: folioBloqueado ? undefined : (numeroGuia || undefined),
        archivo: up.filename,
      })
      toast.success('Guía firmada — ya se puede facturar en Contabilidad')
      onDone()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Error al firmar la guía')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 flex items-center justify-center z-50 p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="max-w-md w-full rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <h2 className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>
            Marcar guía firmada
          </h2>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]" style={{ color: 'var(--text-muted)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            Sube la <b>foto o PDF de la guía firmada</b> por el cliente. Quedará disponible en Contabilidad
            para cobrar (OC + factura + guía firmada).
          </p>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
              Foto / PDF de la guía firmada
            </label>
            <label className="flex items-center gap-2 px-3 py-2 rounded-lg border cursor-pointer hover:bg-[var(--surface-200)]" style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
              <Upload className="w-4 h-4" />
              <span className="text-sm truncate">{file ? file.name : 'Seleccionar archivo…'}</span>
              <input
                type="file"
                accept="image/*,application/pdf"
                onChange={e => setFile(e.target.files?.[0] || null)}
                className="hidden"
              />
            </label>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="N° Guía (opcional)"
              value={folioBloqueado && dteFolio !== 'verificando' && dteFolio !== 'en_emision' ? dteFolio : numeroGuia}
              onChange={setNumeroGuia}
              placeholder="Si falta"
              disabled={folioBloqueado}
              hint={
                dteFolio === 'verificando'
                  ? 'Verificando si este despacho tiene guía electrónica…'
                  : dteFolio === 'en_emision'
                    ? 'Guía electrónica en emisión: el N° quedará fijado por el folio del SII.'
                    : folioBloqueado
                      ? `Guía electrónica emitida al SII (folio ${dteFolio}): el N° no se edita a mano.`
                      : undefined
              }
            />
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                Fecha de firma
              </label>
              <input type="date" value={fecha} onChange={e => setFecha(e.target.value)} className="input" />
            </div>
          </div>
        </div>
        <div className="p-4 border-t flex justify-end gap-2" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <button onClick={onClose} className="btn-secondary text-sm">Cancelar</button>
          <button onClick={submit} disabled={saving || !file} className="btn-primary text-sm flex items-center gap-1.5">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />} Confirmar firma
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Modal: editar transportista / N° de expedición / N° de guía ──────────────
function EditarDespachoModal({
  despacho,
  dteFolio,
  onClose,
  onSaved,
}: {
  despacho: DespachoRow
  dteFolio?: string | null
  onClose: () => void
  onSaved: () => void
}) {
  const [transportista, setTransportista] = useState(despacho.transportista || '')
  const [numeroExpedicion, setNumeroExpedicion] = useState(despacho.numero_expedicion || '')
  const [numeroGuia, setNumeroGuia] = useState(despacho.numero_guia || '')
  const [saving, setSaving] = useState(false)

  // dteFolio: null = sin guía electrónica · 'verificando' = consulta en curso ·
  // 'en_emision' = guía en proceso en el SII (ver folioParaModal) · otro valor =
  // folio SII real. Cualquier valor no-null bloquea la edición manual del N°.
  const folioBloqueado = dteFolio !== null && dteFolio !== undefined

  const submit = async () => {
    setSaving(true)
    try {
      await despachosAPI.update(despacho.id, {
        transportista: transportista || null,
        numero_expedicion: numeroExpedicion || null,
        // Guía electrónica emitida (o sin verificar): el folio no se edita a mano
        ...(folioBloqueado ? {} : { numero_guia: numeroGuia || null }),
      })
      toast.success('Datos del despacho actualizados')
      onSaved()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Error al guardar')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 flex items-center justify-center z-50 p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="max-w-md w-full rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <h2 className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>
            Editar {despacho.numero_despacho}
          </h2>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]" style={{ color: 'var(--text-muted)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4 space-y-3">
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            Puedes completar el transportista o el N° de expedición en cualquier momento del despacho.
          </p>
          <Input label="Transportista" value={transportista} onChange={setTransportista} placeholder="Ej: Samex" />
          <Input label="N° Expedición (courier / Samex)" value={numeroExpedicion} onChange={setNumeroExpedicion} />
          <Input
            label="N° Guía"
            value={numeroGuia}
            onChange={setNumeroGuia}
            disabled={folioBloqueado}
            hint={
              dteFolio === 'verificando'
                ? 'Verificando si este despacho tiene guía electrónica… (reabre el detalle para editar)'
                : dteFolio === 'en_emision'
                  ? 'Guía electrónica en emisión: el N° quedará fijado por el folio del SII al terminar.'
                  : folioBloqueado
                    ? `Guía electrónica emitida al SII (folio ${dteFolio}): el N° no se edita a mano.`
                    : 'Solo para guía en papel. Si vas a emitir la guía SII, déjalo vacío: el folio lo pone el SII.'
            }
          />
        </div>
        <div className="p-4 border-t flex justify-end gap-2" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <button onClick={onClose} className="btn-secondary text-sm">Cancelar</button>
          <button onClick={submit} disabled={saving} className="btn-primary text-sm flex items-center gap-1.5">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />} Guardar
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Modal: emitir guía de despacho electrónica (SII 52) vía Wasabil ──────────
// Protocolo de seguridad: PASO 1 previsualización (no toca el SII) → PASO 2 con
// el OK explícito del usuario se emite (IRREVERSIBLE) → sondeo del estado hasta
// Emitido (folio real + PDF) o Fallido (motivo del SII + reintento seguro).
const fmtCLP = (n: number) => '$' + Math.round(n || 0).toLocaleString('es-CL')

function EmitirGuiaSIIModal({
  despacho,
  onClose,
  onDone,
}: {
  despacho: DespachoRow
  onClose: () => void
  onDone: () => void
}) {
  type Fase = 'cargando' | 'preview' | 'error_carga' | 'emitiendo' | 'sondeo' | 'exito' | 'fallido' | 'pendiente'
  const [fase, setFase] = useState<Fase>('cargando')
  const [preview, setPreview] = useState<any>(null)
  const [dte, setDte] = useState<any>(null)
  const [error, setError] = useState('')
  // Tipo de traslado del SII (dispatchTypeCode): 1 venta por defecto, 5 traslado
  // interno hacia bodega propia, etc. El operador lo elige antes de emitir.
  const [tipoTraslado, setTipoTraslado] = useState(1)

  // Decide la fase según el DTE previo (única fuente: puede_reintentar del backend)
  const faseSegunDte = (d: any): Fase => {
    if (!d || d.estado === 'no_enviado') return 'preview'
    if (d.estado === 'emitido') return 'exito'
    if (d.puede_reintentar) return 'fallido'          // fallido del SII o error de envío
    if (d.uuid) return 'sondeo'                       // procesando/pendiente en Wasabil
    return 'pendiente'                                // claim en vuelo de otro intento
  }

  // PASO 1: cargar la previsualización (si ya había una emisión previa, retomarla).
  // También se usa para RESINCRONIZAR tras un error al emitir.
  const cargarPreview = () => {
    setFase('cargando')
    wasabilAPI.previewGuia(despacho.id)
      .then(({ data }) => {
        setPreview(data)
        if (data.dte) setDte(data.dte)
        setFase(faseSegunDte(data.dte))
      })
      .catch((e: any) => {
        setError(e?.response?.data?.detail || 'No se pudo cargar la previsualización')
        setFase('error_carga')
      })
  }
  useEffect(cargarPreview, [despacho.id])

  // Sondeo: el envío al SII es asíncrono (segundos a minutos)
  useEffect(() => {
    if (fase !== 'sondeo') return
    let vivo = true
    let intentos = 0
    const tick = async () => {
      if (!vivo) return
      intentos += 1
      try {
        const { data } = await wasabilAPI.estadoGuia(despacho.id)
        if (!vivo) return
        setDte(data)
        if (data.estado === 'emitido') { setFase('exito'); onDone(); return }
        if (data.puede_reintentar) { setFase('fallido'); return }
      } catch { /* error transitorio: se reintenta en el próximo tick */ }
      if (intentos >= 30) { setFase('pendiente'); return }  // ~90 s: seguir después
      window.setTimeout(tick, 3000)
    }
    tick()
    return () => { vivo = false }
  }, [fase, despacho.id])

  // PASO 2: emitir (solo tras ver la previsualización) o reintentar un fallido
  const emitir = async (reintento: boolean) => {
    setFase('emitiendo')
    setError('')
    try {
      const { data } = reintento
        ? await wasabilAPI.reintentarGuia(despacho.id, tipoTraslado)
        : await wasabilAPI.emitirGuia(despacho.id, tipoTraslado)
      setDte(data)
      if (data.estado === 'emitido') { setFase('exito'); onDone() }
      else if (data.puede_reintentar) setFase('fallido')
      else setFase('sondeo')
    } catch (e: any) {
      // 409/502/timeout: RESINCRONIZAR con el backend en vez de adivinar la fase
      // (el estado real pudo cambiar: claim en curso, fallido, incluso emitido)
      setError(e?.response?.data?.detail || 'No se pudo emitir la guía')
      cargarPreview()
    }
  }

  const receptor = preview?.receptor
  const referencia = preview?.referencias?.[0]

  return (
    <div
      className="fixed inset-0 flex items-center justify-center z-50 p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="max-w-2xl w-full max-h-[90vh] overflow-hidden flex flex-col rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div>
            <h2 className="text-lg font-bold flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
              <Receipt className="w-5 h-5 text-blue-500" /> Emitir guía de despacho SII
            </h2>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {despacho.numero_despacho} · vía Wasabil (GRUPO AM SPA)
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]" style={{ color: 'var(--text-muted)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 space-y-4 overflow-y-auto">
          {fase === 'cargando' && (
            <div className="py-10 text-center" style={{ color: 'var(--text-muted)' }}>
              <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" /> Armando la previsualización…
            </div>
          )}

          {(fase === 'emitiendo' || fase === 'sondeo') && (
            <div className="py-10 text-center" style={{ color: 'var(--text-muted)' }}>
              <Loader2 className="w-8 h-8 animate-spin mx-auto mb-3 text-blue-500" />
              <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                {fase === 'emitiendo' ? 'Enviando a Wasabil…' : 'Procesando en el SII…'}
              </p>
              <p className="text-xs mt-1">El SII puede tardar de segundos a un par de minutos. No cierres esta ventana.</p>
            </div>
          )}

          {fase === 'exito' && dte && (
            <div className="py-8 text-center space-y-3">
              <CheckCircle2 className="w-10 h-10 mx-auto text-emerald-500" />
              <p className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>
                Guía emitida — Folio SII {dte.folio}
              </p>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                El folio quedó registrado en el despacho. Imprime el PDF para que viaje con la carga.
                Cierra esta ventana para agregar el transportista y confirmar el despacho.
              </p>
              {dte.pdf_url && (
                <button
                  onClick={() => window.open(dte.pdf_url, '_blank', 'noopener,noreferrer')}
                  className="btn-primary text-sm inline-flex items-center gap-1.5"
                >
                  <FileText className="w-4 h-4" /> Ver PDF de la guía
                </button>
              )}
            </div>
          )}

          {fase === 'pendiente' && (
            <div className="py-8 text-center space-y-2">
              <Clock className="w-8 h-8 mx-auto text-amber-500" />
              <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>Emisión en curso</p>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                Hay una emisión en proceso para este despacho (puede ser de otra pestaña u
                otro usuario). Puedes cerrar esta ventana: el estado se actualizará al volver
                a abrir el despacho y <b>no se emitirá dos veces</b>.
              </p>
            </div>
          )}

          {fase === 'error_carga' && (
            <div className="py-8 text-center space-y-2">
              <AlertTriangle className="w-8 h-8 mx-auto text-red-500" />
              <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                No se pudo cargar la previsualización
              </p>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>{error}</p>
              <button onClick={cargarPreview} className="btn-secondary text-sm">Volver a intentar</button>
            </div>
          )}

          {fase === 'fallido' && (
            <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30 space-y-1">
              <p className="text-sm font-semibold text-red-500 flex items-center gap-1.5">
                <AlertTriangle className="w-4 h-4" />
                {dte?.estado === 'error_envio' ? 'La emisión no llegó a Wasabil' : 'El SII rechazó la guía'}
              </p>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                {error || dte?.error || 'Sin detalle del motivo'}
              </p>
            </div>
          )}

          {(fase === 'preview' || fase === 'fallido') && preview && (
            <>
              {preview.problemas?.length > 0 && fase === 'preview' && (
                <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30">
                  <p className="text-xs font-semibold text-red-500 mb-1">Para emitir falta resolver:</p>
                  <ul className="text-xs space-y-0.5 list-disc pl-4" style={{ color: 'var(--text-muted)' }}>
                    {preview.problemas.map((p: string, i: number) => <li key={i}>{p}</li>)}
                  </ul>
                </div>
              )}
              {preview.advertencias?.length > 0 && (
                <div className="p-3 rounded-xl border bg-amber-500/10 border-amber-500/30">
                  <ul className="text-xs space-y-0.5 list-disc pl-4" style={{ color: 'var(--text-muted)' }}>
                    {preview.advertencias.map((a: string, i: number) => <li key={i}>{a}</li>)}
                  </ul>
                </div>
              )}

              {/* Receptor (ficha real en Wasabil = lo que verá el SII) */}
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                    Receptor {receptor?.fuente === 'wasabil' ? '(ficha Wasabil)' : '(datos locales)'}
                  </div>
                  <div className="font-semibold" style={{ color: 'var(--text-primary)' }}>{receptor?.razon_social || '—'}</div>
                  <div className="text-xs" style={{ color: 'var(--text-muted)' }}>RUT {receptor?.rut || '—'}</div>
                  {receptor?.giro && <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{receptor.giro}</div>}
                  {receptor?.direccion && (
                    <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      {receptor.direccion}{receptor.comuna ? `, ${receptor.comuna}` : ''}
                    </div>
                  )}
                </div>
                <div>
                  <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                    Referencia (SII 801)
                  </div>
                  <div className="font-semibold" style={{ color: 'var(--text-primary)' }}>OC {referencia?.folio || '—'}</div>
                  <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    Fecha OC: {referencia?.fecha || '—'}
                  </div>
                  <label className="block mt-2">
                    <span className="text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                      Tipo de traslado (SII)
                    </span>
                    <select
                      value={tipoTraslado}
                      onChange={(e) => setTipoTraslado(Number(e.target.value))}
                      disabled={fase !== 'preview' && fase !== 'fallido'}
                      className="w-full mt-0.5 px-2 py-1 rounded-lg text-xs border disabled:opacity-60"
                      style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
                    >
                      {(preview.tipos_traslado ?? [{ codigo: 1, label: 'Operación constituye venta' }]).map(
                        (t: { codigo: number; label: string }) => (
                          <option key={t.codigo} value={t.codigo}>{t.label}</option>
                        ),
                      )}
                    </select>
                  </label>
                </div>
              </div>

              {/* Líneas */}
              {preview.lineas?.length > 0 && (
                <div className="border rounded-xl overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                  <table className="w-full text-xs">
                    <thead>
                      <tr style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)' }}>
                        <th className="p-2 text-left font-semibold uppercase tracking-wider">Ítem</th>
                        <th className="p-2 text-right font-semibold uppercase tracking-wider">Cant.</th>
                        <th className="p-2 text-right font-semibold uppercase tracking-wider">P. unit. neto</th>
                        <th className="p-2 text-right font-semibold uppercase tracking-wider">Total neto</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.lineas.map((ln: any, i: number) => (
                        <tr key={i} className="border-t" style={{ borderColor: 'var(--border)' }}>
                          <td className="p-2">
                            <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>{ln.name}</span>
                            {ln.description && (
                              <span className="block text-[11px]" style={{ color: 'var(--text-faint)' }}>{ln.description}</span>
                            )}
                          </td>
                          <td className="p-2 text-right" style={{ color: 'var(--text-primary)' }}>{ln.quantity}</td>
                          <td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtCLP(ln.price)}</td>
                          <td className="p-2 text-right font-semibold" style={{ color: 'var(--text-primary)' }}>
                            {fmtCLP(ln.price * ln.quantity)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot style={{ backgroundColor: 'var(--surface-200)' }}>
                      <tr className="border-t" style={{ borderColor: 'var(--border)' }}>
                        <td colSpan={3} className="p-2 text-right text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Neto</td>
                        <td className="p-2 text-right font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtCLP(preview.totales?.neto)}</td>
                      </tr>
                      <tr>
                        <td colSpan={3} className="p-2 text-right text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>IVA 19%</td>
                        <td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtCLP(preview.totales?.iva)}</td>
                      </tr>
                      <tr>
                        <td colSpan={3} className="p-2 text-right text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Total</td>
                        <td className="p-2 text-right font-bold text-blue-500">{fmtCLP(preview.totales?.total)}</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              )}

              {error && fase === 'preview' && (
                <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30 text-xs text-red-500">{error}</div>
              )}
            </>
          )}
        </div>

        {(fase === 'preview' || fase === 'fallido') && (
          <div className="p-4 border-t space-y-2" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
            <p className="text-[11px] flex items-center gap-1.5" style={{ color: 'var(--text-muted)' }}>
              <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0" />
              Al confirmar, la guía se emite al SII a través de Wasabil. Esta acción es IRREVERSIBLE.
            </p>
            <div className="flex justify-end gap-2">
              <button onClick={onClose} className="btn-secondary text-sm">Cancelar</button>
              {fase === 'fallido' ? (
                <button onClick={() => emitir(true)} className="btn-primary text-sm flex items-center gap-1.5">
                  <Send className="w-4 h-4" /> Reintentar emisión
                </button>
              ) : (
                <button
                  onClick={() => emitir(false)}
                  disabled={!preview?.puede_emitir}
                  className="btn-primary text-sm flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <Send className="w-4 h-4" /> Confirmar y emitir al SII
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
