import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { despachosAPI, cotizacionesAPI, cotizadorAPI, comprasAPI } from '../services/api'
import {
  Truck, Package, CheckCircle2, AlertCircle, Search, X,
  ChevronRight, ChevronDown, Plus, Trash2, Send,
  FileSpreadsheet, FileText, FileDown, Loader2,
  Clock, AlertTriangle,
} from 'lucide-react'
import toast from 'react-hot-toast'

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

function OcRow({
  oc,
  expanded,
  onExpand,
  detail,
  onCrearDespacho,
  onCerrarDespacho,
  onAnularDespacho,
}: any) {
  const badge = estadoLabel[oc.estado] ?? estadoLabel.pendiente
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
                {detail.despachos.map((d: DespachoRow) => (
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
                      </div>
                      <div className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
                        {d.items_count} ítems
                        {d.transportista && ` · ${d.transportista}`}
                        {d.numero_guia && ` · Guía: ${d.numero_guia}`}
                        {d.fecha_despacho && ` · ${new Date(d.fecha_despacho).toLocaleDateString('es-CL')}`}
                      </div>
                    </div>
                    {d.estado === 'en_preparacion' && (
                      <div className="flex gap-2 shrink-0">
                        <button
                          onClick={() => onCerrarDespacho(d.id)}
                          className="px-3 py-1.5 text-xs bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 rounded-lg hover:bg-emerald-500/25 flex items-center gap-1 font-semibold"
                        >
                          <Send className="w-3 h-3" /> Confirmar
                        </button>
                        <button
                          onClick={() => onAnularDespacho(d.id)}
                          className="px-3 py-1.5 text-xs bg-red-500/10 text-red-500 rounded-lg hover:bg-red-500/20"
                        >
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Action */}
          {detail.items.some((it: ItemRow) => it.qty_disponible > 0) && (
            <button
              onClick={onCrearDespacho}
              className="btn-primary w-full justify-center"
            >
              <Plus className="w-4 h-4" /> Crear Despacho
            </button>
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
  const [transportista, setTransportista] = useState('')
  const [numeroGuia, setNumeroGuia] = useState('')
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
    setSelectedItems(prev => ({ ...prev, [id]: value }))
  }

  const seleccionTotal = Object.keys(selectedItems).length

  const createMut = useMutation({
    mutationFn: () =>
      despachosAPI.create({
        oc_cliente_id: oc.id,
        transportista: transportista || null,
        numero_guia: numeroGuia || null,
        contacto_destinatario: contacto || null,
        direccion_entrega: direccion || null,
        observaciones: observaciones || null,
        items: Object.entries(selectedItems).map(([id, qty]) => ({
          item_cotizacion_id: Number(id),
          qty_despachada: qty,
        })),
      }),
    onSuccess: (data: any) => {
      toast.success(`Despacho ${data.numero_despacho} creado`)
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
              Nuevo Despacho
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
          {/* Datos despacho */}
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Transportista"
              value={transportista}
              onChange={setTransportista}
              placeholder="Ej: Chilexpress"
            />
            <Input
              label="N° Guía"
              value={numeroGuia}
              onChange={setNumeroGuia}
              placeholder="Manual / SII"
            />
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

function Input({ label, value, onChange, placeholder }: any) {
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
        className="input"
      />
    </div>
  )
}
