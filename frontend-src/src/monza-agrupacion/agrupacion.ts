/**
 * Agrupación por proveedor/OC en Logística y Seguimiento de MonzaParts.
 *
 * SOLO funciones puras + tipos: agrupar la lista plana que devuelve el backend,
 * ordenar los grupos, sumar la selección para la barra de totales y armar los
 * textos del semáforo/completitud/badge aduanero. NADA de React acá — las dos
 * páginas (MonzaLogisticaPage, MonzaSeguimientoPage) pintan sus propias filas.
 *
 * Contrato backend (aditivo, idéntico en GET /logistica/preparados y
 * GET /abastecimiento/seguimiento): cada ítem GANA `costo`, `moneda`, `peso_kg`,
 * `peso_total_kg`, `fob_total` y `ocp` (objeto normalizado con semáforo y
 * completitud CALCULADOS EN EL BACKEND). Mientras el backend no exista, las
 * claves llegan undefined y todo degrada: sin ocp → grupo "Sin OC"; sin
 * semáforo → sin chip. El navegador JAMÁS deriva plata ni días de atraso:
 * solo suma lo que recibe.
 */

// ── Tipos del contrato ────────────────────────────────────────────────────────

export interface MonzaSemaforo {
  /** Días hábiles chilenos desde la emisión de la OC (calculado en el backend). */
  dias_habiles: number;
  plazo_dias: number | null;
  /** verde <80% | amarillo 80-100% | rojo vencido | sin_plazo | sin_fecha */
  estado: "verde" | "amarillo" | "rojo" | "sin_plazo" | "sin_fecha";
  /** Negativo = vencido. */
  dias_habiles_restantes: number | null;
}

export interface MonzaCompletitud {
  /** Ítems vivos de TODA la OC (no solo los visibles en el listado). */
  total: number;
  /** Conteo por estado_linea, ej. { comprado: 2, preparado: 6, embarcado: 1 }. */
  por_estado: Record<string, number>;
}

/** Objeto `ocp` normalizado del contrato: numero (correlativo interno) y
 *  numero_oc (N° manual del proveedor) van SEPARADOS y explícitos. */
export interface MonzaOcp {
  id: number;
  numero: string | null;
  numero_oc: string | null;
  proveedor_nombre: string | null;
  pais?: string | null;
  moneda?: string | null;
  tipo_origen?: string | null;
  plazo_dias?: number | null;
  awb?: string | null;
  tracking?: string | null;
  /** ocp.created_at en ISO date; null si legado. */
  fecha_emision?: string | null;
  semaforo?: MonzaSemaforo | null;
  completitud?: MonzaCompletitud | null;
}

/** Claves nuevas (aditivas) que gana cada ítem de los dos endpoints. */
export interface MonzaClavesAgrupacion {
  /** Costo unitario en la moneda del ítem; null si no hay. */
  costo?: number | null;
  /** Moneda del ÍTEM (una OC puede venir mixta). */
  moneda?: string | null;
  /** Peso unitario; 0/null se preserva. */
  peso_kg?: number | null;
  /** peso_kg × cantidad, CALCULADO EN EL BACKEND; null si peso_kg es null. */
  peso_total_kg?: number | null;
  /** costo × cantidad; null si costo es null (JAMÁS 0 mudo). */
  fob_total?: number | null;
  ocp?: MonzaOcp | null;
}

/** Lo mínimo que un ítem necesita para agruparse y sumarse. */
export interface MonzaItemAgrupable extends MonzaClavesAgrupacion {
  id: number;
  cantidad: number;
}

export interface MonzaGrupo<T> {
  /** "oc-<id>" o "sin-oc". */
  key: string;
  ocp: MonzaOcp | null;
  items: T[];
}

// ── Agrupar y ordenar ─────────────────────────────────────────────────────────

/**
 * Agrupa la lista plana en UN nivel: un grupo por OC. Orden: proveedor_nombre
 * asc (null al final), luego fecha_emision asc (null al final), desempate por
 * id de OC (estable). El grupo "Sin OC" (ítems con ocp null/undefined) SIEMPRE
 * va al final.
 */
export function agruparPorOc<T extends { ocp?: MonzaOcp | null }>(items: readonly T[]): MonzaGrupo<T>[] {
  const mapa = new Map<string, MonzaGrupo<T>>();
  for (const it of items) {
    const ocp = it.ocp ?? null;
    const key = ocp ? `oc-${ocp.id}` : "sin-oc";
    const g = mapa.get(key);
    if (g) mapa.set(key, { ...g, items: [...g.items, it] });
    else mapa.set(key, { key, ocp, items: [it] });
  }
  return Array.from(mapa.values()).sort(compararGrupos);
}

