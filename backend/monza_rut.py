"""RUT normalizado para BUSCAR (MonzaParts).

EL PROBLEMA QUE CIERRA
    `monza_clientes.rut` guarda el RUT tal como lo digitó quien creó la ficha: conviven
    «76.000.000-0», «76000000-0» y «76.000.000-K». Un buscador con `ilike '%q%'` sobre la
    columna cruda solo encuentra al que teclea EXACTAMENTE el mismo formato, así que el
    vendedor que busca «76000000» no ve al cliente que existe — y el dedupe de fichas,
    que compara el RUT crudo, crea una ficha nueva sobre un RUT que ya existía.

POR QUÉ NO REUSA `monza_wasabil_compras/rut.py`
    Aquel es un canonizador ESTRICTO para el libro del SII, donde el RUT es llave de
    cruce: rechaza («devuelve None») todo lo que no sea un RUT completo y bien formado.
    Buscar es lo contrario: el operador teclea PEDAZOS («76000», «9123456-k») y espera
    coincidencias parciales. Un canonizador estricto devolvería None y la búsqueda no
    encontraría nada. Son dos trabajos distintos sobre el mismo dato.

CÓMO FUNCIONA
    Normaliza LOS DOS LADOS —lo tecleado y la columna— quitando puntos, guiones y
    espacios y subiendo a mayúscula. Así el match es bilateral: da igual el formato en
    que esté guardado y el formato en que se busque. Sin columna nueva y sin migración:
    la normalización de la columna ocurre en el SELECT.

    El costo es el mismo `LIKE '%…%'` que ya tienen todos los buscadores de la casa (sin
    índice por definición); a la escala de clientes de MonzaParts es imperceptible.
"""
from sqlalchemy import func, or_

# Un RUT chileno tiene 7-9 caracteres normalizados (6-8 de cuerpo + DV). Por debajo de
# este umbral, el término tecleado se parece más a un número de teléfono, un año o un
# monto que a un RUT: aplicar la rama igual llenaría los resultados de falsos positivos.
RUT_MIN_CARACTERES = 7


def rut_norm_py(valor) -> str:
    """Normaliza lo que el OPERADOR tecleó: sin puntos, guiones ni espacios, en mayúscula."""
    if not valor:
        return ""
    return (str(valor).strip().upper()
            .replace(".", "").replace("-", "").replace(" ", ""))


def rut_norm_sql(col):
    """La MISMA normalización, aplicada a la columna dentro del SELECT.

    `REPLACE` y `UPPER` existen tanto en MySQL (producción) como en SQLite (algunas
    suites), así que compila en ambos sin ramas por dialecto.
    """
    return func.upper(
        func.replace(func.replace(func.replace(col, ".", ""), "-", ""), " ", "")
    )


def rut_identidad(valor) -> str:
    """Llave de IDENTIDAD del RUT: cuerpo sin ceros a la izquierda + DV, o "" si no lo es.

    BUSCAR y DECIDIR QUIÉN ES QUIÉN son trabajos distintos, y confundirlos costó caro:
    `rut_norm_py` (el de buscar) solo borra puntos, guiones y espacios, así que un RUT de
    pura puntuación —'-', '.', '  '— colapsa a la cadena VACÍA… y esa llave vacía calza
    con TODA ficha cuyo RUT también sea vacío. Usada para dedupear, devolvía la ficha de
    un TERCERO y el vendedor seguía trabajando sobre ella.

    Esta función es la estricta: exige cuerpo numérico de 6 a 8 dígitos y un DV (0-9 o K),
    y normaliza los ceros a la izquierda ('076.543.210-8' y '76543210-8' son el MISMO
    contribuyente). Cadena vacía = «esto NO identifica a nadie», y quien decide identidad
    debe tratarla como «no hay RUT», nunca como una coincidencia.
    """
    t = rut_norm_py(valor)
    if len(t) < 2:
        return ""
    cuerpo, dv = t[:-1], t[-1]
    if not cuerpo.isdigit() or not (dv.isdigit() or dv == "K"):
        return ""
    cuerpo = cuerpo.lstrip("0")
    if not (6 <= len(cuerpo) <= 8):
        return ""
    return f"{cuerpo}{dv}"


def parece_rut(termino: str) -> bool:
    """¿El término tecleado amerita buscar por RUT?

    Solo si, ya normalizado, tiene largo de RUT y está compuesto por dígitos y —a lo
    más— una K final. Sin este filtro, buscar «MARIA» o «2026» activaría la rama de RUT
    y traería fichas que no tienen nada que ver.
    """
    t = rut_norm_py(termino)
    if len(t) < RUT_MIN_CARACTERES:
        return False
    cuerpo, dv = (t[:-1], t[-1]) if not t.isdigit() else (t, "0")
    return cuerpo.isdigit() and (dv.isdigit() or dv == "K")


def buscar_ficha_por_rut(db, modelo, valor):
    """La ficha que corresponde a ese RUT, o None. ÚNICA puerta del dedupe por RUT.

    Existe para que las DOS puertas que crean fichas —POST /clientes y el «cliente al
    vuelo» de POST /leads— decidan igual: cuando cada una tenía su propia comparación,
    una dedupeaba por formato y la otra por texto literal, y el mismo cliente terminaba
    con dos fichas según por dónde entrara.

    Reglas, en este orden:
      · Sin llave de IDENTIDAD (`rut_identidad` vacío: RUT malformado o pura puntuación)
        NO se dedupea nunca. Antes, un '-' mal tipeado enganchaba con la ficha de un
        tercero cuyo RUT también estaba vacío.
      · Con llave, el prefiltro SQL trae candidatos por la forma laxa (barato, superset)
        y la decisión final la toma la llave estricta en Python — así '076.543.210-8'
        encuentra a '76543210-8' en las DOS direcciones, sin falsos positivos.
    """
    llave = rut_identidad(valor)
    if not llave:
        return None
    laxo = rut_norm_py(valor)
    cuerpo, dv = llave[:-1], llave[-1]
    candidatos = (db.query(modelo)
                  .filter(or_(rut_norm_sql(modelo.rut) == laxo,
                              rut_norm_sql(modelo.rut) == llave,
                              rut_norm_sql(modelo.rut).like(f"%{cuerpo}{dv}")))
                  .all())
    return next((c for c in candidatos if rut_identidad(c.rut) == llave), None)
