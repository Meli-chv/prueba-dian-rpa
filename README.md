# RPA DIAN + OCR de facturas electrónicas

Prueba técnica — Analista de Innovación · **Anyi Melisa Chaparro Vargas**

## Mi enfoque

Trabajo en automatización dentro de un NOC, y ahí aprendí que un robot que funciona una vez no es suficiente: tiene que **decir qué pasó, avisar cuando algo falla y dejar evidencia para no tener que adivinar**. Por eso construí esta prueba como construyo mis automatizaciones en producción:

- **Escalación por capas**, igual que un incidente: primero lo automático, luego el apoyo externo y al final la persona. Así resuelvo el captcha.
- **Trazabilidad**: bitácora por CUFE, log de ejecución y captura + HTML de cada falla.
- **Observabilidad**: métricas en formato Prometheus para Grafana, resumen JSON y aviso a Teams/Slack.
- **Umbrales de calidad**: si el OCR no está seguro de una factura, no la entrega como buena; la marca para revisión humana.
- **Orquestación**: un flujo de n8n (incluido) la programa, lee el resultado y escala solo si hay algo que revisar.

```
config/cufes.txt ─► rpa_dian.py ─► output/pdfs/*.pdf (protegidos con NIT) ─► ocr_facturas.py ─► output/csv/*.csv
                      │ captcha_solver.py                                       │
                      └─► bitacora_rpa.csv + evidencias/                        └─► metricas_ocr.csv
                                          └──────────► observabilidad.py ◄──────┘
                                                        resumen_ejecucion.json · metricas.prom · Teams/Slack · exit code
```

## Instalación

Requisitos: Python 3.10+ y Tesseract OCR con el idioma español.

```bash
# Tesseract
#   Windows: https://github.com/UB-Mannheim/tesseract/wiki  (marcar "Spanish" en el instalador)
#   Ubuntu : sudo apt install tesseract-ocr tesseract-ocr-spa
#   macOS  : brew install tesseract tesseract-lang

python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env                                   # poner DIAN_NIT y, si se quiere, los webhooks
```

## Uso

```bash
python main.py todo --nit 900XXXXXX     # RPA + OCR + resumen + métricas + aviso
python main.py rpa  --nit 900XXXXXX     # solo Punto 1: descarga de los PDF
python main.py ocr  --nit 900XXXXXX     # solo Punto 2: OCR de los PDF descargados
```

> **El NIT es obligatorio**: el portal lo pide junto al CUFE (NIT del emisor o del receptor) y además es la contraseña del PDF que entrega la DIAN.

Otras opciones: `--cufes archivo.txt`, `--pdfs carpeta`, `--salida carpeta`, `--sin-aviso`.
Códigos de salida: `0` OK · `1` parcial (hay algo para revisar) · `2` falla. Sirven para que n8n, Zabbix o cron sepan cómo terminó sin leer el log.

## Punto 1 — RPA (`src/rpa_dian.py`, `src/captcha_solver.py`)

Flujo por CUFE, construido sobre el DOM real del portal:

1. Abre `https://catalogo-vpfe.dian.gov.co/User/SearchDocument` y resuelve el captcha si aparece.
2. Escribe el CUFE en `#DocumentKey`; al hacerlo aparece `#SearchDocumentNit` y escribe el NIT.
3. Presiona `button.search-document` y espera la navegación (el portal nunca llega a `networkidle`).
4. Resuelve el **segundo captcha** si aparece en la página de detalle.
5. Si dice “No se encontraron resultados”, lo registra como `no_encontrado` con evidencia y sigue con el siguiente.
6. Hace clic en **Descargar PDF**, acepta el modal de aviso de contraseña y guarda `output/pdfs/<CUFE>.pdf`. Si el clic abre el visor en vez de descargar, pide el archivo con la misma sesión del navegador.

Además: validación del CUFE (SHA-384 de 96 caracteres en hexadecimal), 3 reintentos con espera creciente, **pausa de 4 s entre consultas** para no saturar el servicio público, perfil de navegador persistente (las cookies de Cloudflare se reutilizan) y selectores agrupados al inicio del archivo, por si la DIAN cambia el HTML.

### Captcha: escalación en 3 capas

