"""Mutaciones de las dos suites de fotos de recepción (routers/bodega.py).

· test_bodega_fotos_candado.py — CUÁNDO se puede subir o borrar: recepción cerrada,
  última foto de un ítem dañado, candado de la recepción, archivo y fila juntos.
· test_bodega_fotos_auth.py — QUIÉN puede: sesión, marca, confinamiento y pertenencia.
  Cada entrada es una sonda del encabezado de la suite, escrita como texto ejecutable.

Las mutaciones que viven en el frontend (components/FotoBodega.tsx,
components/FotosItemModal.tsx, utils/format.ts, probadas por
routers/tests/test_fotos_bodega_espejo_frontend.py) todavía no entran acá; pueden
entrar nombrando el archivo desde la raíz del repo (`frontend-src/src/…`, como hace
frontend.py). Mientras tanto se verifican a mano sobre una copia temporal de frontend-src.
"""

_HELPER_FIN = (
    '    if not rec or rec.estado == "cerrada":\n'
    '        raise HTTPException(400, "Recepción no encontrada o ya cerrada")\n'
    '    return rec\n'
)

_BLOQUE_DANADO = (
    '    if "danado" in (ri.estado_recepcion or ""):\n'
    '        otras = db.query(RecepcionItemFoto).filter(\n'
    '            RecepcionItemFoto.recepcion_item_id == ri.id,\n'
    '            RecepcionItemFoto.id != foto.id,\n'
    '        ).count()\n'
    '        if otras == 0:\n'
    '            raise HTTPException(\n'
    '                409,\n'
    '                "Un ítem guardado como dañado necesita al menos una foto: sube la de "\n'
    '                "reemplazo antes de borrar esta, o cambia su estado y guarda.",\n'
    '            )\n'
    '\n'
)

_SERVE_FOTO_FIRMA = (
    '@router.get("/fotos/{foto_id}", dependencies=[Depends(require_empresa("mineria"))])\n'
    'def serve_foto(\n'
    '    foto_id: int,\n'
    '    db: Session = Depends(get_db),\n'
    '    current_user: User = Depends(get_current_user),\n'
    '):\n'
)

