import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { despachosAPI } from '../services/api'
import {
  Truck, Package, CheckCircle2, AlertCircle, Search, X,
  ChevronRight, ChevronDown, Plus, FileText, Trash2, Send,
} from 'lucide-react'
import toast from 'react-hot-toast'

type Tab = 'listas' | 'en_curso' | 'historial'

interface OcCard {
  id: number
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
  estado: 'listo' | 'parcial' | 'completado' | 'pendiente'
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
}

const estadoLabel: Record<string, { label: string; color: string }> = {
  listo: { label: 'Listo para despacho', color: 'bg-emerald-500/20 text-emerald-300' },
  parcial: { label: 'Parcial', color: 'bg-amber-500/20 text-amber-300' },
  completado: { label: 'Completado', color: 'bg-blue-500/20 text-blue-300' },
  pendiente: { label: 'Pendiente bodega', color: 'bg-zinc-500/20 text-zinc-400' },
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
          color="text-blue-400"
          sub="Con ítems en bodega"
        />
        <KpiCard
          label="Items Disponibles"
          value={counts?.items_listos ?? 0}
          icon={<CheckCircle2 className="w-5 h-5" />}
          color="text-emerald-400"
          sub="Listos para despacho"
        />
        <KpiCard
          label="Items Despachados"
          value={counts?.items_despachados ?? 0}
          icon={<Truck className="w-5 h-5" />}
          color="text-purple-400"
          sub="Total histórico"
        />
        <KpiCard
          label="OCs En Curso"
          value={ocs.filter((o: OcCard) => o.estado === 'parcial').length}
          icon={<AlertCircle className="w-5 h-5" />}
          color="text-amber-400"
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
        <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
        <input
          type="text"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Buscar por cliente, N° COT u OC..."
          className="w-full pl-10 pr-10 py-2.5 bg-zinc-800/50 border border-zinc-700 rounded-lg text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-blue-500"
        />
        {search && (
          <button
            onClick={() => setSearch('')}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* List */}
      {isLoading ? (
        <div className="text-center text-zinc-500 py-12">Cargando...</div>
      ) : ocs.length === 0 ? (
        <div className="text-center text-zinc-500 py-12">
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
    <div className="bg-zinc-900/60 border border-zinc-800 rounded-xl p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs uppercase tracking-wider text-zinc-500">{label}</span>
        <span className={color}>{icon}</span>
      </div>
      <div className={`text-3xl font-bold ${color}`}>{value}</div>
      <div className="text-xs text-zinc-400 mt-1">{sub}</div>
    </div>
  )
}

function TabBtn({ active, onClick, children }: any) {
  return (
    <button
      onClick={onClick}
      className={`px-4 py-2 rounded-lg text-sm font-medium transition ${
        active
          ? 'bg-blue-500/20 text-blue-300 border border-blue-500/40'
          : 'bg-zinc-900/60 text-zinc-400 border border-zinc-800 hover:text-zinc-200'
      }`}
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
    <div className="bg-zinc-900/60 border border-zinc-800 rounded-xl overflow-hidden">
      <button
        onClick={onExpand}
        className="w-full p-4 flex items-center gap-4 hover:bg-zinc-900/80 transition text-left"
      >
        <div className="w-10 h-10 rounded-lg bg-blue-500/10 flex items-center justify-center text-blue-400 shrink-0">
          <Package className="w-5 h-5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs text-blue-400 font-mono">OC-{oc.numero_oc}</span>
            <span className={`text-xs px-2 py-0.5 rounded ${badge.color}`}>
              {badge.label}
            </span>
          </div>
          <div className="font-semibold text-zinc-100 truncate">{oc.cliente}</div>
          <div className="text-xs text-zinc-500 truncate">
            {oc.numero_cotizacion} · OC #{oc.numero_oc}
            {oc.fecha_oc && ` · ${oc.fecha_oc}`}
            {oc.cond_pago && ` · ${oc.cond_pago}`}
            {oc.fecha_entrega && ` · Entrega: ${oc.fecha_entrega}`}
          </div>
          {/* Progress bar */}
          <div className="mt-2">
            <div className="flex items-center justify-between text-xs mb-1">
              <span className="text-zinc-500 uppercase tracking-wider">Progreso Despacho</span>
              <span className={oc.progreso_pct === 100 ? 'text-emerald-400' : 'text-zinc-400'}>
                {oc.items_despachados}/{oc.total_items} · {oc.progreso_pct}%
              </span>
            </div>
            <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
              <div
                className={`h-full transition-all ${
                  oc.progreso_pct === 100 ? 'bg-emerald-500' : 'bg-blue-500'
                }`}
                style={{ width: `${oc.progreso_pct}%` }}
              />
            </div>
          </div>
        </div>
        <div className="text-zinc-500 shrink-0">
          {expanded ? <ChevronDown className="w-5 h-5" /> : <ChevronRight className="w-5 h-5" />}
        </div>
      </button>

      {expanded && detail && (
        <div className="border-t border-zinc-800 p-4 space-y-4">
          {/* Datos destinatario */}
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <div className="text-xs uppercase tracking-wider text-zinc-500 mb-1">Destinatario</div>
              <div className="text-zinc-200">{detail.contacto || '—'}</div>
              <div className="text-zinc-400 text-xs">{detail.telefono || ''}</div>
              <div className="text-zinc-400 text-xs">{detail.email || ''}</div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider text-zinc-500 mb-1">Dirección de Entrega</div>
              <div className="text-zinc-200 text-xs">{detail.direccion || 'Sin dirección'}</div>
            </div>
          </div>

          {/* Items */}
          <div>
            <div className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
              Items ({detail.items.length})
            </div>
            <div className="border border-zinc-800 rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-zinc-800/50 text-xs uppercase text-zinc-400">
                  <tr>
                    <th className="text-left p-2">N° Parte</th>
                    <th className="text-left p-2">Descripción</th>
                    <th className="text-left p-2">Marca</th>
                    <th className="text-right p-2">Cant.</th>
                    <th className="text-right p-2">Despachado</th>
                    <th className="text-right p-2">Disponible</th>
                    <th className="text-center p-2">Estado</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.items.map((it: ItemRow) => (
                    <tr key={it.id} className="border-t border-zinc-800">
                      <td className="p-2 font-mono text-xs text-blue-300">{it.numero_parte}</td>
                      <td className="p-2 text-zinc-300">{it.descripcion}</td>
                      <td className="p-2 text-zinc-400 text-xs">{it.marca}</td>
                      <td className="p-2 text-right text-zinc-300">{it.cantidad}</td>
                      <td className="p-2 text-right text-zinc-400">{it.qty_despachada}</td>
                      <td className="p-2 text-right">
                        <span className={it.qty_disponible > 0 ? 'text-emerald-400 font-semibold' : 'text-zinc-600'}>
                          {it.qty_disponible}
                        </span>
                      </td>
                      <td className="p-2 text-center">
                        <ItemEstadoBadge estado={it.estado_item} reclamo={it.en_reclamo} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Despachos */}
          {detail.despachos.length > 0 && (
            <div>
              <div className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
                Despachos ({detail.despachos.length})
              </div>
              <div className="space-y-2">
                {detail.despachos.map((d: DespachoRow) => (
                  <div
                    key={d.id}
                    className="flex items-center justify-between p-3 bg-zinc-800/30 border border-zinc-800 rounded-lg"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-sm text-zinc-200">{d.numero_despacho}</span>
                        <DespachoEstadoBadge estado={d.estado} />
                      </div>
                      <div className="text-xs text-zinc-500 mt-0.5">
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
                          className="px-3 py-1.5 text-xs bg-emerald-500/20 text-emerald-300 rounded hover:bg-emerald-500/30 flex items-center gap-1"
                        >
                          <Send className="w-3 h-3" /> Confirmar
                        </button>
                        <button
                          onClick={() => onAnularDespacho(d.id)}
                          className="px-3 py-1.5 text-xs bg-red-500/10 text-red-400 rounded hover:bg-red-500/20"
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
              className="w-full py-2.5 bg-blue-500/20 text-blue-300 border border-blue-500/40 rounded-lg hover:bg-blue-500/30 flex items-center justify-center gap-2 font-medium"
            >
              <Plus className="w-4 h-4" /> Crear Despacho
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function ItemEstadoBadge({ estado, reclamo }: { estado: string; reclamo: boolean }) {
  if (reclamo) {
    return <span className="text-xs px-2 py-0.5 rounded bg-red-500/20 text-red-300">Reclamo</span>
  }
  const map: Record<string, { label: string; color: string }> = {
    en_bodega: { label: 'En Bodega', color: 'bg-emerald-500/20 text-emerald-300' },
    despachado: { label: 'Despachado', color: 'bg-blue-500/20 text-blue-300' },
    embarcado: { label: 'En Tránsito', color: 'bg-amber-500/20 text-amber-300' },
    reclamo_proveedor: { label: 'Reclamo', color: 'bg-red-500/20 text-red-300' },
  }
  const cfg = map[estado] ?? { label: estado, color: 'bg-zinc-500/20 text-zinc-400' }
  return <span className={`text-xs px-2 py-0.5 rounded ${cfg.color}`}>{cfg.label}</span>
}

function DespachoEstadoBadge({ estado }: { estado: string }) {
  const map: Record<string, { label: string; color: string }> = {
    en_preparacion: { label: 'En Preparación', color: 'bg-amber-500/20 text-amber-300' },
    despachado: { label: 'Despachado', color: 'bg-emerald-500/20 text-emerald-300' },
    anulado: { label: 'Anulado', color: 'bg-zinc-500/20 text-zinc-400' },
  }
  const cfg = map[estado] ?? { label: estado, color: 'bg-zinc-500/20 text-zinc-400' }
  return <span className={`text-xs px-2 py-0.5 rounded ${cfg.color}`}>{cfg.label}</span>
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
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={onClose}
    >
      <div
        className="bg-zinc-900 border border-zinc-800 rounded-xl max-w-3xl w-full max-h-[90vh] overflow-hidden flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        <div className="p-4 border-b border-zinc-800 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold text-zinc-100">Nuevo Despacho</h2>
            <p className="text-xs text-zinc-500">
              OC {oc.numero_oc} · {oc.cliente}
            </p>
          </div>
          <button onClick={onClose} className="text-zinc-500 hover:text-zinc-300">
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
            <label className="text-xs uppercase tracking-wider text-zinc-500 mb-1 block">
              Observaciones
            </label>
            <textarea
              value={observaciones}
              onChange={e => setObservaciones(e.target.value)}
              rows={2}
              className="w-full px-3 py-2 bg-zinc-800/50 border border-zinc-700 rounded-lg text-sm text-zinc-100 focus:outline-none focus:border-blue-500"
              placeholder="Opcional..."
            />
          </div>

          {/* Items disponibles */}
          <div>
            <div className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
              Items disponibles ({disponibles.length})
            </div>
            {disponibles.length === 0 ? (
              <div className="text-center text-zinc-500 py-4">
                No hay items disponibles para despacho
              </div>
            ) : (
              <div className="border border-zinc-800 rounded-lg overflow-hidden">
                {disponibles.map((it: ItemRow) => {
                  const selected = selectedItems[it.id] !== undefined
                  return (
                    <div
                      key={it.id}
                      className={`p-3 flex items-center gap-3 border-b border-zinc-800 last:border-0 ${
                        selected ? 'bg-blue-500/5' : ''
                      }`}
                    >
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => toggleItem(it)}
                        className="w-4 h-4 accent-blue-500"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="font-mono text-xs text-blue-300">{it.numero_parte}</div>
                        <div className="text-sm text-zinc-200 truncate">{it.descripcion}</div>
                        <div className="text-xs text-zinc-500">{it.marca}</div>
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
                          className="w-20 px-2 py-1 bg-zinc-800 border border-zinc-700 rounded text-sm text-zinc-100 text-right"
                        />
                      ) : (
                        <span className="text-sm text-zinc-400 w-20 text-right">
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

        <div className="p-4 border-t border-zinc-800 flex items-center justify-between">
          <div className="text-sm text-zinc-400">
            {seleccionTotal} ítem{seleccionTotal === 1 ? '' : 's'} seleccionado{seleccionTotal === 1 ? '' : 's'}
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm text-zinc-400 hover:text-zinc-200"
            >
              Cancelar
            </button>
            <button
              onClick={() => createMut.mutate()}
              disabled={seleccionTotal === 0 || createMut.isPending}
              className="px-4 py-2 text-sm bg-blue-500 text-white rounded-lg hover:bg-blue-600 disabled:bg-zinc-700 disabled:text-zinc-500 flex items-center gap-2"
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
      <label className="text-xs uppercase tracking-wider text-zinc-500 mb-1 block">{label}</label>
      <input
        type="text"
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full px-3 py-2 bg-zinc-800/50 border border-zinc-700 rounded-lg text-sm text-zinc-100 focus:outline-none focus:border-blue-500"
      />
    </div>
  )
}
