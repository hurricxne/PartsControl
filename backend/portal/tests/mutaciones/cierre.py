"""Mutaciones del Cierre de Venta y las fechas: test_fecha_promesa.py,
test_cierre_espejo_frontend.py y test_clientes_espejo_frontend.py (routers/tests/).

Los espejos leen la pantalla en frontend-src, junto a backend/. El arnés copia también
frontend-src (src/ copiado, node_modules enlazado) junto a su copia de backend/, así que
los espejos no se saltan y leen esa copia.

Las mutaciones que viven en la PANTALLA (CierreVentaPage.tsx, ClientesPage.tsx) pueden
entrar en esta lista nombrando el archivo desde la raíz del repo (`frontend-src/src/…`,
como hace frontend.py); mientras no entren, se verifican a mano sobre una COPIA de la
pantalla (una carpeta temporal con src/pages/… y node_modules enlazado), apuntando
PARTSCONTROL_FRONTEND_SRC a esa copia; el árbol de trabajo no se toca:
  test_cierre_espejo_frontend.py
    · reponer Jueves Santo en isHoliday                               → m1 rojo (2026-04-02)
    · quitar [6,20] de la tabla de la pantalla                        → m1 rojo (2026-06-20)
    · cambiar la regla de fin de semana (`dow !== 0 && dow !== 6`)   → m2 rojo
    · plazoDeLaOrden vuelve a ignorar los ítems sin plazo             → m3 rojo
  test_clientes_espejo_frontend.py
    · onError vuelve a `() => toast.error('Error al eliminar')`       → rojo
"""

