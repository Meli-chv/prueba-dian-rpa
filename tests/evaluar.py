"""
Calidad de la extracción contra el valor esperado (ground truth):
precisión, recall y F1 por campo y global, y conteo de ítems.

  TP = celda extraída y correcta
  FP = celda extraída pero incorrecta (o ítem que sobra)
  FN = celda esperada que no se extrajo o quedó incorrecta

    python tests/evaluar.py <carpeta_salida> [esperado.csv]   -> imprime y guarda <carpeta_salida>/metricas_calidad.json
"""
import json
import sys
from pathlib import Path

import pandas as pd

salida = Path(sys.argv[1] if len(sys.argv) > 1 else "output_pruebas")
esp = pd.read_csv(Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "muestras" / "esperado.csv", dtype=str)
got = pd.read_csv(salida / "csv" / "consolidado.csv", dtype=str)
esp["n"] = esp.groupby("archivo").cumcount()
got["n"] = got.groupby("archivo").cumcount()
m = esp.merge(got, on=["archivo", "n"], how="outer", suffixes=("_esp", "_ocr"), indicator=True)
campos = ["numero_factura", "fecha_emision", "nit_emisor", "codigo", "descripcion", "cantidad", "precio_unitario"]


def iguales(a, b, numerico):
    if pd.isna(a) or pd.isna(b):
        return False
    if numerico:
        try:
            return abs(float(a) - float(b)) < 0.01
        except ValueError:
            return False
    return a.strip() == b.strip()


res, TP, FP, FN = {}, 0, 0, 0
print(f"Ítems esperados: {len(esp)} | extraídos: {len(got)}")
print(f"{'campo':16s} {'precisión':>9s} {'recall':>7s} {'F1':>6s}")
for c in campos:
    tp = fp = fn = 0
    for _, r in m.iterrows():
        a, b = r.get(f"{c}_esp"), r.get(f"{c}_ocr")
        ok = iguales(a, b, c in ("cantidad", "precio_unitario"))
        tp += ok
        fp += (not ok) and pd.notna(b) and str(b).strip() != ""
        fn += (not ok) and pd.notna(a)
    p = tp / (tp + fp) if tp + fp else 0
    rc = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * p * rc / (p + rc) if p + rc else 0
    res[c] = {"precision": round(p, 4), "recall": round(rc, 4), "f1": round(f1, 4), "tp": tp, "fp": int(fp), "fn": int(fn)}
    TP, FP, FN = TP + tp, FP + fp, FN + fn
    print(f"{c:16s} {p:9.1%} {rc:7.1%} {f1:6.3f}")
P, R = TP / (TP + FP), TP / (TP + FN)
res["global"] = {"precision": round(P, 4), "recall": round(R, 4), "f1": round(2 * P * R / (P + R), 4),
                 "tp": TP, "fp": int(FP), "fn": int(FN), "items_esperados": len(esp), "items_extraidos": len(got)}
print(f"{'GLOBAL':16s} {P:9.1%} {R:7.1%} {res['global']['f1']:6.3f}")
(salida / "metricas_calidad.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
