"""
Prueba técnica - Analista de Innovación
  python main.py validar               -> valida el formato de los CUFE (deja validacion_cufes.csv)
  python main.py rpa  --nit 900XXXXXX   -> Punto 1: consulta los CUFE en la DIAN y descarga los PDF
  python main.py ocr  --nit 900XXXXXX   -> Punto 2: PDF -> imágenes -> OCR -> DataFrames -> CSV + métricas
  python main.py todo --nit 900XXXXXX   -> ambos en secuencia, con resumen, métricas y aviso

El NIT (del emisor o receptor) lo pide el portal junto al CUFE y además es la
contraseña del PDF. Puede ir por CUFE en config/cufes.txt ("CUFE,NIT"), o uno
general con --nit / DIAN_NIT para las líneas que no lo traigan.

Código de salida: 0 = OK, 1 = parcial (hay algo para revisar), 2 = falla.
"""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).parent
sys.path.insert(0, str(RAIZ / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(RAIZ / ".env")
except ImportError:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paso", choices=["validar", "rpa", "ocr", "todo"])
    ap.add_argument("--nit", default=os.getenv("DIAN_NIT", ""), help="NIT para la consulta y la contraseña del PDF")
    ap.add_argument("--cufes", default=RAIZ / "config" / "cufes.txt", type=Path)
    ap.add_argument("--pdfs", default=None, type=Path, help="Carpeta de PDFs para el OCR (por defecto output/pdfs)")
    ap.add_argument("--salida", default=RAIZ / "output", type=Path)
    ap.add_argument("--sin-aviso", action="store_true", help="No enviar aviso a Teams/Slack")
    a = ap.parse_args()

    a.salida.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(a.salida / "ejecucion.log", encoding="utf-8")])
    inicio = time.time()
    rpa = None

    if a.paso == "validar":
        import rpa_dian
        filas = rpa_dian.validar_cufes(rpa_dian.leer_cufes(a.cufes, a.nit), a.salida)
        for x in filas:
            logging.info("CUFE #%-2d longitud=%d solo_hex=%s nit=%s -> %s", x["n"], x["longitud"],
                         x["solo_hex"], "sí" if x["tiene_nit"] else "no", "VÁLIDO" if x["valido"] else "INVÁLIDO")
        return 0 if all(x["valido"] for x in filas) else 1

    if a.paso in ("rpa", "todo"):
        import rpa_dian
        rpa = rpa_dian.ejecutar(rpa_dian.leer_cufes(a.cufes, a.nit), a.salida)
        logging.info("RPA: %d/%d PDF descargados", sum(r.estado == "descargado" for r in rpa), len(rpa))

    if a.paso in ("ocr", "todo"):
        import ocr_facturas
        carpeta = a.pdfs or a.salida / "pdfs"
        if any(carpeta.glob("*.pdf")):
            ocr_facturas.ejecutar(carpeta, a.salida, a.nit)
        else:
            logging.error("No hay PDFs en %s para el OCR", carpeta)

    import observabilidad as obs
    resumen = obs.construir_resumen(rpa, a.salida, inicio, incluir_ocr=a.paso != "rpa")
    obs.exportar_prometheus(resumen, a.salida)
    if not a.sin_aviso:
        obs.notificar(resumen)
    logging.info("Estado final: %s", resumen["estado"])
    return obs.codigo_salida(resumen)


if __name__ == "__main__":
    sys.exit(main())