function compararGrupos<T>(a: MonzaGrupo<T>, b: MonzaGrupo<T>): number {
  if (!a.ocp && !b.ocp) return 0;
  if (!a.ocp) return 1;   // "Sin OC" al final
  if (!b.ocp) return -1;
  const pa = a.ocp.proveedor_nombre;
  const pb = b.ocp.proveedor_nombre;
  if (pa == null && pb != null) return 1;   // proveedor null después de los con nombre
  if (pa != null && pb == null) return -1;
  if (pa != null && pb != null) {
    const c = pa.localeCompare(pb, "es", { sensitivity: "base" });
    if (c !== 0) return c;
  }
  const fa = a.ocp.fecha_emision ?? null;
  const fb = b.ocp.fecha_emision ?? null;
  if (fa == null && fb != null) return 1;
  if (fa != null && fb == null) return -1;
  if (fa != null && fb != null && fa !== fb) return fa < fb ? -1 : 1; // ISO date: orden lexicográfico = cronológico
  return a.ocp.id - b.ocp.id;
}

/** true cuando no vale la pena pintar cabeceras: todo cayó a "Sin OC" (p. ej.
 *  backend viejo sin las claves nuevas) → la tabla se pinta plana, como antes. */
export function esListadoPlano<T>(grupos: readonly MonzaGrupo<T>[]): boolean {
  return grupos.length === 1 && grupos[0].ocp === null;
}

// ── Semáforo (texto y colores; los DÍAS los calcula el backend) ───────────────

export type MonzaRotuloSemaforo = "edad_oc" | "plazo_proveedor";

export interface MonzaChipSemaforo {
  label: string;
  color: string;
  bg: string;
  /** Tooltip: en Logística el rótulo es «edad de la OC», no «proveedor atrasado». */
  title: string;
}

const SEMAFORO_COLORES: Record<string, { color: string; bg: string }> = {
  verde:    { color: "#15803D", bg: "#DCFCE7" },
  amarillo: { color: "#B45309", bg: "#FEF3C7" },
  rojo:     { color: "#B91C1C", bg: "#FEE2E2" },
};
const SEMAFORO_GRIS = { color: "#64748B", bg: "#F1F5F9" };

/**
 * Chip del semáforo de la OC («Día 34 de 30» con color). Devuelve null si no
 * hay semáforo (backend sin las claves nuevas) o si estado = sin_fecha: sin
 * dato del backend NO hay chip — jamás recalcular días en el navegador.
 */
export function semaforoChip(
  sem: MonzaSemaforo | null | undefined,
  rotulo: MonzaRotuloSemaforo,
): MonzaChipSemaforo | null {
  if (!sem || sem.estado === "sin_fecha") return null;
  const base = rotulo === "edad_oc"
    ? "Edad de la OC (días hábiles desde su emisión)"
    : "Plazo del proveedor (días hábiles desde la emisión de la OC)";
  if (sem.estado === "sin_plazo") {
    return {
      label: `Día ${sem.dias_habiles} · sin plazo`,
      ...SEMAFORO_GRIS,
      title: `${base} — la OC no tiene plazo definido`,
    };
  }
  const c = SEMAFORO_COLORES[sem.estado] ?? SEMAFORO_GRIS;
  const rest = sem.dias_habiles_restantes;
  const cola = rest == null
    ? ""
    : rest >= 0
      ? ` — quedan ${rest} día(s) hábil(es)`
      : ` — vencida hace ${Math.abs(rest)} día(s) hábil(es)`;
  return {
    label: `Día ${sem.dias_habiles} de ${sem.plazo_dias ?? "?"}`,
    color: c.color,
    bg: c.bg,
    title: `${base}${cola}`,
  };
}

/** «preparados 6 de 9» / «completa» a partir de la completitud del backend. */
export function completitudTexto(comp: MonzaCompletitud | null | undefined): string | null {
  if (!comp || !comp.total) return null;
  const preparados = comp.por_estado?.preparado ?? 0;
  if (preparados >= comp.total) return "completa";
  return `preparados ${preparados} de ${comp.total}`;
}

// ── Totales de la selección (el frontend SOLO suma lo que el backend mandó) ───

