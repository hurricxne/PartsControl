"""
Migración catálogo CARAT: Postgres supplier_parts → MariaDB monza_partes_catalogo
Lee directamente de Postgres con cursor server-side para no cargar todo en memoria.
Inserta en batches de 10.000 filas en MariaDB.
"""
import psycopg2
import psycopg2.extras
import pymysql
from datetime import datetime

# ── Conexiones ─────────────────────────────────────────────────────────────────
PG = dict(host="localhost", port=5432, user="postgres", password="monza_pg_2026",
          dbname="monzaparts_extract", cursor_factory=psycopg2.extras.DictCursor)

MARIA = dict(host="localhost", user="machparts_user",
             password="Zp2Rnw7yorWWypd2peUPX4JDV0",
             db="machparts_db", charset="utf8mb4", autocommit=False)

BATCH = 10_000  # filas por commit


def run():
    pg_conn   = psycopg2.connect(**PG)
    my_conn   = pymysql.connect(**MARIA)
    my_cur    = my_conn.cursor()

    # ── 1. Cargar mapa lista pg_id → MariaDB id ──────────────────────────────
    print("── Cargando mapa de listas de precios ──")
    my_cur.execute("SELECT pg_id, id FROM monza_listas_precios WHERE pg_id IS NOT NULL")
    lista_map = {row[0]: row[1] for row in my_cur.fetchall()}
    print(f"   {len(lista_map)} listas mapeadas: {list(lista_map.keys())}")

    # ── 2. Verificar cuántas partes ya existen ─────────────────────────────────
    my_cur.execute("SELECT COUNT(*) FROM monza_partes_catalogo")
    existing = my_cur.fetchone()[0]
    print(f"   {existing} partes ya en MariaDB")

    # ── 3. Cursor server-side en Postgres ─────────────────────────────────────
    print("── Abriendo cursor Postgres (server-side) ──")
    pg_cur = pg_conn.cursor("carat_cursor")  # named cursor = server-side
    pg_cur.execute("""
        SELECT
            sp.id               AS pg_id,
            sp."priceListId"    AS price_list_pg_id,
            sp.sku,
            sp.description,
            sp.brand,
            sp.quality,
            sp."costAmount"     AS costo,
            sp.currency         AS moneda,
            sp."realWeightKg"   AS peso_real_kg,
            sp."volumetricWeightKg" AS peso_vol_kg,
            sp.pfand,
            sp."minimumOrderQuantity" AS moq,
            sp."newArticleSku"  AS nueva_parte_sku,
            sp."isAvailable"    AS disponible,
            sp."lastImportedAt" AS ultima_importacion,
            sp."createdAt"      AS fecha_creacion
        FROM supplier_parts sp
    """)

    INSERT_SQL = """
        INSERT IGNORE INTO monza_partes_catalogo
            (lista_id, sku, descripcion, marca, calidad, costo, moneda,
             peso_real_kg, peso_vol_kg, pfand, moq, nueva_parte_sku,
             disponible, pg_id, ultima_importacion, fecha_creacion)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    # ── 4. Procesamiento en batches ────────────────────────────────────────────
    print("── Importando partes a MariaDB ──")
    batch = []
    total_ok = 0
    total_skip = 0
    t0 = datetime.now()
    report_every = 100_000

    while True:
        rows = pg_cur.fetchmany(BATCH)
        if not rows:
            break

        for r in rows:
            lista_id = lista_map.get(r["price_list_pg_id"])
            if not lista_id:
                total_skip += 1
                continue

            batch.append((
                lista_id,
                r["sku"] or "",
                (r["description"] or "")[:500],
                r["brand"],
                r["quality"] or "GENUINO",   # GENUINO/OEM/AFTERMARKET
                r["costo"],
                r["moneda"] or "EUR",
                r["peso_real_kg"],
                r["peso_vol_kg"],
                r["pfand"],
                r["moq"],
                r["nueva_parte_sku"],
                1 if r["disponible"] else 0,
                r["pg_id"],
                r["ultima_importacion"],
                r["fecha_creacion"],
            ))

        if batch:
            my_cur.executemany(INSERT_SQL, batch)
            my_conn.commit()
            total_ok += len(batch)
            batch = []

        if (total_ok + total_skip) % report_every == 0:
            elapsed = (datetime.now() - t0).seconds
            rate = total_ok / max(elapsed, 1)
            print(f"   {total_ok:,} insertadas | {total_skip:,} sin lista | {rate:.0f} filas/s")

    # Flush remaining
    if batch:
        my_cur.executemany(INSERT_SQL, batch)
        my_conn.commit()
        total_ok += len(batch)

    elapsed = (datetime.now() - t0).total_seconds()
    print(f"\n✅ Importación completada")
    print(f"   Insertadas: {total_ok:,}")
    print(f"   Saltadas (sin lista): {total_skip:,}")
    print(f"   Tiempo: {elapsed:.1f}s  ({total_ok/max(elapsed,1):.0f} filas/s)")

    # ── 5. Verificación final ─────────────────────────────────────────────────
    my_cur.execute("SELECT COUNT(*) FROM monza_partes_catalogo")
    final = my_cur.fetchone()[0]
    print(f"   Total en MariaDB: {final:,}")

    pg_cur.close()
    pg_conn.close()
    my_cur.close()
    my_conn.close()


if __name__ == "__main__":
    run()