El portal usa **Cloudflare Turnstile**. El widget está en un iframe dentro de un *shadow DOM* cerrado: `querySelector` no lo ve, pero `page.frames` sí. Cuando valida, deja el token en `[name=cf-turnstile-response]`.

| Capa | Qué hace | Costo |
|---|---|---|
| 1. Automática | Espera a que Turnstile valide el navegador real. Si no lo hace, mueve el mouse de forma gradual y hace clic en la casilla (hasta 4 intentos). | Ninguno |
| 2. Servicio | Toma la `sitekey` (del atributo o de la URL del iframe), la envía con la URL a una API compatible con 2Captcha (`CAPTCHA_API_KEY`) e inyecta el token. | ≈ USD 1–3 por 1000 |
| 3. Persona | En ejecución interactiva, pide resolverlo en la ventana y el robot sigue solo apenas detecta el token. | Tiempo de una persona |

La bitácora registra qué capa resolvió cada captcha y cuánto tardó (por ejemplo, `clic+no_requerido` = buscador por clic, detalle sin captcha).

## Punto 2 — OCR (`src/ocr_facturas.py`)

1. **Desbloqueo** del PDF con el NIT (se prueba tal cual, sin dígito de verificación y solo con dígitos).
2. **PDF → imagen**: PNG de 300 dpi por página (`output/imagenes/`).
3. **Preprocesamiento (OpenCV)**: escala de grises, filtro de mediana, enderezado automático (Hough), umbral adaptativo (no pierde el texto sobre el fondo gris de la tabla) y borrado de bordes de tablas.
4. **OCR**: Tesseract 5 (LSTM, `spa`) con coordenadas por palabra; reconstruyo los renglones por posición.
5. **Encabezado**: expresiones regulares tolerantes a errores de OCR para Número de factura, Fecha de emisión (AAAA-MM-DD) y NIT del emisor, con segunda lectura de NIT y fecha usando solo dígitos.
6. **Tabla “Detalles de Productos”**: columnas a partir de las líneas verticales reales de la tabla (con respaldo por posición en el formato DIAN de 13 columnas), descripciones de varias líneas y segunda lectura de Código, Cantidad y Precio unitario con lista blanca de caracteres. Los números en formato colombiano (`1.234.567,89`) se convierten a decimal.
7. **Salida**: un DataFrame por factura → `output/csv/<factura>.csv` + `consolidado.csv`. Columnas: `archivo, numero_factura, fecha_emision, nit_emisor, codigo, descripcion, cantidad, precio_unitario`.

### Control de calidad

- **Exactitud medida, no supuesta**: el PDF de la DIAN trae capa de texto, así que corro el mismo parser sobre ella y la comparo celda por celda con el OCR (`exactitud_vs_capa_texto_pct`).
- **Umbrales de revisión** (`requiere_revision` en `metricas_ocr.csv`): encabezado incompleto, factura sin ítems, ítems con campos vacíos, confianza media menor a 80 o exactitud menor a 95 %. Estas facturas salen en el aviso para revisión humana.

## Observabilidad (`src/observabilidad.py`)

| Salida | Para qué |
|---|---|
| `output/resumen_ejecucion.json` | Estado consolidado (OK / PARCIAL / FALLA), conteo por estado, CUFE con problemas y facturas para revisar. Lo consume n8n. |
| `output/metricas.prom` | Métricas para el *textfile collector* de node-exporter → Prometheus → Grafana. |
| Teams / Slack | Resumen de la corrida con tarjeta adaptable (`TEAMS_WEBHOOK_URL`, `SLACK_WEBHOOK_URL`). Con `NOTIFICAR_SOLO_ALERTAS=1` solo avisa si algo salió mal. |
| `output/ejecucion.log`, `bitacora_rpa.csv`, `evidencias/` | Diagnóstico y trazabilidad. |

Consultas PromQL útiles para un tablero en Grafana:

```promql
dian_rpa_cufes_total{estado="descargado"} / ignoring(estado) sum(dian_rpa_cufes_total)   # tasa de éxito del RPA
dian_rpa_captcha_seg_promedio                                                            # tendencia del captcha
dian_ocr_exactitud_pct                                                                   # calidad del OCR
time() - dian_proceso_ultima_ejecucion_timestamp > 86400                                 # alerta: no corrió hoy
```

## Orquestación con n8n (`n8n/flujo_rpa_dian.json`)

