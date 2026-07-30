import { useState, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  LifeBuoy, Plus, RefreshCw, Send, X, MessageSquare, ArrowLeft, Clock,
  Paperclip, Upload, Download, Trash2, FileText, Image as ImageIcon,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { ticketsAPI } from '../services/api'
import { useAuthStore } from '../stores/authStore'

interface Respuesta {
  id: number
  autor_id: number | null
  autor_nombre: string | null
  es_solicitante: boolean
  mensaje: string
  fecha_creacion: string | null
}
interface Ticket {
  id: number
  numero: string
  titulo: string
  descripcion: string
  categoria: string
  prioridad: string
  estado: string
  solicitante_id: number | null
  solicitante_nombre: string | null
  fecha_creacion: string | null
  fecha_actualizacion: string | null
  fecha_cierre: string | null
  n_respuestas?: number
  respuestas?: Respuesta[]
}

const CATEGORIAS = [
  { v: 'bug', l: 'Error / Bug' },
  { v: 'mejora', l: 'Mejora' },
  { v: 'soporte', l: 'Soporte' },
  { v: 'consulta', l: 'Consulta' },
]
const PRIORIDADES = [
  { v: 'baja', l: 'Baja' },
  { v: 'media', l: 'Media' },
  { v: 'alta', l: 'Alta' },
  { v: 'urgente', l: 'Urgente' },
]
const ESTADOS = [
  { v: 'abierto', l: 'Abierto' },
  { v: 'en_progreso', l: 'En progreso' },
  { v: 'resuelto', l: 'Resuelto' },
  { v: 'cerrado', l: 'Cerrado' },
]

const ESTADO_STYLE: Record<string, { bg: string; color: string; bd: string }> = {
  abierto:     { bg: 'rgba(59,130,246,.12)',  color: '#3B82F6', bd: 'rgba(59,130,246,.3)' },
  en_progreso: { bg: 'rgba(245,158,11,.12)',  color: '#F59E0B', bd: 'rgba(245,158,11,.3)' },
  resuelto:    { bg: 'rgba(16,185,129,.12)',  color: '#10B981', bd: 'rgba(16,185,129,.3)' },
  cerrado:     { bg: 'rgba(148,163,184,.14)', color: '#94A3B8', bd: 'rgba(148,163,184,.3)' },
}
const PRIORIDAD_COLOR: Record<string, string> = {
  baja: '#94A3B8', media: '#3B82F6', alta: '#F59E0B', urgente: '#EF4444',
}
const CAT_LABEL: Record<string, string> = Object.fromEntries(CATEGORIAS.map((c) => [c.v, c.l]))

function fmt(d: string | null) {
  if (!d) return '—'
  const dt = new Date(d)
  return dt.toLocaleDateString('es-CL', { day: '2-digit', month: '2-digit', year: '2-digit' }) +
    ' ' + dt.toLocaleTimeString('es-CL', { hour: '2-digit', minute: '2-digit' })
}

function Badge({ estado }: { estado: string }) {
  const s = ESTADO_STYLE[estado] || ESTADO_STYLE.cerrado
  const l = ESTADOS.find((e) => e.v === estado)?.l || estado
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold border"
      style={{ background: s.bg, color: s.color, borderColor: s.bd }}>{l}</span>
  )
}

