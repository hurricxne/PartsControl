"""
Migración datos de negocio MonzaParts (Postgres → MariaDB)
Crea tablas nuevas e importa clientes, asesores, tags, markups,
proveedores, listas de precios, leads, ítems, actividades, próximos pasos.
"""
import csv
import pymysql
import os
from datetime import datetime

# ── Conexión MariaDB ───────────────────────────────────────────────────────
DB = dict(host="localhost", user="machparts_user",
          password="Zp2Rnw7yorWWypd2peUPX4JDV0",
          db="machparts_db", charset="utf8mb4",
          autocommit=False)

CSV_DIR = "/tmp/monza_csv"

def csv_read(filename):
    path = os.path.join(CSV_DIR, filename)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def parse_dt(s):
    """Postgres timestamp → Python datetime."""
    if not s or s.strip() == "":
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def parse_float(s, default=None):
    if not s or s.strip() == "":
        return default
    try:
        return float(s.strip())
    except ValueError:
        return default

def parse_int(s, default=None):
    if not s or s.strip() == "":
        return default
    try:
        return int(float(s.strip()))
    except ValueError:
        return default

def bool_val(s):
    return s.strip().lower() in ("t", "true", "1", "yes")

# ── DDL: tablas nuevas en MariaDB ──────────────────────────────────────────
DDL = [
    # Asesores comerciales
    """CREATE TABLE IF NOT EXISTS monza_asesores (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        slug        VARCHAR(50)  NOT NULL UNIQUE,
        nombre      VARCHAR(200) NOT NULL,
        email       VARCHAR(100),
        user_id     INT          REFERENCES users(id) ON DELETE SET NULL,
        activo      TINYINT(1)   NOT NULL DEFAULT 1,
        pg_id       VARCHAR(30)  UNIQUE,
        fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Tags / etiquetas
    """CREATE TABLE IF NOT EXISTS monza_tags (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        label       VARCHAR(100) NOT NULL UNIQUE,
        color_token VARCHAR(100),
        activo      TINYINT(1)   NOT NULL DEFAULT 1,
        pg_id       VARCHAR(30)  UNIQUE,
        fecha_creacion DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Relación cliente → tag
    """CREATE TABLE IF NOT EXISTS monza_cliente_tags (
        cliente_id  INT NOT NULL REFERENCES monza_clientes(id) ON DELETE CASCADE,
        tag_id      INT NOT NULL REFERENCES monza_tags(id)     ON DELETE CASCADE,
        asignado_en DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (cliente_id, tag_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Matriz de markup por cliente × calidad
    """CREATE TABLE IF NOT EXISTS monza_markups (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        cliente_id  INT NOT NULL REFERENCES monza_clientes(id) ON DELETE CASCADE,
        calidad     ENUM('genuino','oem','aftermarket') NOT NULL,
        valor       DECIMAL(8,4) NOT NULL,
        UNIQUE KEY uk_markup_cli_cal (cliente_id, calidad)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Proveedores de repuestos
    """CREATE TABLE IF NOT EXISTS monza_proveedores (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        code            VARCHAR(50)  NOT NULL UNIQUE,
        nombre          VARCHAR(200) NOT NULL,
        pais            VARCHAR(10),
        formato         VARCHAR(20),
        linea_default   VARCHAR(20),
        notas           TEXT,
        activo          TINYINT(1)   NOT NULL DEFAULT 1,
        pg_id           VARCHAR(30)  UNIQUE,
        fecha_creacion  DATETIME DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Listas de precios por proveedor (CARAT9, JAGUAR-3D, etc.)
    """CREATE TABLE IF NOT EXISTS monza_listas_precios (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        proveedor_id    INT NOT NULL REFERENCES monza_proveedores(id) ON DELETE RESTRICT,
        code            VARCHAR(50)  NOT NULL,
        nombre          VARCHAR(200),
        lead_time_dias  INT,
        delivery_profile VARCHAR(200),
        notas           TEXT,
        activo          TINYINT(1)   NOT NULL DEFAULT 1,
        pg_id           VARCHAR(30)  UNIQUE,
        fecha_creacion  DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_prov_code (proveedor_id, code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",

    # Catálogo de partes (se llena con CARAT en fase posterior)
    """CREATE TABLE IF NOT EXISTS monza_partes_catalogo (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        lista_id        INT NOT NULL REFERENCES monza_listas_precios(id) ON DELETE RESTRICT,
        sku             VARCHAR(100) NOT NULL,
        descripcion     VARCHAR(500) NOT NULL,
        marca           VARCHAR(100),
        calidad         ENUM('GENUINO','OEM','AFTERMARKET') NOT NULL DEFAULT 'GENUINO',
        costo           DECIMAL(12,4) NOT NULL,
        moneda          VARCHAR(5) NOT NULL DEFAULT 'EUR',
        peso_real_kg    DECIMAL(12,4),
        peso_vol_kg     DECIMAL(12,4),
        pfand           DECIMAL(12,4),
        moq             INT,
        nueva_parte_sku VARCHAR(100),
        disponible      TINYINT(1)   NOT NULL DEFAULT 1,
        pg_id           VARCHAR(30)  UNIQUE,
        ultima_importacion DATETIME,
        fecha_creacion  DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_lista_marca_sku (lista_id, marca, sku),
        FULLTEXT KEY ft_partes (sku, descripcion, marca)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]

# Columnas a agregar a tablas existentes (si no existen)
ALTER_CLIENTES = [
    "ADD COLUMN IF NOT EXISTS code         VARCHAR(50)",
    "ADD COLUMN IF NOT EXISTS customer_since DATETIME",
    "ADD COLUMN IF NOT EXISTS is_active    TINYINT(1) DEFAULT 1",
    "ADD COLUMN IF NOT EXISTS pg_id        VARCHAR(30) UNIQUE",
]

ALTER_LEADS = [
    # asesor_id ya existe en la tabla — no re-agregar con FK nueva
    "ADD COLUMN IF NOT EXISTS origen_crm  VARCHAR(30)",
    "ADD COLUMN IF NOT EXISTS vehicle_label VARCHAR(200)",
    "ADD COLUMN IF NOT EXISTS pg_id       VARCHAR(30) UNIQUE",
]

ALTER_LEAD_ITEMS = [
    "ADD COLUMN IF NOT EXISTS supplier_part_pg_id VARCHAR(30)",
]

ALTER_CONFIG = [
    "ADD COLUMN IF NOT EXISTS tc_usd_clp_real   FLOAT DEFAULT 950",
    "ADD COLUMN IF NOT EXISTS tc_eur_clp_real   FLOAT DEFAULT 1100",
    "ADD COLUMN IF NOT EXISTS tarifa_aerea_real FLOAT DEFAULT 4.5",
]

def run_migration():
    conn = pymysql.connect(**DB)
    cur  = conn.cursor()

    print("── 1. Creando tablas nuevas ──")
    for ddl in DDL:
        cur.execute(ddl)
    conn.commit()
    print("   ✓ Tablas creadas")

    print("── 2. Extendiendo tablas existentes ──")
    for col_def in ALTER_CLIENTES:
        try:
            cur.execute(f"ALTER TABLE monza_clientes {col_def}")
        except Exception as e:
            if "Duplicate column" in str(e): pass
            else: print(f"   ! {e}")
    for col_def in ALTER_LEADS:
        try:
            cur.execute(f"ALTER TABLE monza_leads {col_def}")
        except Exception as e:
            if "Duplicate column" in str(e): pass
            else: print(f"   ! {e}")
    for col_def in ALTER_LEAD_ITEMS:
        try:
            cur.execute(f"ALTER TABLE monza_lead_items {col_def}")
        except Exception as e:
            if "Duplicate column" in str(e): pass
            else: print(f"   ! {e}")
    conn.commit()
    print("   ✓ Columnas extendidas")

    # ── Asesores ──
    print("── 3. Asesores ──")
    asesor_map = {}  # pg_id → maria id
    for row in csv_read("advisors.csv"):
        cur.execute("SELECT id FROM monza_asesores WHERE pg_id=%s", (row["id"],))
        if cur.fetchone():
            cur.execute("SELECT id FROM monza_asesores WHERE pg_id=%s", (row["id"],))
            asesor_map[row["id"]] = cur.fetchone()[0]
            continue
        # Buscar user_id por slug/username
        cur.execute("SELECT id FROM users WHERE email LIKE %s OR nombre LIKE %s",
                    (f"%{row['slug']}%", f"%{row['name']}%"))
        r = cur.fetchone()
        uid = r[0] if r else None
        cur.execute("""INSERT IGNORE INTO monza_asesores (slug,nombre,email,user_id,activo,pg_id,fecha_creacion)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (row["slug"], row["name"], row["email"] or None, uid,
                     1 if bool_val(row["isActive"]) else 0,
                     row["id"], parse_dt(row["createdAt"])))
        cur.execute("SELECT id FROM monza_asesores WHERE pg_id=%s", (row["id"],))
        res = cur.fetchone()
        if res:
            asesor_map[row["id"]] = res[0]
    conn.commit()
    print(f"   ✓ {len(asesor_map)} asesores")

    # ── Tags ──
    print("── 4. Tags ──")
    tag_map = {}
    for row in csv_read("tags.csv"):
        cur.execute("INSERT IGNORE INTO monza_tags (label,color_token,activo,pg_id,fecha_creacion) VALUES (%s,%s,%s,%s,%s)",
                    (row["label"], row["colorToken"] or None,
                     1 if bool_val(row["isActive"]) else 0,
                     row["id"], parse_dt(row["createdAt"])))
        cur.execute("SELECT id FROM monza_tags WHERE pg_id=%s", (row["id"],))
        res = cur.fetchone()
        if res:
            tag_map[row["id"]] = res[0]
    conn.commit()
    print(f"   ✓ {len(tag_map)} tags")

    # ── Clientes ──
    print("── 5. Clientes ──")
    cliente_map = {}  # pg_id → maria id
    for row in csv_read("customers.csv"):
        # Verificar si ya existe por pg_id
        cur.execute("SELECT id FROM monza_clientes WHERE pg_id=%s", (row["id"],))
        ex = cur.fetchone()
        if ex:
            cliente_map[row["id"]] = ex[0]
            continue
        # Nuevo cliente
        nombre = row["name"].strip()
        rut    = row["rut"].strip() or None
        email_field = row.get("email","").strip() or None
        phone  = row.get("phone","").strip() or None
        # contactInfo puede tener email/phone si no están en columnas separadas
        contact_info = row.get("contactInfo","").strip()
        if not email_field and "@" in contact_info:
            # extraer email del texto libre
            for part in contact_info.split("·"):
                part = part.strip()
                if "@" in part:
                    email_field = part
                    break
        code   = row.get("code","").strip() or None
        since  = parse_dt(row.get("customerSince",""))

        cur.execute("""INSERT INTO monza_clientes
            (nombre,rut,email,telefono,code,customer_since,is_active,pg_id,fecha_creacion)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (nombre, rut, email_field, phone, code,
             since, 1 if bool_val(row["isActive"]) else 0,
             row["id"], parse_dt(row["createdAt"])))
        cliente_map[row["id"]] = cur.lastrowid
    conn.commit()
    print(f"   ✓ {len(cliente_map)} clientes")

    # ── Cliente → Tags ──
    print("── 5b. Customer tags ──")
    for row in csv_read("customer_tags.csv"):
        cid = cliente_map.get(row["customerId"])
        tid = tag_map.get(row["tagId"])
        if cid and tid:
            cur.execute("INSERT IGNORE INTO monza_cliente_tags (cliente_id,tag_id) VALUES (%s,%s)", (cid, tid))
    conn.commit()
    print("   ✓ tags asignados")

    # ── Markups ──
    print("── 6. Markups ──")
    calidad_map = {"GENUINO":"genuino","OEM":"oem","AFTERMARKET":"aftermarket"}
    mk_count = 0
    for row in csv_read("markups.csv"):
        cid = cliente_map.get(row["customerId"])
        if not cid:
            continue
        cal = calidad_map.get(row["quality"], "genuino")
        val = parse_float(row["value"], 0)
        cur.execute("""INSERT IGNORE INTO monza_markups (cliente_id,calidad,valor)
                       VALUES (%s,%s,%s)""", (cid, cal, val))
        mk_count += 1
    conn.commit()
    print(f"   ✓ {mk_count} markups")

    # ── Proveedores ──
    print("── 7. Proveedores ──")
    prov_map = {}
    line_map = {"HEAVY_MACHINERY":"maquinaria","AUTOMOTIVE":"autos"}
    for row in csv_read("suppliers.csv"):
        cur.execute("""INSERT IGNORE INTO monza_proveedores
            (code,nombre,pais,formato,linea_default,notas,activo,pg_id,fecha_creacion)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (row["code"], row["name"], row["country"] or None, row["ingestionFormat"],
             line_map.get(row.get("defaultLine",""), None), row["notes"] or None,
             1 if bool_val(row["isActive"]) else 0,
             row["id"], parse_dt(row["createdAt"])))
        cur.execute("SELECT id FROM monza_proveedores WHERE pg_id=%s", (row["id"],))
        res = cur.fetchone()
        if res:
            prov_map[row["id"]] = res[0]
    conn.commit()
    print(f"   ✓ {len(prov_map)} proveedores")

    # ── Listas de precios ──
    print("── 8. Listas de precios ──")
    lista_map = {}
    for row in csv_read("supplier_price_lists.csv"):
        pid = prov_map.get(row["supplierId"])
        if not pid:
            continue
        cur.execute("""INSERT IGNORE INTO monza_listas_precios
            (proveedor_id,code,nombre,lead_time_dias,delivery_profile,notas,activo,pg_id,fecha_creacion)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (pid, row["code"], row["name"] or row["code"],
             parse_int(row["leadTimeDays"]), row["deliveryProfile"] or None,
             row["notes"] or None, 1 if bool_val(row["isActive"]) else 0,
             row["id"], parse_dt(row["createdAt"])))
        cur.execute("SELECT id FROM monza_listas_precios WHERE pg_id=%s", (row["id"],))
        res = cur.fetchone()
        if res:
            lista_map[row["id"]] = res[0]
    conn.commit()
    print(f"   ✓ {len(lista_map)} listas de precios")

    # ── Leads ──
    print("── 9. Leads ──")
    lead_map = {}
    estado_map = {
        "PENDIENTE":"pendiente","EN_TRABAJO":"en_proceso",
        "COTIZADO":"en_proceso","VENDIDO":"vendido","DESCARTADO":"rechazado"
    }
    origen_map = {
        "WHATSAPP":"WhatsApp","EMAIL":"Email","TELEFONO":"Llamada",
        "WEB":"Web","MOSTRADOR":"Presencial","REFERIDO":"Referido",
        "INSTAGRAM":"Instagram","OTRO":"Otro"
    }
    # Determinar próximo número de lead
    cur.execute("SELECT MAX(CAST(SUBSTRING_INDEX(numero,'-',-1) AS UNSIGNED)) FROM monza_leads")
    r = cur.fetchone()
    lead_seq = (r[0] or 0)
    # Números ya usados en MariaDB (precarga para evitar colisiones dentro de la misma transacción)
    cur.execute("SELECT numero FROM monza_leads")
    numeros_existentes = {row2[0] for row2 in cur.fetchall()}

    for row in csv_read("leads.csv"):
        # Verificar si ya existe
        cur.execute("SELECT id FROM monza_leads WHERE pg_id=%s", (row["id"],))
        ex = cur.fetchone()
        if ex:
            lead_map[row["id"]] = ex[0]
            continue
        cid = cliente_map.get(row["customerId"])
        if not cid:
            continue
        aid = asesor_map.get(row["advisorId"])
        # Usar número de Postgres si no existe conflicto, si no generar nuevo
        numero_pg = row["numero"]  # e.g. L-2026-0001
        if numero_pg in numeros_existentes:
            lead_seq += 1
            numero = f"L-{datetime.now().year}-{lead_seq:04d}"
            # Asegurarse de que el nuevo número tampoco existe
            while numero in numeros_existentes:
                lead_seq += 1
                numero = f"L-{datetime.now().year}-{lead_seq:04d}"
        else:
            numero = numero_pg
        numeros_existentes.add(numero)  # registrar para próximas iteraciones
        estado = estado_map.get(row["estado"], "pendiente")
        origen = origen_map.get(row["origen"], "WhatsApp")
        vehiculo = row.get("vehicleLabel","").strip() or None
        vin = row.get("vin","").strip() or None
        nota = row.get("notaInterna","").strip() or None
        cur.execute("""INSERT INTO monza_leads
            (numero,cliente_id,vehiculo,vin,canal_origen,estado,asesor_id,
             comentario,linea,origen_crm,vehicle_label,pg_id,fecha_creacion,fecha_actualizacion)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (numero, cid, vehiculo, vin, origen, estado, aid,
             nota, "autos", origen, vehiculo,
             row["id"], parse_dt(row["fechaCreacion"]), parse_dt(row["updatedAt"])))
        lid = cur.lastrowid
        lead_map[row["id"]] = lid
    conn.commit()
    print(f"   ✓ {len(lead_map)} leads")

    # ── Lead Items ──
    print("── 10. Lead items ──")
    item_count = 0
    calidad_item_map = {"GENUINO":"genuine","OEM":"oem","AFTERMARKET":"aftermarket",
                        None:"sin_calificar","":"sin_calificar"}
    for row in csv_read("lead_items.csv"):
        lid = lead_map.get(row["leadId"])
        if not lid:
            continue
        cal = calidad_item_map.get(row.get("calidad","") or "", "sin_calificar")
        precio = parse_float(row.get("precio",""))
        cur.execute("""INSERT IGNORE INTO monza_lead_items
            (lead_id,descripcion,numero_parte,marca,procedencia,calidad,cantidad,
             precio_clp,supplier_part_pg_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (lid, row["repuesto"], row["numeroParte"] or None,
             row["brand"] or None, row["procedencia"] or None, cal,
             parse_int(row["quantity"],1), precio,
             row.get("supplierPartId","") or None))
        item_count += 1
    conn.commit()
    print(f"   ✓ {item_count} ítems")

    # ── Lead Actividades ──
    print("── 11. Actividades ──")
    tipo_act_map = {
        "CREACION":"lead_creado","WHATSAPP":"whatsapp","LLAMADA":"llamada",
        "EMAIL":"email","COTIZACION":"cotizacion","ETIQUETA":"nota",
        "NOTA":"nota","VENTA":"cotizacion","DESCARTE":"nota",
        "REASIGNACION":"nota","CAMBIO_ESTADO":"nota"
    }
    act_count = 0
    for row in csv_read("lead_activity.csv"):
        lid = lead_map.get(row["leadId"])
        if not lid:
            continue
        tipo = tipo_act_map.get(row["tipo"], "nota")
        cur.execute("""INSERT INTO monza_lead_actividades
            (lead_id,tipo,descripcion,usuario,fecha)
            VALUES (%s,%s,%s,%s,%s)""",
            (lid, tipo, row["descripcion"],
             "sistema", parse_dt(row["cuando"])))
        act_count += 1
    conn.commit()
    print(f"   ✓ {act_count} actividades")

    # ── Próximos pasos ──
    print("── 12. Próximos pasos ──")
    tipo_paso_map = {"LLAMADA":"llamada","WHATSAPP":"whatsapp","EMAIL":"email","VISITA":"visita"}
    paso_count = 0
    for row in csv_read("lead_next_steps.csv"):
        lid = lead_map.get(row["leadId"])
        if not lid:
            continue
        tipo = tipo_paso_map.get(row["tipo"],"llamada")
        completado = 1 if row.get("estado","PENDIENTE") == "COMPLETADO" else 0
        aid = asesor_map.get(row["advisorId"])
        cur.execute("""INSERT INTO monza_proximos_pasos
            (lead_id,tipo,cuando,asesor_id,completado,fecha_creacion)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (lid, tipo, parse_dt(row["cuando"]),
             aid, completado, parse_dt(row["createdAt"])))
        paso_count += 1
    conn.commit()
    print(f"   ✓ {paso_count} próximos pasos")

    # ── Actualizar monza_config con parámetros reales ──
    print("── 13. Actualizando monza_config ──")
    params = {r["key"]: r["value"] for r in csv_read("parameters.csv")}
    tc_usd = parse_float(params.get("tipo_cambio_clp","950"), 950)
    tc_eur = parse_float(params.get("tipo_cambio_eur_clp","1100"), 1100)
    tarifa = parse_float(params.get("tarifa_aerea_usd_kg","4.5"), 4.5)
    iva    = parse_float(params.get("iva_pct","0.19"), 0.19) * 100  # a %

    # Actualizar config existente
    cur.execute("SELECT id FROM monza_config LIMIT 1")
    row = cur.fetchone()
    if row:
        cur.execute("""UPDATE monza_config SET
            tc_usd_clp=%s, tc_eur_clp=%s, tarifa_aerea_por_kg=%s, iva_pct=%s
            WHERE id=%s""", (tc_usd, tc_eur, tarifa, iva, row[0]))
    else:
        cur.execute("""INSERT INTO monza_config
            (tc_usd_clp,tc_eur_clp,tarifa_aerea_por_kg,iva_pct) VALUES (%s,%s,%s,%s)""",
            (tc_usd, tc_eur, tarifa, iva))
    conn.commit()
    print(f"   ✓ TC USD={tc_usd}, EUR={tc_eur}, Tarifa={tarifa}, IVA={iva}%")

    # ── Actualizar company settings ──
    print("── 14. Company settings → monza_config ──")
    cfg = {r["key"]: r["value"] for r in csv_read("company_settings.csv")}
    razon  = cfg.get("company_legal_name","MonzaParts SpA")
    rut_e  = cfg.get("company_rut","78.121.316-0")
    dir_e  = cfg.get("company_address","")
    email_e = cfg.get("company_email","")
    giro_e = cfg.get("company_giro","")
    banco  = cfg.get("bank_name","")
    tipo_c = cfg.get("bank_account_type","")
    num_c  = cfg.get("bank_account_number","")
    cur.execute("""UPDATE monza_config SET
        razon_social=%s, rut_empresa=%s, direccion=%s, email_empresa=%s,
        giro=%s, banco=%s, tipo_cuenta=%s, numero_cuenta=%s
        WHERE id=(SELECT id FROM (SELECT id FROM monza_config LIMIT 1) t)""",
        (razon, rut_e, dir_e, email_e, giro_e, banco, tipo_c, num_c))
    conn.commit()
    print("   ✓ company settings guardados")

    cur.close()
    conn.close()
    print("\n✅ Migración de datos de negocio completada")

if __name__ == "__main__":
    run_migration()
