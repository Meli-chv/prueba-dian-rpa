"""
Prueba del flujo completo contra una SIMULACIÓN local del portal DIAN, sin
salir a internet. Imita el DOM real: Turnstile dentro de un iframe que solo
valida tras un clic, campo de NIT que aparece al escribir el CUFE,
button.search-document, página de detalle con "Descargar PDF", modal
"Aceptar" y PDF protegido con el NIT. Luego corre el OCR sobre ese PDF.

    HEADLESS=1 python tests/test_rpa_mock.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("HEADLESS", "1")
RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))

import pymupdf  # noqa: E402

import ocr_facturas  # noqa: E402
import rpa_dian  # noqa: E402

NIT = "900123456"
WIDGET = """<html><body style="margin:0"><div id="box" style="width:300px;height:65px;background:#eee"
 onclick="parent.postMessage('ok','*')">&#9744; Verifica que eres humano</div></body></html>"""
BUSCADOR = """<html><body>
<form id="search-document-form" action="/Document/Search" method="get">
 <input id="DocumentKey" name="DocumentKey" type="text">
 <input id="SearchDocumentNit" name="Nit" type="text" style="display:none">
 <div class="cf-turnstile" data-sitekey="0x4AAAAAAAmockkey"></div>
 <iframe src="https://challenges.cloudflare.com/turnstile/0x4AAAAAAAmockkey/w" width="300" height="65"></iframe>
 <input type="hidden" name="cf-turnstile-response" value="">
 <button class="search-document">Buscar</button>
</form>
<script>
 document.getElementById('DocumentKey').addEventListener('input', () =>
   document.getElementById('SearchDocumentNit').style.display = 'block');
 window.addEventListener('message', () =>
   document.querySelector("[name='cf-turnstile-response']").value = 'tok-123');
</script></body></html>"""
DETALLE = """<html><body><h3>Factura Electrónica</h3><a href="#" id="dl">Descargar PDF</a>
<div class="modal" id="m" style="display:none"><p>El PDF está protegido con el NIT.</p>
<a href="/Document/DownloadPDF?trackId={k}" id="ok">Aceptar</a></div>
<script>document.getElementById('dl').onclick = e => {{ e.preventDefault();
 document.getElementById('m').style.display = 'block'; }};</script></body></html>"""
NO_ENCONTRADO = "<html><body><p>No se encontraron resultados.</p></body></html>"


def pdf_protegido() -> bytes:
    doc = pymupdf.open(sorted((RAIZ / "tests" / "muestras").glob("*.pdf"))[0])
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=NIT, owner_pw=NIT + "x")


PDF = pdf_protegido()


def simular(route):
    url = route.request.url
    if "challenges.cloudflare.com" in url:
        return route.fulfill(body=WIDGET, content_type="text/html")
    if "SearchDocument" in url:
        return route.fulfill(body=BUSCADOR, content_type="text/html")
    if "/Document/Search" in url:
        q = url.split("?")[1]
        k = q.split("DocumentKey=")[1].split("&")[0]
        ok = f"Nit={NIT}" in q and not k.startswith("0000")
        return route.fulfill(body=DETALLE.format(k=k) if ok else NO_ENCONTRADO, content_type="text/html")
    if "DownloadPDF" in url:
        return route.fulfill(body=PDF, content_type="application/pdf",
                             headers={"Content-Disposition": "attachment; filename=factura.pdf"})
    return route.abort()


if __name__ == "__main__":
    import motor
    BT = motor.BrowserType
    orig_launch, orig_cdp = BT.launch_persistent_context, BT.connect_over_cdp

    def con_simulacion(self, *a, **kw):
        ctx = orig_launch(self, *a, **kw)
        ctx.route("**/*", simular)
        return ctx

    def cdp_con_simulacion(self, *a, **kw):
        nav = orig_cdp(self, *a, **kw)
        for c in nav.contexts:
            c.route("**/*", simular)
        return nav

    BT.launch_persistent_context, BT.connect_over_cdp = con_simulacion, cdp_con_simulacion
    print("Motor:", motor.MOTOR, "| modo:", "conectado a Chrome" if os.getenv("CONECTAR_CHROME") else "lanzado por el robot")
    import observabilidad as obs
    import time as _t
    pares = [(rpa_dian.leer_cufes(RAIZ / "config/cufes.txt")[0][0], NIT), ("0" * 96, NIT), ("no-es-un-cufe", NIT)]
    destino = Path(sys.argv[sys.argv.index("--conservar") + 1]) if "--conservar" in sys.argv else None
    with tempfile.TemporaryDirectory() as tmp:
        salida = destino or Path(tmp)
        inicio = _t.time()
        res = rpa_dian.ejecutar(pares, salida, pausa_s=0)
        for r in res:
            print(f"{r.cufe[:16]:16s} {r.estado:14s} captcha={r.metodo_captcha:22s} {r.seg_total}s {r.bytes}B")
        assert [r.estado for r in res] == ["descargado", "no_encontrado", "cufe_invalido"], res
        assert res[0].metodo_captcha.startswith("clic"), res[0].metodo_captcha

        df = ocr_facturas.ejecutar(salida / "pdfs", salida)   # NIT por CUFE desde la bitácora -> desbloqueo -> OCR
        assert len(df) > 0 and df["numero_factura"].notna().all()
        print(df[["numero_factura", "nit_emisor", "codigo", "cantidad", "precio_unitario"]].to_string(index=False))
        resumen = obs.construir_resumen(res, salida, inicio)
        obs.exportar_prometheus(resumen, salida)
        print("Estado:", resumen["estado"], "| RPA:", {k: resumen["rpa"][k] for k in ("tasa_reintento", "throughput_cufes_hora", "captcha_por_metodo")})
    print("OK: RPA + desbloqueo con NIT + OCR validados contra la simulación")