MUTACIONES = {
    'routers/tests/test_fecha_promesa.py': [
        ('el reloj de los días restantes vuelve al del servidor (UTC)',  # → §G
         'routers/compras.py',
         '    today = hoy_chile()\n',
         '    today = date.today()\n'),
        ('_item_deadline calcula con el plazo sin filtrar',  # → e4, e5
         'routers/despachos.py',
         '    plazo = plazo_creible(item.plazo_entrega_max)\n',
         '    plazo = item.plazo_entrega_max\n'),
        ('_item_to_dict calcula con el plazo sin filtrar',  # → h1
         'routers/compras.py',
         '    plazo_calculable = plazo_creible(plazo_max)\n',
         '    plazo_calculable = plazo_max\n'),
        ('plazo_creible rechaza todo',  # → h2, §I
         'routers/compras.py',
         '    return dias if 0 < dias <= MAX_PLAZO_DIAS_PROMESA else None\n',
         '    return None\n'),
        ('plazo_creible sin cota superior',  # → §I (3651), a3
         'routers/compras.py',
         '    return dias if 0 < dias <= MAX_PLAZO_DIAS_PROMESA else None\n',
         '    return dias if 0 < dias else None\n'),
        ('plazo_creible sin cota inferior',  # → §I (-5 y 0)
         'routers/compras.py',
         '    return dias if 0 < dias <= MAX_PLAZO_DIAS_PROMESA else None\n',
         '    return dias if dias <= MAX_PLAZO_DIAS_PROMESA else None\n'),
        ('el congelado solo con el plazo propio del ítem',  # → a2, a2b
         'routers/compras.py',
         '        plazo_it = plazo_prometido(it_cot.plazo_entrega_max, plazo_por_defecto)\n',
         '        plazo_it = plazo_creible(it_cot.plazo_entrega_max)\n'),
        ('el plazo mínimo como respaldo del máximo al congelar',  # → a2b
         'routers/compras.py',
         '        plazo_it = plazo_prometido(it_cot.plazo_entrega_max, plazo_por_defecto)\n',
         '        plazo_it = plazo_prometido(it_cot.plazo_entrega_max or it_cot.plazo_entrega_min, plazo_por_defecto)\n'),
        ('la congelada deja de mandar en _item_deadline',  # → e1
         'routers/despachos.py',
         '    if item.fecha_promesa_cliente:\n        return item.fecha_promesa_cliente\n',
         ''),
        ('_item_deadline sin el respaldo oc.fecha_entrega',  # → e3, e4, e5
         'routers/despachos.py',
         '    if oc.fecha_entrega:\n        return oc.fecha_entrega\n    return None\n',
         '    return None\n'),
        ('el clon del split nace sin la fecha congelada',  # → d1
         'routers/compras.py',
         '        fecha_promesa_cliente = item.fecha_promesa_cliente,\n',
         '        fecha_promesa_cliente = None,\n'),
        ('el respaldo del plazo por defecto es el 15 de _item_to_dict',  # → §I: (None, None) y paridad
         'routers/compras.py',
         'PLAZO_MAX_SIN_CONFIG = 45\n',
         'PLAZO_MAX_SIN_CONFIG = 15\n'),
        ('la cotización formal cambia su respaldo sin avisar',  # → §I paridad
         'routers/cotizador.py',
         '        "plazo_max_default": config.plazo_max_default or 45,\n',
         '        "plazo_max_default": config.plazo_max_default or 15,\n'),
        ('un plazo propio que no sirve cae al plazo por defecto',  # → §I (3651, 60)
         'routers/compras.py',
         '    return plazo_creible(plazo_item or plazo_por_defecto or PLAZO_MAX_SIN_CONFIG)\n',
         '    return plazo_creible(plazo_item) or plazo_creible(plazo_por_defecto or PLAZO_MAX_SIN_CONFIG)\n'),
        ('el cierre lee la cotización sin lock',  # → f1 (y f2, f3): dos OC
         'routers/compras.py',
         '        .filter(Cotizacion.id == body.cotizacion_id)\n        .populate_existing()\n        .with_for_update()\n',
         '        .filter(Cotizacion.id == body.cotizacion_id)\n        .populate_existing()\n'),
        ('hoy_chile vuelve a importarse dentro del cierre',  # → f0: el punto de encuentro no se pisa
         'routers/compras.py',
         '    ancla_cierre = hoy_chile()\n',
         '    from wasabil_dte.service import hoy_chile as _hoy_local\n    ancla_cierre = _hoy_local()\n'),
        ('un re-sellado activo en el camino idempotente',  # → b1
         'routers/compras.py',
         '    if oc_existente:\n        return {"id": oc_existente.id, "cotizacion_id": oc_existente.cotizacion_id}\n',
         '    if oc_existente:\n'
         '        for it_cot in db.query(ItemCotizacion).filter(\n'
         '            ItemCotizacion.cotizacion_id == body.cotizacion_id,\n'
         '        ).all():\n'
         '            it_cot.fecha_promesa_cliente = add_business_days(hoy_chile(), 10)\n'
         '        db.commit()\n'
         '        return {"id": oc_existente.id, "cotizacion_id": oc_existente.cotizacion_id}\n'),
    ],
    'routers/tests/test_cierre_espejo_frontend.py': [
        ('Jueves Santo entra al calendario del backend',  # → m0 (30 hábiles → 2027-03-29), m1
         'routers/compras.py',
         '    good_friday = easter - timedelta(days=2)\n    return d == good_friday\n',
         '    good_friday = easter - timedelta(days=2)\n    return d == good_friday or d == easter - timedelta(days=3)\n'),
        ('Viernes Santo sale del calendario del backend',  # → m0 (31 hábiles → 2027-03-26), m1
         'routers/compras.py',
         '    good_friday = easter - timedelta(days=2)\n    return d == good_friday\n',
         '    good_friday = easter - timedelta(days=2)\n    return False\n'),
        ('el 20 de junio sale del calendario del backend',  # → m0 (→ 2028-06-20), m1
         'routers/compras.py',
         '        (6, 20),  # Día de los Pueblos Indígenas (solsticio: 20 o 21 de junio; acá fijo el 20)\n',
         ''),
        ('un feriado fijo sale solo del backend',  # → m1 (necesita la pantalla a la vista)
         'routers/compras.py',
         '        (10, 31), # Día de las Iglesias Evangélicas\n',
         ''),
        ('un feriado nuevo entra solo al backend',  # → m1 (necesita la pantalla a la vista)
         'routers/compras.py',
         '        (12, 25), # Navidad\n',
         '        (12, 25), # Navidad\n        (12, 31),\n'),
        ('el plazo por defecto manda sobre el plazo propio',  # → m3: ([10, 60, undefined], 45) da 45 y no 60
         'routers/compras.py',
         '    return plazo_creible(plazo_item or plazo_por_defecto or PLAZO_MAX_SIN_CONFIG)\n',
         '    return plazo_creible(plazo_por_defecto or plazo_item or PLAZO_MAX_SIN_CONFIG)\n'),
    ],
    'routers/tests/test_clientes_espejo_frontend.py': [
        ('eliminar_cliente deja pasar el IntegrityError (vuelve el 500 sin detail)',
         'routers/clientes.py',
         '    except IntegrityError:\n',
         '    except ValueError:\n'),
        ('el motivo del 409 deja de ser texto',
         'routers/clientes.py',
         "            'No se puede eliminar: el cliente tiene cuentas del Portal de Clientes. '\n"
         "            'Elimínalas primero en Admin → Cuentas del portal.',\n",
         "            {'mensaje': 'No se puede eliminar: el cliente tiene cuentas del Portal de Clientes.'},\n"),
    ],
}
