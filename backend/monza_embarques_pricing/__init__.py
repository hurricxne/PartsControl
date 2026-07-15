"""Módulo Embarques Pricing MonzaParts — costo landed por ítem de un embarque.

Aislado y SOLO para MonzaParts (empresa 'automotriz'). Es el espejo del módulo de
Grupo AM (backend/embarques_pricing/) con la MISMA metodología de costeo landed
(shipping prorrateado por peso, gastos locales prorrateados por CIF), pero apuntando
a las tablas monza_*:
  - Embarque/ítems de Logística: MonzaEmbarque + MonzaEmbarqueItem (→ MonzaCotizacionItem).
  - FOB y peso por ítem: del ítem de cotización (costo / peso_kg) como DEFAULT; editables
    a mano (el dueño sube FOB, pesos y gastos manualmente).
  - TC y datos de empresa: MonzaConfig.

Overlay NO invasivo: lee los embarques que crea Logística y superpone su pricing; el
registro de pricing se crea diferido la primera vez que Contabilidad abre el embarque.

Tablas nuevas (aditivas): monza_emb_pricing / _gasto / _item.

Activación: main.py importa `monza_embarques_pricing.router` y lo monta sin prefix
(el router ya trae prefix=/api/monza/embarques-pricing). Protegido por
require_empresa("automotriz").
"""
