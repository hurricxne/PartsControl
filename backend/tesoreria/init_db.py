"""Crea/actualiza las tablas del módulo Tesorería (standalone, idempotente).

Uso (desde backend/):
    python -m tesoreria.init_db

1) Importa los modelos referenciados por las FK (users, cont_egreso, cont_cobranza) para
   resolver las referencias y crea SOLO las tablas que falten (checkfirst=True), incluida
   cont_adelanto (adelantos de cliente Grupo AM).
2) Migra columnas/constraints ADITIVAS en tablas existentes (create_all NO las altera):
   · cont_factura_cliente.es_anticipo            (factura de anticipo, sin guía) y su
     normalización a NOT NULL DEFAULT 0 (un NULL invertiría el ORDER BY que rutea el
     adelanto: paridad con monza_contabilidad/init_db.py)
   · cont_factura_cliente_item.anticipo_factura_id (línea de descuento por anticipo)
   · conc_conciliacion_ingreso.adelanto_id + cobranza_id NULLABLE + UNIQUE + CHECK
     (vía de conciliación abono ↔ adelanto, espejo de monza_tes_conciliacion)
   · conc_conciliacion.UNIQUE(egreso_id) — conciliación 1:1 cargo↔egreso respaldada por
     la BD y no solo por el lock del router (espejo de monza_tes_conciliacion). Se salta
     con aviso si hubiera duplicados legados, para no tumbar el resto de la migración.
Las conc_* del antiguo módulo Conciliación Bancaria se conservan con sus datos.

`main()` devuelve la lista de pasos que quedaron PENDIENTES y, si hay alguno, imprime un
recuadro al FINAL: un paso saltado no puede pasar por un deploy exitoso en silencio. El
script nunca revienta por un dato legado (el resto del módulo sí queda migrado).
Conducta probada en `tesoreria/tests/test_lecturas_de_plata.py` (se suelta el UNIQUE, se
siembra el duplicado y se corre este script de verdad).
"""
from sqlalchemy import inspect, text

from database import Base, engine
import models.models            # noqa: F401  users / cont_factura_cliente / cont_adelanto
import compras_contab.models    # noqa: F401  cont_egreso (FK destino de conc_conciliacion)
from . import models as _tes_models  # noqa: F401  conc_* + conc_conciliacion_ingreso

TABLES = (
    "conc_cuenta_bancaria", "conc_cartola", "conc_movimiento",
    "conc_conciliacion", "conc_conciliacion_ingreso", "cont_adelanto",
)


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _columna_nullable(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT IS_NULLABLE FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return (row or "").upper() == "YES"


def _columna_not_null_default_0(conn, tabla: str, columna: str) -> bool:
    """¿La columna ya está NOT NULL con DEFAULT 0?

    Se consulta ANTES de emitir el MODIFY para que el paso sea idempotente Y barato: un
    `ALTER ... MODIFY` incondicional reconstruye la tabla en cada corrida del init_db."""
    row = conn.execute(
        text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).first()
    return bool(row) and row[0] == "NO" and str(row[1]) == "0"


def _constraint_existe(conn, tabla: str, nombre: str) -> bool:
    """UNIQUE / FOREIGN KEY / CHECK, por nombre, en information_schema."""
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.table_constraints "
            "WHERE table_schema = DATABASE() AND table_name = :t AND constraint_name = :n"
        ),
        {"t": tabla, "n": nombre},
    ).scalar()
    return bool(row)


def _alter(conn, sql: str, msg: str) -> None:
    conn.execute(text(sql))
    print(f"[tesoreria] + {msg}")


def _egresos_con_enlace_duplicado(conn, tope: int = 20) -> list:
    """egreso_id que aparecen en MÁS DE UN enlace de conc_conciliacion (dato legado, de
    antes del guard 1:1). Se consulta ANTES de emitir el ALTER del UNIQUE: con duplicados
    vivos MySQL responde 1062 y —al ir todo en una sola transacción— se caería el resto de
    la migración. Devuelve los ids para poder NOMBRARLOS en el aviso: 'hay duplicados' sin
    decir cuáles no es accionable para el operador."""
    filas = conn.execute(text(
        "SELECT egreso_id, COUNT(*) n FROM conc_conciliacion "
        "WHERE egreso_id IS NOT NULL GROUP BY egreso_id HAVING COUNT(*) > 1 "
        "ORDER BY egreso_id LIMIT :lim"
    ), {"lim": int(tope) + 1}).all()
    return [(int(f[0]), int(f[1])) for f in filas]


