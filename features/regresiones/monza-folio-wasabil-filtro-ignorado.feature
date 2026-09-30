# language: es
# Regresión de producción (2026-09-30, pase 0e77e7f): Wasabil IGNORA el filtro `folio` de
# POST /documents/query aunque su documentación lo describe; sí respeta la búsqueda
# `search: "folio:N"`. Consultando con `folio`, la factura 33 N° 223 devolvió las últimas
# 10 facturas y la verificación respondió «no se pudo consultar Wasabil» para todo.
Característica: La verificación del folio encuentra el documento en Wasabil
  Como usuario de Contabilidad de MonzaParts
  Quiero que el folio que tecleo se busque de verdad en Wasabil
  Para poder registrar documentos emitidos en Wasabil

  Escenario: Una factura emitida en Wasabil se encuentra por su folio
    Dado que en Wasabil hay varias facturas emitidas, entre ellas la N° 223
    Cuando el usuario verifica la factura N° 223 indicando que se emitió en Wasabil
    Entonces el sistema la encuentra y la verifica
    Y no responde que no pudo consultar Wasabil
