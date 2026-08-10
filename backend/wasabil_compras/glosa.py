"""Normalización de glosas bancarias y extracción de señales — módulo PURO.

POR QUÉ EXISTE: la glosa del banco es el único texto que describe un movimiento, y es
hostil por diseño — truncada a ~20-30 caracteres, sin tildes según el canal, con prefijos
de canal (TEF/TRANSFERENCIA A) que no son parte del nombre de nadie, y con números que
IGUAL pueden ser un RUT, un N° de operación o un folio. Este módulo concentra TODAS las
reglas deterministas de la sección «Normalización de glosa y nombres» de la spec del
matcher (docs/spec-matcher-banco-libro-2026-08-06.md) para que cada señal sea explicable
en motivo[] y probeable sin base de datos.

Módulo PURO: sin BD, sin red, sin imports del resto del backend (solo rut.py, que también
es puro). Así lo usan el motor, el router y los tests sin arrastrar nada.

PROHIBIDO acá (por la spec, sección «Lo que el matcher NUNCA intenta»):
  · Levenshtein/fuzzy: no es explicable y fabrica falsos justo donde el banco corta a
    mitad de palabra. El truncado se resuelve con PREFIJOS deterministas (≥4 chars).
  · Fabricar RUTs desde corridas de 9+ dígitos: una corrida larga que «contiene» un
    cuerpo no es un RUT (frontera de token dura, coherente con rut.py).
"""
import re
import unicodedata
from typing import List, Optional, Tuple

from .rut import rut_canonico, rut_valido

# ── Regla 2 de la spec: prefijos de canal — no son parte del nombre de nadie ──────
# Frases (se quitan como secuencia) y palabras sueltas (se quitan como token).
PREFIJOS_CANAL_FRASES = ("TRANSFERENCIA A", "TRANSFERENCIA DE", "COMPRA WEB")
PREFIJOS_CANAL_PALABRAS = {"TRANSFERENCIA", "TRANSF", "TEF", "PAGO", "ABONO", "CARGO",
                           "PAC", "PAT"}

# ── Regla 3: expansión de abreviaturas (ambos lados ANTES de comparar) ────────────
# 'LT' entra por tabla (regla 5 de la spec: 'LT' no llega a prefijo de 4 chars de
# LIMITADA, pero es la abreviatura real que el banco imprime al truncar).
ABREVIATURAS = {
    "CIA": "COMPANIA", "SOC": "SOCIEDAD", "HNOS": "HERMANOS",
    "LTDA": "LIMITADA", "LT": "LIMITADA", "STGO": "SANTIAGO", "FCO": "FRANCISCO",
}

# ── Regla 4: stopwords societarias/genéricas (unión de los 3 lentes de la spec) ───
# No cuentan para el umbral de tokens del nombre: mil razones sociales chilenas las
# comparten y matchearlas fabrica «coincidencias» sin identidad.
STOPWORDS_SOCIETARIAS = {
    "SA", "SPA", "LTDA", "LIMITADA", "EIRL", "CIA", "COMPANIA", "SOC", "SOCIEDAD",
    "COMERCIAL", "INDUSTRIAL", "SERVICIOS", "INVERSIONES", "INGENIERIA",
    "IMPORTADORA", "EXPORTADORA", "DISTRIBUIDORA", "TRANSPORTES", "CONSTRUCTORA",
    "EMPRESA", "EMPRESAS", "HNOS", "HERMANOS", "CHILE", "DEL", "DE", "LA", "LOS",
    "EL", "Y", "E",
}

# ── Regla 6: contexto operacional que APAGA la señal RUT-en-glosa ────────────────
# Un número precedido (±1 token) por estas palabras es un N° de operación / folio /
# cuenta, no un RUT — salvo formato explícito chileno (puntos o guión), que lo relaja.
CONTEXTO_OPERACIONAL = {"OP", "OPER", "NOMINA", "FOLIO", "N", "NRO", "CTA", "CUENTA",
                        "DOC"}

# ── Regla 8: keywords de clase (listas DEFAULT; el motor las puede extender por
#    configuración sin redeploy) ─────────────────────────────────────────────────
# Blocklist: movimientos SIN contraparte estructural en el libro de compras DTE.
KEYWORDS_BLOCKLIST = (
    "TGR", "TESORERIA", "SII", "F29", "F50", "PPM", "IMPUESTO", "TRASPASO",
    "LINEA DE CREDITO", "REMUNERACION", "SUELDO", "HONORARIO", "PREVIRED",
    "AFP", "ISAPRE", "FONASA", "MUTUAL",
)
# Nómina / pago masivo: jamás subset-sum (una coincidencia casual de un solo RUT
# parece sólida y es falsa) — se resuelven por tercera pata o por LOTE de egresos.
KEYWORDS_NOMINA = ("NOMINA", "PAGO PROVEEDORES", "PAGO MASIVO")
# Comisiones bancarias: van por la rama de agregado mensual (V4), nunca al pool.
KEYWORDS_COMISION = ("COMISION", "COM.")
# Factors conocidos (regla 11): su nombre en la glosa NO discrimina — el pago vía
# factoring pone el nombre del FACTOR, no del proveedor verdadero.
FACTORS = ("TANNER", "FACTOTAL", "FACTORING", "EUROCAPITAL", "PROGRESO", "COVAL")

