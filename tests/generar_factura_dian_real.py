"""
Réplica FICTICIA del formato real de la "Representación Gráfica" que entrega el
catálogo de la DIAN (Solución Gratuita DIAN), construida a partir de capturas de
facturas reales: franjas verdes por sección, letra pequeña en la tabla, encabezado
con la franja "IMPUESTOS", "$" separado del valor, código a veces vacío, sección
"Descuentos y Recargos Globales" después de la tabla y "Datos Totales" en la hoja 2.

    python tests/generar_factura_dian_real.py      -> tests/muestras_formato_dian/
"""
import csv
import random
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).parent / "muestras_formato_dian"
VERDE, VERDE_CLARO = colors.HexColor("#86E5BE"), colors.HexColor("#C8F3E1")
SERVICIOS = [
    ("", "Consulta De Primera Vez Por Medicina General"),
    ("85121701", "CONSULTA DE PRIMERA VEZ POR TRABAJO SOCIAL"),
    ("85121701", "VALORACION POR PSICOLOGO"),
    ("890201", "Consulta de control por enfermeria"),
    ("903841", "GLUCOSA EN SUERO U OTRO FLUIDO"),
    ("", "Terapia fisica integral"),
    ("990211", "Educacion grupal en salud por higiene oral"),
]

st_lab = ParagraphStyle("lab", fontName="Helvetica", fontSize=7.2, leading=10)
st_sec = ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=9.5, textColor=colors.HexColor("#555555"), leftIndent=14 * mm)
st_celda = ParagraphStyle("c", fontName="Helvetica", fontSize=5.6, leading=7, alignment=1)
st_enc = ParagraphStyle("e", fontName="Helvetica-Bold", fontSize=5.6, leading=7, alignment=1)


def cop(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def seccion(titulo):
    linea = Table([[""]], colWidths=[190 * mm], rowHeights=[1.2])
    linea.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), VERDE)]))
    return [Spacer(1, 6), Paragraph(titulo, st_sec), linea, Spacer(1, 4)]


def pares(filas):
    data = [[Paragraph(f"<b>{a}</b> {b}", st_lab), Paragraph(f"<b>{c}</b> {d}" if c else "", st_lab)] for a, b, c, d in filas]
    t = Table(data, colWidths=[84 * mm, 86 * mm], hAlign="RIGHT")
    t.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t


