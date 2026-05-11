import { useState, useEffect, useRef, useCallback } from 'react'
import { Bell, X, CheckCheck, Info, AlertTriangle, AlertOctagon, ExternalLink } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import toast from 'react-hot-toast'
import { notificacionesAPI } from '../services/api'

interface Notif {
  id: number
  destinatario_rol: string
  severidad: 'info' | 'warning' | 'critical'
  titulo: string
  mensaje: string
  entidad_tipo: string | null
  entidad_id: number | null
  link: string | null
  leida: boolean
  fecha_creacion: string
  regla: string | null
}

function SeverityIcon({ s }: { s: Notif['severidad'] }) {
  if (s === 'critical') return <AlertOctagon className="w-4 h-4 text-red-400 shrink-0" />
  if (s === 'warning')  return <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
  return <Info className="w-4 h-4 text-blue-400 shrink-0" />
}

function severityBorder(s: Notif['severidad']) {
  if (s === 'critical') return 'border-l-2 border-red-400'
  if (s === 'warning')  return 'border-l-2 border-amber-400'
  return 'border-l-2 border-blue-400'
}

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'ahora'
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h`
  return `${Math.floor(h / 24)}d`
}

export default function NotificationBell() {
  const [open, setOpen]     = useState(false)
  const [notifs, setNotifs] = useState<Notif[]>([])
  const [count, setCount]   = useState(0)
  const [loading, setLoading] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  const fetchCount = useCallback(async () => {
    try {
      const { data } = await notificacionesAPI.countNoLeidas()
      setCount(data.count ?? 0)
    } catch {/* silent */}
  }, [])

  const fetchList = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await notificacionesAPI.list()
      setNotifs(data.slice(0, 20))
    } catch {
      toast.error('Error al cargar notificaciones')
    } finally {
      setLoading(false)
    }
  }, [])

  // Poll count every 60 s
  useEffect(() => {
    fetchCount()
    const id = setInterval(fetchCount, 60_000)
    return () => clearInterval(id)
  }, [fetchCount])

  // When panel opens, fetch full list
  useEffect(() => {
    if (open) fetchList()
  }, [open, fetchList])

  // Close on outside click
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const markOne = async (n: Notif) => {
    if (!n.leida) {
      try {
        await notificacionesAPI.marcarLeida(n.id)
        setNotifs(prev => prev.map(x => x.id === n.id ? { ...x, leida: true } : x))
        setCount(c => Math.max(0, c - 1))
      } catch {/* silent */}
    }
    if (n.link) {
      setOpen(false)
      navigate(n.link)
    }
  }

  const markAll = async () => {
    try {
      await notificacionesAPI.marcarTodasLeidas()
      setNotifs(prev => prev.map(x => ({ ...x, leida: true })))
      setCount(0)
      toast.success('Todas marcadas como leídas')
    } catch {
      toast.error('Error')
    }
  }

  return (
    <div className="relative" ref={panelRef}>
      {/* Bell button */}
      <button
        onClick={() => setOpen(v => !v)}
        className="relative p-2 rounded-lg transition-colors hover:bg-[var(--surface-200)]"
        title="Notificaciones"
      >
        <Bell className="w-5 h-5" style={{ color: 'var(--text-muted)' }} />
        {count > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] rounded-full bg-red-500 text-white text-[10px] font-bold flex items-center justify-center px-1 leading-none">
            {count > 99 ? '99+' : count}
          </span>
        )}
      </button>

      {/* Dropdown panel */}
      {open && (
        <div className="absolute right-0 top-full mt-2 w-80 rounded-2xl border shadow-2xl z-50 overflow-hidden"
          style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-3 border-b"
            style={{ borderColor: 'var(--border)' }}>
            <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
              Notificaciones
              {count > 0 && (
                <span className="ml-2 text-xs px-1.5 py-0.5 rounded-full bg-red-500/10 text-red-400 font-bold">
                  {count}
                </span>
              )}
            </span>
            <div className="flex items-center gap-2">
              {count > 0 && (
                <button onClick={markAll} title="Marcar todas como leídas"
                  className="p-1 rounded hover:bg-[var(--surface-300)] transition-colors"
                  style={{ color: 'var(--text-faint)' }}>
                  <CheckCheck className="w-4 h-4" />
                </button>
              )}
              <button onClick={() => setOpen(false)} className="p-1 rounded hover:bg-[var(--surface-300)] transition-colors"
                style={{ color: 'var(--text-faint)' }}>
                <X className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* List */}
          <div className="max-h-96 overflow-y-auto">
            {loading ? (
              <div className="py-8 text-center text-xs" style={{ color: 'var(--text-faint)' }}>
                Cargando…
              </div>
            ) : notifs.length === 0 ? (
              <div className="py-8 text-center text-xs" style={{ color: 'var(--text-faint)' }}>
                Sin notificaciones
              </div>
            ) : (
              notifs.map(n => (
                <button key={n.id} onClick={() => markOne(n)}
                  className={`w-full text-left px-4 py-3 flex gap-3 transition-colors hover:bg-[var(--surface-200)] ${severityBorder(n.severidad)} ${!n.leida ? 'bg-brand-500/5' : ''}`}
                  style={{ borderBottom: '1px solid var(--border)' }}>
                  <SeverityIcon s={n.severidad} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-start justify-between gap-2">
                      <p className={`text-xs font-semibold leading-tight truncate ${!n.leida ? '' : 'opacity-70'}`}
                        style={{ color: 'var(--text-primary)' }}>
                        {n.titulo}
                      </p>
                      <span className="text-[10px] shrink-0" style={{ color: 'var(--text-faint)' }}>
                        {timeAgo(n.fecha_creacion)}
                      </span>
                    </div>
                    <p className="text-[11px] mt-0.5 leading-tight opacity-70" style={{ color: 'var(--text-muted)' }}>
                      {n.mensaje}
                    </p>
                    {n.link && (
                      <span className="text-[10px] text-brand-400 flex items-center gap-0.5 mt-1">
                        <ExternalLink className="w-2.5 h-2.5" /> Ver detalle
                      </span>
                    )}
                  </div>
                  {!n.leida && (
                    <span className="w-2 h-2 rounded-full bg-brand-400 shrink-0 mt-1" />
                  )}
                </button>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  )
}