export interface MonzaTotalesSeleccion {
  /** Líneas seleccionadas. */
  items: number;
  /** Σ cantidad de las líneas seleccionadas. */
  unidades: number;
  /** Σ peso_total_kg de los ítems que lo traen. */
  kg: number;
  /** Ítems seleccionados SIN peso (peso_total_kg null) — se avisa, no se inventa 0. */
  sinPeso: number;
  /** Σ fob_total POR MONEDA (solo ítems con fob_total no-null). */
  fobPorMoneda: Record<string, number>;
  /** Ítems seleccionados con costo/fob_total null («FOB incompleto»). */
  fobIncompleto: number;
  /** Monedas presentes en la selección, en orden de aparición. */
  monedas: string[];
  /** true si hay más de una moneda («mixto EUR/USD»). */
  mixto: boolean;
}

export function sumarSeleccion(items: readonly MonzaItemAgrupable[]): MonzaTotalesSeleccion {
  let unidades = 0;
  let kg = 0;
  let sinPeso = 0;
  let fobIncompleto = 0;
  const fobPorMoneda: Record<string, number> = {};
  const monedas: string[] = [];
  for (const it of items) {
    unidades += it.cantidad || 0;
    if (it.peso_total_kg == null) sinPeso += 1;
    else kg += it.peso_total_kg;
    if (it.fob_total == null) fobIncompleto += 1;
    else {
      const mon = it.moneda || "?";
      if (fobPorMoneda[mon] === undefined) monedas.push(mon);
      fobPorMoneda[mon] = (fobPorMoneda[mon] ?? 0) + it.fob_total;
    }
  }
  return {
    items: items.length,
    unidades,
    kg,
    sinPeso,
    fobPorMoneda,
    fobIncompleto,
    monedas,
    mixto: monedas.length > 1,
  };
}

// ── Badge aduanero REFERENCIAL ────────────────────────────────────────────────

/** Umbrales referenciales en USD FOB para el canal aduanero. */
export const UMBRAL_COURIER_USD = 1000;
export const UMBRAL_AGENCIA_USD = 3000;

/** Texto fijo que acompaña SIEMPRE al badge (y a la barra de totales). */
export const TEXTO_ADUANA_REFERENCIAL =
  "Referencial. Fraccionar embarques para calzar el umbral es sanción aduanera.";

export interface MonzaBadgeAduanero {
  /** FOB de la selección convertido a USD (redondeado). */
  usd: number;
  nivel: "courier" | "revisar" | "agencia";
  label: string;
  color: string;
  bg: string;
}

const BADGE_NIVELES: Record<MonzaBadgeAduanero["nivel"], { texto: string; color: string; bg: string }> = {
  courier: { texto: "courier simplificado", color: "#15803D", bg: "#DCFCE7" },
  revisar: { texto: "revisar canal",        color: "#B45309", bg: "#FEF3C7" },
  agencia: { texto: "requiere agencia",     color: "#6D28D9", bg: "#EDE9FE" },
};

/**
 * Convierte el FOB de la selección a USD con los TCs de GET /config y clasifica
 * el canal aduanero. Fail-closed en TODO lo dudoso — el badge NO se muestra
 * (devuelve null) cuando:
 *  - falta un TC necesario o viene ≤ 0 (jamás dividir por un TC inválido);
 *  - hay una moneda desconocida (sin regla de conversión no se inventa);
 *  - la selección tiene FOB incompleto (clasificar con plata parcial subestima
 *    el total y podría decir «courier» donde en verdad «requiere agencia»);
 *  - no hay ningún FOB que sumar.
 */
export function badgeAduanero(
  tot: MonzaTotalesSeleccion,
  tcUsdClp?: number | null,
  tcEurClp?: number | null,
): MonzaBadgeAduanero | null {
  if (tot.fobIncompleto > 0) return null;
  const entradas = Object.entries(tot.fobPorMoneda);
  if (entradas.length === 0) return null;
  const tcUsd = Number(tcUsdClp);
  if (!(tcUsd > 0)) return null;
  let usd = 0;
  for (const [mon, monto] of entradas) {
    if (mon === "USD") usd += monto;
    else if (mon === "EUR") {
      const tcEur = Number(tcEurClp);
      if (!(tcEur > 0)) return null;
      usd += (monto * tcEur) / tcUsd;
    } else if (mon === "CLP") usd += monto / tcUsd;
    else return null; // moneda sin regla de conversión: sin badge
  }
  const nivel: MonzaBadgeAduanero["nivel"] =
    usd < UMBRAL_COURIER_USD ? "courier" : usd <= UMBRAL_AGENCIA_USD ? "revisar" : "agencia";
  const n = BADGE_NIVELES[nivel];
  const redondeado = Math.round(usd);
  return {
    usd: redondeado,
    nivel,
    label: `≈ US$ ${redondeado.toLocaleString("es-CL")} · ${n.texto}`,
    color: n.color,
    bg: n.bg,
  };
}
