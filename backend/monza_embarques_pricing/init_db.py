"""Inicializa el módulo Embarques Pricing MonzaParts en la BD (idempotente).

1) Crea SOLO las 3 tablas nuevas (monza_emb_pricing / _gasto / _item). No altera
   ninguna tabla existente (los embarques/ítems ya los crea Logística). En una BD
   nueva las tablas ya nacen con peso_origen (models.py la declara).
2) Migra las columnas ADITIVAS de las tablas que YA existen (create_all NO altera
   tablas ya creadas → sin esto el backend revienta con "Unknown column"):
     · monza_emb_pricing_item.peso_origen  (origen del peso del prorrateo: auto|manual)
     · monza_config.desconsolidado_clp / .bodegaje_clp / .costo_agencia_minimo_clp
       (gastos locales de internación por defecto con los que nace cada pricing)

Es seguro correr las veces que sea; los datos del módulo se conservan intactos.

Correr una vez por entorno (local y producción), ANTES de reiniciar el backend:
    cd backend && python -m monza_embarques_pricing.init_db

PROTOCOLO DE PENDIENTES (espejo de `tesoreria/init_db.py`)
---------------------------------------------------------
Un paso que se salta a propósito NO puede pasar por un deploy exitoso en silencio.
`main()` devuelve la lista de pasos PENDIENTES y, si hay alguno, la ÚLTIMA cosa que
imprime el script es un recuadro `ATENCIÓN: N migración(es) quedó/quedaron PENDIENTE(S)`
con el detalle y el remedio; cuando no hay ninguno la línea de cierre es distinta
(`init OK (sin migraciones pendientes)`). Antes el aviso quedaba enterrado a mitad de la
salida y la última línea seguía diciendo `init OK.`: quien mira el final del log daba el
paso por bueno **con el candado ausente**, y ese candado es lo que impide que la factura
del forwarder entre dos veces (ver el bloque del UNIQUE, más abajo).

⚠️  En MonzaParts el recuadro es MÁS importante que en Grupo AM: `deploy/audit_schema.py`
    clasifica las tablas `monza_emb_*` como «solo con el gate» y, con
    `MONZA_CONTAB_ENABLED=false`, un `UNIQUE FALTANTE` de este módulo lo reporta como AVISO
    y sale rc=0 (audit_schema.py:216, PREFIJOS_SOLO_CON_GATE). O sea: con el gate apagado
    este recuadro es la ÚNICA señal fuerte que queda en todo el deploy.
El script nunca revienta por un dato legado: el resto del módulo sí queda migrado.
Conducta probada en `monza_embarques_pricing/tests/test_migracion_llave_pendiente.py` (se
suelta el índice, se siembra el duplicado marcado y se corre este script de verdad).
"""
import sys

from sqlalchemy import bindparam, text

from database import Base, engine
# Registra en el metadata las tablas referenciadas por las FK (users, monza_embarques)
# y los modelos propios del módulo, antes del create_all.
import models.models  # noqa: F401  (users, etc.)
import monza_models  # noqa: F401  (monza_embarques, monza_embarque_items, etc.)
from monza_embarques_pricing import models as _ep_models  # noqa: F401

TABLAS = ("monza_emb_pricing", "monza_emb_pricing_gasto", "monza_emb_pricing_item")


def _tabla_existe(conn, tabla: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    ).scalar()
    return bool(row)


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


def _alter(conn, sql: str, msg: str) -> None:
    conn.execute(text(sql))
    print(f"[monza_embarques_pricing] + {msg}")


