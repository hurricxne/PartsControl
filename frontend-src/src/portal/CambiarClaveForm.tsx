// Formulario "Cambiar mi clave" del Portal de Clientes (POST /api/portal/yo/clave).
//
// POR QUÉ EXISTE EN LA FASE 1: el ejecutivo entrega una clave TEMPORAL por correo o
// teléfono. Sin esta pantalla esa clave —que ya pasó por un canal ajeno— sería la
// definitiva del cliente.
//
// Las reglas (mínimo 10, distinta de la actual) se revisan acá solo para responder al
// instante; quien decide es el backend, y su `detail` se muestra tal cual.
import { useId, useRef, useState } from 'react'
import axios from 'axios'
import { AlertCircle, CheckCircle2, KeyRound, Loader2 } from 'lucide-react'
import { useAuthStore } from '../stores/authStore'
import { portalAPI, detalleDe, estadoHttp } from './api'

// Espejo de LARGO_MINIMO_CLAVE en backend/portal/service.py.
const LARGO_MINIMO_CLAVE = 10

type Campo = 'actual' | 'nueva' | 'repetir'

interface ErrorFormulario {
  mensaje: string
  // Campo culpable, cuando se sabe: recibe el foco y queda marcado como inválido.
  campo?: Campo
}

function validar(actual: string, nueva: string, repetir: string): ErrorFormulario | null {
  if (!actual) return { campo: 'actual', mensaje: 'Escribe tu clave actual.' }
  if (nueva.length < LARGO_MINIMO_CLAVE) {
    return { campo: 'nueva', mensaje: `La clave nueva debe tener al menos ${LARGO_MINIMO_CLAVE} caracteres.` }
  }
  if (nueva === actual) {
    return { campo: 'nueva', mensaje: 'La clave nueva debe ser distinta de la actual.' }
  }
  if (nueva !== repetir) {
    return { campo: 'repetir', mensaje: 'Las dos claves nuevas no coinciden.' }
  }
  return null
}

// Respuestas cuyo `detail` está escrito para el cliente: 400 (la clave actual no es
// correcta, la nueva no cumple), 403 (la cuenta no puede usar el portal) y 429 (demasiados
// intentos). El resto (500, 503) puede traer texto para el equipo técnico.
const ESTADOS_CON_MENSAJE_PARA_EL_CLIENTE = [400, 403, 429]

function mensajeDeFalla(err: unknown): string {
  if (err instanceof Error && !axios.isAxiosError(err)) return err.message
  const estado = estadoHttp(err)
  if (estado === undefined) {
    // Sin respuesta no se sabe si el cambio llegó a hacerse: no se afirma que no.
    return 'No pudimos confirmar el cambio de clave. Revisa tu conexión; si al reintentar la clave actual ya no sirve, prueba con la nueva.'
  }
  return (ESTADOS_CON_MENSAJE_PARA_EL_CLIENTE.includes(estado) && detalleDe(err))
    || 'No se pudo cambiar la clave. Intenta nuevamente en unos minutos.'
}

interface CambiarClaveFormProps {
  /** Se llama una vez cuando el backend confirma el cambio de clave. */
  onCambiada?: () => void
}

