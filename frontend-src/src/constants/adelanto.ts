// Constantes del flujo de adelanto (ej. 50% personas naturales), compartidas entre el
// cierre de venta (Comercial), Contabilidad y Abastecimiento. Tener un único lugar evita
// "números mágicos" (|| 50) y etiquetas duplicadas.

export const ADELANTO_PCT_DEFECTO = 50;

export interface PagoOpcion {
  id: string;
  label: string;
  pct: number;   // % de adelanto que dispara la verificación de pago (0 = sin adelanto)
  forma: string; // texto guardado en cotizacion.forma_pago
}

export const PAGO_OPCIONES: PagoOpcion[] = [
  { id: "contado", label: "Contado", pct: 0, forma: "Contado" },
  { id: "adelanto50", label: "50% adelanto (personas naturales)", pct: ADELANTO_PCT_DEFECTO, forma: "50% adelanto" },
  { id: "credito", label: "Crédito (30 días contra factura)", pct: 0, forma: "30 días contra factura" },
];
