// Bloque "Vinculación de ventas" de la pantalla interna Cuentas del portal.
//
// POR QUÉ IMPORTA: el portal le mostrará al cliente SOLO las ventas cuya cotización está
// vinculada a su ficha de cliente (cotizaciones.cliente_id). El vínculo se calcula por
// RUT. Una venta cerrada que no calza con ninguna ficha queda "huérfana": existe en el
// ERP, pero el cliente no la va a ver. Este bloque muestra el ENSAYO (GET, no escribe)
// y deja aplicarlo (POST …/aplicar).
// Aplicar hace DOS cosas: vincula las ventas que calzan y DESVINCULA las que dejaron de
// calzar (se corrigió el RUT de la ficha, o quedó ambigua). Las dos se cuentan y se
// avisan: la segunda es la que evita que un cliente vea una venta ajena.
//
// Las huérfanas no se arreglan desde acá: se corrige el RUT en la cotización, se crea la
// ficha que falta o se unifican las duplicadas (Admin → Clientes) y se vuelve a vincular.
import { useState } from 'react'
import { AlertCircle, CheckCircle2, Link2, Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { portalAdminAPI, detalleDe } from './api'
import type { ClienteAdmin, Vinculacion } from './types'
import { ConfirmarModal } from './modalesAdmin'
import { TONO_ATENCION, TONO_ERROR, TONO_OK } from './tonos'

interface VinculacionPanelProps {
  // null = todavía cargando (o falló: ver `error`).
  vinculacion: Vinculacion | null
  error: string | null
  // Para ponerle nombre a las fichas de un RUT ambiguo (el backend manda solo los ids).
  clientes: ClienteAdmin[]
  // Tras aplicar: el padre vuelve a pedir el ensayo y las fichas (cambian sus contadores).
  onAplicada: () => Promise<void>
}

interface Cifra {
  label: string
  valor: number
  // Color SOLO cuando el número pide atención (hay algo por hacer o algo mal).
  color?: string
}

const ventas = (n: number): string => (n === 1 ? '1 venta' : `${n} ventas`)

const thCls = 'text-left px-4 py-3 text-[10px] font-semibold uppercase tracking-wider whitespace-nowrap'

export default function VinculacionPanel({ vinculacion, error, clientes, onAplicada }: VinculacionPanelProps) {
  const [confirmando, setConfirmando] = useState(false)
  const [aplicando, setAplicando] = useState(false)

  const aplicar = async () => {
    setAplicando(true)
    try {
      const { data } = await portalAdminAPI.aplicarVinculacion()
      const hechas = [
        data.vinculadas_ahora > 0 ? `${ventas(data.vinculadas_ahora)} vinculada${data.vinculadas_ahora !== 1 ? 's' : ''} a su ficha` : null,
        (data.desvinculadas_ahora ?? 0) > 0 ? `${ventas(data.desvinculadas_ahora)} desvinculada${data.desvinculadas_ahora !== 1 ? 's' : ''}` : null,
      ].filter(Boolean)
      toast.success(hechas.length > 0 ? `Listo: ${hechas.join(' y ')}` : 'No había cambios que aplicar')
      setConfirmando(false)
      await onAplicada()
    } catch (err) {
      toast.error(detalleDe(err) ?? 'No se pudo vincular. Intenta nuevamente.')
    } finally {
      setAplicando(false)
    }
  }

  const nombreFicha = (clienteId: number): string => {
    const ficha = clientes.find((c) => c.id === clienteId)
    return ficha ? `${ficha.nombre || '(sin nombre)'} (ficha ${clienteId})` : `ficha ${clienteId}`
  }

  const porVincular = vinculacion?.vinculadas_ahora ?? 0
  const porDesvincular = vinculacion?.desvinculadas_ahora ?? 0
  // Lo que «Vincular ventas» cambiaría. Con solo desvinculaciones pendientes el botón
  // TAMBIÉN tiene que estar disponible: es la única forma de soltar desde la pantalla una
  // venta que quedó colgada de la ficha equivocada.
  const porAplicar = porVincular + porDesvincular
  const cifras: Cifra[] = vinculacion ? [
    { label: 'Ya vinculadas', valor: vinculacion.ya_vinculadas },
    { label: 'Se vincularían ahora', valor: porVincular, color: porVincular > 0 ? TONO_OK : undefined },
    { label: 'Se desvincularían', valor: porDesvincular, color: porDesvincular > 0 ? TONO_ATENCION : undefined },
    { label: 'Ventas huérfanas', valor: vinculacion.huerfanas.length, color: vinculacion.huerfanas.length > 0 ? TONO_ATENCION : undefined },
    { label: 'RUT ambiguos', valor: vinculacion.ruts_ambiguos.length, color: vinculacion.ruts_ambiguos.length > 0 ? TONO_ERROR : undefined },
  ] : []

  const partesConfirmar = [
    porVincular > 0 ? `Se ${porVincular === 1 ? 'vinculará' : 'vincularán'} ${ventas(porVincular)} a su ficha de cliente según el RUT.` : null,
    porDesvincular > 0
      ? `${porDesvincular === 1 ? 'Se desvinculará 1 venta' : `Se desvincularán ${porDesvincular} ventas`} cuyo RUT ya no calza con su ficha: ${porDesvincular === 1 ? 'dejará' : 'dejarán'} de estar asociada${porDesvincular !== 1 ? 's' : ''} a ese cliente.`
      : null,
  ].filter(Boolean)

  return (
    <section className="space-y-3" aria-labelledby="vinculacion-titulo">
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-3">
        <div className="min-w-0">
          <h2 id="vinculacion-titulo" className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>Vinculación de ventas</h2>
          <p className="text-sm mt-0.5" style={{ color: 'var(--text-muted)' }}>
            El portal le mostrará a cada cliente solo las ventas vinculadas a su ficha. El vínculo se calcula por RUT.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setConfirmando(true)}
          disabled={!vinculacion || porAplicar === 0}
          title={vinculacion && porAplicar === 0 ? 'No hay ventas por vincular ni por desvincular' : undefined}
          className="btn-primary shrink-0 self-start sm:self-auto"
        >
          <Link2 className="w-4 h-4" /> Vincular ventas
        </button>
      </div>

      {error && (
        <p role="alert" className={`card p-4 flex items-start gap-2 text-sm ${TONO_ERROR}`}>
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" /> <span>{error}</span>
        </p>
      )}

      {!vinculacion && !error && (
        <div className="flex items-center gap-2 text-sm py-6" style={{ color: 'var(--text-faint)' }}>
          <Loader2 className="w-4 h-4 animate-spin" /> Calculando el ensayo…
        </div>
      )}

      {vinculacion && (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
            {cifras.map((c) => (
              <div key={c.label} className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
                <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{c.label}</p>
                <p className={`text-xl font-bold mt-1 ${c.color ?? ''}`} style={c.color ? undefined : { color: 'var(--text-primary)' }}>{c.valor}</p>
              </div>
            ))}
          </div>

          {vinculacion.huerfanas.length === 0 && vinculacion.ruts_ambiguos.length === 0 && (
            <p className={`flex items-center gap-2 text-sm ${TONO_OK}`}>
              <CheckCircle2 className="w-4 h-4 shrink-0" aria-hidden="true" /> No hay ventas cerradas huérfanas ni RUT ambiguos.
            </p>
          )}

          {vinculacion.huerfanas.length > 0 && (
            <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <p className="px-4 py-3 text-xs border-b" style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}>
                <strong className={TONO_ATENCION}>Ventas huérfanas:</strong> ventas cerradas que su cliente no verá en el portal.
                Según el motivo: corrige el RUT de la cotización, crea la ficha que falta o unifica las duplicadas
                (Admin → Clientes), y vuelve a vincular.
              </p>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '2px solid var(--border)' }}>
                      {['Cotización', 'Cliente', 'RUT', 'Motivo'].map((h) => (
                        <th key={h} scope="col" className={thCls} style={{ color: 'var(--text-faint)' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {vinculacion.huerfanas.map((h) => (
                      <tr key={h.cotizacion_id} style={{ borderBottom: '1px solid var(--border)' }}>
                        <th scope="row" className="px-4 py-2.5 whitespace-nowrap font-medium text-left" style={{ color: 'var(--text-primary)' }}>{h.numero || `#${h.cotizacion_id}`}</th>
                        <td className="px-4 py-2.5" style={{ color: 'var(--text-primary)' }}>{h.cliente || '—'}</td>
                        <td className="px-4 py-2.5 whitespace-nowrap font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{h.rut_cliente || '—'}</td>
                        <td className="px-4 py-2.5 text-xs" style={{ color: 'var(--text-muted)' }}>{h.motivo}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {vinculacion.ruts_ambiguos.length > 0 && (
            <div className="card p-4">
              <p className="text-xs mb-2" style={{ color: 'var(--text-muted)' }}>
                <strong className={TONO_ERROR}>RUT ambiguos:</strong> el mismo RUT está en más de una ficha de cliente (escrito
                distinto), así que sus ventas no se vinculan a ninguna (y las que ya estaban vinculadas se desvinculan al
                aplicar): elegir una al azar podría mostrárselas al contacto equivocado. Deja una sola ficha por RUT en
                Admin → Clientes; si la ficha que sobra tiene cuentas del portal, elimínalas primero en la tabla de arriba.
              </p>
              <ul className="space-y-1.5">
                {vinculacion.ruts_ambiguos.map((r) => (
                  <li key={r.rut} className="text-sm" style={{ color: 'var(--text-primary)' }}>
                    <span className="font-mono text-xs">{r.rut}</span>
                    <span style={{ color: 'var(--text-muted)' }}> → {r.cliente_ids.map(nombreFicha).join(' · ')}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      {confirmando && vinculacion && (
        <ConfirmarModal
          title="Vincular ventas"
          mensaje={`${partesConfirmar.join(' ')} Las huérfanas no se tocan. Las ventas vinculadas son las que el portal le mostrará al cliente de cada ficha.`}
          etiquetaConfirmar="Vincular ventas"
          ocupado={aplicando}
          onConfirmar={aplicar}
          onClose={() => setConfirmando(false)}
        />
      )}
    </section>
  )
}
