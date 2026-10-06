// Página INTERNA "Cuentas del portal" (ruta /cuentas-portal, menú Admin). Solo personal
// de Grupo AM: el backend (/api/portal-admin/*) exige empresa 'mineria'.
//
// Desde acá el personal le abre la puerta del Portal de Clientes a los contactos de un
// cliente. Tres bloques:
//   a) Cuentas       quién tiene acceso, a qué cliente(s), en qué estado y cuándo entró.
//                    Acciones: restablecer clave, desactivar, reactivar, eliminar.
//   b) Nueva cuenta  NuevaCuentaForm → devuelve una clave temporal que se muestra UNA vez.
//   c) Vinculación   VinculacionPanel → qué ventas va a ver cada cliente (vínculo por RUT).
//
// LA CLAVE TEMPORAL vive en el estado de esta página solo mientras el cuadro está
// abierto. No se guarda en localStorage, no va a un toast y no se registra en consola:
// al cerrar el cuadro deja de existir en el navegador (el backend guarda solo el hash).
//
// Las fichas de cliente se crean y editan en Admin → Clientes, no acá.
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle, Ban, CheckCircle2, Clock, KeyRound, Plus, RefreshCw, RotateCcw, Trash2, UserX, Users,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { portalAdminAPI, detalleDe } from './api'
import type { ClienteAdmin, CuentaCreada, CuentaPortal, EstadoCuenta, Vinculacion } from './types'
import { etiquetaPerfil } from './perfiles'
import { ClaveTemporalModal, ConfirmarModal, motivoNoEntra, type ClaveEntregada } from './modalesAdmin'
import NuevaCuentaForm, { type CargaFichas } from './NuevaCuentaForm'
import VinculacionPanel from './VinculacionPanel'
import { TONO_ATENCION, TONO_ERROR, TONO_OK } from './tonos'

// Estado con PALABRA + ícono + color: el color solo no alcanza (daltonismo, impresión).
const ESTADOS: Record<EstadoCuenta, { label: string; cls: string; icon: React.ElementType }> = {
  activa: { label: 'Activa', cls: `bg-emerald-500/10 ${TONO_OK}`, icon: CheckCircle2 },
  vencida: { label: 'Vencida por inactividad', cls: `bg-amber-500/10 ${TONO_ATENCION}`, icon: Clock },
  desactivada: { label: 'Desactivada', cls: `bg-red-500/10 ${TONO_ERROR}`, icon: Ban },
}
// `estado` mira solo inactividad. Una cuenta 'activa' cuya clave TEMPORAL venció tampoco
// entra (el login la rechaza): si la insignia dijera «Activa», la pantalla prometería lo
// contrario y el operador concluiría que la clave falló.
const ESTADO_CLAVE_VENCIDA = { label: 'Clave temporal vencida', cls: `bg-amber-500/10 ${TONO_ATENCION}`, icon: KeyRound }

/**
 * Fecha y hora de CHILE de una marca de tiempo del backend del portal.
 *
 * Las fechas de portal_acceso son `datetime.utcnow()` y llegan SIN zona
 * ("2026-10-02T13:45:12.123456"). `new Date(iso)` las leería como hora local del
 * navegador: un acceso de las 21:30 de Chile se pintaría como del día siguiente. Acá se
 * declara el UTC y se formatea en la zona de Chile (misma convención que
 * utils/format.ts → fmtFechaServidor, que solo da el día). Si el ISO ya trae zona, se
 * respeta. Los microsegundos se recortan a milisegundos, que es lo que el estándar de
 * JavaScript garantiza leer.
 */
function fmtFechaHoraChile(iso: string | null): string | null {
  if (!iso) return null
  const recortado = iso.replace(/(\.\d{3})\d+/, '$1')
  const tieneZona = /(?:Z|[+-]\d{2}:?\d{2})$/.test(recortado)
  const d = new Date(tieneZona ? recortado : `${recortado}Z`)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleString('es-CL', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'America/Santiago',
  })
}

type Accion = 'restablecer' | 'desactivar' | 'reactivar' | 'eliminar'

interface Pendiente {
  accion: Accion
  cuenta: CuentaPortal
}

interface TextosConfirmacion {
  title: string
  mensaje: string
  aviso?: string | null
  etiqueta: string
  peligro?: boolean
}

const FALLOS: Record<Accion, string> = {
  restablecer: 'No se pudo restablecer la clave',
  desactivar: 'No se pudo desactivar la cuenta',
  reactivar: 'No se pudo reactivar la cuenta',
  eliminar: 'No se pudo eliminar la cuenta',
}

