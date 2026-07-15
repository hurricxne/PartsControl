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

/** Fecha de hoy LOCAL como 'YYYY-MM-DD' (toISOString usa UTC y de noche en Chile daría el día siguiente). */
export const hoyLocal = (): string => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
