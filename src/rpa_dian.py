"""
RPA - Consulta de facturas electrónicas por CUFE en el catálogo de la DIAN
y descarga del PDF de la representación gráfica.

Flujo por CUFE (según el DOM real del portal):
    /User/SearchDocument -> captcha -> #DocumentKey (CUFE) -> #SearchDocumentNit (NIT)
    -> button.search-document -> captcha en el detalle (si aparece)
    -> "Descargar PDF" -> modal "Aceptar" -> output/pdfs/<CUFE>.pdf

Notas del portal:
- El campo del NIT aparece solo cuando #DocumentKey tiene texto.
- El PDF viene protegido con contraseña: el NIT (se desbloquea en el OCR).
- "networkidle" nunca se alcanza; se espera la navegación del clic.

Pensado como un proceso de producción: reintentos con espera creciente,
evidencia (captura + HTML) por cada falla, bitácora por CUFE y pausa entre
consultas para no saturar un servicio público.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import BrowserContext, Page, TimeoutError as PWTimeout, sync_playwright

from captcha_solver import resolver as resolver_captcha

log = logging.getLogger("rpa")

URL_BUSQUEDA = "https://catalogo-vpfe.dian.gov.co/User/SearchDocument"
CUFE_RE = re.compile(r"^[0-9a-f]{96}$")  # CUFE = SHA-384 en hexadecimal

# Selectores centralizados: si la DIAN cambia el HTML, solo se toca aquí
SEL_CUFE = "#DocumentKey"
SEL_NIT = "#SearchDocumentNit"
SEL_BUSCAR = "#search-document-form button.search-document, button.search-document, button:has-text('Buscar')"
SEL_DESCARGA = "a:has-text('Descargar PDF'), button:has-text('Descargar PDF')"
SEL_MODAL = ".modal:visible"
SEL_ACEPTAR = "button:has-text('Aceptar'), a:has-text('Aceptar')"
TEXTOS_NO_ENCONTRADO = ("no se encontraron resultados", "no se encontró", "documento no encontrado")


@dataclass
class ResultadoConsulta:
    cufe: str
    nit: str = ""
    estado: str = "pendiente"   # descargado | no_encontrado | captcha_fallido | error | cufe_invalido
    archivo: str = ""
    bytes: int = 0
    intentos: int = 0
    metodo_captcha: str = ""    # p. ej. "auto+no_requerido" (buscador + detalle)
    seg_captcha: float = 0.0
    seg_total: float = 0.0
    detalle: str = ""


def _evidencia(page: Page, carpeta: Path, nombre: str) -> None:
    carpeta.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(carpeta / f"{nombre}.png"), full_page=True)
        (carpeta / f"{nombre}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:  # la evidencia nunca debe tumbar el proceso
        log.debug("No se pudo guardar evidencia: %s", e)


def _aceptar_modal(page: Page) -> None:
    """El portal avisa en un modal que el PDF tiene contraseña; la descarga arranca al aceptar."""
    try:
        btn = page.locator(SEL_MODAL).locator(SEL_ACEPTAR).first
        btn.wait_for(state="visible", timeout=8000)
        btn.click(timeout=10000)
    except PWTimeout:
        log.debug("No apareció el modal de aceptación")


def _descargar_pdf(page: Page, ctx: BrowserContext, destino: Path) -> int:
    link = page.locator(SEL_DESCARGA).first
    link.wait_for(state="visible", timeout=20000)
    href = link.get_attribute("href") or ""
    try:
        with page.expect_download(timeout=45000) as dl:
            link.click(timeout=10000)
            _aceptar_modal(page)
        dl.value.save_as(destino)
    except PWTimeout:
        # El clic abrió el visor en vez de descargar: se pide la URL con la misma sesión
        if not href or href.startswith(("#", "javascript:")):
            raise
        resp = ctx.request.get(urljoin(page.url, href), timeout=60000)
        if not resp.ok or not resp.body().startswith(b"%PDF"):
            raise RuntimeError(f"La URL no devolvió un PDF (HTTP {resp.status})")
        destino.write_bytes(resp.body())

    data = destino.read_bytes()
    if not data.startswith(b"%PDF"):
        destino.unlink(missing_ok=True)
        raise RuntimeError("El archivo descargado no es un PDF válido")
    return len(data)


def consultar_cufe(page: Page, ctx: BrowserContext, cufe: str, nit: str, carpeta_pdf: Path,
                   carpeta_evid: Path, max_intentos: int = 3) -> ResultadoConsulta:
    res = ResultadoConsulta(cufe=cufe, nit=nit)
    t0 = time.perf_counter()
    if not CUFE_RE.match(cufe):
        res.estado, res.detalle = "cufe_invalido", "No es un SHA-384 hexadecimal de 96 caracteres"
        return res

    for intento in range(1, max_intentos + 1):
        res.intentos, res.estado = intento, "pendiente"
        try:
            page.goto(URL_BUSQUEDA, wait_until="domcontentloaded", timeout=60000)

            c1 = resolver_captcha(page)                         # captcha del buscador
            if not c1.resuelto:
                res.estado = "captcha_fallido"
                raise RuntimeError("Captcha del buscador no resuelto")

            page.locator(SEL_CUFE).fill(cufe)
            if nit:
                page.locator(SEL_NIT).wait_for(state="visible", timeout=10000)
                page.locator(SEL_NIT).fill(nit)

            with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                page.locator(SEL_BUSCAR).first.click(timeout=10000)

            c2 = resolver_captcha(page)                         # captcha del detalle
            res.metodo_captcha = f"{c1.metodo}+{c2.metodo}"
            res.seg_captcha = round(c1.segundos + c2.segundos, 2)
            if not c2.resuelto:
                res.estado = "captcha_fallido"
                raise RuntimeError("Captcha del detalle no resuelto")

            cuerpo = page.inner_text("body").lower()
            if any(t in cuerpo for t in TEXTOS_NO_ENCONTRADO):
                res.estado, res.detalle = "no_encontrado", "La DIAN no reporta el documento con ese CUFE/NIT"
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
            page.wait_for_timeout(3000 * intento)   # backoff

    res.seg_total = round(time.perf_counter() - t0, 2)
    return res


def leer_cufes(ruta: Path, nit_general: str = "") -> list[tuple[str, str]]:
    """Lee config/cufes.txt. Cada línea: CUFE, o CUFE y NIT separados por coma,
    punto y coma, tabulador o espacio. Si la línea no trae NIT se usa el general."""
    pares = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.split("#")[0].strip()
        if not linea:
            continue
        partes = re.split(r"[,;\t ]+", linea)
        pares.append((partes[0].strip().lower(), (partes[1] if len(partes) > 1 else nit_general).strip()))
    return pares


def validar_cufes(pares: list[tuple[str, str]], salida: Path) -> list[dict]:
    """Valida formato (SHA-384 = 96 caracteres hexadecimales) y deja evidencia en CSV."""
    filas = []
    for i, (cufe, nit) in enumerate(pares, 1):
        no_hex = sorted(set(re.sub(r"[0-9a-f]", "", cufe)))
        filas.append({"n": i, "cufe": cufe, "longitud": len(cufe), "solo_hex": not no_hex,
                      "caracteres_invalidos": "".join(no_hex), "tiene_nit": bool(nit),
                      "valido": bool(CUFE_RE.match(cufe))})
    salida.mkdir(parents=True, exist_ok=True)
    with (salida / "validacion_cufes.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0].keys()))
        w.writeheader()
        w.writerows(filas)
    ok = sum(x["valido"] for x in filas)
    log.info("Validación de CUFE: %d/%d con formato válido (SHA-384, 96 hex)", ok, len(filas))
    for x in filas:
        if not x["valido"]:
            log.warning("CUFE #%d inválido: longitud=%d, caracteres no hex=%r", x["n"], x["longitud"], x["caracteres_invalidos"])
    return filas


def ejecutar(pares: list[tuple[str, str]], salida: Path, pausa_s: float = 4.0) -> list[ResultadoConsulta]:
    carpeta_pdf, carpeta_evid = salida / "pdfs", salida / "evidencias"
    carpeta_pdf.mkdir(parents=True, exist_ok=True)
    validar_cufes(pares, salida)
    if any(not nit for _, nit in pares):
        log.warning("Hay CUFE sin NIT: el portal exige el NIT del emisor o receptor (usa CUFE,NIT en el archivo, --nit o DIAN_NIT)")
    resultados: list[ResultadoConsulta] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(salida / ".perfil_navegador"),  # reutiliza cookies de Cloudflare
            headless=os.getenv("HEADLESS", "0") == "1",
            accept_downloads=True, locale="es-CO", timezone_id="America/Bogota",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, (cufe, nit) in enumerate(pares, 1):
            log.info("[%d/%d] Consultando CUFE %s…", i, len(pares), cufe[:16])
            r = consultar_cufe(page, ctx, cufe, nit, carpeta_pdf, carpeta_evid)
            log.info("  -> %s (%ss, captcha=%s, intentos=%d)", r.estado, r.seg_total, r.metodo_captcha, r.intentos)
            resultados.append(r)
            if i < len(pares):
                time.sleep(pausa_s)   # cortesía con el servicio público
        ctx.close()

    with (salida / "bitacora_rpa.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(resultados[0]).keys()))
        w.writeheader()
        w.writerows(asdict(r) for r in resultados)
    return resultados
