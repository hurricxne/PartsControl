import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X, RefreshCw, CheckCircle2, AlertCircle, Download, MonitorDown } from 'lucide-react'
import { cotizacionesAPI } from '../../services/api'

interface Props {
  cotizacionId: number
  onClose: () => void
}

export default function ProcessingCard({ cotizacionId, onClose }: Props) {
  const queryClient = useQueryClient()

  const { data: status } = useQuery<any>({
    queryKey: ['cotizacion-status', cotizacionId],
    queryFn: () => cotizacionesAPI.status(cotizacionId).then((r) => r.data),
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 2000
      const done = data.estado === 'completado' || data.estado === 'error'
      return done ? false : 3000
    },
  })

  useEffect(() => {
    if (status?.estado === 'completado' || status?.estado === 'error') {
      queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })
    }
  }, [status?.estado, queryClient])

  if (!status) return null

  const progreso = status.progreso || 0
  const isCompleted = status.estado === 'completado'
  const isError = status.estado === 'error'
  const isWaitingAgent = status.estado === 'esperando_agente'
  const isProcessing = !isCompleted && !isError && !isWaitingAgent

  return (
    <div className={`card p-5 border ${
      isCompleted ? 'border-green-500/30 bg-green-500/5' :
      isError ? 'border-red-500/30 bg-red-500/5' :
      isWaitingAgent ? 'border-amber-500/30 bg-amber-500/5' :
      'border-brand-600/30 bg-brand-600/5'
    }`}>
      <div className="flex items-start justify-between mb-4">
        <div className="flex items-center gap-3">
          {isCompleted ? (
            <div className="w-9 h-9 rounded-xl bg-green-500/20 flex items-center justify-center">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
            </div>
          ) : isError ? (
            <div className="w-9 h-9 rounded-xl bg-red-500/20 flex items-center justify-center">
              <AlertCircle className="w-5 h-5 text-red-400" />
            </div>
          ) : isWaitingAgent ? (
            <div className="w-9 h-9 rounded-xl bg-amber-500/20 flex items-center justify-center">
              <MonitorDown className="w-5 h-5 text-amber-400" />
            </div>
          ) : (
            <div className="w-9 h-9 rounded-xl bg-brand-600/20 flex items-center justify-center">
              <RefreshCw className="w-5 h-5 text-brand-400 animate-spin" />
            </div>
          )}
          <div>
            <p className="font-semibold text-white text-sm">
              {isCompleted ? '¡Procesamiento completado!' :
               isError ? 'Error en procesamiento' :
               isWaitingAgent ? 'Esperando agente local' :
               'Procesando cotización...'}
            </p>
            <p className="text-xs text-slate-400 mt-0.5">
              {isCompleted
                ? `${status.items_encontrados} de ${status.total_items} partes encontradas en parts.cat.com`
                : isError
                ? status.error_msg
                : isWaitingAgent
                ? `${status.total_items} partes listas para scraping — inicia el agente en tu PC`
                : `Buscando ${status.items_procesados} de ${status.total_items} partes en parts.cat.com`}
            </p>
          </div>
        </div>
        <button onClick={onClose} className="text-slate-500 hover:text-slate-300 transition-colors p-1">
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Instrucciones agente local */}
      {isWaitingAgent && (
        <div className="mt-3 rounded-lg bg-amber-500/10 border border-amber-500/20 p-4 space-y-3">
          <p className="text-xs font-semibold text-amber-300 uppercase tracking-wide">
            ¿Por qué necesito el agente?
          </p>
          <p className="text-xs text-slate-300 leading-relaxed">
            parts.cat.com bloquea las IPs de servidores (Akamai Bot Manager).
            El agente corre en <strong className="text-white">tu propia computadora</strong> con tu IP residencial,
            donde el sitio sí responde.
          </p>
          <div className="border-t border-amber-500/20 pt-3">
            <p className="text-xs font-semibold text-amber-300 mb-2">Inicia el agente local:</p>
            <div className="space-y-1.5">
              <div className="flex items-start gap-2">
                <span className="text-amber-400 font-bold text-xs mt-0.5">1.</span>
                <code className="text-xs bg-surface-300 px-2 py-0.5 rounded text-slate-300">
                  cd MachPartsApp/local_agent
                </code>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-amber-400 font-bold text-xs mt-0.5">2.</span>
                <code className="text-xs bg-surface-300 px-2 py-0.5 rounded text-slate-300">
                  python scraper_agent.py
                </code>
              </div>
            </div>
            <p className="text-xs text-slate-500 mt-2">
              Esta tarjeta se actualizará automáticamente cuando el agente suba los resultados.
            </p>
          </div>
        </div>
      )}

      {/* Barra de progreso (procesando) */}
      {isProcessing && (
        <div className="space-y-2">
          <div className="flex justify-between text-xs">
            <span className="text-slate-400">{status.items_procesados} / {status.total_items} partes</span>
            <span className="text-brand-400 font-semibold">{progreso}%</span>
          </div>
          <div className="h-2 bg-surface-300 rounded-full overflow-hidden">
            <div
              className="h-full bg-gradient-to-r from-brand-600 to-brand-400 rounded-full transition-all duration-700 ease-out"
              style={{ width: `${progreso}%` }}
            />
          </div>
          <p className="text-xs text-slate-600 animate-pulse">
            Extrayendo datos desde parts.cat.com/es/finningchile...
          </p>
        </div>
      )}

      {/* Stats completado */}
      {isCompleted && (
        <div className="flex items-center gap-4 mt-2">
          <div className="flex items-center gap-2 text-sm">
            <span className="w-2 h-2 rounded-full bg-green-400" />
            <span className="text-green-400 font-semibold">{status.items_encontrados}</span>
            <span className="text-slate-500">encontradas</span>
          </div>
          <div className="flex items-center gap-2 text-sm">
            <span className="w-2 h-2 rounded-full bg-red-400" />
            <span className="text-red-400 font-semibold">{status.total_items - status.items_encontrados}</span>
            <span className="text-slate-500">no encontradas</span>
          </div>
          <a
            href={cotizacionesAPI.downloadUrl(cotizacionId)}
            download
            className="btn-primary text-xs py-1.5 px-4 ml-auto"
          >
            <Download className="w-3.5 h-3.5" />
            Descargar Excel
          </a>
        </div>
      )}
    </div>
  )
}
