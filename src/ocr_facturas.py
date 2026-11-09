"""
OCR de la representación gráfica de facturas electrónicas DIAN.

Para cada PDF descargado:
  1. Exporta cada página a imagen PNG (300 dpi)            -> output/imagenes/
  2. Preprocesa la imagen (escala de grises + Otsu)
  3. Extrae el texto con Tesseract (idioma español) con coordenadas por palabra
  4. Reconstruye las filas visuales y extrae:
       - "Datos del Documento"        : Número de factura, Fecha de emisión
       - "Datos del Emisor/Vendedor"  : NIT del emisor
       - "Detalles de Productos"      : Código, Descripción, Cantidad, Precio unitario
  5. Arma un DataFrame por archivo y lo exporta a CSV       -> output/csv/<archivo>.csv
     y un consolidado de todas las facturas                 -> output/csv/consolidado.csv
  6. Registra tiempos por etapa y métricas de calidad      -> output/metricas_ocr.csv
                                                              output/resumen_metricas.json

Control de calidad: si el PDF trae capa de texto, se corre el MISMO parser
sobre esa capa y se compara celda a celda con lo que leyó el OCR. Así se
obtiene la exactitud del OCR sin necesidad de etiquetar datos a mano.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import cv2
import pymupdf as fitz
import numpy as np
import pandas as pd
import pytesseract

log = logging.getLogger("ocr")

DPI = 300
TESS_CONFIG = "--oem 1 --psm 3"
COLUMNAS = ["archivo", "numero_factura", "fecha_emision", "nit_emisor",
            "codigo", "descripcion", "cantidad", "precio_unitario"]
RELEER_CELDAS = True
PLANTILLA_DIAN = ["nro", "codigo", "descripcion", "u/m", "cantidad", "precio unitario", "descuento",
                  "recargo", "iva", "%", "inc", "%", "precio unitario de venta"]
WL_CODIGO = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_./"
WL_NUMERO = "0123456789.,"
UMBRAL_CONFIANZA = 80      # confianza media de Tesseract por debajo de esto -> revisión
UMBRAL_EXACTITUD = 95      # % de coincidencia con la capa de texto por debajo de esto -> revisión
FIN_TABLA = ("notas", "datos totales", "subtotal", "total bruto", "valor total",
             "moneda", "total factura", "observaciones", "informacion adicional")


# --------------------------------------------------------------------------- #
# Utilidades de texto
# --------------------------------------------------------------------------- #
def norm(s: str) -> str:
    """minúsculas sin tildes, para comparar etiquetas sin depender del OCR de acentos."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def a_numero(txt: str) -> float | None:
    """Convierte números en formato colombiano (1.234.567,89) o anglosajón."""
    if not txt:
        return None
    t = txt.replace("$", "").replace(" ", "").strip()
    t = t.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5"}))
    t = re.sub(r"[^\d.,\-]", "", t)
    if not re.search(r"\d", t):
        return None
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        mil = "." if dec == "," else ","
        t = t.replace(mil, "").replace(dec, ".")
    elif "," in t:
        t = t.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+", t) else t.replace(",", ".")
    elif "." in t and re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Fuentes de palabras (OCR o capa de texto) con el mismo formato
# --------------------------------------------------------------------------- #
@dataclass
class Palabra:
    texto: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 100.0

    @property
    def cx(self): return (self.x0 + self.x1) / 2

    @property
    def cy(self): return (self.y0 + self.y1) / 2


