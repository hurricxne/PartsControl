"""Mutaciones de las cuentas del portal y del vínculo venta ↔ cliente:
portal/tests/test_cuentas.py (bitácora, carreras del alta, forma del correo, plazo de la
clave temporal, cuenta sin registro de vigencia, 409 de eliminar, origen en el log del
limitador, candado del guard de RUT), portal/tests/test_vinculacion.py (helpers de RUT,
confirmación de la ficha con candado, dar acceso resincroniza, consulta de 5 columnas) y
portal/tests/test_aislamiento.py (503 de esquema en los routers, script de rollback).

Cada entrada rompe UNA regla y la suite que la vigila tiene que ponerse roja. Las
mutaciones de portal/router.py que ya lista login.py no se repiten acá."""

_PRIMER_FLUSH = (
    '    try:\n'
    '        db.flush()  # INSERT en users: hace falta usuario.id para el puente\n'
    '    except IntegrityError as exc:\n'
    '        # Dos altas del mismo correo a la vez: las dos pasan el chequeo de existencia y la\n'
    '        # segunda choca acá con el UNIQUE de users.email (el único de la tabla). El error\n'
    '        # real de la base queda en el log, para que el 409 no tape otra cosa.\n'
    '        logger.warning("portal: alta de %r rechazada por la base: %s", email, exc.orig)\n'
    '        raise CuentaError(\n'
    '            409, f"El correo {email} ya tiene una cuenta: alguien la creó recién. Actualiza la lista de cuentas.",\n'
    '        ) from exc\n'
)
_SEGUNDO_FLUSH = (
    '    try:\n'
    '        db.flush()\n'
    '    except IntegrityError as exc:\n'
    '        # La ficha existía al revisarla y ya no: alguien la eliminó mientras tanto (la FK\n'
    '        # de usuario_cliente). El motivo exacto queda en el log.\n'
    '        logger.warning("portal: alta de %r rechazada por la base: %s", email, exc.orig)\n'
    '        raise CuentaError(\n'
    '            409, "Los clientes elegidos cambiaron mientras se creaba la cuenta (¿se eliminó alguno?). "\n'
    '                 "Actualiza la lista y vuelve a intentar.",\n'
    '        ) from exc\n'
)
_COMMIT_DE_ELIMINAR = (
    '    try:\n'
    '        db.commit()\n'
    '    except IntegrityError as exc:\n'
    '        db.rollback()\n'
    '        # exc.orig nombra la tabla que todavía apunta a la cuenta (su bitácora de descargas).\n'
    '        logger.warning("portal-admin: usuario %s no pudo eliminar la cuenta portal %s (%r): %s",\n'
    '                       actor_id, usuario_id, correo, exc.orig)\n'
    '        raise HTTPException(\n'
    '            409, "La cuenta tiene historial asociado y no se puede borrar: desactívala.")\n'
)
_PREDICADO_ROLLBACK = '_FUERA_DEL_ERP = "COALESCE(tipo_acceso, \'\') <> \'interno\'"\n'
_SANEADO_TOLERANTE = (
    '    texto = (rut or "").translate(_A_GUION)\n'
    '    limpio = "".join(c for c in texto if not c.isspace() and unicodedata.category(c) != "Cf")\n'
    '    return limpio.replace(".", "").upper()\n'
)
_CONFIRMAR_EN_EL_GANCHO = (
    '        with session.no_autoflush:\n'
    '            destino = cliente_para_rut_confirmado(session, mapa, cot.rut_cliente, confirmadas)\n'
)

