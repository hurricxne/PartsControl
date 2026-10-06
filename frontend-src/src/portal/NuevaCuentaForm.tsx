// Formulario "Nueva cuenta" de la pantalla interna Cuentas del portal
// (POST /api/portal-admin/cuentas).
//
// Reglas que decide el BACKEND y acá solo se adelantan para no hacer un viaje de más:
//   · correo con forma de correo, nombre obligatorio, al menos un cliente.
// Reglas que SOLO puede decidir el backend, y cuyo `detail` se muestra tal cual:
//   · 409 si el correo ya existe (jamás se convierte una cuenta existente: si fuera del
//     personal, pasarla a portal lo dejaría fuera del ERP),
//   · 404 si una ficha de cliente ya no existe.
import { useId, useMemo, useRef, useState } from 'react'
import { AlertCircle, KeyRound, RefreshCw, Save, Search, X } from 'lucide-react'
import toast from 'react-hot-toast'
import { portalAdminAPI, detalleDe } from './api'
import type { ClienteAdmin, CuentaCreada, PerfilPortal } from './types'
import { PERFILES } from './perfiles'
import { TONO_ERROR } from './tonos'

// Las fichas de cliente tienen TRES estados y el formulario no puede confundirlos: con
// la lista vacía "todavía cargando" y "falló la carga" se veían igual que "no hay
// fichas", y el operador iba a crear fichas que ya existían (un RUT duplicado deja sus
// ventas sin vincular). «No hay fichas» se dice solo cuando el backend respondió vacío.
export type CargaFichas =
  | { estado: 'cargando' }
  | { estado: 'error'; mensaje: string }
  | { estado: 'lista' }

interface NuevaCuentaFormProps {
  clientes: ClienteAdmin[]
  cargaFichas: CargaFichas
  onReintentarFichas: () => void
  onCancelar: () => void
  // `nombre` y `clientes` (nombre + RUT de cada ficha asignada) viajan aparte porque la
  // respuesta del backend no los repite.
  onCreada: (cuenta: CuentaCreada, nombre: string, clientes: string[]) => void
}

// Para buscar: sin mayúsculas, sin tildes y sin la puntuación del RUT, así "76123" calza
// con "76.123.456-7" y "minera" con "Minería".
const llave = (texto: string): string =>
  texto.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[.\-\s]/g, '').toLowerCase()

// El color de las etiquetas es --text-muted y no --text-faint (el del resto del ERP): a
// este tamaño el tenue queda en 2.6:1 sobre blanco y la etiqueta es lo que dice qué
// escribir en cada campo.
const labelCls = 'block text-[11px] font-semibold uppercase tracking-wider mb-1'
const labelColor = { color: 'var(--text-muted)' }

const nombreFicha = (c: ClienteAdmin): string => c.nombre || '(sin nombre)'
const rutFicha = (c: ClienteAdmin): string => c.rut || 'sin RUT'