def angulo_inclinacion(binaria: np.ndarray) -> float:
    """Estima la inclinación de la página con las líneas horizontales largas
    (bordes de cajas y tablas) usando la transformada de Hough. Se calcula
    sobre la imagen reducida a la mitad para que sea rápido."""
    peq = cv2.resize(binaria, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    inv = cv2.threshold(255 - peq, 127, 255, cv2.THRESH_BINARY)[1]
    lineas = cv2.HoughLinesP(inv, 1, np.pi / 1800, threshold=200,
                             minLineLength=peq.shape[1] // 3, maxLineGap=10)
    if lineas is None:
        return 0.0
    angs = [np.degrees(np.arctan2(y2 - y1, x2 - x1)) for x1, y1, x2, y2 in lineas.reshape(-1, 4)]
    angs = [a for a in angs if abs(a) < 5]
    return float(np.median(angs)) if angs else 0.0


def preprocesar(img_bgr: np.ndarray) -> tuple[np.ndarray, float]:
    """Gris -> suavizado (ruido de escáner) -> enderezado -> umbral adaptativo.
    El umbral adaptativo (y no uno global) evita perder el texto sobre fondos
    sombreados, como el encabezado gris de la tabla de productos."""
    gris = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gris = cv2.medianBlur(gris, 3)
    otsu = cv2.threshold(gris, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    ang = angulo_inclinacion(otsu)
    if abs(ang) > 0.05:
        h, w = gris.shape
        M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
        gris = cv2.warpAffine(gris, M, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)
    binaria = cv2.adaptiveThreshold(gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 41, 15)
    return binaria, ang


def quitar_lineas(img_bin: np.ndarray) -> np.ndarray:
    """Borra los bordes de tablas/cajas: Tesseract los lee como '|' y '_'."""
    inv = 255 - img_bin
    h = cv2.morphologyEx(inv, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (60, 1)))
    v = cv2.morphologyEx(inv, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 45)))
    lineas = cv2.dilate(cv2.bitwise_or(h, v), np.ones((3, 3), np.uint8))
    return cv2.bitwise_or(img_bin, lineas)


def palabras_ocr(img_bin: np.ndarray) -> list[Palabra]:
    img_bin = quitar_lineas(img_bin)
    d = pytesseract.image_to_data(img_bin, lang="spa", config=TESS_CONFIG,
                                  output_type=pytesseract.Output.DICT)
    out = []
    for i, txt in enumerate(d["text"]):
        txt = (txt or "").strip()
        conf = float(d["conf"][i])
        if txt and conf >= 0 and not re.fullmatch(r"[|_\[\]{}!]+", txt):
            x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            out.append(Palabra(txt, x, y, x + w, y + h, conf))
    return out


def palabras_capa_texto(pagina: fitz.Page) -> list[Palabra]:
    k = DPI / 72  # puntos PDF -> píxeles de la imagen a 300 dpi
    return [Palabra(w[4], w[0] * k, w[1] * k, w[2] * k, w[3] * k)
            for w in pagina.get_text("words") if w[4].strip()]


