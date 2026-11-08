# RPA DIAN + OCR de facturas electrónicas

Prueba técnica — Analista de Innovación.

1. **RPA**: entra al catálogo de la DIAN, consulta cada CUFE, resuelve el captcha y descarga el PDF de la representación gráfica de la factura.
2. **OCR**: convierte cada PDF en imagen, extrae el texto con Tesseract, arma un DataFrame por factura y lo exporta a CSV, con medición de tiempos y calidad.

```
config/cufes.txt ──► rpa_dian.py ──► output/pdfs/*.pdf ──► ocr_facturas.py ──► output/imagenes/*.png
                       │  captcha_solver.py                     │                 output/csv/<factura>.csv
                       └─► output/bitacora_rpa.csv              └───────────────► output/csv/consolidado.csv
                                                                                  output/metricas_ocr.csv
                                                                                  output/resumen_metricas.json
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
cp .env.example .env                                   # opcional: API key del servicio de captcha
```

## Uso

```bash
python main.py rpa     # Punto 1: descarga los PDF de los 10 CUFE   -> output/pdfs/
python main.py ocr     # Punto 2: OCR de los PDF descargados        -> output/csv/
python main.py todo    # ambos en secuencia
```

Opciones: `--cufes archivo.txt` (otra lista), `--pdfs carpeta` (OCR sobre otra carpeta), `--salida carpeta`.

## Punto 1 — RPA (`src/rpa_dian.py`, `src/captcha_solver.py`)

- **Playwright + Chromium** con perfil persistente, idioma `es-CO` y zona horaria de Bogotá. Las cookies de Cloudflare se reutilizan entre consultas, así que después de la primera validación el captcha casi siempre pasa solo.
- **Validación del CUFE** antes de consultar: debe ser un SHA-384 en hexadecimal (96 caracteres).
- **Selectores con alternativas** (`#DocumentKey`, `input[name=DocumentKey]`, placeholder “CUFE”…), porque el portal cambia su HTML.
- **Descarga**: primero por el evento de descarga del navegador; si el PDF se abre en el visor, se baja con la misma sesión (`context.request`). Se comprueba que el archivo empiece por `%PDF`.
- **Reintentos** (3 por CUFE, con espera creciente) y **pausa de 4 s entre consultas** para no saturar un servicio público.
- **Evidencia**: si una consulta falla, se guardan una captura de pantalla y el HTML en `output/evidencias/`.
- **Bitácora** `output/bitacora_rpa.csv`: estado (`descargado`, `no_encontrado`, `captcha_fallido`, `error`, `cufe_invalido`), intentos, método de captcha, segundos de captcha y segundos totales por CUFE.

### Cómo se resuelve el captcha

`captcha_solver.py` detecta el tipo de captcha leyendo el DOM (Cloudflare Turnstile, reCAPTCHA v2 o v3) y obtiene su `sitekey`. Luego prueba tres capas, de la más barata a la más costosa:

| Capa | Cómo funciona | Cuándo aplica |
|---|---|---|
| 1. Automática | Turnstile en modo *managed* se valida solo en un navegador real. El robot espera hasta 25 s a que el widget deje el token en el campo oculto `cf-turnstile-response`. | Siempre es el primer intento. Sin costo. |
| 2. Servicio externo | Envía `sitekey` + URL a una API compatible con 2Captcha (`CAPTCHA_API_KEY` en `.env`), espera el token, lo inyecta en el formulario y ejecuta el *callback* del widget. | Si la capa 1 no pasa (por ejemplo en modo sin ventana). |
| 3. Persona en el ciclo | Si la ejecución es interactiva, pide resolverlo en la ventana y el robot continúa solo en cuanto detecta el token. | Último recurso. |

El método que funcionó y lo que tardó quedan en la bitácora de cada CUFE.

## Punto 2 — OCR (`src/ocr_facturas.py`)

1. **PDF → imagen**: cada página a PNG de 300 dpi con PyMuPDF (`output/imagenes/`).
2. **Preprocesamiento (OpenCV)**: escala de grises, filtro de mediana (ruido de escáner), **enderezado** automático (ángulo estimado con la transformada de Hough sobre los bordes de las cajas) y **umbral adaptativo**, que no pierde el texto sobre el fondo gris del encabezado de la tabla.
3. **Borrado de bordes** de tablas y cajas antes del OCR (Tesseract los lee como `|`).
4. **OCR** con Tesseract 5 (LSTM, idioma `spa`), con coordenadas por palabra.
5. **Reconstrucción de renglones** por posición vertical, independiente de los bloques que arma Tesseract.
6. **Encabezado** (expresiones regulares tolerantes a errores de OCR en tildes): `Número de Factura`, `Fecha de Emisión` (normalizada a AAAA-MM-DD) y `Nit del Emisor`.
7. **Tabla “Detalles de Productos”**:
   - Las columnas salen de las **líneas verticales reales de la tabla** (morfología OpenCV). Si la tabla no tiene bordes, se infieren de los huecos entre títulos.
   - Si se detectan las 13 columnas del formato DIAN y el OCR dañó un título, la columna se identifica por su posición.
   - Un renglón con número en `Nro.` abre un ítem; los siguientes renglones completan la descripción (descripciones de varias líneas).
   - **Segunda lectura** de las celdas Código, Cantidad y Precio unitario (y del NIT y la fecha) como renglón único, solo con caracteres permitidos (alfanuméricos o dígitos). Esto corrige confusiones típicas como `k/K`, `O/0` o `S/5`.
   - Números en formato colombiano (`1.234.567,89`) convertidos a decimal.
