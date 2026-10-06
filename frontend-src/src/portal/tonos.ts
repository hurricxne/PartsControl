// Colores de TEXTO con significado (bien / atención / mal) de la pantalla interna
// Cuentas del portal, en pares por tema.
//
// POR QUÉ NO los -500/-400 sueltos que usa el resto del ERP: sobre la superficie blanca
// del tema claro quedan cerca de 2:1 en texto chico (amber-500 2.15, emerald-500 2.54,
// red-400 2.77), y acá ese texto DECIDE una acción: el estado de la cuenta, el motivo de
// un rechazo. Los -700 pasan 4.5:1 en claro, también sobre el fondo teñido de la
// insignia; los -400 pasan de sobra en oscuro.
//
// Van escritos completos (sin armar el nombre por partes) porque Tailwind solo genera
// las clases que encuentra literales en el código.
export const TONO_OK = 'text-emerald-700 dark:text-emerald-400'
export const TONO_ATENCION = 'text-amber-700 dark:text-amber-400'
export const TONO_ERROR = 'text-red-700 dark:text-red-400'
