"""Compara output/csv/consolidado.csv contra tests/muestras/esperado.csv (exactitud por campo)."""
import sys
from pathlib import Path

import pandas as pd

salida = Path(sys.argv[1] if len(sys.argv) > 1 else "output_pruebas")
esp = pd.read_csv(Path(__file__).parent / "muestras" / "esperado.csv", dtype=str)
got = pd.read_csv(salida / "csv" / "consolidado.csv", dtype=str)
esp["n"] = esp.groupby("archivo").cumcount()
got["n"] = got.groupby("archivo").cumcount()
m = esp.merge(got, on=["archivo", "n"], how="left", suffixes=("_esp", "_ocr"))
campos = ["numero_factura", "fecha_emision", "nit_emisor", "codigo", "descripcion", "cantidad", "precio_unitario"]
print(f"Ítems esperados: {len(esp)} | extraídos: {len(got)}")
for c in campos:
    a, b = m[f"{c}_esp"], m[f"{c}_ocr"]
    if c in ("cantidad", "precio_unitario"):
        ok = (pd.to_numeric(a) - pd.to_numeric(b)).abs() < 0.01
    else:
        ok = a.str.strip() == b.fillna("").str.strip()
    print(f"  {c:16s} {ok.mean()*100:6.1f}%")
    for _, r in m[~ok].iterrows():
        if (~ok).sum() <= 6: print(f"      ✗ {r['archivo']} #{r['n']}: esperado={r[f'{c}_esp']!r} ocr={r[f'{c}_ocr']!r}")
