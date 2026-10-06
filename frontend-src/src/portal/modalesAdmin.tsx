// Modales de la pantalla interna "Cuentas del portal" (mismo aspecto que el Modal de
// tesoreria/TesoreriaPage.tsx). Tres piezas:
//   · Modal               el marco.
//   · ConfirmarModal      pregunta antes de una acción que deja a un cliente sin entrar.
//   · ClaveTemporalModal  muestra la clave temporal UNA sola vez.
//
// EL FOCO es parte del modal, no un adorno: sin él, quien usa teclado o lector de
// pantalla queda DETRÁS del velo (el lector no anuncia el cuadro y Tab recorre la tabla
// y el menú que no se ven), justo cuando aparece una clave que se muestra una sola vez.
// Por eso el marco (a) mete el foco al abrir, (b) no lo deja salir con Tab y (c) lo
// devuelve al control que abrió el cuadro al cerrar.
import { useEffect, useId, useRef, useState } from 'react'
import { AlertTriangle, Check, Copy, Loader2, X } from 'lucide-react'
import toast from 'react-hot-toast'
import type { EstadoCuenta } from './types'
import { TONO_ATENCION, TONO_OK } from './tonos'

const SELECTOR_ENFOCABLES =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

interface ModalProps {
  title: string
  // Sin `onClose` el modal NO se cierra con clic afuera, con Escape ni con la X: solo con
  // los botones que traiga adentro. Lo usa la clave temporal, que se pierde al cerrar.
  onClose?: () => void
  // Control que recibe el foco al abrir. Sin él lo recibe el cuadro mismo. Va por ref y
  // no por `autoFocus`: cuando un modal reemplaza a otro, el que se cierra devuelve el
  // foco a la página DESPUÉS de que `autoFocus` ya actuó, y se lo quitaría al nuevo.
  focoInicial?: React.RefObject<HTMLElement>
  // ids de los textos que el lector de pantalla lee al abrir, además del título.
  descritoPor?: string
  children: React.ReactNode
}