# Regex del N° de operación (regla 7 de normalización): 'OP 309570', 'OPER. N° 309570',
# 'OP309570'. Los números que captura quedan VETADOS como candidatos a RUT.
_RE_NUMERO_OPERACION = re.compile(r"\bOP(?:ER)?\.?\s*N?[°º]?\s*(\d{5,})")
# RUT en formato explícito chileno: con puntos+guión o cuerpo pegado+guión. Las
# lookarounds imponen frontera dura: un dígito pegado a la izquierda/derecha lo anula
# (una corrida más larga que «contiene» un RUT no es un RUT).
_RE_RUT_EXPLICITO = re.compile(
    r"(?<![\dA-Z])(\d{1,3}(?:\.\d{3}){1,2}|\d{7,8})-([0-9K])(?![\dA-Z])")


def normalizar_texto(texto) -> str:
    """Regla 1 (base, calca el patrón _norm de tesoreria/service.py, ya probado):
    NFKD → ASCII ignore (fuera tildes; Ñ→N en ambos lados, porque el canal bancario
    despoja la Ñ según origen) → MAYÚSCULAS → puntuación a espacio → colapso → strip.

    Se aplica SIMÉTRICAMENTE a la glosa del banco y al receiver_name del libro: si un
    solo lado se normaliza, 'MUÑOZ' jamás encuentra a 'MUNOZ'.
    """
    t = unicodedata.normalize("NFKD", str(texto or "")).encode(
        "ascii", "ignore").decode("ascii").upper()
    t = re.sub(r"""[.,;:\-/'"]""", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _expandir(token: str, extra_abrev: Optional[dict] = None) -> str:
    """Abreviatura → forma canónica larga ('CIA'→'COMPANIA'). Tabla ampliable por
    configuración (extra_abrev) sin tocar código."""
    if extra_abrev and token in extra_abrev:
        return extra_abrev[token]
    return ABREVIATURAS.get(token, token)


def es_generico(token: str, extra_stop=None) -> bool:
    """¿El token es societario/genérico (no cuenta para identidad de nombre)?"""
    return token in STOPWORDS_SOCIETARIAS or (extra_stop is not None and token in extra_stop)


def tokens_nombre(texto, extra_abrev: Optional[dict] = None,
                  extra_stop=None) -> List[str]:
    """Tokens del NOMBRE: normaliza, quita prefijos de canal y expande abreviaturas.

    Devuelve TODOS los tokens (genéricos incluidos) porque la fuerza del nombre
    necesita contar ambos: los genéricos coincidentes dan señal MEDIA (regla 10:
    'COMERCIAL E INDUSTRIAL S' es débil, no nula), los no genéricos dan la FUERTE.
    El filtrado por es_generico() lo hace el que compara, no este tokenizador.
    """
    norm = " " + normalizar_texto(texto) + " "
    for frase in PREFIJOS_CANAL_FRASES:
        norm = norm.replace(f" {frase} ", " ")
    tokens = [t for t in norm.split() if t not in PREFIJOS_CANAL_PALABRAS]
    return [_expandir(t, extra_abrev) for t in tokens]


def tokens_coinciden(a: str, b: str) -> bool:
    """Regla 5: matching por PREFIJO determinista — idénticos, o uno prefijo del otro
    con ≥4 caracteres (maneja el truncado del banco: 'REPUEST' ~ 'REPUESTOS').
    PROHIBIDO fuzzy: 'SILVA' vs 'SILVE' NO coinciden (difieren, y ninguno es prefijo
    del otro) — esa es exactamente la sonda anti-fuzzy de la spec."""
    if a == b:
        return True
    corto, largo = (a, b) if len(a) <= len(b) else (b, a)
    return len(corto) >= 4 and largo.startswith(corto)


def fuerza_nombre(glosa, nombre_doc, extra_abrev=None, extra_stop=None
                  ) -> Tuple[str, dict]:
    """Fuerza del match de nombre entre la glosa y el emisor del libro.

    Devuelve ('fuerte'|'media'|'nula', detalle) SIN considerar colisiones con otros
    emisores (eso necesita el libro completo y lo resuelve el motor degradando
    fuerte→media). Reglas (spec §5 + tabla de pares dorados):
      · FUERTE: ≥2 tokens NO genéricos coincidentes, O 1 no genérico coincidente
        cuando NINGÚN lado queda con tokens no genéricos sin matchear (el par dorado
        'MUNOZ Y CIA LTDA' ↔ 'SOCIEDAD MUÑOZ Y COMPAÑIA LIMITADA': un solo apellido,
        pero es TODA la identidad de ambos lados).
      · MEDIA: 1 token no genérico coincidente con sobras, o solo coincidencias
        genéricas (≥2) — 'COMERCIAL E INDUSTRIAL S' contra SILVA: débil, no nula.
      · NULA: nada de lo anterior.
    Cada token se usa UNA vez (matching 1:1 greedy): 'SILVA SILVA' no infla el conteo.
    """
    tg = tokens_nombre(glosa, extra_abrev, extra_stop)
    td = tokens_nombre(nombre_doc, extra_abrev, extra_stop)
    usados = set()
    fuertes, genericos, pares = 0, 0, []
    for a in tg:
        for j, b in enumerate(td):
            if j in usados or not tokens_coinciden(a, b):
                continue
            usados.add(j)
            if es_generico(a, extra_stop) or es_generico(b, extra_stop):
                genericos += 1
            else:
                fuertes += 1
            pares.append((a, b))
            break
    ng_glosa = [t for t in tg if not es_generico(t, extra_stop)]
    ng_doc = [t for t in td if not es_generico(t, extra_stop)]
    sin_matchear = (len(ng_glosa) + len(ng_doc)) - 2 * fuertes
    if fuertes >= 2 or (fuertes == 1 and sin_matchear == 0):
        fuerza = "fuerte"
    elif fuertes == 1 or genericos >= 2:
        fuerza = "media"
    else:
        fuerza = "nula"
    return fuerza, {"fuertes": fuertes, "genericos": genericos, "pares": pares}


def extraer_numero_operacion(glosa) -> Optional[str]:
    """Regla 7: el N° de operación de una TEF viene INCRUSTADO en la glosa
    ('ABONO FACTORING SANTANDER OP 309570' → '309570'). Se devuelve tal cual se
    capturó; el strip de ceros a la izquierda lo hace la COMPARACIÓN (ambos lados),
    no la extracción — así el motivo puede citar el texto literal."""
    t = unicodedata.normalize("NFKD", str(glosa or "")).encode(
        "ascii", "ignore").decode("ascii").upper()
    m = _RE_NUMERO_OPERACION.search(t)
    return m.group(1) if m else None


def _numeros_operacion_todos(texto_upper: str) -> set:
    """Todos los números capturados por la regex OP: quedan VETADOS como candidatos a
    RUT (regla 7: la extracción de operación no fabrica RUTs)."""
    return {m.group(1) for m in _RE_NUMERO_OPERACION.finditer(texto_upper)}


def _palabra_base(token: str) -> str:
    """Solo las letras de un token, para el chequeo de contexto ('N°'→'N', 'OP.'→'OP')."""
    return re.sub(r"[^A-Z]", "", token)


def extraer_ruts_de_glosa(glosa) -> List[str]:
    """Regla 6 de normalización (y regla 9 de la tabla): RUTs REALES en la glosa.

    Exigencias ACUMULATIVAS (cada una mata una clase de falso positivo):
      1. Corridas MAXIMALES de dígitos de largo 7-8 — una corrida de 9+ que «contiene»
         un cuerpo NO matchea ('TEF 0976513680123' no menciona a nadie): frontera de
         token dura, coherente con rut.py.
      2. rut_valido == True (módulo 11): un DV contradictorio ANULA la señal — no
         basta rut_canonico, que a propósito no valida.
      3. Contexto NO operacional: ±1 token sin OP/OPER/NOMINA/FOLIO/N/NRO/CTA/CUENTA/
         DOC ('PAGO PROVEEDORES NOMINA 76849301' es un N° de nómina, no un RUT).
         El formato explícito chileno (puntos o guión) RELAJA el filtro: nadie escribe
         '76.849.030-1' para un número de operación.
      4. Los números capturados por la regex OP quedan VETADOS (ya son operación).

    La coincidencia con un candidato que pasó la puerta de monto y el interruptor de
    configuración (la señal nace APAGADA hasta la sonda de ingestión con ≥3 cartolas
    reales) son responsabilidad del MOTOR, no de esta extracción pura.
    """
    texto = unicodedata.normalize("NFKD", str(glosa or "")).encode(
        "ascii", "ignore").decode("ascii").upper()
    if not texto.strip():
        return []
    vetados = _numeros_operacion_todos(texto)
    encontrados: List[str] = []

    # 1) Formato explícito (puntos/guión): contexto relajado, pero módulo 11 y veto
    #    de operación se exigen igual.
    spans_explicitos = []
    for m in _RE_RUT_EXPLICITO.finditer(texto):
        spans_explicitos.append(m.span())
        cuerpo_digitos = re.sub(r"\D", "", m.group(1))
        if cuerpo_digitos in vetados:
            continue
        canon = rut_canonico(m.group(0))
        if canon and rut_valido(canon) and canon not in encontrados:
            encontrados.append(canon)

    # 2) Corridas peladas de dígitos, con contexto ±1 token.
    tokens = [(t.group(0), t.start(), t.end())
              for t in re.finditer(r"\S+", texto)]
    for i, (tok, t_ini, _t_fin) in enumerate(tokens):
        for m in re.finditer(r"\d+", tok):
            corrida = m.group(0)
            ini_abs = t_ini + m.start()
            if any(a <= ini_abs < b for a, b in spans_explicitos):
                continue                      # ya procesada como formato explícito
            if len(corrida) not in (7, 8) or corrida in vetados:
                continue
            canon = rut_canonico(corrida)     # el último dígito es el DV
            if not canon or not rut_valido(canon):
                continue
            # Contexto ±1 token, incluyendo el prefijo alfabético del PROPIO token
            # ('NRO12345678' se veta aunque venga pegado).
            contexto = {_palabra_base(tok[:m.start()])}
            if i > 0:
                contexto.add(_palabra_base(tokens[i - 1][0]))
            if i + 1 < len(tokens):
                contexto.add(_palabra_base(tokens[i + 1][0]))
            if contexto & CONTEXTO_OPERACIONAL:
                continue
            if canon not in encontrados:
                encontrados.append(canon)
    return encontrados


def glosa_tiene_keyword(glosa, keywords) -> Optional[str]:
    """¿La glosa contiene alguna keyword de clase? Devuelve LA keyword (para el motivo
    legible) o None.

    Matching deliberadamente conservador y explicable:
      · frases ('PAGO PROVEEDORES', 'LINEA DE CREDITO') → secuencia exacta de tokens;
      · palabras → token idéntico, o prefijo cuando la keyword tiene ≥6 letras
        ('IMPUESTO' enciende con 'IMPUESTOS'; 'SII' o 'AFP' solo exactos — un prefijo
        de 3 letras matchearía media lengua).
    """
    norm = normalizar_texto(glosa)
    if not norm:
        return None
    tokens = norm.split()
    con_bordes = f" {norm} "
    for kw in keywords:
        kwn = normalizar_texto(kw)
        if not kwn:
            continue
        if " " in kwn:
            if f" {kwn} " in con_bordes:
                return kw
        else:
            for t in tokens:
                if t == kwn or (len(kwn) >= 6 and t.startswith(kwn)):
                    return kw
    return None


def clasificar_movimiento(glosa, extra_blocklist=(), extra_nomina=(),
                          extra_comision=()) -> Optional[Tuple[str, str]]:
    """Clase de exclusión del pool para un movimiento, por su glosa.

    Precedencia deliberada blocklist > nómina > comisión: 'PAGO TESORERIA' es TGR
    aunque diga PAGO; una nómina jamás cae a la rama de comisiones. Devuelve
    (etiqueta, keyword_que_encendió) o None (movimiento matcheable normal).
    """
    kw = glosa_tiene_keyword(glosa, tuple(KEYWORDS_BLOCKLIST) + tuple(extra_blocklist))
    if kw:
        return ("blocklist_sin_sii", kw)
    kw = glosa_tiene_keyword(glosa, tuple(KEYWORDS_NOMINA) + tuple(extra_nomina))
    if kw:
        return ("nomina", kw)
    kw = glosa_tiene_keyword(glosa, tuple(KEYWORDS_COMISION) + tuple(extra_comision))
    if kw:
        return ("comision_banco", kw)
    return None


def nombre_de_factor(glosa, extra_factors=()) -> Optional[str]:
    """Regla 11: ¿la glosa nombra a un FACTOR? El factoring es operación cotidiana del
    dueño ('ABONO FACTORING SANTANDER OP 309570' es glosa real): el nombre del factor
    NO suma para nadie ni resta al doc verdadero — solo pone techo sugerido con motivo
    'posible cesión'."""
    return glosa_tiene_keyword(glosa, tuple(FACTORS) + tuple(extra_factors))
