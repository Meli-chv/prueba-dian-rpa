"""
Genera PDFs FICTICIOS con la misma estructura de la representación gráfica
de la DIAN, para probar el OCR sin depender del portal. También deja el
valor esperado (ground truth) en tests/muestras/esperado.csv.

    python tests/generar_factura_muestra.py
    python main.py ocr --pdfs tests/muestras --salida output_pruebas
    python tests/evaluar.py output_pruebas
"""
import csv
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).parent / "muestras"
PRODUCTOS = [
    ("PRD-001", "Servicio de soporte técnico mensual nivel 2 para infraestructura"),
    ("SKU4520", "Licencia anual de software de monitoreo"),
    ("MAT-77", "Cable UTP categoría 6 x 305 m"),
    ("SRV-1002", "Mantenimiento preventivo de servidores en sitio con reporte"),
    ("A-3301", "Switch administrable 24 puertos"),
    ("HW-889", "Disco SSD 1 TB empresarial"),
    ("CONS-15", "Consultoría en automatización de procesos (horas)"),
]


def cop(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def factura(i: int, rnd: random.Random):
    st = getSampleStyleSheet()
    peq = st["BodyText"].clone("peq", fontSize=7.5, leading=9)
    tit = st["Heading4"].clone("tit", fontSize=9, spaceAfter=2)
    numero = f"FEV{rnd.randint(1000, 99999)}"
    fecha = f"{rnd.randint(1, 28):02d}-{rnd.randint(1, 12):02d}-2025"
    nit = f"{rnd.randint(800000000, 901999999)}"
    cufe = "".join(rnd.choice("0123456789abcdef") for _ in range(96))

    def caja(titulo, pares):
        filas = [[Paragraph(f"<b>{a}:</b> {b}", peq), Paragraph(f"<b>{c}:</b> {d}", peq)] for a, b, c, d in pares]
        t = Table(filas, colWidths=[95 * mm, 95 * mm])
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, colors.black)]))
        return [Paragraph(titulo, tit), t, Spacer(1, 4)]

    el = [Paragraph("<b>Factura Electrónica de Venta</b>", st["Title"])]
    el += caja("Datos del Documento", [
        ("Código Único de Factura - CUFE", cufe[:48], "Tipo de Operación", "10 - Estándar"),
        ("Número de Factura", numero, "Forma de pago", "Contado"),
        ("Fecha de Emisión", fecha, "Medio de Pago", "Transferencia"),
    ])
    el += caja("Datos del Emisor / Vendedor", [
        ("Razón Social", "Empresa Ficticia de Pruebas S.A.S.", "Nombre Comercial", "Ficticia"),
        ("Nit del Emisor", nit, "Tipo de Contribuyente", "Persona Jurídica"),
        ("Municipio / Ciudad", "Bogotá D.C.", "Dirección", "Calle 1 # 2 - 3"),
    ])
    el += caja("Datos del Adquiriente / Comprador", [
        ("Nombre o Razón Social", "Cliente de Ejemplo Ltda.", "Número Documento", "830000000"),
    ])

    enc = ["Nro.", "Código", "Descripción", "U/M", "Cantidad", "Precio unitario",
           "Descuento", "Recargo", "IVA", "%", "INC", "%", "Precio unitario de venta"]
    data = [[Paragraph(f"<b>{h}</b>", peq) for h in enc]]
    esperado = []
    for n, (cod, desc) in enumerate(rnd.sample(PRODUCTOS, rnd.randint(2, 5)), 1):
        cant = rnd.choice([1, 2, 3, 5, 10, 12, 24])
        precio = round(rnd.uniform(15000, 4800000), 2)
        iva = round(precio * cant * 0.19, 2)
        data.append([str(n), cod, Paragraph(desc, peq), "94", cop(cant), cop(precio),
                     "0,00", "0,00", cop(iva), "19,00", "0,00", "0,00", cop(precio * 1.19)])
        esperado.append(dict(archivo=f"factura_prueba_{i:02d}", numero_factura=numero,
                             fecha_emision="-".join(reversed(fecha.split("-"))), nit_emisor=nit,
                             codigo=cod, descripcion=desc, cantidad=float(cant), precio_unitario=precio))
    anchos = [9, 17, 40, 9, 15, 20, 15, 13, 17, 9, 9, 7, 20]
    t = Table(data, colWidths=[w * mm for w in anchos], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                           ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6e6e6"))]))
    el += [Paragraph("Detalles de Productos", tit), t, Spacer(1, 6),
           Paragraph("<b>Notas Finales</b>", tit), Paragraph("Documento ficticio para pruebas.", peq)]

    SimpleDocTemplate(str(OUT / f"factura_prueba_{i:02d}.pdf"), pagesize=letter,
                      leftMargin=8 * mm, rightMargin=8 * mm, topMargin=10 * mm).build(el)
    return esperado


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    rnd = random.Random(2026)
    filas = [r for i in range(1, 6) for r in factura(i, rnd)]
    with (OUT / "esperado.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)
    print(f"{len(filas)} ítems en 5 facturas -> {OUT}")
