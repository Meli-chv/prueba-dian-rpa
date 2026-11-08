"""
Prueba del flujo del RPA contra una SIMULACIÓN local del portal DIAN
(sin salir a internet). Intercepta las URL del portal con Playwright y
responde con páginas que imitan: buscador + captcha tipo Turnstile que se
valida solo a los 2 s, página de resultado con "Descargar PDF", y un caso
de "documento no encontrado".

    HEADLESS=1 python tests/test_rpa_mock.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("HEADLESS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import rpa_dian  # noqa: E402

BUSCADOR = """<html><body><form action="/Document/Search" method="get">
<input id="DocumentKey" name="DocumentKey" type="text" placeholder="CUFE">
<div class="cf-turnstile" data-sitekey="0x4AAAAAAAmock"></div>
<input type="hidden" name="cf-turnstile-response" value="">
<button type="submit">Buscar</button></form>
<script>setTimeout(()=>{document.querySelector("[name='cf-turnstile-response']").value='tok-123'},2000)</script>
</body></html>"""
RESULTADO = """<html><body><h3>Factura Electrónica</h3>
<a href="/Document/DownloadPDF?trackId={k}">Descargar PDF</a></body></html>"""
NO_ENCONTRADO = "<html><body><p>Documento no encontrado en los registros de la DIAN.</p></body></html>"
MUESTRA = sorted((Path(__file__).parent / "muestras").glob("*.pdf"))[0].read_bytes()


def simular(route):
    url = route.request.url
    if "SearchDocument" in url:
        return route.fulfill(body=BUSCADOR, content_type="text/html")
    if "/Document/Search" in url:
        k = url.split("DocumentKey=")[1].split("&")[0]
        body = NO_ENCONTRADO if k.startswith("0000") else RESULTADO.format(k=k)
        return route.fulfill(body=body, content_type="text/html")
    if "DownloadPDF" in url:
        return route.fulfill(body=MUESTRA, content_type="application/pdf",
                             headers={"Content-Disposition": "attachment; filename=factura.pdf"})
    return route.abort()


if __name__ == "__main__":
    import playwright.sync_api as pw

    original = pw.BrowserType.launch_persistent_context

    def con_simulacion(self, *a, **kw):
        ctx = original(self, *a, **kw)
        ctx.route("https://catalogo-vpfe.dian.gov.co/**", simular)
        return ctx

    pw.BrowserType.launch_persistent_context = con_simulacion
    cufes = [Path(__file__).resolve().parents[1].joinpath("config/cufes.txt").read_text().split()[0],
             "0" * 96, "no-es-un-cufe"]
    with tempfile.TemporaryDirectory() as tmp:
        res = rpa_dian.ejecutar(cufes, Path(tmp), pausa_s=0)
        for r in res:
            print(f"{r.cufe[:16]:16s} {r.estado:14s} captcha={r.metodo_captcha:6s} {r.seg_total}s {r.bytes}B")
        assert [r.estado for r in res] == ["descargado", "no_encontrado", "cufe_invalido"], res
        assert res[0].metodo_captcha == "auto"
    print("OK: flujo RPA validado contra la simulación")