def _gastos_duplicados(conn, tope: int = 20) -> list:
    """Grupos (pricing_id, tipo) con MÁS DE UNA fila en monza_emb_pricing_gasto (dato legado,
    de antes de la llave natural). Se consulta ANTES de emitir el ALTER del UNIQUE: con
    duplicados vivos MySQL responde 1062 y —al ir todo en una sola transacción— se caería
    el resto de la migración.

    Devuelve, por grupo: `(pricing_id, tipo, numero_embarque, [ids], [ids_con_cxp_viva])`.
    Los dos últimos datos no son adorno:
      · el N° de embarque es lo ÚNICO con lo que el operador encuentra la fila en pantalla
        (un `pricing_id` no se muestra en ninguna parte);
      · la FK `monza_cont_compra.emb_pricing_gasto_id` es ON DELETE **SET NULL**, así que
        borrar justo la fila referenciada desengancha la CxP, reaparece «Registrar como
        compra» y la factura del forwarder entra dos veces. Decir «deje la de menor id» sin
        mirar eso puede CAUSAR el daño que este UNIQUE previene: se nombra cuál no tocar.
    """
    grupos = conn.execute(text(
        "SELECT pricing_id, tipo, COUNT(*) n FROM monza_emb_pricing_gasto "
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
        text("SELECT p.id, e.numero FROM monza_emb_pricing p "
             "LEFT JOIN monza_embarques e ON e.id = p.embarque_id "
             "WHERE p.id IN :ids").bindparams(_en_ids()),
        {"ids": ids_pricing}).all()}

    # CxP VIVAS colgadas de cada línea. monza_cont_compra es del bloque CONTABLE (🟡 del
    # checklist): con el gate apagado puede no existir todavía, y un 1146 acá abortaría
    # toda la migración —incluidas las columnas de monza_config que sí tumban el núcleo.
    cxp = {}
    if _tabla_existe(conn, "monza_cont_compra"):
        cxp = {int(r[0]): int(r[1] or 0) for r in conn.execute(
            text("SELECT g.id, COUNT(c.id) FROM monza_emb_pricing_gasto g "
                 "LEFT JOIN monza_cont_compra c "
                 "  ON c.emb_pricing_gasto_id = g.id AND c.anulado = 0 "
                 "WHERE g.pricing_id IN :ids GROUP BY g.id").bindparams(_en_ids()),
            {"ids": ids_pricing}).all()}

    por_grupo: dict = {}
    for pid, tipo, gid in conn.execute(
            text("SELECT pricing_id, tipo, id FROM monza_emb_pricing_gasto "
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
    Base.metadata.create_all(bind=engine, checkfirst=True)
    with engine.begin() as conn:
        for t in TABLAS:
            estado = "OK" if _tabla_existe(conn, t) else "FALTA"
            print(f"[monza_embarques_pricing] tabla {t}: {estado}")

        # ── Migración aditiva: peso editable por ítem ────────────────────────────
        # Espejo de fob_origen. 'auto' = el peso se lee de la cotización; 'manual'
        # = Contabilidad lo fijó a mano (re-prorratea el flete). Las filas viejas
        # quedan en NULL y el backend las trata como 'auto' (s.peso_origen or ...).
        if not _columna_existe(conn, "monza_emb_pricing_item", "peso_origen"):
            _alter(conn,
                   "ALTER TABLE monza_emb_pricing_item "
                   "ADD COLUMN peso_origen VARCHAR(20) DEFAULT 'auto'",
                   "columna monza_emb_pricing_item.peso_origen")
        else:
            print("[monza_embarques_pricing] = monza_emb_pricing_item.peso_origen ya existe")

        # ── Migración aditiva: gastos locales por defecto en la configuración ────
        # Las 6 líneas de gastos del pricing nacían en 0 y, si el contador se
        # olvidaba de cargarlas, el costo landed se CONGELABA sin gastos de
        # internación (cerrar_pricing solo exige costo_total > 0). Ahora se precargan
        # desde MonzaConfig, espejo de ConfiguracionCotizador de Grupo AM (mismos
        # nombres de columna). DEFAULT 0: no se copian los montos de Grupo AM (otro
        # negocio) — mientras estén en 0 el comportamiento es idéntico al de hoy y el
        # contador los carga UNA vez en Configuración. Siguen editables por embarque.
        for col, glosa in (
            ("desconsolidado_clp", "desconsolidación"),
            ("bodegaje_clp", "almacenaje"),
            ("costo_agencia_minimo_clp", "agencia de aduana"),
        ):
            if not _columna_existe(conn, "monza_config", col):
                _alter(conn,
                       f"ALTER TABLE monza_config ADD COLUMN {col} FLOAT DEFAULT 0",
                       f"columna monza_config.{col} (gasto local por defecto: {glosa})")
            else:
                print(f"[monza_embarques_pricing] = monza_config.{col} ya existe")

        # ── Migración aditiva: LLAVE NATURAL del gasto de embarque ───────────────
        # (pricing_id, tipo) es la identidad ESTABLE de la línea de gastos. La PK de
        # monza_emb_pricing_gasto es una llave de plata: monza_cont_compra.emb_pricing_
        # gasto_id la referencia para saber que la factura del forwarder ya está registrada
        # como CxP, y esa FK es ON DELETE **SET NULL** → borrar la fila desengancha la CxP
        # en silencio, el botón «Registrar como compra» reaparece y la misma factura entra
        # dos veces. Con este UNIQUE el upsert del PUT (router.py, paso 2) tiene una llave
        # garantizada por la BD. ADITIVO: solo agrega un índice; no borra ni transforma nada.
        if _indice_existe(conn, "monza_emb_pricing_gasto", "uq_monza_emb_pricing_gasto_tipo"):
            print("[monza_embarques_pricing] = uq_monza_emb_pricing_gasto_tipo ya existe")
        else:
            dups = _gastos_duplicados(conn, tope=20)
            if dups:
                # No debería pasar nunca (los 2 escritores hacen 1 fila por tipo), pero si
                # pasa NO se borra nada a ciegas: se anota como PENDIENTE y se sigue, para
                # que el resto de la migración (las columnas de monza_config, que tumban el
                # núcleo Monza con el gate apagado) se aplique igual. El aviso va acá Y en el
                # recuadro final: un print a mitad de la salida no lo ve nadie.
                # La consulta pide 21 para poder decir "más de 20" sin mentir el conteo.
                cuantos = "más de 20" if len(dups) > 20 else str(len(dups))
                detalle = _detalle_duplicados(dups)
                if len(dups) > 20:
                    detalle += " | … (hay más)"
                print(f"[monza_embarques_pricing] ! monza_emb_pricing_gasto tiene {cuantos} "
                      f"grupo(s) (pricing_id, tipo) DUPLICADOS: NO se creó "
                      f"uq_monza_emb_pricing_gasto_tipo. Duplicados: {detalle}")
                pendientes.append(
                    "UNIQUE (pricing_id, tipo) uq_monza_emb_pricing_gasto_tipo en "
                    f"monza_emb_pricing_gasto — SALTADO por {cuantos} grupo(s) con líneas "
                    f"duplicadas: {detalle}. "
                    "Qué hacer: deje UNA fila por (embarque, tipo) —la que tenga la CxP viva "
                    "colgada, NO la de menor id a ciegas: la FK "
                    "monza_cont_compra.emb_pricing_gasto_id es ON DELETE SET NULL y borrar la "
                    "referenciada desengancha la factura del forwarder— y vuelva a correr "
                    "`python -m monza_embarques_pricing.init_db`. "
                    "RIESGO MIENTRAS FALTE: la identidad (pricing_id, tipo) de la línea de "
                    "gastos no tiene respaldo de BD, así que un delete + re-insert vuelve a "
                    "desenganchar la CxP, el overlay dice «no registrado» y la MISMA factura "
                    "del forwarder se carga DOS veces (Σ CxP al doble). "
                    "`deploy/audit_schema.py` lo canta como UNIQUE FALTANTE, pero con "
                    "MONZA_CONTAB_ENABLED=false lo baja a AVISO y sale rc=0: con el gate "
                    "apagado este recuadro es la única señal fuerte.")
            else:
                _alter(conn,
                       "ALTER TABLE monza_emb_pricing_gasto ADD CONSTRAINT "
                       "uq_monza_emb_pricing_gasto_tipo UNIQUE (pricing_id, tipo)",
                       "UNIQUE monza_emb_pricing_gasto(pricing_id, tipo) — llave natural estable")

    if pendientes:
        # Resumen FINAL y visible: un `print` perdido en el medio de ~10 líneas de salida
        # deja al operador creyendo que el deploy salió completo.
        print("\n" + "=" * 78)
        print(f"[monza_embarques_pricing] ATENCIÓN: {len(pendientes)} migración(es) "
              "quedó/quedaron PENDIENTE(S)")
        for p in pendientes:
            print(f"  · {p}")
        print("El resto del módulo SÍ quedó migrado y el sistema arranca igual.")
        print("Para que un deploy AUTOMATIZADO se caiga acá: correr con --exigir-completo (rc=2).")
        print("=" * 78)
    else:
        print("[monza_embarques_pricing] init OK (sin migraciones pendientes).")
    return pendientes


if __name__ == "__main__":
    # rc=0 POR DEFECTO, a propósito. Este script es la PRIMERA línea del bloque Monza del
    # deploy (§1.b del checklist) y las que vienen detrás son las que producen el `1054 →
    # HTTP 500` con el gate apagado. Si saliera rc≠0 por un paso de ENDURECIMIENTO,
    # cualquier envoltorio con `set -e` o `&&` abortaría las 7 migraciones siguientes: se
    # cambiaría un riesgo latente (que necesita un delete + re-insert que hoy ningún
    # endpoint hace) por una caída SEGURA de toda la app. El corte duro pertenece al paso
    # que corre AL FINAL y cuyo aborto no cuesta nada: `deploy/audit_schema.py` (§1.e).
    # OJO: para ESTE módulo el auditor sale rc=0 si el gate está apagado (baja el hallazgo a
    # AVISO), así que un deploy scripteado de MonzaParts es justo el que conviene que pida:
    #     python -m monza_embarques_pricing.init_db --exigir-completo   → rc=2 si hay pendiente
    _pendientes = main()
    if _pendientes and "--exigir-completo" in sys.argv[1:]:
        sys.exit(2)