export default function TicketsPage() {
  const qc = useQueryClient()
  const currentUser = useAuthStore((s) => s.user)
  const [tab, setTab] = useState<string>('abiertos')
  const [showNuevo, setShowNuevo] = useState(false)
  const [selId, setSelId] = useState<number | null>(null)

  const estadoFiltro = tab === 'abiertos' ? undefined : tab === 'todos' ? undefined : tab

  const { data: tickets = [], isLoading, refetch } = useQuery({
    queryKey: ['tickets', tab],
    queryFn: async () => {
      const { data } = await ticketsAPI.list(estadoFiltro ? { estado: estadoFiltro } : {})
      return data as Ticket[]
    },
  })
  const { data: counts } = useQuery({
    queryKey: ['tickets-counts'],
    queryFn: async () => (await ticketsAPI.counts()).data as Record<string, number>,
  })

  const visibles = tab === 'abiertos'
    ? tickets.filter((t) => t.estado === 'abierto' || t.estado === 'en_progreso')
    : tickets

  const abiertosTotal = counts?.abiertos_total ?? 0

  return (
    <div className="space-y-5 max-w-6xl">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-3">
            <LifeBuoy className="w-6 h-6 text-brand-400" />
            <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Tickets de soporte</h1>
          </div>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Registra solicitudes de cambio, errores y consultas. Cada ticket es una conversación: tú y el
            equipo pueden responder y re-responder hasta resolverlo.
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button onClick={() => refetch()} className="btn-secondary text-sm" title="Actualizar">
            <RefreshCw className="w-4 h-4" />
          </button>
          <button onClick={() => setShowNuevo(true)} className="btn-primary text-sm">
            <Plus className="w-4 h-4" /> Nuevo ticket
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 flex-wrap">
        {[
          { v: 'abiertos', l: `Abiertos (${abiertosTotal})` },
          { v: 'resuelto', l: `Resueltos (${counts?.resuelto ?? 0})` },
          { v: 'cerrado', l: `Cerrados (${counts?.cerrado ?? 0})` },
          { v: 'todos', l: 'Todos' },
        ].map((t) => (
          <button key={t.v} onClick={() => setTab(t.v)}
            className="px-3 py-1.5 rounded-lg text-sm font-medium border transition"
            style={{
              background: tab === t.v ? 'var(--surface-200)' : 'transparent',
              color: tab === t.v ? 'var(--text-primary)' : 'var(--text-muted)',
              borderColor: tab === t.v ? 'var(--border)' : 'transparent',
            }}>
            {t.l}
          </button>
        ))}
      </div>

      {/* Lista */}
      <div className="card overflow-hidden">
        {isLoading ? (
          <div className="flex items-center justify-center p-10 gap-3" style={{ color: 'var(--text-faint)' }}>
            <RefreshCw className="w-5 h-5 animate-spin" /> Cargando tickets...
          </div>
        ) : visibles.length === 0 ? (
          <div className="p-12 text-center" style={{ color: 'var(--text-faint)' }}>
            <LifeBuoy className="w-9 h-9 mx-auto mb-2 opacity-40" />
            No hay tickets en esta vista.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr style={{ borderBottom: '1px solid var(--border)', backgroundColor: 'var(--surface-200)' }}>
                  {['Folio', 'Título', 'Categoría', 'Prioridad', 'Estado', 'Actualizado', ''].map((h) => (
                    <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap"
                      style={{ color: 'var(--text-faint)' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visibles.map((t) => (
                  <tr key={t.id} className="cursor-pointer hover:opacity-90 transition" onClick={() => setSelId(t.id)}
                    style={{ borderBottom: '1px solid var(--border)' }}>
                    <td className="px-4 py-3 font-mono text-xs font-semibold" style={{ color: 'var(--text-primary)' }}>{t.numero}</td>
                    <td className="px-4 py-3">
                      <div className="font-medium" style={{ color: 'var(--text-primary)' }}>{t.titulo}</div>
                      {(t.n_respuestas ?? 0) > 0 && (
                        <div className="flex items-center gap-1 text-xs mt-0.5" style={{ color: 'var(--text-faint)' }}>
                          <MessageSquare className="w-3 h-3" /> {t.n_respuestas} respuesta{(t.n_respuestas ?? 0) > 1 ? 's' : ''}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-muted)' }}>{CAT_LABEL[t.categoria] || t.categoria}</td>
                    <td className="px-4 py-3">
                      <span className="text-xs font-semibold" style={{ color: PRIORIDAD_COLOR[t.prioridad] || '#94A3B8' }}>
                        {PRIORIDADES.find((p) => p.v === t.prioridad)?.l || t.prioridad}
                      </span>
                    </td>
                    <td className="px-4 py-3"><Badge estado={t.estado} /></td>
                    <td className="px-4 py-3 text-xs whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{fmt(t.fecha_actualizacion)}</td>
                    <td className="px-4 py-3 text-right"><ArrowLeft className="w-4 h-4 rotate-180 inline" style={{ color: 'var(--text-faint)' }} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {showNuevo && (
        <NuevoTicketModal
          onClose={() => setShowNuevo(false)}
          onCreated={(id) => {
            setShowNuevo(false)
            qc.invalidateQueries({ queryKey: ['tickets'] })
            qc.invalidateQueries({ queryKey: ['tickets-counts'] })
            setSelId(id)
          }}
        />
      )}
      {selId != null && (
        <TicketDetalle
          id={selId}
          currentUserId={currentUser?.id ?? null}
          onClose={() => { setSelId(null); qc.invalidateQueries({ queryKey: ['tickets'] }); qc.invalidateQueries({ queryKey: ['tickets-counts'] }) }}
        />
      )}
    </div>
  )
}

function NuevoTicketModal({ onClose, onCreated }: { onClose: () => void; onCreated: (id: number) => void }) {
  const [titulo, setTitulo] = useState('')
  const [descripcion, setDescripcion] = useState('')
  const [categoria, setCategoria] = useState('soporte')
  const [prioridad, setPrioridad] = useState('media')

  const crear = useMutation({
    mutationFn: async () => (await ticketsAPI.crear({ titulo, descripcion, categoria, prioridad })).data as Ticket,
    onSuccess: (t) => { toast.success(`Ticket ${t.numero} creado`); onCreated(t.id) },
    onError: () => toast.error('No se pudo crear el ticket'),
  })

  const inputCls = 'w-full px-3 py-2 rounded-lg text-sm border outline-none'
  const inputStyle = { background: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0,0,0,.5)' }} onClick={onClose}>
      <div className="w-full max-w-lg rounded-xl border shadow-2xl" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor: 'var(--border)' }}>
          <h2 className="font-semibold flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
            <Plus className="w-4 h-4 text-brand-400" /> Nuevo ticket
          </h2>
          <button onClick={onClose} style={{ color: 'var(--text-faint)' }}><X className="w-5 h-5" /></button>
        </div>
        <div className="p-5 space-y-4">
          <div>
            <label className="block text-xs font-medium mb-1" style={{ color: 'var(--text-muted)' }}>Título</label>
            <input className={inputCls} style={inputStyle} value={titulo} onChange={(e) => setTitulo(e.target.value)}
              placeholder="Resumen breve de la solicitud" autoFocus />
          </div>
          <div>
            <label className="block text-xs font-medium mb-1" style={{ color: 'var(--text-muted)' }}>Descripción</label>
            <textarea className={inputCls} style={{ ...inputStyle, minHeight: 110, resize: 'vertical' }} value={descripcion}
              onChange={(e) => setDescripcion(e.target.value)} placeholder="Detalla qué necesitas, en qué pantalla, y con qué datos ocurre." />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium mb-1" style={{ color: 'var(--text-muted)' }}>Categoría</label>
              <select className={inputCls} style={inputStyle} value={categoria} onChange={(e) => setCategoria(e.target.value)}>
                {CATEGORIAS.map((c) => <option key={c.v} value={c.v}>{c.l}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium mb-1" style={{ color: 'var(--text-muted)' }}>Prioridad</label>
              <select className={inputCls} style={inputStyle} value={prioridad} onChange={(e) => setPrioridad(e.target.value)}>
                {PRIORIDADES.map((p) => <option key={p.v} value={p.v}>{p.l}</option>)}
              </select>
            </div>
          </div>
        </div>
        <div className="flex items-center justify-end gap-2 px-5 py-4 border-t" style={{ borderColor: 'var(--border)' }}>
          <button onClick={onClose} className="btn-secondary text-sm">Cancelar</button>
          <button className="btn-primary text-sm"
            disabled={!titulo.trim() || !descripcion.trim() || crear.isPending}
            onClick={() => crear.mutate()}>
            {crear.isPending ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />} Crear ticket
          </button>
        </div>
      </div>
    </div>
  )
}

function TicketDetalle({ id, currentUserId, onClose }: { id: number; currentUserId: number | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [msg, setMsg] = useState('')

  const { data: t, isLoading } = useQuery({
    queryKey: ['ticket', id],
    queryFn: async () => (await ticketsAPI.get(id)).data as Ticket,
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['ticket', id] })
    qc.invalidateQueries({ queryKey: ['tickets'] })
    qc.invalidateQueries({ queryKey: ['tickets-counts'] })
  }

  const responder = useMutation({
    mutationFn: async () => (await ticketsAPI.responder(id, msg)).data,
    onSuccess: () => { setMsg(''); invalidate(); },
    onError: () => toast.error('No se pudo enviar la respuesta'),
  })
  const cambiarEstado = useMutation({
    mutationFn: async (estado: string) => (await ticketsAPI.cambiarEstado(id, estado)).data,
    onSuccess: (_d, estado) => { toast.success(`Ticket marcado como ${ESTADOS.find((e) => e.v === estado)?.l || estado}`); invalidate(); },
    onError: () => toast.error('No se pudo cambiar el estado'),
  })

  const cerrado = t?.estado === 'cerrado'

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: 'rgba(0,0,0,.5)' }} onClick={onClose}>
      <div className="w-full max-w-2xl rounded-xl border shadow-2xl flex flex-col" style={{ background: 'var(--surface)', borderColor: 'var(--border)', maxHeight: '90vh' }} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-start justify-between px-5 py-4 border-b" style={{ borderColor: 'var(--border)' }}>
          <div className="min-w-0">
            {isLoading || !t ? (
              <div className="font-mono text-sm" style={{ color: 'var(--text-faint)' }}>Cargando...</div>
            ) : (
              <>
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="font-mono text-xs font-semibold" style={{ color: 'var(--text-faint)' }}>{t.numero}</span>
                  <Badge estado={t.estado} />
                  <span className="text-xs font-semibold" style={{ color: PRIORIDAD_COLOR[t.prioridad] }}>
                    {PRIORIDADES.find((p) => p.v === t.prioridad)?.l}
                  </span>
                  <span className="text-xs" style={{ color: 'var(--text-muted)' }}>· {CAT_LABEL[t.categoria] || t.categoria}</span>
                </div>
                <h2 className="text-lg font-bold mt-1" style={{ color: 'var(--text-primary)' }}>{t.titulo}</h2>
              </>
            )}
          </div>
          <button onClick={onClose} style={{ color: 'var(--text-faint)' }}><X className="w-5 h-5" /></button>
        </div>

        {/* Cuerpo conversación */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3">
          {t && (
            <>
              {/* Mensaje inicial */}
              <div className="rounded-lg border p-3" style={{ borderColor: 'var(--border)', background: 'var(--surface-100)' }}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-semibold" style={{ color: 'var(--text-primary)' }}>{t.solicitante_nombre || 'Solicitante'}</span>
                  <span className="text-xs flex items-center gap-1" style={{ color: 'var(--text-faint)' }}><Clock className="w-3 h-3" />{fmt(t.fecha_creacion)}</span>
                </div>
                <p className="text-sm whitespace-pre-wrap" style={{ color: 'var(--text-muted)' }}>{t.descripcion}</p>
              </div>

              {/* Hilo */}
              {(t.respuestas || []).map((r) => {
                const mine = r.es_solicitante
                return (
                  <div key={r.id} className="rounded-lg border p-3"
                    style={{
                      borderColor: mine ? 'var(--border)' : 'rgba(59,130,246,.3)',
                      background: mine ? 'var(--surface-100)' : 'rgba(59,130,246,.06)',
                      marginLeft: mine ? 0 : 24, marginRight: mine ? 24 : 0,
                    }}>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs font-semibold" style={{ color: mine ? 'var(--text-primary)' : '#3B82F6' }}>
                        {r.autor_nombre || 'Usuario'} {mine ? '· solicitante' : '· equipo'}
                      </span>
                      <span className="text-xs flex items-center gap-1" style={{ color: 'var(--text-faint)' }}><Clock className="w-3 h-3" />{fmt(r.fecha_creacion)}</span>
                    </div>
                    <p className="text-sm whitespace-pre-wrap" style={{ color: 'var(--text-muted)' }}>{r.mensaje}</p>
                  </div>
                )
              })}

              <AdjuntosPanel ticketId={id} />
            </>
          )}
        </div>

        {/* Footer: responder + acciones estado */}
        <div className="border-t px-5 py-4 space-y-3" style={{ borderColor: 'var(--border)' }}>
          {cerrado ? (
            <div className="text-sm text-center py-2" style={{ color: 'var(--text-faint)' }}>
              Este ticket está cerrado. Crea uno nuevo si necesitas retomar el tema.
            </div>
          ) : (
            <div className="flex items-end gap-2">
              <textarea className="flex-1 px-3 py-2 rounded-lg text-sm border outline-none"
                style={{ background: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)', minHeight: 44, maxHeight: 120, resize: 'vertical' }}
                placeholder="Escribe una respuesta..." value={msg} onChange={(e) => setMsg(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && msg.trim()) responder.mutate() }} />
              <button className="btn-primary text-sm" disabled={!msg.trim() || responder.isPending} onClick={() => responder.mutate()}>
                {responder.isPending ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              </button>
            </div>
          )}

          {t && (
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs" style={{ color: 'var(--text-faint)' }}>Cambiar estado:</span>
              {ESTADOS.filter((e) => e.v !== t.estado).map((e) => (
                <button key={e.v} onClick={() => cambiarEstado.mutate(e.v)} disabled={cambiarEstado.isPending}
                  className="px-2.5 py-1 rounded-md text-xs font-medium border transition"
                  style={{ background: 'transparent', borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
                  {e.l}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

interface Adjunto {
  id: number
  original_name: string
  content_type: string | null
  size_bytes: number | null
  uploaded_by: string | null
  fecha: string | null
  es_imagen: boolean
}

function fmtSize(b: number | null) {
  if (!b) return ''
  if (b < 1024) return `${b} B`
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(0)} KB`
  return `${(b / 1024 / 1024).toFixed(1)} MB`
}

const ADJ_ACCEPT = '.jpg,.jpeg,.png,.gif,.webp,.pdf,.doc,.docx,.xls,.xlsx,.txt,.csv'

function AdjuntosPanel({ ticketId }: { ticketId: number }) {
  const qc = useQueryClient()
  const fileRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)

  const { data: adjuntos = [] } = useQuery({
    queryKey: ['ticket-adjuntos', ticketId],
    queryFn: async () => (await ticketsAPI.listAdjuntos(ticketId)).data as Adjunto[],
  })

  const onPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) e.target.value = ''
    if (!file) return
    if (file.size > 15 * 1024 * 1024) { toast.error('El archivo supera los 15 MB'); return }
    setUploading(true)
    try {
      await ticketsAPI.subirAdjunto(ticketId, file)
      qc.invalidateQueries({ queryKey: ['ticket-adjuntos', ticketId] })
      toast.success('Adjunto subido')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'No se pudo subir el archivo')
    } finally { setUploading(false) }
  }

  const descargar = async (a: Adjunto) => {
    try {
      const resp = await ticketsAPI.descargarAdjunto(a.id)
      const blob = new Blob([resp.data], { type: a.content_type || 'application/octet-stream' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url; link.download = a.original_name
      document.body.appendChild(link); link.click(); document.body.removeChild(link)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { toast.error('No se pudo descargar') }
  }

  const borrar = async (a: Adjunto) => {
    try {
      await ticketsAPI.borrarAdjunto(a.id)
      qc.invalidateQueries({ queryKey: ['ticket-adjuntos', ticketId] })
    } catch { toast.error('No se pudo eliminar') }
  }

  return (
    <div className="rounded-lg border p-3" style={{ borderColor: 'var(--border)' }}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold flex items-center gap-1.5" style={{ color: 'var(--text-primary)' }}>
          <Paperclip className="w-3.5 h-3.5" /> Adjuntos {adjuntos.length > 0 && `(${adjuntos.length})`}
        </span>
        <input ref={fileRef} type="file" accept={ADJ_ACCEPT} className="hidden" onChange={onPick} />
        <button onClick={() => fileRef.current?.click()} disabled={uploading}
          className="px-2.5 py-1 rounded-md text-xs font-medium border flex items-center gap-1.5 transition"
          style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
          {uploading ? <RefreshCw className="w-3 h-3 animate-spin" /> : <Upload className="w-3 h-3" />}
          {uploading ? 'Subiendo...' : 'Subir archivo'}
        </button>
      </div>
      {adjuntos.length === 0 ? (
        <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
          Sin archivos. Imágenes o documentos (PDF, Word, Excel, TXT, CSV), hasta 15 MB.
        </p>
      ) : (
        <div className="space-y-1.5">
          {adjuntos.map((a) => (
            <div key={a.id} className="flex items-center gap-2 rounded-md px-2 py-1.5" style={{ background: 'var(--surface-100)' }}>
              {a.es_imagen ? <ImageIcon className="w-4 h-4 shrink-0" style={{ color: 'var(--text-faint)' }} />
                : <FileText className="w-4 h-4 shrink-0" style={{ color: 'var(--text-faint)' }} />}
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium truncate" style={{ color: 'var(--text-primary)' }}>{a.original_name}</div>
                <div className="text-xs" style={{ color: 'var(--text-faint)' }}>
                  {fmtSize(a.size_bytes)}{a.uploaded_by ? ` · ${a.uploaded_by}` : ''}
                </div>
              </div>
              <button onClick={() => descargar(a)} title="Descargar" style={{ color: 'var(--text-muted)' }}><Download className="w-4 h-4" /></button>
              <button onClick={() => borrar(a)} title="Eliminar" style={{ color: '#EF4444' }}><Trash2 className="w-4 h-4" /></button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
