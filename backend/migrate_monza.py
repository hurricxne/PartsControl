"""Script de migración: crea tablas MonzaParts en machparts_db."""
import sys
sys.path.insert(0, '/var/www/machparts.bigcode.cl/backend')

from sqlalchemy import create_engine, text
from models.models import Base
import monza_models  # noqa: F401 — registra modelos en Base.metadata

from dotenv import load_dotenv
import os

load_dotenv('/var/www/machparts.bigcode.cl/backend/.env')
DATABASE_URL = os.getenv('DATABASE_URL')

engine = create_engine(DATABASE_URL)

tables = [
    'monza_clientes',
    'monza_leads',
    'monza_lead_items',
    'monza_lead_actividades',
    'monza_proximos_pasos',
    'monza_cotizaciones',
    'monza_cotizacion_items',
    'monza_config',
]

print("Creando tablas MonzaParts...")
Base.metadata.create_all(engine, tables=[
    Base.metadata.tables[t] for t in tables
    if t in Base.metadata.tables
])
print("Tablas creadas.")

# Insertar fila de config inicial si no existe
with engine.connect() as conn:
    result = conn.execute(text("SELECT COUNT(*) FROM monza_config")).scalar()
    if result == 0:
        conn.execute(text("""
            INSERT INTO monza_config (
                id, tc_usd_clp, tc_eur_clp, tarifa_aerea_por_kg, moneda_tarifa, iva_pct,
                razon_social, rut_empresa, direccion, giro, email_empresa,
                condiciones_default, ultima_actualizacion
            ) VALUES (
                1, 900, 1060, 4.5, 'EUR', 19,
                'MonzaParts SpA', '70.121.315-0', 'Roger de Flor 2998',
                'Venta de repuestos automotrices', 'logistica@monzaparts.cl',
                '1.- Validez de la cotización 5 días y sujeta a disponibilidad de los repuestos.\\n2.- Se considera entrega a cliente en terreno.\\n3.- Repuestos genuinos, en su packing original.',
                NOW()
            )
        """))
        conn.commit()
        print("Config inicial insertada.")
    else:
        print("Config ya existe, no se modifica.")

print("Migración completa.")