8. **Salida**: un DataFrame por archivo → `output/csv/<factura>.csv` y un `consolidado.csv` (UTF-8 con BOM, abre bien en Excel). Columnas: `archivo, numero_factura, fecha_emision, nit_emisor, codigo, descripcion, cantidad, precio_unitario`.

### Control de calidad automático

Si el PDF trae capa de texto (los de la DIAN la traen), se ejecuta **el mismo parser** sobre esa capa y se compara celda por celda con lo que leyó el OCR. El resultado queda en `exactitud_vs_capa_texto_pct`, así que la exactitud del OCR se mide sin etiquetar datos a mano.

## Métricas y tiempos

Cada ejecución escribe `output/metricas_ocr.csv` (por archivo) y `output/resumen_metricas.json`. Para cada archivo registra: segundos de render, OCR y parseo, inclinación corregida, número de palabras, confianza media de Tesseract, % de palabras con confianza menor a 60, campos de encabezado encontrados, ítems extraídos, ítems con campos vacíos y exactitud frente a la capa de texto.

Resultados de referencia con 5 facturas de prueba con la estructura DIAN (18 ítems, 126 celdas). Equipo: 2 vCPU, sin GPU.

| Escenario | Tiempo por factura | OCR por página | Confianza media | Encabezado | Celdas de la tabla |
|---|---|---|---|---|---|
| PDF digital (como el que entrega la DIAN) | 3,1 s | 1,0 s | 89,7 | 100 % | 100 % (72/72) |
| Escaneo simulado (200 dpi, ruido, JPEG 70 %, inclinación de 0,4°) | 4,6 s | 0,9 s | 88,5 | 100 % | 94 % (68/72) |

De los 3,1 s, unos 0,8 s son de render y preprocesamiento, 1,0 s de OCR de la página y 1,3 s de segundas lecturas de celdas (opcional: `RELEER_CELDAS = False` lo baja a unos 1,8 s, con algo menos de exactitud en códigos).

**RPA**: la etapa que más tarda por CUFE es el captcha. En la simulación local, cada consulta completa tomó unos 4 s (2 s de captcha automático). Contra el portal real hay que sumar la latencia de la DIAN, la pausa de cortesía de 4 s y, si se usa, el servicio de captcha (normalmente entre 10 y 40 s). Una estimación prudente para los 10 CUFE es de 2 a 8 minutos.

## Restricciones y limitaciones

- **Captcha**: la capa 1 depende de que Cloudflare considere “humano” al navegador. En modo sin ventana (`HEADLESS=1`), en servidores o en IPs de nube es muy probable que haga falta la capa 2 (servicio pago, aprox. USD 1–3 por cada 1000 captchas) o la capa 3.
- **Volumen**: se consulta en secuencia, con pausa entre consultas, para no saturar el portal. Paralelizar aumentaría la tasa de captcha y el riesgo de bloqueo de la IP.
- **Cambios del portal**: si la DIAN cambia el HTML, hay que ajustar los selectores (están agrupados al inicio de `rpa_dian.py`). La evidencia HTML de los errores ayuda a ubicar el cambio rápido.
- **CUFE inexistentes**: los datos de la prueba son ficticios; si la DIAN no encuentra un CUFE, queda como `no_encontrado` con evidencia y el proceso sigue con el siguiente.
- **OCR**: Tesseract pierde exactitud con escaneos de baja resolución (menos de 200 dpi), sellos o firmas sobre el texto y tablas sin bordes. Los dígitos parecidos (`8/3`, `0/6`) son el error más frecuente en escaneos degradados. Por eso se reporta la confianza por archivo y se marcan los ítems con campos vacíos para revisión humana.
- **Tiempo de CPU**: el OCR va de 1 a 1,5 s por página. Para miles de facturas conviene procesar en paralelo (`ProcessPoolExecutor`) o leer directamente la capa de texto/XML cuando exista.
- **Formato**: el parser está ajustado a la representación gráfica estándar de la DIAN (13 columnas). Otros formatos de proveedores pueden requerir ajustar las palabras clave del encabezado.

## Pruebas

```bash
python tests/generar_factura_muestra.py                    # 5 facturas ficticias + valores esperados
python main.py ocr --pdfs tests/muestras --salida output_pruebas
python tests/evaluar.py output_pruebas                     # exactitud por campo
HEADLESS=1 python tests/test_rpa_mock.py                   # flujo RPA contra una simulación local del portal
```

## Estructura

```
├── main.py                       # punto de entrada (rpa | ocr | todo)
├── config/cufes.txt              # los 10 CUFE de la prueba
├── src/
│   ├── rpa_dian.py               # navegación, búsqueda, descarga, reintentos, bitácora
│   ├── captcha_solver.py         # detección y resolución de captcha en 3 capas
│   └── ocr_facturas.py           # PDF→imagen→OCR→DataFrame→CSV + métricas
├── tests/
│   ├── generar_factura_muestra.py
│   ├── evaluar.py
│   └── test_rpa_mock.py
├── requirements.txt
└── .env.example
```

> Uso responsable: el robot consulta un servicio público de la DIAN con pausas entre peticiones y solo descarga documentos cuyo CUFE se conoce, igual que lo haría una persona en el portal.