def precio(v):
    t = Table([["$", cop(v)]], colWidths=[3 * mm, 15 * mm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 5.6), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t


def factura(i, rnd):
    pref = rnd.choice(["HTFE", "FEHP", "FEV", "SETT"])
    numero = f"{pref}-{rnd.randint(1000, 999999)}"
    fecha = f"{rnd.randint(1, 28):02d}/{rnd.randint(1, 12):02d}/2024"
    nit = str(rnd.randint(800000000, 901999999))
    cufe = "".join(rnd.choice("0123456789abcdef") for _ in range(96))
    el = [Table([["DIAN", "FACTURA ELECTRÓNICA DE VENTA", "DIAN"]], colWidths=[40 * mm, 110 * mm, 40 * mm],
                style=[("FONTNAME", (1, 0), (1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 13),
                       ("ALIGN", (1, 0), (1, 0), "CENTER"), ("ALIGN", (2, 0), (2, 0), "RIGHT")]),
          Paragraph("<b>Representación Gráfica</b>", ParagraphStyle("t", fontSize=11, alignment=1, spaceAfter=6))]
    el += seccion("Datos del Documento")
    el += [Paragraph("<b>Código Único de Factura - CUFE :</b>", ParagraphStyle("x", parent=st_lab, leftIndent=20 * mm)),
           Paragraph(cufe, ParagraphStyle("y", parent=st_lab, leftIndent=20 * mm)),
           pares([("Número de Factura:", numero, "Forma de pago:", "Crédito"),
                  ("Fecha de Emisión:", fecha, "Medio de Pago:", "Acuerdo mutuo"),
                  ("Fecha de Vencimiento:", fecha, "Orden de pedido:", ""),
                  ("Tipo de Operación:", "10 - Estándar", "Fecha de orden de pedido:", "")])]
    el += seccion("Datos del Emisor / Vendedor")
    el += [pares([("Razón Social:", "E.S.E HOSPITAL FICTICIO DE PRUEBA", "", ""),
                  ("Nombre Comercial:", "E.S.E HOSPITAL FICTICIO DE PRUEBA", "", ""),
                  ("Nit del Emisor:", nit, "País:", "Colombia"),
                  ("Tipo de Contribuyente:", "Persona Jurídica", "Departamento:", "CUNDINAMARCA"),
                  ("Régimen Fiscal:", "O-23", "Municipio / Ciudad:", "PACHO"),
                  ("Responsabilidad tributaria:", "01 - IVA", "Dirección:", "Calle 9 No. 16 - 35"),
                  ("Actividad Económica:", "", "Correo:", "contacto@ficticio.gov.co")])]
    el += seccion("Datos del Adquiriente / Comprador")
    el += [pares([("Nombre o Razón Social:", "CLIENTE FICTICIO S.A.", "País:", "Colombia"),
                  ("Tipo de Documento:", "NIT", "Departamento:", "BOGOTÁ D.C."),
                  ("Número Documento:", "900000001", "Municipio / Ciudad:", "BOGOTÁ D.C.")])]
    el += seccion("Detalles de Productos")

    enc1 = [""] * 8 + [Paragraph("IMPUESTOS", st_enc), "", "", "", Paragraph("Precio unitario de venta", st_enc)]
    enc2 = [Paragraph(h, st_enc) for h in ["Nro.", "Código", "Descripción", "U/M", "Cantidad", "Precio unitario",
                                           "Descuento detalle", "Recargo detalle", "IVA", "%", "INC", "%"]] + [""]
    data, esperado = [enc1, enc2], []
    for n, (cod, desc) in enumerate(rnd.sample(SERVICIOS, rnd.randint(1, 4)), 1):
        cant = float(rnd.choice([1, 1, 1, 2, 3]))
        pu = float(rnd.choice([21800, 28800, 52000, 9500, 135000, 7850.5]))
        data.append([Paragraph(str(n), st_celda), Paragraph(cod, st_celda), Paragraph(desc, st_celda), Paragraph("94", st_celda),
                     Paragraph(cop(cant), st_celda), precio(pu), precio(0), precio(0), "", "", "", "", precio(pu)])
        esperado.append(dict(archivo=f"dian_formato_{i:02d}", numero_factura=numero,
                             fecha_emision="-".join(reversed(fecha.split("/"))), nit_emisor=nit,
                             codigo=cod, descripcion=desc, cantidad=cant, precio_unitario=pu))
    anchos = [8, 11, 26, 10, 13, 22, 21, 21, 13, 5, 13, 5, 21]
    t = Table(data, colWidths=[w * mm for w in anchos], repeatRows=2)
    t.setStyle(TableStyle([
        ("SPAN", (8, 0), (11, 0)), ("SPAN", (12, 0), (12, 1)), ("SPAN", (0, 0), (7, 0)),
        ("BACKGROUND", (0, 1), (11, 1), VERDE_CLARO), ("BACKGROUND", (8, 0), (12, 0), VERDE_CLARO),
        ("GRID", (0, 1), (-1, -1), 0.4, colors.HexColor("#555555")),
        ("BOX", (8, 0), (12, 0), 0.4, colors.HexColor("#555555")), ("LINEAFTER", (11, 0), (11, 0), 0.4, colors.HexColor("#555555")),
        ("BOX", (0, 1), (-1, -1), 1.2, VERDE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 0), (-1, -1), 5.6), ("LEFTPADDING", (0, 0), (-1, -1), 1), ("RIGHTPADDING", (0, 0), (-1, -1), 1),
    ]))
    el += [t, Spacer(1, 8)]
    el += seccion("Descuentos y Recargos Globales")
    g = Table([["Nro.", "Tipo", "Código", "Descripción", "%", "Valor"], ["1", "Descuento", "00", "Descuento por copagos", "0,00", "$ 0,00"]],
              colWidths=[15 * mm, 35 * mm, 20 * mm, 70 * mm, 20 * mm, 25 * mm])
    g.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 6.5), ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#EEEEEE"))]))
    el += [g, Spacer(1, 30), Paragraph("Hoja 1 de 2", ParagraphStyle("h", fontSize=9, alignment=2)), PageBreak()]
    el += seccion("Datos Totales")
    tot = sum(r["precio_unitario"] * r["cantidad"] for r in esperado)
    el += [Table([["Subtotal", cop(tot)], ["Total Bruto Factura", cop(tot)], ["Total factura (=) COP $", cop(tot)]],
                 colWidths=[60 * mm, 30 * mm], style=[("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 6.5)]),
           Spacer(1, 10), Paragraph("Numero de Autorización: 18764000000000 · PDF Generado por: Solución Gratuita DIAN", st_lab)]
    SimpleDocTemplate(str(OUT / f"dian_formato_{i:02d}.pdf"), pagesize=letter, leftMargin=12 * mm, rightMargin=12 * mm,
                      topMargin=12 * mm, title="Representación Gráfica Dian").build(el)
    return esperado


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    rnd = random.Random(1102)
    filas = [r for i in range(1, 7) for r in factura(i, rnd)]
    with (OUT / "esperado.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)
    print(f"{len(filas)} ítems en 6 facturas -> {OUT}")
