// Constantes del flujo de adelanto (ej. 50% personas naturales), compartidas entre el
// cierre de venta (Comercial), Contabilidad y Abastecimiento. Tener un único lugar evita
// "números mágicos" (|| 50) y etiquetas duplicadas.

export const ADELANTO_PCT_DEFECTO = 50;

/** Id de la opción de adelanto con % LIBRE (1-100) o monto exacto en pesos.
 *
 *  POR QUÉ existe (hallazgo A2 de la paridad contable Monza ↔ MachParts): el cierre de
 *  venta de Monza solo ofrecía 0% o exactamente 50%, así que un adelanto pactado de 30%
 *  o de "$2.000.000 y el resto contra entrega" no se podía informar — quedaba en un
 *  correo y Tesorería aprobaba a ciegas. Grupo AM ofrece % libre 1-100 **o** monto
 *  exacto en CLP con IVA, con espejo bidireccional (CierreVentaPage.tsx).
 *
 *  Se identifica por id (no por índice ni por label) porque el % de esta opción NO vive
 *  en la constante: lo digita el usuario. */
export const PAGO_ADELANTO_LIBRE_ID = "adelanto_libre";

export interface PagoOpcion {
  id: string;
  label: string;
  pct: number;   // % de adelanto que dispara la verificación de pago (0 = sin adelanto)
  forma: string; // texto guardado en cotizacion.forma_pago
  /** true = el % NO está fijo en la opción: lo digita el usuario (o se deriva de un
   *  monto exacto en pesos). Solo la opción de adelanto libre lo lleva; en ella `pct`
   *  y `forma` son placeholders y se calculan con `formaPagoAdelanto(pct)`. */
  pctLibre?: boolean;
}

export const PAGO_OPCIONES: PagoOpcion[] = [
  // CONTADO = adelanto del 100% (arreglos del equipo 2026-08-21): «cuando un cliente
  // paga al contado, no se pide verificación de Tesorería — debería pedirse». Con pct
  // 100 la maquinaria EXISTENTE del adelanto se activa sola: la venta entra a la cola
  // de Tesorería por el total, y el cortafuego de Abastecimiento frena la OC de
  // proveedor hasta que el pago se verifique. Antes pct era 0 y contado compraba sin
  // confirmar que la plata llegó. Ventas viejas (forma "Contado", pct 0): ver el caso
  // legado de opcionDeVenta en MonzaCotizacionesPage — el re-cierre sube a 100 CON
  // aviso en pantalla (freeze-forward deliberado, no silencioso).
  { id: "contado", label: "Contado", pct: 100, forma: "Contado" },
  { id: "adelanto50", label: "50% adelanto (personas naturales)", pct: ADELANTO_PCT_DEFECTO, forma: "50% adelanto" },
  { id: PAGO_ADELANTO_LIBRE_ID, label: "Otro adelanto (% libre o monto exacto)", pct: 0, forma: "adelanto", pctLibre: true },
  { id: "credito", label: "Crédito (30 días contra factura)", pct: 0, forma: "30 días contra factura" },
  // Hallazgo A4: sin estas dos opciones una venta pactada a 60 o 90 días quedaba
  // grabada como 30 (la única alternativa de crédito), y con ella el vencimiento, la
  // cobranza y la antigüedad de cartera nacían equivocadas. Grupo AM sí las ofrece
  // (CierreVentaPage.tsx). El texto lleva el número pegado a "días" a propósito:
  // así `plazoDeCondPago` de MonzaFacturasPage precarga 60/90 en la factura.
  { id: "credito60", label: "Crédito (60 días contra factura)", pct: 0, forma: "60 días contra factura" },
  { id: "credito90", label: "Crédito (90 días contra factura)", pct: 0, forma: "90 días contra factura" },
];

/** Texto de `cotizacion.forma_pago` para un adelanto de `pct`%.
 *
 *  Mismo formato que la opción fija de 50% ("50% adelanto"), para que las dos vías
 *  produzcan el MISMO tipo de dato: `plazoDeCondPago` (MonzaFacturasPage) no encuentra
 *  "días" en este texto y cae en su default, igual que con "50% adelanto". */
export const formaPagoAdelanto = (pct: number): string => `${Math.round(pct)}% adelanto`;