export default function CambiarClaveForm({ onCambiada }: CambiarClaveFormProps) {
  const [actual, setActual] = useState('')
  const [nueva, setNueva] = useState('')
  const [repetir, setRepetir] = useState('')
  const [mostrar, setMostrar] = useState(false)
  const [guardando, setGuardando] = useState(false)
  const [error, setError] = useState<ErrorFormulario | null>(null)
  const [exito, setExito] = useState(false)
  // Número de intento fallido: es la `key` del mensaje de error. Un error IGUAL al
  // anterior (dos veces «La clave actual no es correcta») no cambia el nodo, y un lector
  // de pantalla no lo repite; con la key el aviso se vuelve a insertar y se vuelve a leer.
  const [intento, setIntento] = useState(0)

  const id = useId()
  const idError = `${id}-error`
  const idAyuda = `${id}-ayuda`
  const refs: Record<Campo, React.RefObject<HTMLInputElement>> = {
    actual: useRef<HTMLInputElement>(null),
    nueva: useRef<HTMLInputElement>(null),
    repetir: useRef<HTMLInputElement>(null),
  }

  const fallar = (e: ErrorFormulario) => {
    setError(e)
    setIntento((n) => n + 1)
    if (e.campo) refs[e.campo].current?.focus()
  }

  const enviar = async (ev: React.FormEvent) => {
    ev.preventDefault()
    if (guardando) return
    setExito(false)
    const invalido = validar(actual, nueva, repetir)
    if (invalido) { fallar(invalido); return }
    setError(null)
    setGuardando(true)
    try {
      const { data } = await portalAPI.cambiarClave({ clave_actual: actual, clave_nueva: nueva })
      // El token con que se pidió el cambio dejó de valer en ese mismo instante (va atado
      // a la clave). Se guarda el nuevo ANTES de avisar a la pantalla: si no, su primera
      // consulta respondería 401 y el portal expulsaría a la persona justo después de
      // cambiar la clave. Las otras pestañas adoptan el token nuevo solas (authStore.ts).
      const { user, setAuth } = useAuthStore.getState()
      if (!user || !data.access_token) {
        // Sin el token nuevo la siguiente consulta respondería 401 y el portal expulsaría
        // a la persona: se dice ahora, con la clave ya cambiada.
        throw new Error('La clave cambió, pero no pudimos renovar tu sesión. Vuelve a ingresar con la clave nueva.')
      }
      setAuth(data.access_token, user)
      // Las claves no se quedan en memoria ni en los campos después de usarlas.
      setActual(''); setNueva(''); setRepetir('')
      setExito(true)
      onCambiada?.()
    } catch (err) {
      fallar({ mensaje: mensajeDeFalla(err) })
    } finally {
      setGuardando(false)
    }
  }

  const tipo = mostrar ? 'text' : 'password'
  // Cada campo apunta al mensaje de error solo cuando el error es SUYO: así el lector de
  // pantalla lo lee al enfocarlo, y no lo repite en los otros dos.
  const descritoPor = (campo: Campo, extra?: string) =>
    [extra, error?.campo === campo ? idError : null].filter(Boolean).join(' ') || undefined

  return (
    <form onSubmit={enviar} noValidate className="space-y-4">
      <div>
        <label htmlFor={`${id}-actual`} className="portal-etiqueta">Clave actual</label>
        <input
          ref={refs.actual}
          id={`${id}-actual`}
          type={tipo}
          className="portal-campo"
          value={actual}
          onChange={(e) => setActual(e.target.value)}
          autoComplete="current-password"
          aria-invalid={error?.campo === 'actual'}
          aria-describedby={descritoPor('actual')}
        />
      </div>

      <div>
        <label htmlFor={`${id}-nueva`} className="portal-etiqueta">Clave nueva</label>
        <input
          ref={refs.nueva}
          id={`${id}-nueva`}
          type={tipo}
          className="portal-campo"
          value={nueva}
          onChange={(e) => setNueva(e.target.value)}
          autoComplete="new-password"
          aria-invalid={error?.campo === 'nueva'}
          aria-describedby={descritoPor('nueva', idAyuda)}
        />
        <p id={idAyuda} className="portal-suave text-sm mt-1.5">
          Mínimo {LARGO_MINIMO_CLAVE} caracteres y distinta de la actual.
        </p>
      </div>

      <div>
        <label htmlFor={`${id}-repetir`} className="portal-etiqueta">Repetir clave nueva</label>
        <input
          ref={refs.repetir}
          id={`${id}-repetir`}
          type={tipo}
          className="portal-campo"
          value={repetir}
          onChange={(e) => setRepetir(e.target.value)}
          autoComplete="new-password"
          aria-invalid={error?.campo === 'repetir'}
          aria-describedby={descritoPor('repetir')}
        />
      </div>

      <label htmlFor={`${id}-mostrar`} className="flex items-center gap-2.5 text-sm min-h-[2.75rem] cursor-pointer w-fit">
        <input
          id={`${id}-mostrar`}
          type="checkbox"
          className="w-4 h-4 shrink-0"
          style={{ accentColor: 'var(--portal-acento)' }}
          checked={mostrar}
          onChange={(e) => setMostrar(e.target.checked)}
        />
        Mostrar las claves mientras escribo
      </label>

      {error && (
        <p key={intento} id={idError} role="alert" className="portal-error">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
          <span>{error.mensaje}</span>
        </p>
      )}

      <div>
        {/* La región role="status" está SIEMPRE montada y solo cambia lo de adentro: una
            región "polite" que nace ya con el texto no se anuncia de forma fiable
            (VoiceOver la ignora), y el cliente quedaba con los tres campos vacíos sin
            saber si la clave cambió. Vive junto al botón, en el mismo bloque, para que
            vacía no agregue un espacio entre los campos y el botón. */}
        <div role="status">
          {exito && (
            <p className="portal-ok mb-4">
              <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
              <span>Tu clave fue actualizada. Úsala la próxima vez que ingreses.</span>
            </p>
          )}
        </div>

        {/* aria-disabled y no disabled: un botón deshabilitado suelta el foco del teclado
            justo mientras se espera (`enviar` ignora el segundo clic). */}
        <button type="submit" aria-disabled={guardando} className="portal-boton w-full sm:w-auto">
          {guardando
            ? <Loader2 className="w-4 h-4 portal-giro" aria-hidden="true" />
            : <KeyRound className="w-4 h-4" aria-hidden="true" />}
          {guardando ? 'Guardando…' : 'Guardar clave nueva'}
        </button>
      </div>
    </form>
  )
}
