// Helpers de formato compartidos del módulo Ventas/Contabilidad.
// fmtClp: monto en pesos chilenos ("$1.234.567"); fmtDate: fecha corta es-CL (dd/mm/aaaa).

/** Formatea un número como pesos chilenos. 0/undefined → "$0". */
export const fmtClp = (n: number): string =>
  n ? '$' + Math.round(n).toLocaleString('es-CL') : '$0'

/** Formatea una fecha ISO a dd/mm/aaaa (es-CL). null → "—"; si no parsea, devuelve el original. */
export const fmtDate = (s: string | null): string => {
  if (!s) return '—'
  // Fechas puras 'YYYY-MM-DD' (sin hora) se parsean como fecha LOCAL: new Date('YYYY-MM-DD')
  // las interpreta en UTC y en Chile (UTC-4/-3) quedarían corridas un día hacia atrás.
  const d = /^\d{4}-\d{2}-\d{2}$/.test(s) ? new Date(s + 'T00:00:00') : new Date(s)
  return isNaN(d.getTime()) ? s : d.toLocaleDateString('es-CL', { day: '2-digit', month: '2-digit', year: 'numeric' })
}

/** Día CHILENO de un timestamp del SERVIDOR (UTC sin offset) → dd/mm/aaaa.
 *
 *  POR QUÉ existe además de fmtDate: las columnas DateTime del backend se estampan
 *  con `datetime.now()` en un server que corre en UTC, y MySQL las devuelve SIN
 *  offset. `new Date(iso)` las lee entonces como hora LOCAL, y un despacho cerrado
 *  a las 20:30 de Chile (guardado 00:30 UTC del día siguiente) se pinta corrido un
 *  día. Acá se declara el UTC y se formatea en la zona de Chile — la MISMA
 *  convención que usa el backend en `_cerrado_hoy` (routers/despachos.py) para
 *  decidir si un despacho salió hoy: display y decisión no pueden discrepar.
 *  Si el ISO YA trae zona (Z o ±hh:mm) se respeta la suya.
 *  OJO en desarrollo: con el backend corriendo en un equipo en hora de Chile, el
 *  valor guardado NO es UTC y este helper lo corre al revés. Es artefacto de dev;
 *  la convención de producción es la que manda (ver monza_fechas.py).
 */
export const fmtFechaServidor = (s: string | null): string => {
  if (!s) return '—'
  const tieneZona = /(?:Z|[+-]\d{2}:?\d{2})$/.test(s)
  const d = new Date(tieneZona ? s : s + 'Z')
  return isNaN(d.getTime())
    ? s
    : d.toLocaleDateString('es-CL', {
        day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'America/Santiago',
      })
}

/** Fecha de hoy LOCAL como 'YYYY-MM-DD' (toISOString usa UTC y de noche en Chile daría el día siguiente). */
export const hoyLocal = (): string => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
