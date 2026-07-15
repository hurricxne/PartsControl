"""
Scraper para parts.cat.com/es/finningchile
Usa Playwright con técnicas anti-detección para evadir Akamai Bot Manager.
"""
import asyncio
import logging
import re
import json
import random
from typing import Optional, Dict
from playwright.async_api import async_playwright, Page, Browser


logger = logging.getLogger("scraper")

BASE_URL = "https://parts.cat.com/es/finningchile"
SEARCH_URL = f"{BASE_URL}/search"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-accelerated-2d-canvas",
    "--no-first-run",
    "--no-zygote",
    "--disable-gpu",
    "--window-size=1366,768",
]


async def create_stealth_page(browser: Browser) -> Page:
    """Crea una página con configuración stealth para evadir detección."""
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={"width": 1366, "height": 768},
        locale="es-CL",
        timezone_id="America/Santiago",
        extra_http_headers={
            "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
    )

    page = await context.new_page()

    # Inyectar scripts anti-detección antes de cargar cada página
    await page.add_init_script("""
        // Ocultar webdriver
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // Simular plugins reales
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
            ]
        });

        // Simular idiomas
        Object.defineProperty(navigator, 'languages', { get: () => ['es-CL', 'es', 'en-US', 'en'] });

        // Ocultar automatización Chrome
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
            app: {}
        };

        // Permisos
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
    """)

    return page


def format_part_number(raw: str) -> str:
    """Formatea número de parte: '7T1997' -> '7T-1997'"""
    raw = raw.strip().upper().replace(" ", "").replace("-", "")
    m = re.match(r'^([A-Z0-9]{1,3}[A-Z])(\d{4,})$', raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    if re.match(r'^\d{7}$', raw):
        return f"{raw[:3]}-{raw[3:]}"
    if re.match(r'^\d{6,8}$', raw):
        mid = len(raw) // 2
        return f"{raw[:mid]}-{raw[mid:]}"
    return raw


async def scrape_part(page: Page, numero_parte: str) -> Dict:
    """
    Busca un número de parte en parts.cat.com y extrae sus datos.
    Retorna dict con: nombre_cat, precio_cat, moneda_cat, retiro_estimado, url_cat, imagen_url, encontrado
    """
    result = {
        "numero_parte": numero_parte,
        "nombre_cat": None,
        "precio_cat": None,
        "moneda_cat": "CLP",
        "retiro_estimado": None,
        "url_cat": None,
        "imagen_url": None,
        "encontrado": 0,
        "error": None,
    }

    formatted = format_part_number(numero_parte)

    try:
        # Capturar respuestas XHR/JSON que contengan datos de producto
        api_data = {}

        async def capture_response(response):
            try:
                url = response.url
                if ("product" in url.lower() or "search" in url.lower() or "price" in url.lower()) and response.status == 200:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        body = await response.json()
                        if isinstance(body, dict):
                            api_data.update(body)
            except Exception as e:
                logger.debug("capture_response falló para %s: %s", numero_parte, e)

        page.on("response", capture_response)

        # Navegar a la búsqueda del número de parte
        search_url = f"{SEARCH_URL}?q={formatted}&lang=es"
        result["url_cat"] = search_url

        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(2.0, 3.5))

        # Esperar que cargue contenido
        try:
            await page.wait_for_selector('[data-part-number], .product-item, [class*="product"], [class*="part-detail"]', timeout=15000)
        except Exception:
            logger.warning("Timeout esperando contenido de producto para %s", numero_parte)

        # --- Intentar extraer desde el DOM ---

        # 1. Nombre del producto
        for selector in [
            'h1[class*="product"]', 'h1[class*="title"]', 'h1',
            '[class*="product-name"]', '[class*="part-name"]',
            '[data-automation-id="product-title"]',
        ]:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and len(text) > 3:
                        result["nombre_cat"] = text
                        break
            except Exception:
                pass  # selector no presente; probar el siguiente

        # 2. Precio
        for selector in [
            '[class*="price"]:not([class*="original"])',
            '[data-automation-id="product-price"]',
            '[class*="product-price"]',
            'span[class*="price"]',
        ]:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    price_match = re.search(r'[\$]?\s*([\d.,]+)', text.replace(".", "").replace(",", "."))
                    if price_match:
                        result["precio_cat"] = float(price_match.group(1))
                        # Detectar moneda
                        if "CLP" in text.upper():
                            result["moneda_cat"] = "CLP"
                        elif "USD" in text.upper():
                            result["moneda_cat"] = "USD"
                        break
            except Exception:
                pass

        # 3. Retiro estimado
        for selector in [
            '[class*="pickup"]', '[class*="retiro"]', '[class*="availability"]',
            '[class*="delivery"]', '[class*="stock"]',
            '[data-automation-id="pickup-date"]',
        ]:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and len(text) > 2:
                        result["retiro_estimado"] = text[:200]
                        break
            except Exception:
                pass

        # 4. Imagen
        for selector in ['[class*="product-image"] img', '.product-image img', 'img[class*="product"]']:
            try:
                el = await page.query_selector(selector)
                if el:
                    src = await el.get_attribute("src")
                    if src:
                        result["imagen_url"] = src
                        break
            except Exception:
                pass

        # 5. Verificar si se encontró algo útil
        if result["nombre_cat"] or result["precio_cat"]:
            result["encontrado"] = 1
        else:
            # Intentar detectar "producto no encontrado"
            page_text = await page.inner_text("body")
            if "no se encontr" in page_text.lower() or "0 result" in page_text.lower():
                result["error"] = "Parte no encontrada en el catálogo"
            else:
                result["error"] = "No se pudo extraer datos (posible bloqueo o cambio de estructura)"

        # Remover listener
        page.remove_listener("response", capture_response)

        return result

    except Exception as e:
        result["error"] = str(e)[:300]
        return result


async def scrape_parts_batch(
    part_numbers: list,
    progress_callback=None
) -> list:
    """
    Scrapea una lista de números de parte en batch.
    progress_callback(current, total, result) se llama por cada parte procesada.
    """
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=LAUNCH_ARGS
        )
        page = await create_stealth_page(browser)

        # Warm-up: visitar página principal primero
        try:
            await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(random.uniform(1.5, 2.5))
        except Exception as e:
            logger.warning("Warm-up de %s falló: %s", BASE_URL, e)

        total = len(part_numbers)
        for idx, np in enumerate(part_numbers):
            result = await scrape_part(page, np)
            results.append(result)

            if progress_callback:
                await progress_callback(idx + 1, total, result)

            # Pausa aleatoria entre requests para no ser detectados
            if idx < total - 1:
                await asyncio.sleep(random.uniform(1.5, 3.0))

        await browser.close()

    return results


# --- Ejecución standalone para pruebas ---
if __name__ == "__main__":
    import sys

    test_parts = sys.argv[1:] if len(sys.argv) > 1 else ["7T-1997", "459-0686", "1A-1135"]

    async def main():
        print(f"Scraping {len(test_parts)} partes...")
        results = await scrape_parts_batch(test_parts)
        for r in results:
            print(json.dumps(r, ensure_ascii=False, indent=2))

    asyncio.run(main())
