"""Módulo Wasabil DTE de MonzaParts — emisión de documentos tributarios electrónicos vía Wasabil.

Espejo aislado del módulo batalla-probado wasabil_dte/ de Grupo AM (patrón de la
casa: cero imports cruzados monza_* ↔ GA). Aislado y aditivo: no modifica tablas
ni código existente; solo lee despachos/cotización Monza y escribe el folio en
MonzaDespacho.numero_guia cuando el SII acepta el documento. Usa la cuenta
Wasabil propia de MonzaParts (WASABIL_API_TOKEN_MONZA). Ver README.md.
"""
