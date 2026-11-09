"""Demuestra que el validador acepta los 10 CUFE de la prueba y rechaza CUFE dañados
(caracteres no hexadecimales, espacios internos, longitud distinta de 96).

    python tests/test_validacion.py
"""
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
import rpa_dian  # noqa: E402

pares = rpa_dian.leer_cufes(RAIZ / "config" / "cufes.txt")
bueno = pares[1][0]
danados = [
    ("con letra r (no hex)", bueno.replace("cfeac2a", "cfear2a")),
    ("con espacio interno", bueno[:50] + " " + bueno[50:]),
    ("de 95 caracteres", bueno[:-1]),
]
with tempfile.TemporaryDirectory() as tmp:
    filas = rpa_dian.validar_cufes(pares + [(c, "") for _, c in danados], Path(tmp))
for f in filas[:10]:
    assert f["valido"], f
for (motivo, _), f in zip(danados, filas[10:]):
    assert not f["valido"], f
    print(f"rechazado {motivo:24s} longitud={f['longitud']} caracteres_invalidos={f['caracteres_invalidos']!r}")
print("OK: 10/10 CUFE de la prueba válidos; 3/3 CUFE dañados rechazados")