function textosDe({ accion, cuenta }: Pendiente): TextosConfirmacion {
  const quien = `${cuenta.nombre} (${cuenta.email})`
  switch (accion) {
    case 'desactivar':
      return {
        title: 'Desactivar cuenta',
        mensaje: `${quien} no podrá entrar al Portal de Clientes hasta que reactives su cuenta.`,
        etiqueta: 'Desactivar',
        peligro: true,
      }
    case 'reactivar':
      return {
        title: 'Reactivar cuenta',
        mensaje: `${quien} volverá a tener acceso al Portal de Clientes. Esta cuenta fue desactivada a propósito: confirma que el contacto sigue autorizado por su empresa.`,
        // Reactivar NO renueva la clave temporal (el backend no revive una clave que
        // viajó por correo o WhatsApp): si venció, hace falta un segundo paso.
        aviso: cuenta.clave_temporal_vencida
          ? 'Su clave temporal ya venció: después de reactivarla usa «Restablecer clave» y entrégale la nueva. Sin eso no podrá entrar.'
          : null,
        etiqueta: 'Reactivar',
      }
    case 'eliminar':
      return {
        title: 'Eliminar cuenta',
        mensaje: `Se borrará la cuenta de ${quien} y su asignación a clientes. No se puede deshacer. El correo queda libre para crear la cuenta de nuevo.`,
        aviso: 'Para quitarle el acceso sin perder el rastro de la cuenta, usa «Desactivar» en vez de eliminar.',
        etiqueta: 'Eliminar cuenta',
        peligro: true,
      }
    case 'restablecer': {
      const noEntra = motivoNoEntra(cuenta.estado)
      return {
        title: 'Restablecer clave',
        mensaje: `La clave actual de ${quien} dejará de funcionar de inmediato y se generará una clave temporal nueva, que tendrás que entregarle.`,
        aviso: noEntra && `${noEntra}: la clave nueva no funcionará hasta que además la reactives (botón «Reactivar» en su fila).`,
        etiqueta: 'Restablecer clave',
      }
    }
  }
}

const thCls = 'text-left px-4 py-3 text-[10px] font-semibold uppercase tracking-wider whitespace-nowrap'
const accionCls = 'inline-flex items-center gap-1 text-xs rounded px-2 py-1 transition-colors disabled:opacity-40 disabled:cursor-not-allowed'

