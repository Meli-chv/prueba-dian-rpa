"""
RPA - Consulta de facturas electrónicas por CUFE en el catálogo de la DIAN
y descarga del PDF de la representación gráfica.

Flujo por cada CUFE:
    abrir buscador -> escribir CUFE -> resolver captcha -> Buscar
    -> esperar resultado -> "Descargar PDF" -> guardar output/pdfs/<CUFE>.pdf

Diseño:
- Playwright + Chromium con perfil persistente (cookies de Cloudflare se
  reutilizan entre consultas, lo que baja la tasa de captcha).
- Selectores con alternativas, porque el portal cambia su HTML.
- Reintentos con espera creciente y pausa entre consultas para no saturar
  el servicio público.
- Evidencia: captura de pantalla y HTML cuando una consulta falla.
- Bitácora CSV con estado, método de captcha y tiempos por CUFE.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

from captcha_solver import resolver as resolver_captcha

log = logging.getLogger("rpa")

URL_BUSQUEDA = "https://catalogo-vpfe.dian.gov.co/User/SearchDocument"
URL_BASE = "https://catalogo-vpfe.dian.gov.co"
CUFE_RE = re.compile(r"^[0-9a-f]{96}$")  # CUFE = SHA-384 en hexadecimal

SEL_INPUT = ["#DocumentKey", "input[name='DocumentKey']",
             "input[placeholder*='CUFE' i]", "input[type='text']"]
SEL_BUSCAR = ["button:has-text('Buscar')", "input[type='submit'][value*='Buscar' i]",
              "button[type='submit']"]
SEL_DESCARGA = ["a:has-text('Descargar PDF')", "a[href*='DownloadPDF' i]",
                "button:has-text('Descargar PDF')", "a:has-text('PDF')"]
TEXTOS_NO_ENCONTRADO = ["no encontrado", "no existe", "no se encontr", "sin resultados"]


@dataclass
class ResultadoConsulta:
    cufe: str
    estado: str = "pendiente"      # descargado | no_encontrado | captcha_fallido | error | cufe_invalido
    archivo: str = ""
    bytes: int = 0
    intentos: int = 0
    metodo_captcha: str = ""
    seg_captcha: float = 0.0
    seg_total: float = 0.0
    detalle: str = ""


def _primero(page: Page, selectores: list[str], timeout_ms: int = 8000):
    """Devuelve el primer selector visible de la lista."""
    fin = time.time() + timeout_ms / 1000
    while time.time() < fin:
        for s in selectores:
            loc = page.locator(s).first
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except Exception:
                pass
        page.wait_for_timeout(250)
    return None


def _evidencia(page: Page, carpeta: Path, nombre: str) -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(carpeta / f"{nombre}.png"), full_page=True)
        (carpeta / f"{nombre}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:  # la evidencia nunca debe tumbar el proceso
        log.debug("No se pudo guardar evidencia: %s", e)


def _descargar_pdf(page: Page, ctx: BrowserContext, destino: Path) -> int:
    """Intenta la descarga por evento del navegador; si el PDF se abre en
    línea, lo baja con la sesión (cookies) del mismo contexto."""
    boton = _primero(page, SEL_DESCARGA, timeout_ms=20000)
    if boton is None:
        raise RuntimeError("No apareció el botón 'Descargar PDF'")

    href = boton.get_attribute("href") or ""
    try:
        with page.expect_download(timeout=30000) as dl:
            boton.click()
        dl.value.save_as(destino)
    except PWTimeout:
        if not href:
            raise
        url = href if href.startswith("http") else URL_BASE + href
        resp = ctx.request.get(url, timeout=60000)
        if b"%PDF" not in resp.body()[:1024]:
            raise RuntimeError(f"La respuesta no es un PDF ({resp.headers.get('content-type')})")
        destino.write_bytes(resp.body())

    data = destino.read_bytes()
    if not data.startswith(b"%PDF"):
        destino.unlink(missing_ok=True)
        raise RuntimeError("Archivo descargado no es un PDF válido")
    return len(data)


def consultar_cufe(page: Page, ctx: BrowserContext, cufe: str, carpeta_pdf: Path,
                   carpeta_evid: Path, max_intentos: int = 3) -> ResultadoConsulta:
    res = ResultadoConsulta(cufe=cufe)
    t0 = time.perf_counter()

    if not CUFE_RE.match(cufe):
        res.estado, res.detalle = "cufe_invalido", "No es un SHA-384 hexadecimal de 96 caracteres"
        return res

    for intento in range(1, max_intentos + 1):
        res.intentos = intento
        try:
            page.goto(URL_BUSQUEDA, wait_until="domcontentloaded", timeout=60000)
            campo = _primero(page, SEL_INPUT, timeout_ms=15000)
            if campo is None:
                raise RuntimeError("No se encontró el campo de CUFE")
            campo.fill(cufe)

            cap = resolver_captcha(page)
            res.metodo_captcha, res.seg_captcha = cap.metodo, round(cap.segundos, 2)
            if not cap.resuelto:
                res.estado = "captcha_fallido"
                raise RuntimeError("Captcha no resuelto")

            boton = _primero(page, SEL_BUSCAR)
            if boton is None:
                raise RuntimeError("No se encontró el botón Buscar")
            boton.click()
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)

            cuerpo = page.inner_text("body").lower()
            if any(t in cuerpo for t in TEXTOS_NO_ENCONTRADO) and "descargar" not in cuerpo:
                res.estado, res.detalle = "no_encontrado", "La DIAN no reporta el documento"
                _evidencia(page, carpeta_evid, f"{cufe[:16]}_no_encontrado")
                break

            destino = carpeta_pdf / f"{cufe}.pdf"
            res.bytes = _descargar_pdf(page, ctx, destino)
            res.estado, res.archivo, res.detalle = "descargado", str(destino), ""
            break

        except Exception as e:
            res.detalle = f"{type(e).__name__}: {e}"[:300]
            if res.estado != "captcha_fallido":
                res.estado = "error"
            log.warning("CUFE %s… intento %d/%d falló: %s", cufe[:12], intento, max_intentos, res.detalle)
            _evidencia(page, carpeta_evid, f"{cufe[:16]}_intento{intento}")
            page.wait_for_timeout(3000 * intento)  # backoff

    res.seg_total = round(time.perf_counter() - t0, 2)
    return res


def ejecutar(cufes: list[str], salida: Path, pausa_s: float = 4.0) -> list[ResultadoConsulta]:
    carpeta_pdf = salida / "pdfs"
    carpeta_evid = salida / "evidencias"
    carpeta_pdf.mkdir(parents=True, exist_ok=True)
    headless = os.getenv("HEADLESS", "0") == "1"
    resultados: list[ResultadoConsulta] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(salida / ".perfil_navegador"),
            headless=headless,
            accept_downloads=True,
            locale="es-CO",
            timezone_id="America/Bogota",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for i, cufe in enumerate(cufes, 1):
            log.info("[%d/%d] Consultando CUFE %s…", i, len(cufes), cufe[:16])
            r = consultar_cufe(page, ctx, cufe.strip().lower(), carpeta_pdf, carpeta_evid)
            log.info("  -> %s (%ss, captcha=%s)", r.estado, r.seg_total, r.metodo_captcha)
            resultados.append(r)
            if i < len(cufes):
                time.sleep(pausa_s)  # cortesía con el servicio público
        ctx.close()

    bitacora = salida / "bitacora_rpa.csv"
    with bitacora.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(resultados[0]).keys()))
        w.writeheader()
        w.writerows(asdict(r) for r in resultados)
    log.info("Bitácora: %s", bitacora)
    return resultados
