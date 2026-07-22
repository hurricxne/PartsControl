from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import settings

# isolation_level="READ COMMITTED" — auditoría 2026-07-21, ver docs/regla-lecturas-de-plata.md
#
# Por qué NO el default de MySQL (REPEATABLE READ): en ese nivel InnoDB arma la "foto" de
# la base en la PRIMERA sentencia de la transacción, y toda lectura PLANA posterior sirve
# esa foto aunque otra transacción haya commiteado después. En esta app la primera
# sentencia de TODO request es el SELECT de usuarios del guard de empresa
# (`dependencies=[Depends(require_empresa(...))]` → get_current_user, misma sesión), o sea
# ANTES de cualquier with_for_update(). Consecuencia: los locks serializaban bien, pero los
# topes calculados sobre relaciones perezosas leían datos viejos. Costó cinco fugas de
# plata reproducibles (sobre-cobro al cliente, doble aplicación de adelantos, saldos
# corruptos, retención de factoring perdida, sobre-pago a proveedor).
#
# READ COMMITTED renueva la foto en cada sentencia: no elimina la necesidad de leer bajo
# lock donde se decide plata (eso se conserva como segunda capa), pero cierra la clase
# entera —incluidos los puntos aún no endurecidos— y además REDUCE los deadlocks, porque
# desaparecen los gap locks.
#
# CUIDADO al desplegar:
#  · Requiere binlog_format = ROW o MIXED. Con STATEMENT, MySQL RECHAZA las escrituras.
#    Verificar antes:  SELECT @@global.binlog_format;
#  · El nivel se fija al ABRIR cada conexión → reiniciar el servicio COMPLETO de uvicorn
#    (no --reload), o quedan workers mezclados con el nivel viejo.
#  · Sin gap locks, la unicidad va SIEMPRE en índice UNIQUE + captura de IntegrityError,
#    nunca en un FOR UPDATE sobre un rango vacío.
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    isolation_level="READ COMMITTED",
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
