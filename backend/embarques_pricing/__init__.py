"""Módulo Embarques Pricing — Costo Landed por ítem (Contabilidad).

Carpeta AISLADA y fácil de revertir. Calcula el costo landed (CIF + gastos
locales) por ítem de cada embarque creado por Logística, replicando la lógica
del Sheet de GRUPO AM (creadores de embarque).

Integración con el resto de la app (solo 1 punto):
  · backend/main.py importa `embarques_pricing.router` y lo registra.

No modifica ninguna tabla existente: agrega 3 tablas nuevas (emb_pricing,
emb_pricing_gasto, emb_pricing_item) que se crean solas con create_all().

Ver README.md para el detalle de cómo deshacer.
"""