function Modal({ title, onClose, focoInicial, descritoPor, children }: ModalProps) {
  const idTitulo = useId()
  const refDialogo = useRef<HTMLDivElement>(null)

  // (a) y (c): entrar al abrir, devolver al cerrar. Solo al montar: el control inicial
  // no cambia durante la vida del cuadro.
  useEffect(() => {
    const anterior = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const dialogo = refDialogo.current
    const destino = focoInicial?.current ?? dialogo
    destino?.focus()
    return () => {
      // Se devuelve el foco solo si quedó suelto. Si ya lo tomó otro cuadro (la clave
      // temporal que reemplaza a la confirmación), quitárselo lo dejaría detrás del velo.
      const activo = document.activeElement
      const suelto = !activo || activo === document.body || !!dialogo?.contains(activo)
      // Si el control que abrió el cuadro ya no existe (el formulario de alta se cierra
      // al crear), no hay a dónde volver: de eso se encarga quien cierra.
      if (suelto && anterior && anterior !== document.body && anterior.isConnected) anterior.focus()
    }
  }, [])

  // (b) y Escape. Se escucha en el documento y no en el cuadro: mientras una petición
  // está en vuelo los botones quedan deshabilitados y el navegador suelta el foco al
  // <body>; desde ahí un Tab se iría a la página de atrás sin pasar por el cuadro.
  useEffect(() => {
    const alTeclear = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onClose?.(); return }
      if (e.key !== 'Tab') return
      const dialogo = refDialogo.current
      if (!dialogo) return
      const enfocables = Array.from(dialogo.querySelectorAll<HTMLElement>(SELECTOR_ENFOCABLES))
      if (enfocables.length === 0) { e.preventDefault(); dialogo.focus(); return }
      const primero = enfocables[0]
      const ultimo = enfocables[enfocables.length - 1]
      const activo = document.activeElement
      const afuera = !dialogo.contains(activo)
      if (e.shiftKey) {
        if (afuera || activo === primero || activo === dialogo) { e.preventDefault(); ultimo.focus() }
      } else if (afuera || activo === ultimo) {
        e.preventDefault(); primero.focus()
      }
    }
    document.addEventListener('keydown', alTeclear)
    return () => document.removeEventListener('keydown', alTeclear)
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        ref={refDialogo}
        role="dialog"
        aria-modal="true"
        aria-labelledby={idTitulo}
        aria-describedby={descritoPor}
        // Enfocable por código (no con Tab): es el destino del foco cuando adentro no
        // hay un control mejor. Sin contorno: es un contenedor, no un control.
        tabIndex={-1}
        className="w-full max-w-md rounded-2xl border shadow-2xl max-h-[90vh] overflow-y-auto outline-none"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3 border-b sticky top-0"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
          <h3 id={idTitulo} className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{title}</h3>
          {onClose && (
            <button type="button" onClick={onClose} aria-label="Cerrar" className="p-1 rounded-lg hover:bg-white/10"
              style={{ color: 'var(--text-muted)' }}>
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
        <div className="p-5 space-y-3">{children}</div>
      </div>
    </div>
  )
}

interface ConfirmarModalProps {
  title: string
  mensaje: string
  // Advertencia aparte del mensaje, destacada: algo que el operador tiene que saber
  // ANTES de confirmar y que no es la consecuencia directa de la acción.
  aviso?: string | null
  etiquetaConfirmar: string
  // Acción que le corta el acceso a alguien: el botón va en rojo.
  peligro?: boolean
  ocupado: boolean
  onConfirmar: () => void
  onClose: () => void
}

export function ConfirmarModal({
  title, mensaje, aviso, etiquetaConfirmar, peligro, ocupado, onConfirmar, onClose,
}: ConfirmarModalProps) {
  const refCancelar = useRef<HTMLButtonElement>(null)
  const idMensaje = useId()
  return (
    // Mientras la petición está en vuelo no se puede cerrar: el operador creería que
    // canceló algo que ya se está ejecutando.
    <Modal title={title} onClose={ocupado ? undefined : onClose} focoInicial={refCancelar} descritoPor={idMensaje}>
      <div id={idMensaje} className="space-y-3">
        <p className="text-sm" style={{ color: 'var(--text-muted)' }}>{mensaje}</p>
        {aviso && <AvisoAmbar>{aviso}</AvisoAmbar>}
      </div>
      <div className="flex justify-end gap-3 pt-2">
        {/* El foco nace en Cancelar: un Enter por reflejo no debe ejecutar la acción. */}
        <button ref={refCancelar} type="button" onClick={onClose} disabled={ocupado} className="btn-secondary text-sm">Cancelar</button>
        <button
          type="button"
          onClick={onConfirmar}
          disabled={ocupado}
          className={peligro
            ? 'flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold text-white bg-red-600 hover:bg-red-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed'
            : 'btn-primary text-sm'}
        >
          {ocupado && <Loader2 className="w-4 h-4 animate-spin" />}
          {etiquetaConfirmar}
        </button>
      </div>
    </Modal>
  )
}

function AvisoAmbar({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-2.5 rounded-xl px-3 py-2.5 text-xs bg-amber-500/10 border border-amber-500/30"
      style={{ color: 'var(--text-primary)' }}>
      <AlertTriangle className={`w-4 h-4 mt-0.5 shrink-0 ${TONO_ATENCION}`} aria-hidden="true" />
      <span>{children}</span>
    </div>
  )
}

/**
 * Por qué una clave nueva NO le sirve a esta cuenta, o null si la cuenta entra.
 *
 * «Restablecer clave» solo cambia la clave (el backend no reactiva ni reinicia el plazo
 * de inactividad). Una cuenta vencida sigue rechazada con 403 y una desactivada con el
 * mismo «correo o contraseña incorrectos» de una clave mala: sin este aviso el contacto
 * y el operador concluyen que la clave temporal falló y vuelven a restablecerla, sin
 * salida. Es el caso más común: el cliente llama porque «no puede entrar».
 */
export function motivoNoEntra(estado: EstadoCuenta | undefined): string | null {
  if (estado === 'vencida') return 'Esta cuenta está vencida por inactividad'
  if (estado === 'desactivada') return 'Esta cuenta está desactivada'
  return null
}

export interface ClaveEntregada {
  nombre: string
  email: string
  clave: string
  origen: 'creada' | 'restablecida'
  // Estado de la cuenta al restablecer: si no es 'activa', la clave sola no alcanza.
  estado?: EstadoCuenta
  // Al crear: los clientes que quedaron asignados (nombre + RUT), para que un cliente
  // marcado por error se note mientras todavía se está frente a la cuenta recién hecha.
  clientes?: string[]
  // Hasta cuándo sirve la clave temporal, ya formateada en hora de Chile por la página
  // (el plazo lo calcula el backend; acá no se copia el número de días).
  vence?: string | null
}

interface ClaveTemporalModalProps {
  entrega: ClaveEntregada
  // El padre BORRA la clave de su estado acá: después de esto no existe en ninguna parte
  // del navegador (el backend solo guarda el hash).
  onCerrar: () => void
}

const MS_AVISO_COPIADO = 2000

export function ClaveTemporalModal({ entrega, onCerrar }: ClaveTemporalModalProps) {
  const [copiada, setCopiada] = useState(false)
  const refCopiar = useRef<HTMLButtonElement>(null)
  const id = useId()
  const noEntra = motivoNoEntra(entrega.estado)

  useEffect(() => {
    if (!copiada) return
    const t = setTimeout(() => setCopiada(false), MS_AVISO_COPIADO)
    return () => clearTimeout(t)
  }, [copiada])

  const copiar = async () => {
    try {
      await navigator.clipboard.writeText(entrega.clave)
      setCopiada(true)
    } catch {
      // Sin HTTPS o sin permiso el portapapeles falla: la clave sigue en pantalla y se
      // puede seleccionar a mano (el cuadro tiene select-all).
      toast.error('No se pudo copiar. Selecciona la clave y cópiala a mano.')
    }
  }

  return (
    // El foco nace en «Copiar clave»: es lo primero que hay que hacer, y un Enter por
    // reflejo copia la clave en vez de cerrar el cuadro y perderla.
    <Modal
      title={entrega.origen === 'creada' ? 'Cuenta creada' : 'Clave restablecida'}
      focoInicial={refCopiar}
      descritoPor={`${id}-quien ${id}-aviso`}
    >
      <p id={`${id}-quien`} className="text-sm" style={{ color: 'var(--text-muted)' }}>
        Clave temporal de <strong style={{ color: 'var(--text-primary)' }}>{entrega.nombre}</strong> ({entrega.email}):
      </p>

      <div className="rounded-xl border-2 p-4 text-center" style={{ borderColor: 'var(--empresa-primary)', backgroundColor: 'var(--surface-200)' }}>
        <p className="font-mono text-xl font-bold tracking-wider break-all select-all" style={{ color: 'var(--text-primary)' }}>
          {entrega.clave}
        </p>
      </div>

      <button ref={refCopiar} type="button" onClick={copiar} className="btn-secondary w-full justify-center text-sm">
        {copiada ? <Check className={`w-4 h-4 ${TONO_OK}`} aria-hidden="true" /> : <Copy className="w-4 h-4" aria-hidden="true" />}
        {/* aria-live: el cambio de texto es la única señal de que se copió. */}
        <span aria-live="polite">{copiada ? 'Copiada' : 'Copiar clave'}</span>
      </button>

      {entrega.clientes && entrega.clientes.length > 0 && (
        <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
          <p className="font-semibold">Con esta cuenta representa a:</p>
          <ul className="mt-1 space-y-0.5 list-disc pl-4">
            {entrega.clientes.map((c) => <li key={c} style={{ color: 'var(--text-primary)' }}>{c}</li>)}
          </ul>
          <p className="mt-1">Si sobra o falta un cliente, elimina la cuenta y créala de nuevo.</p>
        </div>
      )}

      <div id={`${id}-aviso`}>
        <AvisoAmbar>
          Esta clave se muestra <strong>una sola vez</strong>: al cerrar este cuadro no se puede volver a ver.{' '}
          {noEntra ? (
            <>
              <strong>{noEntra}: la clave nueva no funcionará hasta que la reactives</strong> (botón «Reactivar» en su
              fila). Reactívala y recién entonces entrégale la clave al contacto.
            </>
          ) : (
            <>Entrégasela ahora al contacto y pídele que la cambie al entrar.</>
          )}{' '}
          {entrega.vence && <>Vence el {entrega.vence} (hora de Chile) si el contacto no la cambia antes.{' '}</>}
          Si se pierde, usa «Restablecer clave».
        </AvisoAmbar>
      </div>

      <button type="button" onClick={onCerrar} className="btn-primary w-full justify-center text-sm">
        Ya la guardé, cerrar
      </button>
    </Modal>
  )
}
