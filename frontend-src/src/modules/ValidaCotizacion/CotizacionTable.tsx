import { useQuery } from '@tanstack/react-query'
import { CheckCircle2, XCircle, ExternalLink, RefreshCw } from 'lucide-react'
import { cotizacionesAPI } from '../../services/api'

interface Props { cotizacionId: number }

export default function CotizacionTable({ cotizacionId }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ['cotizacion-detail', cotizacionId],
    queryFn: () => cotizacionesAPI.get(cotizacionId).then((r) => r.data),
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 p-8 text-slate-500">
        <RefreshCw className="w-4 h-4 animate-spin" />
        <span className="text-sm">Cargando detalle...</span>
      </div>
    )
  }

  if (!data?.items?.length) {
    return <div className="p-6 text-center text-slate-500 text-sm">Sin items para mostrar</div>
  }

  const formatCLP = (v: number | null) => {
    if (!v) return '—'
    return new Intl.NumberFormat('es-CL', { style: 'currency', currency: 'CLP', minimumFractionDigits: 0 }).format(v)
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-surface-DEFAULT/60">
            <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide w-10">#</th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">N° Parte</th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Descripción</th>
            <th className="text-right px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Cant.</th>
            <th className="text-right px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Precio Cot.</th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Nombre CAT</th>
            <th className="text-right px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Precio CAT</th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Retiro Est.</th>
            <th className="text-center px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Estado</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/60">
          {data.items.map((item: any) => (
            <tr
              key={item.id}
              className={`transition-colors ${
                item.encontrado
                  ? 'hover:bg-green-500/5'
                  : 'hover:bg-surface-200/50 opacity-80'
              }`}
            >
              <td className="px-4 py-3 text-slate-600 text-xs">{item.item_num}</td>
              <td className="px-4 py-3">
                <div className="flex items-center gap-1.5">
                  <span className="font-mono font-semibold text-brand-400 text-xs bg-brand-500/10 px-2 py-0.5 rounded-lg">
                    {item.numero_parte}
                  </span>
                  {item.url_cat && (
                    <a href={item.url_cat} target="_blank" rel="noopener noreferrer"
                       className="text-slate-600 hover:text-brand-400 transition-colors">
                      <ExternalLink className="w-3 h-3" />
                    </a>
                  )}
                </div>
              </td>
              <td className="px-4 py-3 text-slate-300 max-w-[150px] truncate" title={item.descripcion}>
                {item.descripcion}
              </td>
              <td className="px-4 py-3 text-right text-slate-400">{item.cantidad}</td>
              <td className="px-4 py-3 text-right text-slate-400 font-mono text-xs">
                {formatCLP(item.precio_unit_cotizacion)}
              </td>
              <td className="px-4 py-3 text-slate-300 max-w-[200px] truncate text-xs" title={item.nombre_cat || ''}>
                {item.nombre_cat || <span className="text-slate-600 italic">—</span>}
              </td>
              <td className="px-4 py-3 text-right font-mono text-xs">
                {item.precio_cat ? (
                  <span className="text-green-400 font-semibold">{formatCLP(item.precio_cat)}</span>
                ) : (
                  <span className="text-slate-600">—</span>
                )}
              </td>
              <td className="px-4 py-3 text-xs text-slate-400 max-w-[120px] truncate" title={item.retiro_estimado || ''}>
                {item.retiro_estimado || <span className="text-slate-600">—</span>}
              </td>
              <td className="px-4 py-3 text-center">
                {item.encontrado ? (
                  <span className="badge bg-green-500/10 text-green-400 border border-green-500/20">
                    <CheckCircle2 className="w-3 h-3" />
                    OK
                  </span>
                ) : (
                  <span className="badge bg-red-500/10 text-red-400 border border-red-500/20" title={item.scraping_error || ''}>
                    <XCircle className="w-3 h-3" />
                    N/E
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