Flujo importable: **Schedule (L-V 6:00 a.m.) → Execute Command (`main.py todo`) → Code (arma el resumen) → IF ¿estado ≠ OK? → alerta a Teams del NOC / resumen OK**. Requiere n8n con acceso al servidor donde está el proyecto y la variable `TEAMS_WEBHOOK_URL`.

## Métricas y tiempos

Pruebas con 5 facturas ficticias con la estructura de la DIAN (18 ítems, 72 celdas de tabla). Equipo: 2 vCPU, sin GPU.

| Escenario | Tiempo por factura | OCR por página | Confianza media | Encabezado | Celdas de la tabla |
|---|---|---|---|---|---|
| PDF digital (como el que entrega la DIAN) | 3,1 s | 1,0 s | 89,7 | 100 % | 100 % (72/72) |
| Escaneo simulado (200 dpi, ruido, JPEG 70 %, inclinación de 0,4°) | 4,6 s | 0,9 s | 88,5 | 100 % | 94 % (68/72) |

Desglose por factura digital: ~0,8 s de render y preprocesamiento, ~1,0 s de OCR y ~1,3 s de segundas lecturas de celdas (con `RELEER_CELDAS = False` baja a ~1,8 s, con menos exactitud en códigos).

**RPA**: el cuello de botella es el captcha. Con la simulación local del portal, cada CUFE tarda de 20 a 30 s. Contra el portal real hay que sumar su latencia y la pausa de cortesía de 4 s. Estimación para los 10 CUFE: **3 a 8 minutos** con captcha automático, o más si se escala al servicio o a una persona. Los tiempos reales quedan en `bitacora_rpa.csv`.

## Restricciones y limitaciones

- **Captcha**: la capa 1 depende de que Cloudflare considere humano el navegador. En modo sin ventana (`HEADLESS=1`), en servidores o en IPs de nube casi siempre hace falta la capa 2 o la 3.
- **NIT**: sin el NIT correcto el portal no devuelve el documento y el PDF no se puede abrir.
- **Volumen**: consultas en secuencia y con pausa. Paralelizar sube la tasa de captcha y el riesgo de bloqueo de la IP.
- **Cambios del portal**: si cambia el HTML, se ajustan los selectores del inicio de `rpa_dian.py`; la evidencia HTML muestra rápido qué cambió.
- **OCR**: pierde exactitud con escaneos de menos de 200 dpi, sellos o firmas sobre el texto y tablas sin bordes. El error típico son dígitos parecidos (`8/3`), que los umbrales de revisión detectan.
- **CPU**: 1–1,5 s de OCR por página. Para miles de facturas: procesar en paralelo (`ProcessPoolExecutor`) o leer directamente la capa de texto o el XML de la factura.

## Pruebas

```bash
python tests/generar_factura_muestra.py                     # 5 facturas ficticias + valores esperados
python main.py ocr --pdfs tests/muestras --salida output_pruebas --sin-aviso
python tests/evaluar.py output_pruebas                      # exactitud por campo
HEADLESS=1 python tests/test_rpa_mock.py                    # RPA + desbloqueo con NIT + OCR contra una simulación del portal
```

La simulación imita el portal real: Turnstile en iframe que solo valida con clic, campo de NIT que aparece al escribir el CUFE, modal “Aceptar” y PDF protegido.

## Estructura

```
├── main.py                       # punto de entrada (rpa | ocr | todo) y código de salida
├── config/cufes.txt              # los 10 CUFE de la prueba
├── src/
│   ├── rpa_dian.py               # navegación, búsqueda, descarga, reintentos, bitácora
│   ├── captcha_solver.py         # Turnstile: escalación en 3 capas
│   ├── ocr_facturas.py           # desbloqueo → imagen → OCR → DataFrame → CSV + calidad
│   └── observabilidad.py         # resumen, métricas Prometheus, avisos Teams/Slack
├── n8n/flujo_rpa_dian.json       # orquestación programada con alerta
├── tests/                        # facturas de muestra, evaluación y simulación del portal
├── requirements.txt
└── .env.example
```

> Uso responsable: el robot consulta un servicio público con pausas entre peticiones y solo descarga documentos cuyo CUFE y NIT se conocen, igual que lo haría una persona en el portal.
