"""
Prueba técnica - Analista de Innovación
  python main.py rpa   -> Punto 1: consulta los CUFE en la DIAN y descarga los PDF
  python main.py ocr   -> Punto 2: PDF -> imágenes -> OCR -> DataFrames -> CSV + métricas
  python main.py todo  -> ambos en secuencia
"""
import argparse
import logging
import sys
from pathlib import Path

RAIZ = Path(__file__).parent
sys.path.insert(0, str(RAIZ / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(RAIZ / ".env")
except ImportError:
    pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paso", choices=["rpa", "ocr", "todo"])
    ap.add_argument("--cufes", default=RAIZ / "config" / "cufes.txt", type=Path)
    ap.add_argument("--pdfs", default=None, type=Path, help="Carpeta de PDFs para el OCR (por defecto output/pdfs)")
    ap.add_argument("--salida", default=RAIZ / "output", type=Path)
    a = ap.parse_args()

    a.salida.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(a.salida / "ejecucion.log", encoding="utf-8")])

    if a.paso in ("rpa", "todo"):
        import rpa_dian
        cufes = [c.strip() for c in a.cufes.read_text().splitlines() if c.strip()]
        res = rpa_dian.ejecutar(cufes, a.salida)
        ok = sum(r.estado == "descargado" for r in res)
        logging.info("RPA terminado: %d/%d PDF descargados", ok, len(res))

    if a.paso in ("ocr", "todo"):
        import ocr_facturas
        ocr_facturas.ejecutar(a.pdfs or a.salida / "pdfs", a.salida)


if __name__ == "__main__":
    main()