export default function CuentasPortalPage() {
  const [cuentas, setCuentas] = useState<CuentaPortal[]>([])
  const [diasCorte, setDiasCorte] = useState<number | null>(null)
  const [clientes, setClientes] = useState<ClienteAdmin[]>([])
  const [vinculacion, setVinculacion] = useState<Vinculacion | null>(null)
  const [cargaFichas, setCargaFichas] = useState<CargaFichas>({ estado: 'cargando' })
  const [cargando, setCargando] = useState(true)
  const [errorCuentas, setErrorCuentas] = useState<string | null>(null)
  const [errorVinculacion, setErrorVinculacion] = useState<string | null>(null)
  const [mostrarForm, setMostrarForm] = useState(false)
  const [pendiente, setPendiente] = useState<Pendiente | null>(null)
  // ids de las cuentas con una acción en vuelo (bloquea sus botones contra el doble
  // clic). Es un CONJUNTO y no un id suelto: con uno solo, la acción de una fila que
  // terminaba primero liberaba también el cuadro de confirmación de OTRA cuenta cuya
  // petición seguía en vuelo, y «Restablecer clave» se podía apretar dos veces.
  const [enVuelo, setEnVuelo] = useState<ReadonlySet<number>>(new Set())
  const [entrega, setEntrega] = useState<ClaveEntregada | null>(null)
  const refNuevaCuenta = useRef<HTMLButtonElement>(null)

  const marcarEnVuelo = (cuentaId: number, activa: boolean) =>
    setEnVuelo((prev) => {
      const sig = new Set(prev)
      if (activa) sig.add(cuentaId)
      else sig.delete(cuentaId)
      return sig
    })

  // Cada carga atrapa SU error: si falla una, las otras dos igual se pintan.
  const cargarCuentas = useCallback(async () => {
    try {
      const { data } = await portalAdminAPI.cuentas()
      setCuentas(data.cuentas)
      setDiasCorte(data.dias_inactividad_corte)
      setErrorCuentas(null)
    } catch (err) {
      setErrorCuentas(detalleDe(err) ?? 'No se pudieron cargar las cuentas del portal.')
    }
  }, [])

  const cargarClientes = useCallback(async () => {
    setCargaFichas({ estado: 'cargando' })
    try {
      const { data } = await portalAdminAPI.clientes()
      setClientes(data)
      setCargaFichas({ estado: 'lista' })
    } catch (err) {
      const mensaje = detalleDe(err) ?? 'No se pudieron cargar las fichas de cliente.'
      // Queda en el estado además del toast: el formulario de alta lo muestra fijo, con
      // su botón Reintentar, en vez de decir «No hay fichas» (que sería falso).
      setCargaFichas({ estado: 'error', mensaje })
      toast.error(mensaje, { id: 'portal-admin-clientes' })
    }
  }, [])

  const cargarVinculacion = useCallback(async () => {
    try {
      const { data } = await portalAdminAPI.vinculacion()
      setVinculacion(data)
      setErrorVinculacion(null)
    } catch (err) {
      setVinculacion(null)
      setErrorVinculacion(detalleDe(err) ?? 'No se pudo calcular la vinculación de ventas.')
    }
  }, [])

  const refrescar = useCallback(async () => {
    setCargando(true)
    await Promise.all([cargarCuentas(), cargarClientes(), cargarVinculacion()])
    setCargando(false)
  }, [cargarCuentas, cargarClientes, cargarVinculacion])

  useEffect(() => { void refrescar() }, [refrescar])

  const alCrear = (cuenta: CuentaCreada, nombre: string, clientesAsignados: string[]) => {
    setMostrarForm(false)
    setEntrega({
      nombre, email: cuenta.email, clave: cuenta.clave_temporal, origen: 'creada', clientes: clientesAsignados,
      vence: fmtFechaHoraChile(cuenta.clave_temporal_vence_at),
    })
    // Las fichas también: cambió su contador de cuentas.
    void cargarCuentas()
    void cargarClientes()
  }

  // Único camino por el que sale una acción sobre una cuenta, con o sin confirmación
  // previa: así el candado contra la doble ejecución no depende de quién la dispare.
  const ejecutar = async ({ accion, cuenta }: Pendiente) => {
    if (enVuelo.has(cuenta.id)) return
    marcarEnVuelo(cuenta.id, true)
    try {
      if (accion === 'restablecer') {
        const { data } = await portalAdminAPI.restablecerClave(cuenta.id)
        setEntrega({
          nombre: cuenta.nombre, email: cuenta.email, clave: data.clave_temporal,
          origen: 'restablecida', estado: cuenta.estado,
          vence: fmtFechaHoraChile(data.clave_temporal_vence_at),
        })
      } else if (accion === 'eliminar') {
        await portalAdminAPI.eliminar(cuenta.id)
        toast.success(`Cuenta de ${cuenta.nombre} eliminada`)
        // Las fichas también: bajó su contador de cuentas (es lo que destraba editarlas).
        await Promise.all([cargarCuentas(), cargarClientes()])
      } else if (accion === 'reactivar') {
        const { data } = await portalAdminAPI.reactivar(cuenta.id)
        if (data.clave_temporal_vencida) {
          // Reactivar no renueva la temporal: la cuenta queda activa pero no entra. Es
          // una instrucción, no un éxito a secas: toast neutro y con tiempo para leerla.
          toast(`Cuenta de ${cuenta.nombre} reactivada. Su clave temporal ya venció: usa «Restablecer clave» para que pueda entrar.`,
            { duration: 9000 })
        } else {
          toast.success(`Cuenta de ${cuenta.nombre} reactivada`)
        }
        await cargarCuentas()
      } else {
        await portalAdminAPI.desactivar(cuenta.id)
        toast.success(`Cuenta de ${cuenta.nombre} desactivada`)
        await cargarCuentas()
      }
      // En el mismo paso que setEntrega: el cuadro de confirmación se cierra en el mismo
      // render en que se abre el de la clave, y el foco pasa de uno a otro sin perderse.
      setPendiente(null)
    } catch (err) {
      toast.error(detalleDe(err) ?? FALLOS[accion])
    } finally {
      marcarEnVuelo(cuenta.id, false)
    }
  }

  const cerrarEntrega = () => {
    // Tras un alta, el control que abrió el cuadro (el formulario) ya no existe: el foco
    // vuelve a «Nueva cuenta» en vez de perderse al principio de la página. Tras un
    // restablecimiento lo devuelve el propio modal al botón de la fila.
    if (entrega?.origen === 'creada') refNuevaCuenta.current?.focus()
    setEntrega(null)
  }

  const textos = pendiente ? textosDe(pendiente) : null

  const alAplicarVinculacion = useCallback(async () => {
    await Promise.all([cargarVinculacion(), cargarClientes()])
  }, [cargarVinculacion, cargarClientes])

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Cuentas del portal</h1>
          <p className="text-sm mt-0.5" style={{ color: 'var(--text-muted)' }}>
            Acceso de los contactos de un cliente al Portal de Clientes.
            {diasCorte !== null && ` Una cuenta sin uso por más de ${diasCorte} días queda vencida hasta que se reactive.`}
          </p>
        </div>
        <div className="flex items-center gap-2 self-start shrink-0">
          <button type="button" onClick={refrescar} disabled={cargando} className="btn-secondary" aria-label="Actualizar" title="Actualizar">
            <RefreshCw className={`w-4 h-4 ${cargando ? 'animate-spin' : ''}`} />
          </button>
          <button ref={refNuevaCuenta} type="button" onClick={() => setMostrarForm(true)} className="btn-primary">
            <Plus className="w-4 h-4" /> Nueva cuenta
          </button>
        </div>
      </div>

      {/* b) Nueva cuenta */}
      {mostrarForm && (
        <NuevaCuentaForm
          clientes={clientes}
          cargaFichas={cargaFichas}
          onReintentarFichas={cargarClientes}
          onCancelar={() => setMostrarForm(false)}
          onCreada={alCrear}
        />
      )}

      {/* a) Cuentas */}
      {errorCuentas ? (
        <div role="alert" className="card p-5 flex flex-col sm:flex-row sm:items-center gap-3">
          <p className={`flex items-start gap-2 text-sm flex-1 ${TONO_ERROR}`}>
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" /> <span>{errorCuentas}</span>
          </p>
          <button type="button" onClick={refrescar} className="btn-secondary text-sm self-start">Reintentar</button>
        </div>
      ) : cargando && cuentas.length === 0 ? (
        <div className="flex items-center justify-center h-40 gap-2" style={{ color: 'var(--text-faint)' }}>
          <RefreshCw className="w-5 h-5 animate-spin" /> Cargando cuentas…
        </div>
      ) : cuentas.length === 0 ? (
        <div className="card p-10 text-center">
          <Users className="w-8 h-8 mx-auto mb-3" style={{ color: 'var(--text-faint)' }} />
          <p className="font-medium" style={{ color: 'var(--text-muted)' }}>Todavía no hay cuentas del portal</p>
          <button type="button" onClick={() => setMostrarForm(true)} className="btn-primary mt-4 mx-auto">
            <Plus className="w-4 h-4" /> Crear la primera cuenta
          </button>
        </div>
      ) : (
        <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '2px solid var(--border)' }}>
                  {['Nombre', 'Correo', 'Cliente(s)', 'Perfil', 'Estado', 'Último acceso', 'Acciones'].map((h) => (
                    <th key={h} scope="col" className={thCls} style={{ color: 'var(--text-faint)' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {cuentas.map((c, idx) => {
                  const temporalVencida = c.estado === 'activa' && c.clave_temporal_vencida
                  const estado = temporalVencida ? ESTADO_CLAVE_VENCIDA : (ESTADOS[c.estado] ?? ESTADOS.desactivada)
                  const IconoEstado = estado.icon
                  const perfiles = [...new Set(c.clientes.map((x) => x.perfil))]
                  const ultimoAcceso = fmtFechaHoraChile(c.ultimo_acceso_at)
                  const enCurso = enVuelo.has(c.id)
                  return (
                    <tr key={c.id} style={{ borderBottom: '1px solid var(--border)', backgroundColor: idx % 2 === 0 ? 'var(--surface-100)' : 'var(--surface)' }}>
                      {/* Encabezado de FILA: el lector de pantalla lo repite al recorrer las celdas. */}
                      <th scope="row" className="px-4 py-3 font-medium text-left" style={{ color: 'var(--text-primary)' }}>{c.nombre}</th>
                      <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-muted)' }}>{c.email}</td>
                      <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-primary)' }}>
                        {c.clientes.length === 0
                          // Sin cliente la cuenta no entra (el backend responde 403): que se note.
                          ? <span className={TONO_ATENCION}>Sin cliente asignado</span>
                          : c.clientes.map((x) => <span key={x.id} className="block">{x.nombre || `Ficha ${x.id}`}</span>)}
                      </td>
                      <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-muted)' }}>
                        {perfiles.length === 0 ? '—' : perfiles.map(etiquetaPerfil).join(' + ')}
                        {/* Con perfiles distintos por cliente el backend aplica el más
                            restrictivo en TODOS (no ve montos en ninguno). */}
                        {perfiles.length > 1 && <span className="block text-[11px]" style={{ color: 'var(--text-faint)' }}>Opera como Bodega</span>}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <span className={`badge ${estado.cls}`}>
                          <IconoEstado className="w-3.5 h-3.5" aria-hidden="true" /> {estado.label}
                        </span>
                        {/* Mientras la cuenta siga con la clave temporal, hasta cuándo sirve
                            (o que ya no sirve): el único dato que distingue «no entra porque
                            la clave venció» de «la clave que le entregué falló». */}
                        {c.clave_temporal_vence_at && (
                          <span className="block text-[11px] mt-1" style={{ color: 'var(--text-faint)' }}>
                            {c.clave_temporal_vencida
                              ? 'No puede entrar hasta que restablezcas su clave'
                              : `Clave temporal · vence el ${fmtFechaHoraChile(c.clave_temporal_vence_at)}`}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-xs whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                        {ultimoAcceso ?? <span style={{ color: 'var(--text-faint)' }}>Nunca ha ingresado</span>}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        {/* aria-label con el nombre: en todas las filas los botones dicen lo
                            mismo, y sin él un lector de pantalla no sabe de qué cuenta son. */}
                        <div className="flex items-center gap-1">
                          <button type="button" disabled={enCurso} onClick={() => setPendiente({ accion: 'restablecer', cuenta: c })}
                            aria-label={`Restablecer clave de ${c.nombre}`}
                            className={`${accionCls} ${TONO_ATENCION} hover:bg-amber-500/10`}>
                            <KeyRound className="w-3.5 h-3.5" aria-hidden="true" /> Restablecer clave
                          </button>
                          {c.estado !== 'activa' && (
                            // Una cuenta VENCIDA se reactiva al tiro (solo se le pasó el
                            // plazo). Una DESACTIVADA la apagó alguien a propósito (p. ej.
                            // el contacto dejó la empresa): devolverle el acceso con un
                            // clic errado no puede ser, así que pide confirmación.
                            <button type="button" disabled={enCurso}
                              onClick={() => (c.estado === 'desactivada'
                                ? setPendiente({ accion: 'reactivar', cuenta: c })
                                : ejecutar({ accion: 'reactivar', cuenta: c }))}
                              aria-label={`Reactivar la cuenta de ${c.nombre}`}
                              className={`${accionCls} ${TONO_OK} hover:bg-emerald-500/10`}>
                              <RotateCcw className="w-3.5 h-3.5" aria-hidden="true" /> Reactivar
                            </button>
                          )}
                          {c.estado !== 'desactivada' && (
                            <button type="button" disabled={enCurso} onClick={() => setPendiente({ accion: 'desactivar', cuenta: c })}
                              aria-label={`Desactivar la cuenta de ${c.nombre}`}
                              className={`${accionCls} ${TONO_ERROR} hover:bg-red-500/10`}>
                              <UserX className="w-3.5 h-3.5" aria-hidden="true" /> Desactivar
                            </button>
                          )}
                          {/* Eliminar: lo que pide el backend para corregir el RUT o borrar
                              una ficha con cuentas, y la salida de un correo mal escrito. */}
                          <button type="button" disabled={enCurso} onClick={() => setPendiente({ accion: 'eliminar', cuenta: c })}
                            aria-label={`Eliminar la cuenta de ${c.nombre}`}
                            className={`${accionCls} ${TONO_ERROR} hover:bg-red-500/10`}>
                            <Trash2 className="w-3.5 h-3.5" aria-hidden="true" /> Eliminar
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <div className="px-4 py-2.5 border-t" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
            <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
              {cuentas.length} cuenta{cuentas.length !== 1 ? 's' : ''} · fechas en hora de Chile
            </p>
          </div>
        </div>
      )}

      {/* c) Vinculación de ventas */}
      <VinculacionPanel
        vinculacion={vinculacion}
        error={errorVinculacion}
        clientes={clientes}
        onAplicada={alAplicarVinculacion}
      />

      {pendiente && textos && (
        <ConfirmarModal
          title={textos.title}
          mensaje={textos.mensaje}
          aviso={textos.aviso}
          etiquetaConfirmar={textos.etiqueta}
          peligro={textos.peligro}
          ocupado={enVuelo.has(pendiente.cuenta.id)}
          onConfirmar={() => ejecutar(pendiente)}
          onClose={() => setPendiente(null)}
        />
      )}

      {entrega && <ClaveTemporalModal entrega={entrega} onCerrar={cerrarEntrega} />}
    </div>
  )
}
