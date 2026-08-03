"""Crea/actualiza las tablas del módulo Embarques Pricing (idempotente).

Uso (desde backend/):
    python -m embarques_pricing.init_db

1) Crea SOLO las tablas que falten (checkfirst=True). En una BD nueva quedan ya
   con la columna peso_origen (models.py la declara). No recrea lo existente.
2) Migra las columnas ADITIVAS en las tablas existentes (create_all NO altera
   tablas ya creadas):
     · emb_pricing_item.peso_origen         (origen del peso del prorrateo: auto|manual)
     · configuracion_cotizador.tipo_cambio_eur  (TC del EURO para embarques Baukat)
Las tablas del módulo y sus datos se conservan intactos.

⚠️  OJO EN EL DEPLOY: `configuracion_cotizador` la lee TODA la app (cotizador,
    ventas, compras, contabilidad). Correr este init_db ANTES de reiniciar uvicorn,
    o el modelo pedirá una columna que la BD no tiene y MySQL responderá 1054 →
    HTTP 500 en cascada. `deploy/audit_schema.py` sí detecta esta columna
    (registra models.models), así que la auditoría previa la delata.

PROTOCOLO DE PENDIENTES (espejo de `tesoreria/init_db.py`)
---------------------------------------------------------
Un paso que se salta a propósito NO puede pasar por un deploy exitoso en silencio.
`main()` devuelve la lista de pasos PENDIENTES y, si hay alguno, la ÚLTIMA cosa que
imprime el script es un recuadro `ATENCIÓN: N migración(es) quedó/quedaron PENDIENTE(S)`
con el detalle y el remedio; cuando no hay ninguno la línea de cierre es distinta
(`Listo (sin migraciones pendientes)`). Antes el aviso quedaba enterrado a mitad de la
salida y la última línea seguía diciendo `Listo.`: quien mira el final del log daba el
paso por bueno **con el candado ausente**, y ese candado es lo que impide que la factura
del forwarder entre dos veces (ver el bloque del UNIQUE, más abajo).
El script nunca revienta por un dato legado: el resto del módulo sí queda migrado.
Conducta probada en `embarques_pricing/tests/test_migracion_llave_pendiente.py` (se suelta
el índice, se siembra el duplicado marcado y se corre este script de verdad).
"""
import sys

from sqlalchemy import bindparam, text

from database import Base, engine
# Registra en el metadata las tablas referenciadas por las FK (users, embarques,
# embarque_items, items_cotizacion) y los modelos propios, antes del create_all.
import models.models  # noqa: F401  (users, embarques, embarque_items, items_cotizacion)
from embarques_pricing import models as _ep_models  # noqa: F401

TABLAS = ("emb_pricing", "emb_pricing_gasto", "emb_pricing_item")


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _indice_existe(conn, tabla: str, indice: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
        ),
        {"t": tabla, "i": indice},
    ).scalar()
    return bool(row)


def _tabla_existe(conn, tabla: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    ).scalar()
    return bool(row)


def _alter(conn, sql: str, msg: str) -> None:
    conn.execute(text(sql))
    print(f"[embarques_pricing] + {msg}")


def _gastos_duplicados(conn, tope: int = 20) -> list:
    """Grupos (pricing_id, tipo) con MÁS DE UNA fila en emb_pricing_gasto (dato legado, de
    antes de la llave natural). Se consulta ANTES de emitir el ALTER del UNIQUE: con
    duplicados vivos MySQL responde 1062 y —al ir todo en una sola transacción— se caería
    el resto de la migración.

    Devuelve, por grupo: `(pricing_id, tipo, numero_embarque, [ids], [ids_con_cxp_viva])`.
    Los dos últimos datos no son adorno:
      · el N° de embarque es lo ÚNICO con lo que el operador encuentra la fila en pantalla
        (un `pricing_id` no se muestra en ninguna parte);
      · la FK `cont_compra.emb_pricing_gasto_id` es ON DELETE **SET NULL**, así que borrar
        justo la fila referenciada desengancha la CxP, reaparece «Registrar como compra» y
        la factura del forwarder entra dos veces. Decir «deje la de menor id» sin mirar eso
        puede CAUSAR el daño que este UNIQUE previene, así que se nombra cuál no tocar.
    """
    grupos = conn.execute(text(
        "SELECT pricing_id, tipo, COUNT(*) n FROM emb_pricing_gasto "
        "GROUP BY pricing_id, tipo HAVING COUNT(*) > 1 "
        "ORDER BY pricing_id, tipo LIMIT :lim"
    ), {"lim": int(tope) + 1}).all()
    if not grupos:
        return []

    ids_pricing = sorted({int(g[0]) for g in grupos})
    # `bindparam(expanding=True)` NUEVO en cada consulta: un mismo objeto compartido entre
    # varios text() es un estado compartido que SQLAlchemy no promete tolerar.
    def _en_ids():
        return bindparam("ids", expanding=True)

    numero = {int(r[0]): (r[1] or "") for r in conn.execute(
        text("SELECT p.id, e.numero FROM emb_pricing p "
             "LEFT JOIN embarques e ON e.id = p.embarque_id "
             "WHERE p.id IN :ids").bindparams(_en_ids()),
        {"ids": ids_pricing}).all()}

    # CxP VIVAS colgadas de cada línea (0 si la tabla de compras aún no existe: este script
    # corre antes que compras_contab.init_db en una BD nueva, y un 1146 acá abortaría todo).
    cxp = {}
    if _tabla_existe(conn, "cont_compra"):
        cxp = {int(r[0]): int(r[1] or 0) for r in conn.execute(
            text("SELECT g.id, COUNT(c.id) FROM emb_pricing_gasto g "
                 "LEFT JOIN cont_compra c ON c.emb_pricing_gasto_id = g.id AND c.anulado = 0 "
                 "WHERE g.pricing_id IN :ids GROUP BY g.id").bindparams(_en_ids()),
            {"ids": ids_pricing}).all()}

    por_grupo: dict = {}
    for pid, tipo, gid in conn.execute(
            text("SELECT pricing_id, tipo, id FROM emb_pricing_gasto "
                 "WHERE pricing_id IN :ids ORDER BY id").bindparams(_en_ids()),
            {"ids": ids_pricing}).all():
        por_grupo.setdefault((int(pid), tipo), []).append(int(gid))

    salida = []
    for pid, tipo, _n in grupos:
        ids = por_grupo.get((int(pid), tipo), [])
        salida.append((int(pid), tipo, numero.get(int(pid), ""), ids,
                       [i for i in ids if cxp.get(i, 0) > 0]))
    return salida


