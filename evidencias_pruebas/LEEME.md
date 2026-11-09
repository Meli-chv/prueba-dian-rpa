# Evidencias de pruebas

> **Todo lo de esta carpeta se generó con datos FICTICIOS o con una SIMULACIÓN del portal.**
> Es evidencia complementaria de que el código corre y de cómo se miden los resultados.
> **No reemplaza** la ejecución contra el portal real de la DIAN, que queda en `output/` al correr
> `python main.py todo` con el NIT (ver README → "Evidencia contra el portal real").

| Carpeta | Qué demuestra | Cómo se reproduce |
|---|---|---|
| `0_validacion_cufes/` | Los 10 CUFE de la prueba tienen formato válido (96 caracteres hex) y el validador **rechaza** CUFE dañados (letra no hex, espacio interno, longitud 95). | `python main.py validar` · `python tests/test_validacion.py` |
| `1_simulacion_portal/` | Flujo completo del RPA contra una **simulación local** que imita el portal: Turnstile con clic, NIT, modal «Aceptar», PDF protegido; luego desbloqueo + OCR. Incluye bitácora, evidencia del caso "no encontrado", resumen y `metricas.prom`. | `HEADLESS=1 python tests/test_rpa_mock.py --conservar <carpeta>` |
| `2_ocr_pdf_digital/` | OCR sobre 5 facturas ficticias con la estructura DIAN (PDF digital). CSV por factura + consolidado, métricas por archivo, precisión/recall/F1. | `python main.py ocr --pdfs tests/muestras --salida <carpeta>` + `python tests/evaluar.py <carpeta>` |
| `3_ocr_escaneo_simulado/` | Las mismas facturas degradadas como escaneo (200 dpi, ruido, JPEG 70 %, 0,4° de inclinación). Muestra los errores reales del OCR y cómo el umbral de calidad marca para revisión la factura afectada. | `python main.py ocr --pdfs tests/muestras_escaneadas --salida <carpeta>` + `python tests/evaluar.py <carpeta>` |
| `4_ocr_formato_dian_replica/` | Réplica ficticia del formato **real** de la DIAN (letra de 5-6 pt, franja «IMPUESTOS», «$» separado, código vacío, sección «Descuentos y Recargos Globales», hoja 2 de totales), construida a partir de facturas reales descargadas por el RPA. F1 = 1,000. | `python tests/generar_factura_dian_real.py` + `python main.py ocr --pdfs tests/muestras_formato_dian --salida <carpeta>` + `python tests/evaluar.py <carpeta> tests/muestras_formato_dian/esperado.csv` |

Las carpetas `imagenes/` conservan una sola imagen de ejemplo para no inflar el repositorio.
Equipo de las mediciones: 2 vCPU, sin GPU; los tiempos varían según el equipo.