def main() -> list:
    """Corre la migración. Devuelve la lista de pasos que quedaron PENDIENTES (vacía = todo
    aplicado). No levanta excepción por un paso saltado a propósito: el deploy no se puede
    quedar a medias por un dato legado, pero el operador TIENE que enterarse — de ahí el
    resumen final en pantalla y este valor de retorno (lo usan las pruebas)."""
    pendientes: list = []
    print("[tesoreria] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    insp = inspect(engine)
    for t in TABLES:
        print(f"  - {t}: {'OK' if insp.has_table(t) else 'FALTA'}")

    # ── Migración aditiva (adelantos de cliente) ────────────────────────────────
    with engine.begin() as conn:
        # Factura de anticipo (respalda un adelanto ante el SII; no exige guía firmada)
        if not _columna_existe(conn, "cont_factura_cliente", "es_anticipo"):
            _alter(conn,
                   "ALTER TABLE cont_factura_cliente ADD COLUMN es_anticipo INT DEFAULT 0",
                   "columna cont_factura_cliente.es_anticipo")
        else:
            print("[tesoreria] = cont_factura_cliente.es_anticipo ya existe")

        # NORMALIZADOR (idempotente) del esquema de es_anticipo — paridad con
        # monza_contabilidad/init_db.py. Cubre las tablas nacidas de create_all: el modelo
        # solo declara default=0 de lado Python (sin server_default), así que la columna
        # quedó NULLABLE y SIN DEFAULT y un INSERT que no la nombre deja NULL — y un NULL
        # invierte el `ORDER BY es_anticipo DESC` que rutea el adelanto (MySQL pone los NULL
        # al final en DESC), mandando la plata a la factura equivocada.
        # El UPDATE va PRIMERO: con filas NULL vivas, el MODIFY ... NOT NULL fallaría (o las
        # convertiría en silencio según el sql_mode).
        if not _columna_not_null_default_0(conn, "cont_factura_cliente", "es_anticipo"):
            conn.execute(text(
                "UPDATE cont_factura_cliente SET es_anticipo = 0 "
                "WHERE es_anticipo IS NULL"))
            conn.execute(text(
                "ALTER TABLE cont_factura_cliente "
                "MODIFY es_anticipo INT NOT NULL DEFAULT 0"))
            print("[tesoreria] ~ cont_factura_cliente.es_anticipo → NOT NULL DEFAULT 0")
        else:
            print("[tesoreria] = cont_factura_cliente.es_anticipo ya es NOT NULL DEFAULT 0")

        # Línea de descuento por anticipo facturado (referencia a la factura de anticipo)
        if not _columna_existe(conn, "cont_factura_cliente_item", "anticipo_factura_id"):
            _alter(conn,
                   "ALTER TABLE cont_factura_cliente_item "
                   "ADD COLUMN anticipo_factura_id INT NULL",
                   "columna cont_factura_cliente_item.anticipo_factura_id")
        else:
            print("[tesoreria] = cont_factura_cliente_item.anticipo_factura_id ya existe")
        if not _constraint_existe(conn, "cont_factura_cliente_item",
                                  "fk_cont_fact_item_anticipo"):
            # Sin ON DELETE: la FK bloquea borrar una factura de anticipo ya descontada
            _alter(conn,
                   "ALTER TABLE cont_factura_cliente_item "
                   "ADD CONSTRAINT fk_cont_fact_item_anticipo "
                   "FOREIGN KEY (anticipo_factura_id) REFERENCES cont_factura_cliente(id)",
                   "FK cont_factura_cliente_item.anticipo_factura_id")
        else:
            print("[tesoreria] = FK fk_cont_fact_item_anticipo ya existe")

        # Trazabilidad cobranza 'adelanto' → adelanto que la generó (reversión exacta)
        if not _columna_existe(conn, "cont_cobranza", "adelanto_id"):
            _alter(conn,
                   "ALTER TABLE cont_cobranza ADD COLUMN adelanto_id INT NULL",
                   "columna cont_cobranza.adelanto_id")
        else:
            print("[tesoreria] = cont_cobranza.adelanto_id ya existe")
        if not _constraint_existe(conn, "cont_cobranza", "fk_cont_cobranza_adelanto"):
            _alter(conn,
                   "ALTER TABLE cont_cobranza "
                   "ADD CONSTRAINT fk_cont_cobranza_adelanto "
                   "FOREIGN KEY (adelanto_id) REFERENCES cont_adelanto(id)",
                   "FK cont_cobranza.adelanto_id")
        else:
            print("[tesoreria] = FK fk_cont_cobranza_adelanto ya existe")

        # Conciliación abono ↔ adelanto (exactamente uno: cobranza_id XOR adelanto_id)
        if not _columna_nullable(conn, "conc_conciliacion_ingreso", "cobranza_id"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso MODIFY cobranza_id INT NULL",
                   "conc_conciliacion_ingreso.cobranza_id ahora NULLABLE")
        else:
            print("[tesoreria] = conc_conciliacion_ingreso.cobranza_id ya es nullable")
        if not _columna_existe(conn, "conc_conciliacion_ingreso", "adelanto_id"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso ADD COLUMN adelanto_id INT NULL",
                   "columna conc_conciliacion_ingreso.adelanto_id")
        else:
            print("[tesoreria] = conc_conciliacion_ingreso.adelanto_id ya existe")
        if not _constraint_existe(conn, "conc_conciliacion_ingreso",
                                  "fk_conc_concil_ing_adelanto"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso "
                   "ADD CONSTRAINT fk_conc_concil_ing_adelanto "
                   "FOREIGN KEY (adelanto_id) REFERENCES cont_adelanto(id) ON DELETE CASCADE",
                   "FK conc_conciliacion_ingreso.adelanto_id")
        else:
            print("[tesoreria] = FK fk_conc_concil_ing_adelanto ya existe")
        if not _constraint_existe(conn, "conc_conciliacion_ingreso",
                                  "uq_conc_concil_ing_adelanto"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso "
                   "ADD CONSTRAINT uq_conc_concil_ing_adelanto UNIQUE (adelanto_id)",
                   "UNIQUE conc_conciliacion_ingreso.adelanto_id")
        else:
            print("[tesoreria] = UNIQUE uq_conc_concil_ing_adelanto ya existe")
        if not _constraint_existe(conn, "conc_conciliacion_ingreso",
                                  "ck_conc_concil_ing_un_destino"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso "
                   "ADD CONSTRAINT ck_conc_concil_ing_un_destino "
                   "CHECK ((cobranza_id IS NOT NULL) + (adelanto_id IS NOT NULL) = 1)",
                   "CHECK exactamente-un-destino en conc_conciliacion_ingreso")
        else:
            print("[tesoreria] = CHECK ck_conc_concil_ing_un_destino ya existe")

        # Snapshot del egreso al conciliar (desconciliar restaura fecha/ref previas
        # del operador en vez de limpiarlas por igualdad de valor)
        if not _columna_existe(conn, "conc_conciliacion", "fecha_egreso_previa"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion "
                   "ADD COLUMN fecha_egreso_previa DATE NULL",
                   "columna conc_conciliacion.fecha_egreso_previa")
        else:
            print("[tesoreria] = conc_conciliacion.fecha_egreso_previa ya existe")
        if not _columna_existe(conn, "conc_conciliacion", "referencia_egreso_previa"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion "
                   "ADD COLUMN referencia_egreso_previa VARCHAR(120) NULL",
                   "columna conc_conciliacion.referencia_egreso_previa")
        else:
            print("[tesoreria] = conc_conciliacion.referencia_egreso_previa ya existe")

        # UNIQUE(egreso_id) en conc_conciliacion: la conciliación 1:1 cargo↔egreso queda
        # respaldada por la BD y no solo por el lock del router (espejo de
        # monza_tes_conciliacion). Sin ella, cualquier camino nuevo —o una reparación
        # hecha en SQL— podía enlazar el MISMO pago contra dos cargos del banco.
        # DEFENSIVO: si la tabla ya trae un egreso con más de un enlace (dato legado,
        # anterior al guard), el ALTER fallaría y —al estar todo en una sola
        # transacción— tumbaría el resto de la migración. Se informa y se salta para
        # que el operador desconcilie el duplicado y vuelva a correr el script.
        if not _constraint_existe(conn, "conc_conciliacion", "uq_conc_concil_egreso"):
            dups = _egresos_con_enlace_duplicado(conn, tope=20)
            if dups:
                detalle = ", ".join(f"egreso {eid} ({n} enlaces)" for eid, n in dups[:20])
                # La consulta pide 21 para poder decir "más de 20" sin mentir el conteo.
                cuantos = "más de 20" if len(dups) > 20 else str(len(dups))
                if len(dups) > 20:
                    detalle += ", … (hay más)"
                print(f"[tesoreria] ! conc_conciliacion tiene {cuantos} egreso(s) con más de "
                      f"un enlace: NO se creó UNIQUE(egreso_id). Duplicados: {detalle}")
                pendientes.append(
                    "UNIQUE(egreso_id) uq_conc_concil_egreso en conc_conciliacion — SALTADO por "
                    f"{cuantos} egreso(s) con enlace duplicado: {detalle}. "
                    "Qué hacer: en Tesorería → Conciliación, desconcilie los movimientos "
                    "sobrantes de esos pagos (cada pago se concilia con UN cargo) y vuelva a "
                    "correr `python -m tesoreria.init_db`. Mientras no se cree, la "
                    "conciliación 1:1 cargo↔egreso la sostiene SOLO el lock del router.")
            else:
                _alter(conn,
                       "ALTER TABLE conc_conciliacion "
                       "ADD CONSTRAINT uq_conc_concil_egreso UNIQUE (egreso_id)",
                       "UNIQUE conc_conciliacion.egreso_id")
        else:
            print("[tesoreria] = UNIQUE uq_conc_concil_egreso ya existe")

    if pendientes:
        # Resumen FINAL y visible: un `print` perdido en el medio de ~20 líneas de salida
        # deja al dueño creyendo que el deploy salió completo.
        print("\n" + "=" * 78)
        print(f"[tesoreria] ATENCIÓN: {len(pendientes)} migración(es) quedó/quedaron PENDIENTE(S)")
        for p in pendientes:
            print(f"  · {p}")
        print("El resto del módulo SÍ quedó migrado y el sistema arranca igual.")
        print("=" * 78)
    else:
        print("[tesoreria] Listo (sin migraciones pendientes).")
    return pendientes


if __name__ == "__main__":
    main()
