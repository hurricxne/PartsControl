/**
 * <FotosItemModal> — las fotos de una línea de venta: las de recepción (solo consulta)
 * y las que ve el cliente en su portal (se suben y se quitan acá).
 * 2026-08-20, pedido del dueño.
 *
 * PARA QUÉ
 *   En bodega se fotografían los ítems de alto valor (sobre $3.000.000 de venta) aunque
 *   lleguen completos, y más de una foto por ítem. Al preparar el despacho hay que poder
 *   mirarlas: son la evidencia del estado en que llegó la pieza.
 *
 * POR QUÉ LAS DE RECEPCIÓN NO SE PUEDEN BORRAR DESDE ACÁ
 *   Son evidencia de la recepción, y alimentan el reclamo al proveedor. Quien las borra
 *   tiene que hacerlo en la pantalla donde las tomó, con su contexto a la vista —el ítem,
 *   su estado, su observación— y no de paso mientras arma un despacho. Por eso la pestaña
 *   de recepción no tiene botón de borrar y el endpoint que consume es solo de lectura.
 *
 * DOS ORÍGENES, UNA VENTANA
 *   · Recepción: evidencia INTERNA del estado en que llegó la pieza. Se piden a
 *     `bodegaAPI.fotosDeItem` y NO se publican: el cliente nunca las ve.
 *   · Despacho:  las que ve el CLIENTE en su portal. Viven en otra tabla
 *     (`foto_articulo`) y las maneja `portal/FotosParaCliente.tsx`: subir una ES
 *     publicarla, sin revisión previa. Una foto de recepción no pasa sola a este lado;
 *     para mostrársela al cliente hay que subirla en la pestaña de despacho.
 */
import { useEffect, useState } from 'react'
import { X, Loader2, ImageOff } from 'lucide-react'
import FotoBodega from './FotoBodega'
import FotosParaCliente from '../portal/FotosParaCliente'
import { bodegaAPI, type FotoRecepcionItem } from '../services/api'
import { fmtFechaServidor } from '../utils/format'

type Props = {
  itemCotizacionId: number
  numeroParte?: string | null
  descripcion?: string | null
  onClose: () => void
}

export default function FotosItemModal({
  itemCotizacionId, numeroParte, descripcion, onClose,
}: Props) {
  const [pestana, setPestana] = useState<'recepcion' | 'despacho'>('recepcion')
  const [fotos, setFotos] = useState<FotoRecepcionItem[] | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    let vivo = true
    setFotos(null)
    setError(false)
    bodegaAPI.fotosDeItem(itemCotizacionId)
      .then((r) => { if (vivo) setFotos(r.data) })
      .catch(() => { if (vivo) setError(true) })
    return () => { vivo = false }
  }, [itemCotizacionId])

  // Escape cierra: es una ventana de consulta, salir tiene que ser barato.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const solapa = (activa: boolean) =>
    `px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
      activa ? 'bg-brand-500/10 text-brand-500' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'
    }`

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
         onClick={onClose} role="dialog" aria-modal="true" aria-label="Fotos del ítem">
      <div className="card w-full max-w-2xl max-h-[85vh] overflow-auto p-5"
           onClick={(e) => e.stopPropagation()}>

        <div className="flex items-start justify-between gap-4 mb-4">
          <div className="min-w-0">
            <h3 className="font-bold text-base" style={{ color: 'var(--text-primary)' }}>
              <span className="font-mono">{numeroParte || 'Ítem'}</span>
            </h3>
            {descripcion && (
              <p className="text-xs mt-0.5 truncate" style={{ color: 'var(--text-muted)' }}>
                {descripcion}
              </p>
            )}
          </div>
          <button onClick={onClose} aria-label="Cerrar"
                  className="shrink-0 p-1 rounded-lg hover:bg-[var(--surface-200)]">
            <X className="w-4 h-4" style={{ color: 'var(--text-muted)' }} />
          </button>
        </div>

        <div className="flex items-center gap-1 mb-4">
          <button className={solapa(pestana === 'recepcion')}
                  onClick={() => setPestana('recepcion')}>
            Foto recepción{fotos?.length ? ` · ${fotos.length}` : ''}
          </button>
          <button className={solapa(pestana === 'despacho')}
                  onClick={() => setPestana('despacho')}>
            Foto despacho
          </button>
        </div>

        {pestana === 'recepcion' && (
          <>
            {fotos === null && !error && (
              <div className="flex items-center justify-center py-12">
                <Loader2 className="w-5 h-5 animate-spin" style={{ color: 'var(--text-faint)' }} />
              </div>
            )}
            {error && (
              <p className="text-center py-12 text-sm" style={{ color: 'var(--text-faint)' }}>
                No pudimos cargar las fotos. Revisa tu conexión y vuelve a intentar.
              </p>
            )}
            {fotos !== null && !error && fotos.length === 0 && (
              <div className="flex flex-col items-center py-12 gap-2">
                <ImageOff className="w-6 h-6" style={{ color: 'var(--text-faint)' }} />
                <p className="text-sm" style={{ color: 'var(--text-faint)' }}>
                  No se tomaron fotos de este ítem en la recepción.
                </p>
              </div>
            )}
            {fotos !== null && fotos.length > 0 && (
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {fotos.map((f, i) => (
                  <figure key={f.id} className="space-y-1">
                    <FotoBodega
                      id={f.id}
                      alt={`Foto ${i + 1} de ${fotos.length} de la recepción de ${numeroParte || 'el ítem'}`}
                      className="w-full aspect-square object-cover rounded-xl border"
                      style={{ borderColor: 'var(--border)' }}
                    />
                    <figcaption className="text-[11px] leading-tight"
                                style={{ color: 'var(--text-muted)' }}>
                      {/* `fecha` viene del servidor en UTC y sin zona: se pinta el día de Chile. */}
                      {fmtFechaServidor(f.fecha)}
                      {f.estado_recepcion && <> · {f.estado_recepcion.replace(/_/g, ' ')}</>}
                      {f.observacion && (
                        <span className="block truncate" title={f.observacion}>
                          {f.observacion}
                        </span>
                      )}
                    </figcaption>
                  </figure>
                ))}
              </div>
            )}
            <p className="text-[11px] mt-4 pt-3 border-t" style={{ color: 'var(--text-faint)', borderColor: 'var(--border)' }}>
              Solo consulta. Estas fotos son la evidencia de la recepción: para borrarlas
              hay que entrar a la recepción donde se tomaron.
            </p>
          </>
        )}

        {/* `key`: si la ventana pasara a otro ítem sin cerrarse, la pestaña nace de nuevo
            y no arrastra el avance ni los mensajes de la subida anterior. */}
        {pestana === 'despacho' && (
          <FotosParaCliente
            key={itemCotizacionId}
            itemCotizacionId={itemCotizacionId}
            numeroParte={numeroParte}
          />
        )}
      </div>
    </div>
  )
}