# --------------------------------------------------------------------------- #
# Reconstrucción de filas visuales
# --------------------------------------------------------------------------- #
def agrupar_filas(palabras: list[Palabra]) -> list[list[Palabra]]:
    """Agrupa palabras en renglones por cercanía vertical (independiente de los
    bloques de Tesseract, que parten las columnas del formulario)."""
    if not palabras:
        return []
    alturas = sorted(p.y1 - p.y0 for p in palabras)
    tol = max(alturas[len(alturas) // 2] * 0.55, 4)
    filas: list[list[Palabra]] = []
    for p in sorted(palabras, key=lambda p: p.cy):
        if filas and abs(p.cy - np.mean([q.cy for q in filas[-1]])) <= tol:
            filas[-1].append(p)
        else:
            filas.append([p])
    return [sorted(f, key=lambda p: p.x0) for f in filas]


def texto_fila(fila: list[Palabra]) -> str:
    return " ".join(p.texto for p in fila)


# --------------------------------------------------------------------------- #
# Encabezado: número, fecha, NIT
# --------------------------------------------------------------------------- #
RE_NUMERO = re.compile(r"n\S{0,2}mero\s+de\s+factura\s*[:;]?\s*([A-Za-z0-9\-]+)", re.I)
RE_FECHA = re.compile(r"fecha\s+de\s+em\S{1,3}si\S{0,2}n\s*[:;]?\s*(\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2,4})", re.I)
RE_NIT = re.compile(r"nit\s+del\s+em\S{1,3}or\s*[:;]?\s*([\d][\d.\s\-]{5,20}\d)", re.I)


def extraer_encabezado(filas: list[list[Palabra]], img: np.ndarray | None = None,
                       releer: bool = False) -> dict:
    texto = "\n".join(texto_fila(f) for f in filas)
    plano = norm(texto)
    out = {"numero_factura": None, "fecha_emision": None, "nit_emisor": None}
    # Se busca sobre el texto normalizado pero se devuelve el valor original
    for campo, rx in (("numero_factura", RE_NUMERO), ("fecha_emision", RE_FECHA), ("nit_emisor", RE_NIT)):
        m = rx.search(plano)
        if m:
            out[campo] = texto[m.start(1):m.end(1)].strip()

    # Segunda lectura de los valores numéricos con lista blanca de dígitos
    if releer and img is not None:
        limpia = quitar_lineas(img)
        for campo, wl in (("nit_emisor", "0123456789.-"), ("fecha_emision", "0123456789/-")):
            val = out[campo]
            if not val:
                continue
            caja = next((p for f in filas for p in f if p.texto.startswith(val.split()[0][:6])), None)
            if caja:
                r = releer_celda(limpia, int(caja.x0) - 4, int(caja.y0) - 6, int(caja.x1) + 8, int(caja.y1) + 6, wl)
                if len(re.sub(r"\D", "", r)) == len(re.sub(r"\D", "", val)):
                    out[campo] = r

    if out["nit_emisor"]:
        out["nit_emisor"] = re.sub(r"\s", "", out["nit_emisor"])
    if out["fecha_emision"]:
        out["fecha_emision"] = normalizar_fecha(out["fecha_emision"])
    return out


def normalizar_fecha(f: str) -> str:
    partes = re.split(r"[/\-.]", f)
    if len(partes[0]) == 4:            # AAAA-MM-DD
        a, m, d = partes
    else:                              # DD-MM-AAAA (formato DIAN)
        d, m, a = partes
    return f"{int(a):04d}-{int(m):02d}-{int(d):02d}"


# --------------------------------------------------------------------------- #
# Tabla "Detalles de Productos"
# --------------------------------------------------------------------------- #
def lineas_verticales(img_bin: np.ndarray, y0: int, y1: int) -> list[int]:
    """Detecta los bordes verticales de la tabla con morfología (OpenCV)."""
    zona = 255 - img_bin[max(y0, 0):y1, :]
    alto = max((y1 - y0) // 3, 15)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, alto))
    vert = cv2.morphologyEx(zona, cv2.MORPH_OPEN, kernel)
    cols = np.where(vert.sum(axis=0) > 0)[0]
    if cols.size == 0:
        return []
    xs, inicio = [], cols[0]
    for a, b in zip(cols, cols[1:]):
        if b - a > 3:
            xs.append(int((inicio + a) / 2))
            inicio = b
    xs.append(int((inicio + cols[-1]) / 2))
    return xs


def _columnas_por_encabezado(enc: list[Palabra]) -> list[tuple[float, float]]:
    """Plan B sin bordes: agrupa las palabras del encabezado por huecos en X."""
    enc = sorted(enc, key=lambda p: p.x0)
    grupos = [[enc[0]]]
    for p in enc[1:]:
        if p.x0 - max(q.x1 for q in grupos[-1]) > 25:
            grupos.append([p])
        else:
            grupos[-1].append(p)
    centros = [(min(p.x0 for p in g) + max(p.x1 for p in g)) / 2 for g in grupos]
    lim = [-1e9] + [(a + b) / 2 for a, b in zip(centros, centros[1:])] + [1e9]
    return list(zip(lim, lim[1:]))


def releer_celda(img_bin: np.ndarray, x0: int, y0: int, x1: int, y1: int, wl: str) -> str:
    """Segunda lectura de una celda como renglón único (psm 7) con lista blanca
    alfanumérica: corrige confusiones típicas en códigos (k/K, O/0)."""
    celda = img_bin[max(y0, 0):y1, max(x0, 0):x1]
    if celda.size == 0:
        return ""
    celda = cv2.copyMakeBorder(celda, 15, 15, 15, 15, cv2.BORDER_CONSTANT, value=255)
    txt = pytesseract.image_to_string(celda, lang="spa", config=f"--oem 1 --psm 7 -c tessedit_char_whitelist={wl}")
    return txt.strip()


def extraer_tabla(filas: list[list[Palabra]], img_bin: np.ndarray | None,
                  releer: bool = False) -> list[dict]:
    # 1) ubicar el encabezado de la tabla
    claves = ("nro", "codigo", "descrip", "cantidad", "precio", "unitario", "u/m", "descuento", "recargo")
    i_enc = next((i for i, f in enumerate(filas)
                  if sum(k in norm(texto_fila(f)) for k in claves) >= 3), None)
    if i_enc is None:
        return []
    alto = np.median([p.y1 - p.y0 for p in filas[i_enc]])
    y_enc = np.mean([p.cy for p in filas[i_enc]])
    # encabezados partidos en 2-3 renglones ("Precio / unitario")
    enc = [p for f in filas for p in f if abs(np.mean([q.cy for q in f]) - y_enc) <= alto * 2.6]
    y_fin_enc = max(p.y1 for p in enc)

    # 2) fin de la tabla
    cuerpo = []
    for f in filas:
        if np.mean([p.cy for p in f]) <= y_fin_enc:
            continue
        if any(norm(texto_fila(f)).startswith(k) or k in norm(texto_fila(f))[:25] for k in FIN_TABLA):
            break
        cuerpo.append(f)
    if not cuerpo:
        return []
    y_fin = max(p.y1 for p in cuerpo[-1])

    # 3) límites de columnas: bordes de la tabla, o huecos del encabezado
    intervalos: list[tuple[float, float]] = []
    if img_bin is not None:
        xs = lineas_verticales(img_bin, int(min(p.y0 for p in enc) - alto), int(y_fin + alto))
        if len(xs) >= 5:
            intervalos = list(zip(xs, xs[1:]))
    if not intervalos:
        intervalos = _columnas_por_encabezado(enc)

    def col_de(p: Palabra) -> int | None:
        for j, (a, b) in enumerate(intervalos):
            if a <= p.cx < b:
                return j
        return None

    nombres = [""] * len(intervalos)
    for p in sorted(enc, key=lambda p: (round(p.cy / alto), p.x0)):
        j = col_de(p)
        if j is not None:
            nombres[j] = (nombres[j] + " " + norm(p.texto)).strip()

    def buscar(pred) -> int | None:
        return next((j for j, n in enumerate(nombres) if pred(n)), None)

    if len(intervalos) == len(PLANTILLA_DIAN):
        # 13 columnas = formato estándar DIAN: si el OCR dañó un título, se usa la posición
        nombres = [n or PLANTILLA_DIAN[j] for j, n in enumerate(nombres)]
        for j, clave in ((1, "codigo"), (2, "descrip"), (4, "cant"), (5, "precio")):
            if not any(clave in n for n in nombres):
                nombres[j] = PLANTILLA_DIAN[j]

    c_nro = buscar(lambda n: n.startswith("nro") or n.startswith("no") or n.startswith("#") or n == "item")
    c_cod = buscar(lambda n: "codigo" in n)
    c_des = buscar(lambda n: "descrip" in n)
    c_can = buscar(lambda n: "cant" in n)
    c_pre = buscar(lambda n: "precio" in n and "venta" not in n)
    if c_pre is None:
        c_pre = buscar(lambda n: "precio" in n)
    if None in (c_cod, c_des, c_can, c_pre):
        log.warning("Encabezado de tabla incompleto: %s", nombres)
        return []

    # 4) recorrer renglones; un renglón con Nro (o código+cantidad) abre ítem,
    #    los demás continúan la descripción del ítem anterior
    items: list[dict] = []
    limpia = quitar_lineas(img_bin) if (img_bin is not None and releer) else None
    for f in cuerpo:
        celdas: dict[int, list[str]] = {}
        for p in f:
            j = col_de(p)
            if j is not None:
                celdas.setdefault(j, []).append(p.texto)
        get = lambda j: " ".join(celdas.get(j, [])) if j is not None else ""
        abre = (c_nro is not None and re.fullmatch(r"\d{1,4}", get(c_nro).strip() or "x")) \
            or (c_nro is None and get(c_cod) and get(c_can))
        if abre:
            if limpia is not None and releer:
                for cj, wl in ((c_cod, WL_CODIGO), (c_can, WL_NUMERO), (c_pre, WL_NUMERO)):
                    cajas = [p for p in f if col_de(p) == cj]
                    if not cajas:
                        continue
                    a, b = intervalos[cj]
                    y0, y1 = min(p.y0 for p in cajas), max(p.y1 for p in cajas)
                    releido = releer_celda(limpia, int(a) + 3, int(y0) - 6, int(b) - 3, int(y1) + 6, wl)
                    if releido and (wl is WL_CODIGO or a_numero(releido) is not None):
                        celdas[cj] = [releido]
            items.append({"codigo": get(c_cod).strip(), "descripcion": get(c_des).strip(),
                          "cantidad": a_numero(get(c_can)), "precio_unitario": a_numero(get(c_pre))})
        elif items and get(c_des):
            items[-1]["descripcion"] = (items[-1]["descripcion"] + " " + get(c_des)).strip()
            if not items[-1]["codigo"] and get(c_cod):
                items[-1]["codigo"] = get(c_cod).strip()
    return items


# --------------------------------------------------------------------------- #
# Proceso por archivo
# --------------------------------------------------------------------------- #
def analizar(paginas: list[tuple[list[Palabra], np.ndarray | None]],
             releer: bool = RELEER_CELDAS) -> tuple[dict, list[dict]]:
    enc: dict = {}
    items: list[dict] = []
    for palabras, img in paginas:
        filas = agrupar_filas(palabras)
        e = extraer_encabezado(filas, img, releer)
        for k, v in e.items():
            enc.setdefault(k, None)
            enc[k] = enc[k] or v
        items += extraer_tabla(filas, img, releer)
    return enc, items


def a_dataframe(nombre: str, enc: dict, items: list[dict]) -> pd.DataFrame:
    filas = [{"archivo": nombre, **enc, **it} for it in items] or [{"archivo": nombre, **enc}]
    return pd.DataFrame(filas).reindex(columns=COLUMNAS)


def comparar(df_ocr: pd.DataFrame, df_ref: pd.DataFrame) -> float | None:
    """% de celdas iguales entre OCR y capa de texto (referencia)."""
    if df_ref.empty or df_ref["codigo"].isna().all():
        return None
    cols = COLUMNAS[1:]
    n = max(len(df_ocr), len(df_ref))
    a = df_ocr.reindex(range(n))[cols].astype(str)
    b = df_ref.reindex(range(n))[cols].astype(str)
    return round(float((a.values == b.values).mean() * 100), 2)


def abrir_pdf(pdf: Path, nit: str = "") -> fitz.Document:
    """Abre el PDF; la DIAN lo entrega protegido con el NIT como contraseña.
    Se prueba el NIT tal cual, sin dígito de verificación y solo con dígitos."""
    doc = fitz.open(pdf)
    if doc.needs_pass:
        base = nit.strip()
        candidatos = dict.fromkeys([base, base.split("-")[0], re.sub(r"\D", "", base),
                                    re.sub(r"\D", "", base)[:-1]])
        if not any(c and doc.authenticate(c) for c in candidatos):
            raise PermissionError("PDF protegido: el NIT no lo desbloquea (revisa --nit / DIAN_NIT)")
    return doc


def requiere_revision(m: dict) -> str:
    """Reglas de calidad para escalar a revisión humana (como umbrales de alerta)."""
    motivos = []
    if m.get("campos_encabezado_ok", 0) < 3:
        motivos.append("encabezado incompleto")
    if m.get("items_extraidos", 0) == 0:
        motivos.append("sin ítems")
    if m.get("items_con_vacios", 0) > 0:
        motivos.append("ítems con campos vacíos")
    if m.get("confianza_media", 100) < UMBRAL_CONFIANZA:
        motivos.append(f"confianza < {UMBRAL_CONFIANZA}")
    ex = m.get("exactitud_vs_capa_texto_pct")
    if ex is not None and ex < UMBRAL_EXACTITUD:
        motivos.append(f"exactitud < {UMBRAL_EXACTITUD}%")
    return "; ".join(motivos)


def procesar_pdf(pdf: Path, dir_img: Path, dir_csv: Path, nit: str = "") -> tuple[pd.DataFrame, dict]:
    m = {"archivo": pdf.name}
    t0 = time.perf_counter()
    doc = abrir_pdf(pdf, nit)
    m["paginas"] = doc.page_count

    paginas_ocr, paginas_ref, confs, angulos = [], [], [], []
    t_render = t_ocr = 0.0
    for n, pag in enumerate(doc, 1):
        a = time.perf_counter()
        pix = pag.get_pixmap(dpi=DPI)
        ruta = dir_img / f"{pdf.stem}_p{n}.png"
        pix.save(ruta)                                     # imagen exportada
        img = cv2.imread(str(ruta))
        binaria, ang = preprocesar(img)
        angulos.append(ang)
        t_render += time.perf_counter() - a

        a = time.perf_counter()
        pals = palabras_ocr(binaria)
        t_ocr += time.perf_counter() - a
        confs += [p.conf for p in pals]
        paginas_ocr.append((pals, binaria))
        ref = palabras_capa_texto(pag)
        if ref:
            paginas_ref.append((ref, binaria))

    a = time.perf_counter()
    enc, items = analizar(paginas_ocr)
    df = a_dataframe(pdf.stem, enc, items)
    t_parse = time.perf_counter() - a

    df.to_csv(dir_csv / f"{pdf.stem}.csv", index=False, encoding="utf-8-sig")

    exactitud = None
    if paginas_ref:
        enc_r, items_r = analizar(paginas_ref, releer=False)  # capa de texto: no requiere relectura
        exactitud = comparar(df, a_dataframe(pdf.stem, enc_r, items_r))

    m.update({
        "seg_render": round(t_render, 3), "seg_ocr": round(t_ocr, 3),
        "seg_parseo": round(t_parse, 3), "seg_total": round(time.perf_counter() - t0, 3),
        "inclinacion_corregida_grados": round(max(angulos, key=abs), 2) if angulos else 0,
        "palabras_ocr": len(confs),
        "confianza_media": round(float(np.mean(confs)), 2) if confs else 0,
        "pct_palabras_conf_bajo_60": round(float(np.mean(np.array(confs) < 60) * 100), 2) if confs else 0,
        "campos_encabezado_ok": sum(v is not None for v in enc.values()),
        "items_extraidos": len(items),
        "items_con_vacios": int(sum(any(it[k] in (None, "") for k in it) for it in items)),
        "exactitud_vs_capa_texto_pct": exactitud,
    })
    m["requiere_revision"] = requiere_revision(m)
    return df, m


def ejecutar(dir_pdf: Path, salida: Path, nit: str = "") -> pd.DataFrame:
    dir_img, dir_csv = salida / "imagenes", salida / "csv"
    dir_img.mkdir(parents=True, exist_ok=True)
    dir_csv.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(dir_pdf.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"No hay PDFs en {dir_pdf}")

    t0 = time.perf_counter()
    dfs, metricas = [], []
    for pdf in pdfs:
        try:
            df, m = procesar_pdf(pdf, dir_img, dir_csv, nit)
            m["estado"] = "ok"
            dfs.append(df)
        except Exception as e:
            log.exception("Falló %s", pdf.name)
            m = {"archivo": pdf.name, "estado": f"error: {e}"}
        metricas.append(m)
        log.info("%s -> %s", pdf.name, m)

    consolidado = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame(columns=COLUMNAS)
    consolidado.to_csv(dir_csv / "consolidado.csv", index=False, encoding="utf-8-sig")
    met = pd.DataFrame(metricas)
    met.to_csv(salida / "metricas_ocr.csv", index=False, encoding="utf-8-sig")

    ok = met[met["estado"] == "ok"]
    resumen = {
        "archivos": len(pdfs), "procesados_ok": int(len(ok)),
        "tiempo_total_seg": round(time.perf_counter() - t0, 2),
        "tiempo_promedio_por_factura_seg": round(float(ok["seg_total"].mean()), 2) if len(ok) else None,
        "tiempo_promedio_ocr_por_pagina_seg": round(float(ok["seg_ocr"].sum() / ok["paginas"].sum()), 2) if len(ok) else None,
        "confianza_media_ocr": round(float(ok["confianza_media"].mean()), 2) if len(ok) else None,
        "items_totales": int(ok["items_extraidos"].sum()) if len(ok) else 0,
        "exactitud_media_vs_capa_texto_pct": (round(float(ok["exactitud_vs_capa_texto_pct"].dropna().mean()), 2)
                                              if len(ok) and ok["exactitud_vs_capa_texto_pct"].notna().any() else None),
        "facturas_para_revision": int((ok["requiere_revision"] != "").sum()) if len(ok) else 0,
        "dpi": DPI, "motor": f"Tesseract {pytesseract.get_tesseract_version()} (spa) {TESS_CONFIG}",
    }
    (salida / "resumen_metricas.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Resumen: %s", resumen)
    return consolidado