export default function NuevaCuentaForm({
  clientes, cargaFichas, onReintentarFichas, onCancelar, onCreada,
}: NuevaCuentaFormProps) {
  const [email, setEmail] = useState('')
  const [nombre, setNombre] = useState('')
  const [perfil, setPerfil] = useState<PerfilPortal>('procurement')
  const [seleccion, setSeleccion] = useState<number[]>([])
  const [filtro, setFiltro] = useState('')
  const [guardando, setGuardando] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const id = useId()
  const refCorreo = useRef<HTMLInputElement>(null)

  const visibles = useMemo(() => {
    const q = llave(filtro)
    return q ? clientes.filter((c) => llave(c.nombre).includes(q) || llave(c.rut).includes(q)) : clientes
  }, [clientes, filtro])

  // Los elegidos como FICHAS, no como ids: se pintan aparte del buscador (ver abajo).
  // Un id cuya ficha ya no viene en la lista se muestra igual, para poder quitarlo.
  const elegidos = useMemo(
    () => seleccion.map((clienteId) => ({ id: clienteId, ficha: clientes.find((c) => c.id === clienteId) })),
    [seleccion, clientes]
  )

  const alternar = (clienteId: number) =>
    setSeleccion((prev) => (prev.includes(clienteId) ? prev.filter((x) => x !== clienteId) : [...prev, clienteId]))

  const enviar = async (ev: React.FormEvent) => {
    ev.preventDefault()
    if (guardando) return
    const correo = email.trim()
    const quien = nombre.trim()
    // El navegador ya trae la regla de un campo de correo, la misma del formulario de
    // ingreso: lo que para él no es una dirección tampoco se podría escribir al entrar.
    // No se copia la expresión del backend (sería un espejo que alguien tiene que
    // vigilar); lo que el navegador acepte y el backend no, vuelve como su 400. El
    // backend decide.
    if (!correo || refCorreo.current?.validity.typeMismatch) {
      toast.error('Escribe solo la dirección de correo, sin nombre ni signos'); return
    }
    if (!quien) { toast.error('El nombre es obligatorio'); return }
    if (seleccion.length === 0) { toast.error('Elige al menos un cliente'); return }
    setError(null)
    setGuardando(true)
    try {
      const { data } = await portalAdminAPI.crearCuenta({ email: correo, nombre: quien, cliente_ids: seleccion, perfil })
      onCreada(data, quien, elegidos.map((e) => (e.ficha ? `${nombreFicha(e.ficha)} · ${rutFicha(e.ficha)}` : `Ficha ${e.id}`)))
    } catch (err) {
      // Queda FIJO en el formulario y no en un toast: el 409 explica por qué el correo no
      // sirve y qué hacer, y son dos frases que un toast no deja leer.
      setError(detalleDe(err) ?? 'No se pudo crear la cuenta. Intenta nuevamente.')
    } finally {
      setGuardando(false)
    }
  }

  return (
    <form onSubmit={enviar} noValidate className="card p-5 border-2" style={{ borderColor: 'var(--empresa-primary)' }}>
      <div className="flex items-center justify-between mb-4">
        <h3 className="font-semibold flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
          <KeyRound className="w-4 h-4" style={{ color: 'var(--empresa-primary)' }} />
          Nueva cuenta del portal
        </h3>
        <button type="button" onClick={onCancelar} aria-label="Cerrar formulario" style={{ color: 'var(--text-faint)' }}>
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
        <div>
          <label htmlFor={`${id}-email`} className={labelCls} style={labelColor}>Correo del contacto *</label>
          <input ref={refCorreo} id={`${id}-email`} type="email" className="input w-full" placeholder="contacto@cliente.cl"
            value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
        </div>
        <div>
          <label htmlFor={`${id}-nombre`} className={labelCls} style={labelColor}>Nombre del contacto *</label>
          <input id={`${id}-nombre`} className="input w-full" placeholder="Nombre y apellido"
            value={nombre} onChange={(e) => setNombre(e.target.value)} autoComplete="off" />
        </div>
        <div>
          <label htmlFor={`${id}-perfil`} className={labelCls} style={labelColor}>Perfil *</label>
          <select id={`${id}-perfil`} className="input w-full" value={perfil}
            onChange={(e) => setPerfil(e.target.value as PerfilPortal)}>
            {(Object.keys(PERFILES) as PerfilPortal[]).map((p) => (
              <option key={p} value={p}>{PERFILES[p].label}</option>
            ))}
          </select>
          <p className="text-[11px] mt-1" style={{ color: 'var(--text-faint)' }}>{PERFILES[perfil].descripcion}</p>
        </div>
      </div>

      {/* min-w-0: un <fieldset> no se encoge por defecto bajo el ancho de su contenido, y
          un nombre de cliente largo (que va truncado) ensanchaba toda la página en móvil. */}
      <fieldset className="mb-4 min-w-0">
        <legend className={labelCls} style={labelColor}>
          Cliente(s) que representa * {seleccion.length > 0 && `· ${seleccion.length} elegido${seleccion.length !== 1 ? 's' : ''}`}
        </legend>
        {/* Los elegidos van SIEMPRE a la vista, fuera del buscador: un cliente marcado
            (o marcado por error) que el filtro ya no muestra igual se asigna al crear, y
            el contacto vería en su portal el nombre y los pedidos de otra empresa. */}
        {elegidos.length > 0 && (
          <ul className="flex flex-wrap gap-2 mb-2" aria-label="Clientes elegidos">
            {elegidos.map(({ id: clienteId, ficha }) => (
              <li key={clienteId} className="inline-flex items-center gap-2 max-w-full rounded-lg border pl-2.5 pr-1 py-1 text-xs"
                style={{ borderColor: 'var(--empresa-primary)', backgroundColor: 'var(--surface-200)', color: 'var(--text-primary)' }}>
                <span className="min-w-0 truncate">
                  <span className="font-medium">{ficha ? nombreFicha(ficha) : `Ficha ${clienteId}`}</span>
                  {ficha && <span className="font-mono" style={{ color: 'var(--text-muted)' }}> · {rutFicha(ficha)}</span>}
                </span>
                <button type="button" onClick={() => alternar(clienteId)}
                  aria-label={`Quitar a ${ficha ? nombreFicha(ficha) : `la ficha ${clienteId}`}`}
                  className="shrink-0 inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-[var(--surface-300)]"
                  style={{ color: 'var(--text-muted)' }}>
                  <X className="w-3 h-3" aria-hidden="true" /> Quitar
                </button>
              </li>
            ))}
          </ul>
        )}
        {clientes.length === 0 && cargaFichas.estado === 'cargando' ? (
          <p className="flex items-center gap-2 text-xs rounded-xl border px-3 py-3" style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}>
            <RefreshCw className="w-4 h-4 animate-spin" aria-hidden="true" /> Cargando fichas de cliente…
          </p>
        ) : clientes.length === 0 && cargaFichas.estado === 'error' ? (
          <div role="alert" className="flex flex-wrap items-center gap-3 rounded-xl border px-3 py-3" style={{ borderColor: 'var(--border)' }}>
            <p className={`flex items-start gap-2 text-xs flex-1 min-w-0 ${TONO_ERROR}`}>
              <AlertCircle className="w-4 h-4 shrink-0" aria-hidden="true" />
              <span>{cargaFichas.mensaje}</span>
            </p>
            <button type="button" onClick={onReintentarFichas} className="btn-secondary text-sm">Reintentar</button>
          </div>
        ) : clientes.length === 0 ? (
          <p className="text-xs rounded-xl border px-3 py-3" style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}>
            No hay fichas de cliente. Créalas primero en Admin → Clientes.
          </p>
        ) : (
          <>
            <div className="relative mb-2 max-w-sm">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
              {/* Enter en el buscador NO envía el formulario: quien busca un cliente y
                  aprieta Enter por costumbre crearía la cuenta a medio llenar. */}
              <input className="input pl-9 w-full" placeholder="Buscar por nombre o RUT…" aria-label="Buscar cliente"
                value={filtro} onChange={(e) => setFiltro(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') e.preventDefault() }} />
            </div>
            <div className="rounded-xl border max-h-56 overflow-y-auto" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
              {visibles.length === 0 ? (
                <p className="text-xs px-3 py-3" style={{ color: 'var(--text-faint)' }}>Sin resultados para «{filtro}»</p>
              ) : visibles.map((c) => (
                <label key={c.id} className="flex items-center gap-3 px-3 py-2 text-sm cursor-pointer border-b last:border-b-0 hover:bg-[var(--surface-300)]"
                  style={{ borderColor: 'var(--border)' }}>
                  <input type="checkbox" className="shrink-0" checked={seleccion.includes(c.id)} onChange={() => alternar(c.id)} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium" style={{ color: 'var(--text-primary)' }}>{c.nombre || '(sin nombre)'}</span>
                    <span className="block text-[11px] font-mono" style={{ color: 'var(--text-muted)' }}>{c.rut || 'sin RUT'}</span>
                  </span>
                  {/* Con 0 ventas vinculadas el contacto entraría a un portal vacío: conviene
                      saberlo ANTES de crearle la cuenta. */}
                  <span className="text-[11px] whitespace-nowrap text-right" style={{ color: 'var(--text-faint)' }}>
                    {c.ventas_vinculadas} venta{c.ventas_vinculadas !== 1 ? 's' : ''} · {c.cuentas} cuenta{c.cuentas !== 1 ? 's' : ''}
                  </span>
                </label>
              ))}
            </div>
          </>
        )}
      </fieldset>

      {error && (
        <p role="alert" className={`flex items-start gap-2 text-xs mb-4 ${TONO_ERROR}`}>
          <AlertCircle className="w-4 h-4 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
          Al crearla se genera una clave temporal que se muestra una sola vez.
        </p>
        <div className="flex gap-3 ml-auto">
          <button type="button" onClick={onCancelar} className="btn-secondary">Cancelar</button>
          {/* Sin fichas no hay cuenta posible: el botón no invita a un envío que solo
              respondería «Elige al menos un cliente». */}
          <button type="submit" disabled={guardando || clientes.length === 0} className="btn-primary">
            {guardando ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            Crear cuenta
          </button>
        </div>
      </div>
    </form>
  )
}