MUTACIONES = {
    'portal/tests/test_cuentas.py': [
        # ── La bitácora del módulo (→ §K) ────────────────────────────────────────────
        ('el logger del portal queda en el nivel heredado (los INFO se descartan)',
         'portal/__init__.py',
         '    registro.setLevel(logging.INFO)\n',
         ''),
        ('la clave temporal entra al mensaje de restablecer',
         'portal/router_admin.py',
         '    logger.info("portal-admin: usuario %s restableció la clave de la cuenta %s", current_user.id, usuario.id)\n',
         '    logger.info("portal-admin: usuario %s restableció la clave de la cuenta %s: %s", current_user.id, usuario.id, temporal)\n'),
        ('el mensaje del alta vuelve a solo ids (sin correo ni clientes)',
         'portal/router_admin.py',
         '    logger.info("portal-admin: usuario %s creó la cuenta portal %s (%r) para los clientes %s",\n'
         '                current_user.id, usuario.id, usuario.email, sorted(set(body.cliente_ids)))\n',
         '    logger.info("portal-admin: usuario %s creó la cuenta portal %s", current_user.id, usuario.id)\n'),
        ('el mensaje del borrado vuelve a solo ids (sin el correo)',
         'portal/router_admin.py',
         '    logger.info("portal-admin: usuario %s eliminó la cuenta portal %s (%r)", actor_id, usuario_id, correo)\n',
         '    logger.info("portal-admin: usuario %s eliminó la cuenta portal %s", actor_id, usuario_id)\n'),
        # ── Las carreras del alta (→ b7, b8) ─────────────────────────────────────────
        ('el UNIQUE del correo ya no se traduce en el primer flush (500)',
         'portal/service.py',
         _PRIMER_FLUSH,
         '    db.flush()  # INSERT en users: hace falta usuario.id para el puente\n'),
        ('la ficha eliminada a mitad del alta ya no se traduce en el segundo flush (500)',
         'portal/service.py',
         _SEGUNDO_FLUSH,
         '    db.flush()\n'),
        ('la ficha eliminada a mitad del alta se anuncia como «el correo ya tiene una cuenta»',
         'portal/service.py',
         '            409, "Los clientes elegidos cambiaron mientras se creaba la cuenta (¿se eliminó alguno?). "\n'
         '                 "Actualiza la lista y vuelve a intentar.",\n',
         '            409, f"El correo {email} ya tiene una cuenta: alguien la creó recién.",\n'),
        # ── La forma del correo (→ b9) ───────────────────────────────────────────────
        ('el alta acepta cualquier texto con «@»',
         'portal/service.py',
         '    if not _CORREO.fullmatch(email):\n',
         '    if not email or "@" not in email:\n'),
        ('match en vez de fullmatch (un punto final pasa)',
         'portal/service.py',
         '    if not _CORREO.fullmatch(email):\n',
         '    if not _CORREO.match(email):\n'),
        # ── El plazo de la clave temporal (→ a8, d2b2, d2b4, d2b5, d9b) ──────────────
        ('el listado no informa el plazo de la clave temporal',
         'portal/router_admin.py',
         '            "vigente_desde": acceso.vigente_desde.isoformat() if acceso and acceso.vigente_desde else None,\n'
         '            **_plazo_de_la_clave(acceso),\n',
         '            "vigente_desde": acceso.vigente_desde.isoformat() if acceso and acceso.vigente_desde else None,\n'),
        ('la clave temporal nunca aparece como vencida',
         'portal/router_admin.py',
         '        "clave_temporal_vencida": clave_temporal_vencida(acceso),\n',
         '        "clave_temporal_vencida": False,\n'),
        ('reactivar no informa el plazo de la clave',
         'portal/router_admin.py',
         '    return {"id": usuario.id, "estado": _estado(usuario, acceso), **_plazo_de_la_clave(acceso)}\n',
         '    return {"id": usuario.id, "estado": _estado(usuario, acceso)}\n'),
        ('reactivar revive la clave temporal (reinicia su plazo)',
         'portal/router_admin.py',
         '    acceso.vigente_desde = ahora  # el plazo de inactividad vuelve a correr\n',
         '    acceso.vigente_desde = ahora  # el plazo de inactividad vuelve a correr\n'
         '    acceso.clave_temporal_desde = ahora\n'),
        # ── Sin registro de vigencia (→ §L) ──────────────────────────────────────────
        ('reactivar recrea el registro con la clave como definitiva',
         'portal/router_admin.py',
         '        acceso = PortalAcceso(usuario_id=usuario.id, creado_por_id=current_user.id, clave_temporal_desde=ahora)\n',
         '        acceso = PortalAcceso(usuario_id=usuario.id, creado_por_id=current_user.id)\n'),
        # ── El 409 de eliminar (→ g3b, g3c) ──────────────────────────────────────────
        ('eliminar no deja en el log qué tabla bloqueó el borrado',
         'portal/router_admin.py',
         '        # exc.orig nombra la tabla que todavía apunta a la cuenta (su bitácora de descargas).\n'
         '        logger.warning("portal-admin: usuario %s no pudo eliminar la cuenta portal %s (%r): %s",\n'
         '                       actor_id, usuario_id, correo, exc.orig)\n',
         ''),
        ('el IntegrityError de eliminar sale sin traducir (500)',
         'portal/router_admin.py',
         _COMMIT_DE_ELIMINAR,
         '    db.commit()\n'),
        # ── El limitador y el cambio de clave (→ d6b2, d7b) ──────────────────────────
        ('los intentos del cambio de clave salen en el log como si fueran del login',
         'portal/router.py',
         '        rl.reservar_intento(llave, ip, origen="cambio-clave")\n',
         '        rl.reservar_intento(llave, ip)\n'),
        ('un cambio de clave correcto deja su marca en el contador',
         'portal/router.py',
         '    if settings.LOGIN_RATE_LIMIT_ENABLED:\n        rl.limpiar_exito(llave, ip)\n',
         ''),
        # ── El guard de RUT decide sin candado (→ g2b) ───────────────────────────────
        ('el guard de cambio de RUT mira las cuentas sin tomar la ficha',
         'routers/clientes.py',
         '    db.query(Cliente.id).filter(Cliente.id == ficha.id).with_for_update().first()\n',
         ''),
    ],
    'portal/tests/test_vinculacion.py': [
        # ── Los helpers de RUT (→ §A, d10) ───────────────────────────────────────────
        ('el módulo 11 reinicia el factor en 6 (generador y validador se equivocan juntos)',
         'portal/rut.py',
         '        factor = 2 if factor == 7 else factor + 1\n',
         '        factor = 2 if factor == 6 else factor + 1\n'),
        ('el saneado vuelve a quitar solo el punto y el espacio del teclado',
         'portal/rut.py',
         _SANEADO_TOLERANTE,
         '    return (rut or "").replace(".", "").replace(" ", "").strip().upper()\n'),
        # ── La ficha se confirma con candado (→ §J) ──────────────────────────────────
        ('la ficha del mapa se da por buena sin confirmarla',
         'portal/service.py',
         '    if destino not in confirmadas:\n'
         '        confirmadas[destino] = _rut_de_ficha_con_candado(db, destino)\n'
         '    return destino if confirmadas[destino] == rut_canonico(rut) else None\n',
         '    return destino\n'),
        ('el gancho vuelve al mapa sin confirmar (solo sus sondas se ponen rojas)',
         'portal/sincronia.py',
         _CONFIRMAR_EN_EL_GANCHO,
         '        with session.no_autoflush:\n'
         '            from .service import cliente_para_rut\n'
         '            destino = cliente_para_rut(mapa, cot.rut_cliente)\n'),
        ('el gancho confirma solo cuando la venta cambia de ficha (la ya vinculada conserva el vínculo viejo)',
         'portal/sincronia.py',
         _CONFIRMAR_EN_EL_GANCHO,
         '        with session.no_autoflush:\n'
         '            from .service import cliente_para_rut\n'
         '            destino = cliente_para_rut(mapa, cot.rut_cliente)\n'
         '            if destino != cot.cliente_id:\n'
         '                destino = cliente_para_rut_confirmado(session, mapa, cot.rut_cliente, confirmadas)\n'),
        ('el Cierre vuelve al mapa sin confirmar',
         'portal/service.py',
         '    destino = cliente_para_rut_confirmado(db, mapa, cot.rut_cliente, {})\n',
         '    destino = cliente_para_rut(mapa, cot.rut_cliente)\n'),
        ('el histórico ancla sin confirmar la ficha',
         'portal/service.py',
         '        if aplicar and destino is not None and destino != cot.cliente_id:\n'
         '            destino = cliente_para_rut_confirmado(db, mapa, cot.rut_cliente, confirmadas)\n',
         ''),
        ('la confirmación lee la ficha sin candado (no espera a quien la edita)',
         'portal/service.py',
         '            .with_for_update(read=True).first())\n',
         '            .first())\n'),
        # ── Dar acceso resincroniza (→ §K) ───────────────────────────────────────────
        ('crear la cuenta no pone al día el vínculo',
         'portal/service.py',
         '    vincular_historico(db, aplicar=True)\n    return usuario, temporal\n',
         '    return usuario, temporal\n'),
        # ── La consulta de 5 columnas (→ §G) ─────────────────────────────────────────
        ('la consulta del histórico omite una columna que las huérfanas usan',
         'portal/service.py',
         '    for cot in db.query(Cotizacion.id, Cotizacion.rut_cliente, Cotizacion.cliente_id,\n'
         '                        Cotizacion.numero, Cotizacion.cliente).all():\n',
         '    for cot in db.query(Cotizacion.id, Cotizacion.rut_cliente, Cotizacion.cliente_id,\n'
         '                        Cotizacion.cliente).all():\n'),
    ],
    'portal/tests/test_aislamiento.py': [
        # ── 503 solo por esquema, y en los cuatro routers (→ §S) ─────────────────────
        ('cualquier error de la base se anuncia como «portal no instalado»',
         'portal/deps.py',
         '                if not esquema_incompleto(error):\n                    raise\n',
         ''),
        ('el caso de esquema no queda en el log',
         'portal/deps.py',
         '                logger.exception("portal: falta una tabla o columna del módulo (%s)", request.url.path)\n',
         ''),
        ('el router de identidad pierde la clase de ruta (500 mudo)',
         'portal/router.py',
         '    route_class=RutaDelPortal,\n',
         ''),
        ('Cuentas del portal pierde la clase de ruta (500 mudo para el personal)',
         'portal/router_admin.py',
         '    route_class=RutaDelPortal,\n',
         ''),
        # ── El script de rollback (→ §R) ─────────────────────────────────────────────
        ('el rollback desactiva solo «las que dicen portal»',
         'portal/desactivar_cuentas.py',
         _PREDICADO_ROLLBACK,
         '_FUERA_DEL_ERP = "tipo_acceso = \'portal\'"\n'),
        ('el rollback desactiva también al personal',
         'portal/desactivar_cuentas.py',
         _PREDICADO_ROLLBACK,
         '_FUERA_DEL_ERP = "tipo_acceso <> \'portal\'"\n'),
    ],
}