MUTACIONES = {
    'routers/tests/test_bodega_fotos_candado.py': [
        # _paso_0 (contrapesos)
        ('el helper acepta solo recepciones CERRADAS',
         'routers/bodega.py',
         _HELPER_FIN,
         _HELPER_FIN.replace('rec.estado == "cerrada"', 'rec.estado != "cerrada"')),
        ('el 409 del dañado sale para cualquier ítem',
         'routers/bodega.py',
         '    if "danado" in (ri.estado_recepcion or ""):\n',
         '    if True:\n'),
        # _paso_1
        ('el helper no mira si la recepción está cerrada',
         'routers/bodega.py',
         _HELPER_FIN,
         _HELPER_FIN.replace('if not rec or rec.estado == "cerrada":', 'if not rec:')),
        ('upload_foto lee la recepción plana, sin candado ni estado',
         'routers/bodega.py',
         '    rec = _recepcion_abierta_con_lock(db, recepcion_id)\n    ri = _resolver_ri(',
         '    rec = db.query(RecepcionEmbarque).filter(RecepcionEmbarque.id == recepcion_id).first()\n'
         '    ri = _resolver_ri('),
        ('delete_foto no pasa por el helper',
         'routers/bodega.py',
         '    _recepcion_abierta_con_lock(db, recepcion_id)\n\n    # PERTENENCIA',
         '    # PERTENENCIA'),
        # _paso_2
        ('el dañado puede quedar sin foto (bloque quitado)',
         'routers/bodega.py',
         _BLOQUE_DANADO,
         ''),
        ('la cuenta incluye la foto que se está borrando',
         'routers/bodega.py',
         '            RecepcionItemFoto.recepcion_item_id == ri.id,\n'
         '            RecepcionItemFoto.id != foto.id,\n'
         '        ).count()',
         '            RecepcionItemFoto.recepcion_item_id == ri.id,\n'
         '        ).count()'),
        # _paso_3
        ('el helper lee sin FOR UPDATE',
         'routers/bodega.py',
         '        .populate_existing()\n        .with_for_update()\n        .first()\n    )\n' + _HELPER_FIN,
         '        .populate_existing()\n        .first()\n    )\n' + _HELPER_FIN),
        ('el candado se toma DESPUÉS de resolver el ítem',
         'routers/bodega.py',
         '    rec = _recepcion_abierta_con_lock(db, recepcion_id)\n'
         '    ri = _resolver_ri(db, recepcion_id, ri_id, embarque_item_id)\n',
         '    ri = _resolver_ri(db, recepcion_id, ri_id, embarque_item_id)\n'
         '    rec = _recepcion_abierta_con_lock(db, recepcion_id)\n'),
        ('upload_foto vuelve a async def (la espera detiene el servidor)',
         'routers/bodega.py',
         '\ndef upload_foto(',
         '\nasync def upload_foto('),
        # _paso_4
        ('el archivo a medias queda en disco',
         'routers/bodega.py',
         '    except OSError as exc:\n        _descartar_archivo(fpath)\n        print(f"[bodega] no se pudo escribir',
         '    except OSError as exc:\n        print(f"[bodega] no se pudo escribir'),
        ('la escritura fallida responde sin mensaje para el operador',
         'routers/bodega.py',
         '        raise HTTPException(500, "No se pudo guardar la foto en el servidor. "\n'
         '                                 "Intenta de nuevo; si se repite, avisa a soporte.")\n',
         '        raise\n'),
        ('el commit fallido deja la foto completa en disco',
         'routers/bodega.py',
         '    db.add(foto)\n    try:\n        db.commit()\n    except Exception:\n'
         '        db.rollback()\n        _descartar_archivo(fpath)\n        raise\n    db.refresh(foto)\n',
         '    db.add(foto)\n    db.commit()\n    db.refresh(foto)\n'),
    ],
    'routers/tests/test_bodega_fotos_auth.py': [
        # _paso_1 — las DOS capas de serve_foto: cualquiera de las dos sola ya corta al anónimo.
        ('serve_foto sin sesión (ni require_empresa ni get_current_user)',
         'routers/bodega.py',
         _SERVE_FOTO_FIRMA,
         '@router.get("/fotos/{foto_id}")\n'
         'def serve_foto(\n'
         '    foto_id: int,\n'
         '    db: Session = Depends(get_db),\n'
         '):\n'),
        # _paso_2
        ('un id inexistente responde 403 (confirma que existe)',
         'routers/bodega.py',
         '    foto = db.query(RecepcionItemFoto).filter(RecepcionItemFoto.id == foto_id).first()\n'
         '    if not foto:\n'
         '        raise HTTPException(404, "Foto no encontrada")\n'
         '\n'
         '    # La foto tiene que colgar',
         '    foto = db.query(RecepcionItemFoto).filter(RecepcionItemFoto.id == foto_id).first()\n'
         '    if not foto:\n'
         '        raise HTTPException(403, "Foto no encontrada")\n'
         '\n'
         '    # La foto tiene que colgar'),
        # _paso_4
        ('la ruta confinada no comprueba el prefijo',
         'routers/bodega.py',
         '    if not full.startswith(_FOTOS_DIR + os.sep):\n        raise HTTPException(400, "Ruta inválida")\n',
         ''),
        # _paso_5 — las tres rutas, por separado
        ('upload_foto sin candado de marca',
         'routers/bodega.py',
         '@router.post("/recepciones/{recepcion_id}/items/{ri_id}/foto", status_code=201,\n'
         '             dependencies=[Depends(require_empresa("mineria"))])\n',
         '@router.post("/recepciones/{recepcion_id}/items/{ri_id}/foto", status_code=201)\n'),
        ('delete_foto sin candado de marca',
         'routers/bodega.py',
         '@router.delete("/recepciones/{recepcion_id}/items/{ri_id}/foto/{foto_id}",\n'
         '               dependencies=[Depends(require_empresa("mineria"))])\n',
         '@router.delete("/recepciones/{recepcion_id}/items/{ri_id}/foto/{foto_id}")\n'),
        ('serve_foto sin candado de marca',
         'routers/bodega.py',
         '@router.get("/fotos/{foto_id}", dependencies=[Depends(require_empresa("mineria"))])\n',
         '@router.get("/fotos/{foto_id}")\n'),
        # _paso_6a
        ('delete_foto no exige que el ítem de la foto sea de la recepción de la URL',
         'routers/bodega.py',
         '        RecepcionEmbarqueItem.id == foto.recepcion_item_id,\n'
         '        RecepcionEmbarqueItem.recepcion_id == recepcion_id,\n',
         '        RecepcionEmbarqueItem.id == foto.recepcion_item_id,\n'),
        # _paso_6b
        ('delete_foto no exige que el ri_id designe al ítem de la foto',
         'routers/bodega.py',
         '    if ri_id not in (ri.id, ri.embarque_item_id):\n'
         '        raise HTTPException(404, "Foto no encontrada")\n',
         ''),
        # _paso_6d — las tres búsquedas de _resolver_ri y la pertenencia de upload_foto
        ('_resolver_ri encuentra por id un ítem de OTRA recepción',
         'routers/bodega.py',
         '        RecepcionEmbarqueItem.id == ri_id,\n'
         '        RecepcionEmbarqueItem.recepcion_id == recepcion_id,\n',
         '        RecepcionEmbarqueItem.id == ri_id,\n'),
        ('_resolver_ri encuentra por embarque_item_id explícito un ítem de OTRA recepción',
         'routers/bodega.py',
         '            RecepcionEmbarqueItem.embarque_item_id == embarque_item_id,\n'
         '            RecepcionEmbarqueItem.recepcion_id == recepcion_id,\n',
         '            RecepcionEmbarqueItem.embarque_item_id == embarque_item_id,\n'),
        ('_resolver_ri encuentra por embarque_item_id de la URL un ítem de OTRA recepción',
         'routers/bodega.py',
         '        RecepcionEmbarqueItem.embarque_item_id == ri_id,\n'
         '        RecepcionEmbarqueItem.recepcion_id == recepcion_id,\n',
         '        RecepcionEmbarqueItem.embarque_item_id == ri_id,\n'),
        ('upload_foto no exige que el ítem sea del embarque de la recepción',
         'routers/bodega.py',
         '            EmbarqueItem.id == emb_item_id,\n'
         '            EmbarqueItem.embarque_id == rec.embarque_id,\n',
         '            EmbarqueItem.id == emb_item_id,\n'),
        # _paso_7
        ('delete_foto solo acepta el id del ítem de recepción',
         'routers/bodega.py',
         '    if ri_id not in (ri.id, ri.embarque_item_id):\n',
         '    if ri_id != ri.id:\n'),
        # _paso_8
        ('delete_foto arma la ruta relativa al directorio de trabajo',
         'routers/bodega.py',
         '        fpath = _ruta_confinada_foto(foto.url_foto)\n        if os.path.isfile(fpath):',
         '        fpath = foto.url_foto.lstrip("/")\n        if os.path.isfile(fpath):'),
        # _paso_9
        ('_resolver_ri ignora el embarque_item_id explícito',
         'routers/bodega.py',
         '    if embarque_item_id is not None:\n        ri = db.query(',
         '    if False:\n        ri = db.query('),
        ('upload_foto inserta sin validar la pertenencia (500 por llave foránea)',
         'routers/bodega.py',
         '        if not ei:\n            raise HTTPException(404, "El ítem no pertenece a esta recepción")\n',
         ''),
        # _paso_10
        ('el historial no devuelve recepcion_id',
         'routers/bodega.py',
         '            "recepcion_id": recepciones.get(e.id),\n',
         ''),
        # _paso_11
        ('fotos_de_item sin candado de marca',
         'routers/bodega.py',
         '@router.get("/items/{item_cotizacion_id}/fotos",\n'
         '            dependencies=[Depends(require_empresa("mineria"))])\n',
         '@router.get("/items/{item_cotizacion_id}/fotos")\n'),
        # _paso_12
        ('el nombre del archivo sale del reloj y no del uuid4',
         'routers/bodega.py',
         '    fname = f"recepcion_{ri.id}_{uuid.uuid4().hex}{ext}"\n',
         '    fname = f"recepcion_{ri.id}_{int(datetime.now().timestamp())}{ext}"\n'),
    ],
}