def _detalle_duplicados(dups: list, tope: int = 20) -> str:
    partes = []
    for pid, tipo, numero, ids, con_cxp in dups[:tope]:
        cuales = "/".join(str(i) for i in con_cxp)
        quien = (f"NO BORRE la id {cuales} (tiene CxP viva colgada)" if con_cxp
                 else "ninguna tiene CxP viva: puede quedarse con la de menor id")
        partes.append(f"embarque {numero or '?'} (pricing {pid}) tipo '{tipo}': ids "
                      f"{', '.join(str(i) for i in ids)} → {quien}")
    return " | ".join(partes)


def main() -> list:
    """Corre la migración. Devuelve la lista de pasos que quedaron PENDIENTES (vacía = todo
    aplicado). No levanta excepción por un paso saltado a propósito —el deploy no se puede
    quedar a medias por un dato legado— pero el operador TIENE que enterarse: de ahí el
    recuadro FINAL en pantalla y este valor de retorno (lo usan las pruebas)."""
    pendientes: list = []
    print("[embarques_pricing] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)

    with engine.begin() as conn:
        for t in TABLAS:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = :t"
                ),
                {"t": t},
            ).scalar()
            print(f"  - {t}: {'OK' if row else 'FALTA'}")

        # ── Migración aditiva: peso editable por ítem ───────────────────────────
        # Espejo de fob_origen. 'auto' = el peso se lee de la cotización; 'manual'
        # = Contabilidad lo fijó a mano (re-prorratea el flete). Las filas viejas
        # quedan en NULL y el backend las trata como 'auto' (s.peso_origen or ...).
        if not _columna_existe(conn, "emb_pricing_item", "peso_origen"):
            _alter(conn,
                   "ALTER TABLE emb_pricing_item "
                   "ADD COLUMN peso_origen VARCHAR(20) DEFAULT 'auto'",
                   "columna emb_pricing_item.peso_origen")
        else:
            print("[embarques_pricing] = emb_pricing_item.peso_origen ya existe")

        # ── Migración aditiva: TC del EURO en la configuración ──────────────────
        # Los embarques Baukat/Europa vienen en EUR y este módulo ya leía
        # `cfg.tipo_cambio_eur` con getattr; la columna no existía, así que el
        # getattr devolvía SIEMPRE 0 → el pricing nacía con TC 0 y sin sugerencia,
        # y el TC se teclaba de memoria sobre el 100% del FOB. Espejo de
        # MonzaConfig.tc_eur_clp. El DEFAULT deja la fila existente en 1100 (el
        # mismo parámetro que usa el dueño); sigue siendo una SUGERENCIA: el TC de
        # cada embarque es editable en Contabilidad.
        if not _columna_existe(conn, "configuracion_cotizador", "tipo_cambio_eur"):
            _alter(conn,
                   "ALTER TABLE configuracion_cotizador "
                   "ADD COLUMN tipo_cambio_eur FLOAT DEFAULT 1100",
                   "columna configuracion_cotizador.tipo_cambio_eur")
        else:
            print("[embarques_pricing] = configuracion_cotizador.tipo_cambio_eur ya existe")

        # ── Migración aditiva: LLAVE NATURAL del gasto de embarque ──────────────
        # (pricing_id, tipo) es la identidad ESTABLE de la línea de gastos. La PK de
        # emb_pricing_gasto es una llave de plata: cont_compra.emb_pricing_gasto_id la
        # referencia para saber que la factura del forwarder ya está registrada como CxP,
        # y esa FK es ON DELETE **SET NULL** → borrar la fila desengancha la CxP en
        # silencio y la misma factura se puede cargar dos veces. Con este UNIQUE el
        # upsert del PUT (router.py, paso 2) tiene una llave garantizada por la BD y el
        # invariante deja de depender de que nadie reintroduzca un delete + re-insert.
        # ADITIVO: solo agrega un índice; no borra ni transforma datos.
        if _indice_existe(conn, "emb_pricing_gasto", "uq_emb_pricing_gasto_tipo"):
            print("[embarques_pricing] = uq_emb_pricing_gasto_tipo ya existe")
        else:
            dups = _gastos_duplicados(conn, tope=20)
            if dups:
                # No debería pasar nunca (los 2 escritores hacen 1 fila por tipo), pero si
                # pasa NO se borra nada a ciegas: se anota como PENDIENTE y se sigue, para
                # que el resto de la migración (las columnas que producen el 1054) se
                # aplique igual. El aviso va acá Y en el recuadro final: un print a mitad
                # de la salida no lo ve nadie.
                # La consulta pide 21 para poder decir "más de 20" sin mentir el conteo.
                cuantos = "más de 20" if len(dups) > 20 else str(len(dups))
                detalle = _detalle_duplicados(dups)
                if len(dups) > 20:
                    detalle += " | … (hay más)"
                print(f"[embarques_pricing] ! emb_pricing_gasto tiene {cuantos} grupo(s) "
                      f"(pricing_id, tipo) DUPLICADOS: NO se creó "
                      f"uq_emb_pricing_gasto_tipo. Duplicados: {detalle}")
                pendientes.append(
                    "UNIQUE (pricing_id, tipo) uq_emb_pricing_gasto_tipo en emb_pricing_gasto "
                    f"— SALTADO por {cuantos} grupo(s) con líneas duplicadas: {detalle}. "
                    "Qué hacer: deje UNA fila por (embarque, tipo) —la que tenga la CxP viva "
                    "colgada, NO la de menor id a ciegas: la FK cont_compra.emb_pricing_gasto_id "
                    "es ON DELETE SET NULL y borrar la referenciada desengancha la factura del "
                    "forwarder— y vuelva a correr `python -m embarques_pricing.init_db`. "
                    "RIESGO MIENTRAS FALTE: la identidad (pricing_id, tipo) de la línea de "
                    "gastos no tiene respaldo de BD, así que un delete + re-insert vuelve a "
                    "desenganchar la CxP, el overlay dice «no registrado» y la MISMA factura "
                    "del forwarder se carga DOS veces (Σ CxP al doble). "
                    "`deploy/audit_schema.py` lo canta como UNIQUE FALTANTE.")
            else:
                _alter(conn,
                       "ALTER TABLE emb_pricing_gasto "
                       "ADD CONSTRAINT uq_emb_pricing_gasto_tipo UNIQUE (pricing_id, tipo)",
                       "UNIQUE emb_pricing_gasto(pricing_id, tipo) — llave natural estable")

    if pendientes:
        # Resumen FINAL y visible: un `print` perdido en el medio de ~10 líneas de salida
        # deja al operador creyendo que el deploy salió completo.
        print("\n" + "=" * 78)
        print(f"[embarques_pricing] ATENCIÓN: {len(pendientes)} migración(es) "
              "quedó/quedaron PENDIENTE(S)")
        for p in pendientes:
            print(f"  · {p}")
        print("El resto del módulo SÍ quedó migrado y el sistema arranca igual.")
        print("Para que un deploy AUTOMATIZADO se caiga acá: correr con --exigir-completo (rc=2).")
        print("=" * 78)
    else:
        print("[embarques_pricing] Listo (sin migraciones pendientes).")
    return pendientes


if __name__ == "__main__":
    # rc=0 POR DEFECTO, a propósito. Este script es la PRIMERA línea del deploy (§1.a del
    # checklist) y las que vienen detrás son las que producen el `1054 → HTTP 500 en
    # cascada`. Si saliera rc≠0 por un paso de ENDURECIMIENTO, cualquier envoltorio con
    # `set -e` o `&&` abortaría las 8 migraciones siguientes: se cambiaría un riesgo latente
    # (que necesita un delete + re-insert que hoy ningún endpoint hace) por una caída SEGURA
    # de toda la app. El corte duro pertenece al paso que corre AL FINAL y cuyo aborto no
    # cuesta nada: `deploy/audit_schema.py` (§1.e), que ya sale rc=1 con UNIQUE FALTANTE.
    # Quien sí quiera el corte acá (CI, deploy scripteado) lo pide explícito:
    #     python -m embarques_pricing.init_db --exigir-completo   → rc=2 si quedó un pendiente
    _pendientes = main()
    if _pendientes and "--exigir-completo" in sys.argv[1:]:
        sys.exit(2)
