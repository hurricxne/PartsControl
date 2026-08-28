/**
 * Resaltado de búsqueda COMPARTIDO.
 *
 * Origen: copia del `Resaltado` local de pages/DespachosPage.tsx (spec de
 * buscadores 2026-08-05) — los duplicados locales de otras páginas NO se tocan;
 * este archivo existe para los buscadores nuevos (SelectorOcFactura).
 *
 * Reglas (que nadie las «arregle»):
 *  · JAMÁS dangerouslySetInnerHTML: el string se parte EN REACT (el repo tiene
 *    0 usos de innerHTML y que siga así — esto pinta texto tecleado por el
 *    usuario).
 *  · Un acierto por pasada COLAPSADA (7T1997 vs 7T-1997) o por RUT CANÓNICO
 *    (78279030 vs 78.279.030-7) NO ilumina un fragmento: ese fragmento literal
 *    NO EXISTE en el texto mostrado. Se resalta el CAMPO COMPLETO (<MarcaCampo>)
 *    — intentar subrayar "lo que coincidió" dentro del campo es exactamente el
 *    bug que esta regla previene.
 */

const MARK_STYLE = { backgroundColor: 'rgba(245, 158, 11, 0.35)', color: 'inherit' } as const

/** Resalta el PRIMER fragmento coincidente (case-insensitive) entre `tokens`,
 *  partiendo el string en React. Sin coincidencia literal → texto tal cual. */
export function Resaltado({ texto, tokens }: { texto?: string | null; tokens: string[] }) {
  if (!texto || tokens.length === 0) return <>{texto ?? ''}</>
  const lower = texto.toLowerCase()
  let idx = -1
  let len = 0
  for (const t of tokens) {
    const v = t.trim()
    if (!v) continue
    const i = lower.indexOf(v.toLowerCase())
    if (i >= 0 && (idx === -1 || i < idx)) {
      idx = i
      len = v.length
    }
  }
  if (idx < 0) return <>{texto}</>
  return (
    <>
      {texto.slice(0, idx)}
      <mark className="rounded px-0.5" style={MARK_STYLE}>
        {texto.slice(idx, idx + len)}
      </mark>
      {texto.slice(idx + len)}
    </>
  )
}

/** Campo completo marcado: para aciertos por pasada colapsada / RUT canónico
 *  (ver regla de arriba — no hay fragmento literal que subrayar). */
export function MarcaCampo({ children }: { children: React.ReactNode }) {
  return (
    <mark className="rounded px-0.5" style={MARK_STYLE}>
      {children}
    </mark>
  )
}

/** Azúcar para el caso común: acierto colapsado → campo completo; si no →
 *  fragmento literal con la query como único token. Molde: el `CampoFiltrado`
 *  de DespachosPage. */
export function CampoResaltado({
  texto,
  query,
  colapsado,
}: {
  texto: string
  query: string
  colapsado: boolean
}) {
  const q = query.trim()
  if (!q) return <>{texto}</>
  if (colapsado) return <MarcaCampo>{texto}</MarcaCampo>
  return <Resaltado texto={texto} tokens={[q]} />
}
