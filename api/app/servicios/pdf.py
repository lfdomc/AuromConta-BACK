"""PDFs: boleta de pago y documentos simples (reportlab)."""
from __future__ import annotations

import io
import pathlib
from datetime import date
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_F = pathlib.Path(__file__).resolve().parents[1] / "fuentes"
pdfmetrics.registerFont(TTFont("DejaVu", str(_F / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(_F / "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold")

AZUL = colors.HexColor("#1F3A5F")
GRIS = colors.HexColor("#666666")
CLARO = colors.HexColor("#E8EEF5")

_ss = getSampleStyleSheet()
TIT = ParagraphStyle("tit", parent=_ss["Title"], fontName="DejaVu-Bold", fontSize=15, textColor=AZUL,
                     alignment=0, spaceAfter=2)
SUB = ParagraphStyle("sub", parent=_ss["Normal"], fontName="DejaVu", fontSize=9, textColor=GRIS)
NOR = ParagraphStyle("nor", parent=_ss["Normal"], fontName="DejaVu", fontSize=9, leading=12)
H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontName="DejaVu-Bold", fontSize=11, textColor=AZUL, spaceBefore=8, spaceAfter=4)


LOGO_MAX_ALTO = 18 * mm
LOGO_MAX_ANCHO = 48 * mm


def normalizar_logo(datos: bytes, max_px: int = 600) -> bytes:
    """Valida que sea una imagen PNG o JPEG real y la reescribe como PNG de máximo `max_px` de lado.
    Reescribirla descarta metadatos y cualquier contenido que no sea la imagen."""
    from PIL import Image as PILImage

    if not datos.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        raise ValueError("El logo debe ser una imagen PNG o JPG")
    PILImage.MAX_IMAGE_PIXELS = 25_000_000
    try:
        with PILImage.open(io.BytesIO(datos)) as im:
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA")
            im.thumbnail((max_px, max_px))
            salida = io.BytesIO()
            im.save(salida, format="PNG", optimize=True)
            return salida.getvalue()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow lanza varios tipos según el formato dañado
        raise ValueError("No se pudo leer la imagen del logo") from exc


def _logo(datos: bytes | None):
    """Imagen del logo escalada para el encabezado, o None si no hay o no se puede leer."""
    if not datos:
        return None
    try:
        from reportlab.lib.utils import ImageReader

        ancho, alto = ImageReader(io.BytesIO(datos)).getSize()
        escala = min(LOGO_MAX_ALTO / alto, LOGO_MAX_ANCHO / ancho)
        return Image(io.BytesIO(datos), width=ancho * escala, height=alto * escala)
    except Exception:  # noqa: BLE001 - un logo dañado nunca impide generar el documento
        return None


def encabezado(titulo: str, subtitulo: str, logo: bytes | None, ancho: float) -> list:
    """Título y subtítulo; con logo, el logo va a la izquierda y el texto a su lado."""
    textos = [Paragraph(titulo, TIT), Paragraph(subtitulo, SUB)]
    img = _logo(logo)
    if img is None:
        return [*textos, Spacer(1, 6)]
    t = Table([[img, textos]], colWidths=[img.drawWidth + 5 * mm, ancho - img.drawWidth - 5 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return [t, Spacer(1, 8)]


def colones(v) -> str:
    if v is None:
        return ""
    d = Decimal(str(v)).quantize(Decimal("0.01"))
    s = f"{abs(d):,.2f}".replace(",", " ").replace(".", ",")
    return f"{'-' if d < 0 else ''}₡{s}"


def fecha(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def _tabla(filas, anchos, total=False):
    t = Table(filas, colWidths=anchos)
    estilo = [("FONTNAME", (0, 0), (-1, -1), "DejaVu"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
              ("ALIGN", (-1, 0), (-1, -1), "RIGHT"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
              ("TOPPADDING", (0, 0), (-1, -1), 3), ("LINEBELOW", (0, 0), (-1, 0), 0.5, AZUL),
              ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"), ("TEXTCOLOR", (0, 0), (-1, 0), AZUL)]
    if total:
        estilo += [("FONTNAME", (0, -1), (-1, -1), "DejaVu-Bold"), ("LINEABOVE", (0, -1), (-1, -1), 0.5, GRIS)]
    t.setStyle(TableStyle(estilo))
    return t


def boleta_pdf(empresa: dict, planilla: dict, empleado: dict, boleta: dict, lineas: list[dict], nota: str | None = None,
               logo: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title="Boleta de pago")
    nombre = f"{empleado['nombre']} {empleado.get('apellidos') or ''}".strip()
    el = encabezado(empresa["nombre"], f"Cédula jurídica {empresa.get('cedula_juridica') or '—'} · Boleta de pago",
                    logo, 173 * mm)
    info = [["Colaborador", nombre, "Periodo", f"{fecha(planilla['desde'])} al {fecha(planilla['hasta'])}"],
            ["Identificación", empleado["identificacion"], "Fecha de pago", fecha(planilla.get("fecha_pago"))],
            ["Puesto", empleado.get("puesto") or "", "Salario mensual", colones(empleado.get("salario_mensual"))]]
    t = Table(info, colWidths=[28 * mm, 60 * mm, 30 * mm, 55 * mm])
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "DejaVu"), ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("TEXTCOLOR", (0, 0), (0, -1), GRIS),
                           ("TEXTCOLOR", (2, 0), (2, -1), GRIS), ("BACKGROUND", (0, 0), (-1, -1), CLARO),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    el += [t, Spacer(1, 8)]

    ing = [l for l in lineas if l["tipo"] == "ingreso"]
    ded = [l for l in lineas if l["tipo"] == "deduccion"]
    tot_ing = sum((Decimal(l["monto"]) for l in ing), Decimal(0))
    tot_ded = sum((Decimal(l["monto"]) for l in ded), Decimal(0))
    el.append(Paragraph("Ingresos", H2))
    el.append(_tabla([["Concepto", "Monto"]] + [[l["descripcion"], colones(l["monto"])] for l in ing]
                     + [["Total ingresos", colones(tot_ing)]], [130 * mm, 43 * mm], total=True))
    el.append(Paragraph("Deducciones", H2))
    el.append(_tabla([["Concepto", "Monto"]] + [[l["descripcion"], colones(l["monto"])] for l in ded]
                     + [["Total deducciones", colones(tot_ded)]], [130 * mm, 43 * mm], total=True))
    el.append(Spacer(1, 10))
    neto = Table([["NETO A PAGAR", colones(boleta["neto"])]], colWidths=[130 * mm, 43 * mm])
    neto.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), AZUL), ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
                              ("FONTNAME", (0, 0), (-1, -1), "DejaVu-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 11),
                              ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    el.append(neto)
    if Decimal(boleta.get("creditos_renta") or 0):
        el.append(Spacer(1, 4))
        el.append(Paragraph(f"Créditos fiscales aplicados al impuesto: {colones(boleta['creditos_renta'])}", SUB))
    if Decimal(boleta.get("subsidio_ins") or 0):
        el.append(Paragraph(f"Subsidio INS pagado directamente por el INS: {colones(boleta['subsidio_ins'])}", SUB))
    if empleado.get("iban"):
        el.append(Paragraph(f"Depósito a la cuenta {empleado['iban']}", SUB))
    if nota:
        el += [Spacer(1, 8), Paragraph(nota, NOR)]
    el += [Spacer(1, 18), Paragraph("Generado con Planilla Costa Rica · auromtec.com", SUB)]
    doc.build(el)
    return buf.getvalue()


def documento_simple(titulo: str, subtitulo: str, secciones: list[tuple[str, list[list[str]]]], pie: str = "",
                     logo: bytes | None = None) -> bytes:
    """PDF genérico: título + tablas de dos o más columnas (liquidaciones, constancias, reportes)."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=titulo)
    ancho = 173 * mm
    el = encabezado(titulo, subtitulo, logo, ancho)
    for nombre, filas in secciones:
        if nombre:
            el.append(Paragraph(nombre, H2))
        if filas:
            n = len(filas[0])
            anchos = [ancho * 0.6] + [ancho * 0.4 / (n - 1)] * (n - 1) if n > 1 else [ancho]
            el.append(_tabla(filas, anchos, total=filas[-1][0].lower().startswith("total")))
    if pie:
        el += [Spacer(1, 10), Paragraph(pie, NOR)]
    doc.build(el)
    return buf.getvalue()


def reporte_tabla(titulo: str, subtitulo: str, encabezados: list[str], filas: list[list[str]],
                  derecha: set[int] | None = None, total: list[str] | None = None, pie: str = "",
                  logo: bytes | None = None) -> bytes:
    """PDF de un reporte tabular (Fase 4). Hoja horizontal con más de 6 columnas; el ancho de
    cada columna sale de su contenido y el tamaño de letra se ajusta para que todo quepa."""
    from reportlab.lib.pagesizes import landscape

    derecha = derecha or set()
    horizontal = len(encabezados) > 6
    tam = landscape(LETTER) if horizontal else LETTER
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=tam, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title=titulo)
    ancho = tam[0] - 24 * mm
    # Ancho según el dato más largo; del encabezado solo cuenta su palabra más larga
    # (puede partirse en dos líneas). Negritas (encabezado y totales) cuentan 30 % más.
    largos = []
    for i, enc in enumerate(encabezados):
        dato = max((len(str(f[i] or "")) for f in filas), default=0)
        if total:
            dato = max(dato, int(len(str(total[i] or "")) * 1.3) + 1)
        palabra = int(max((len(w) for w in str(enc).split()), default=0) * 1.3) + 1
        largos.append(max(4, min(40, max(dato, palabra))) + 1)
    suma = sum(largos)
    anchos = [ancho * l / suma for l in largos]
    # ~0,64 em por carácter (dígitos de DejaVu) + 4 pt de relleno por celda
    fuente = max(5.5, min(8.5, (ancho - 4 * len(largos)) / (suma * 0.64)))
    celda = ParagraphStyle("celda", parent=NOR, fontSize=fuente, leading=fuente + 2)
    celda_d = ParagraphStyle("celda_d", parent=celda, alignment=2)
    cab = ParagraphStyle("cab", parent=celda, fontName="DejaVu-Bold", textColor=AZUL)
    cab_d = ParagraphStyle("cab_d", parent=cab, alignment=2)

    def fila_p(f, cabecera=False, es_total=False):
        out = []
        for i, v in enumerate(f):
            if cabecera:
                est = cab_d if i in derecha else cab
            else:
                est = celda_d if i in derecha else celda
            texto = str(v if v is not None else "")
            texto = texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if i in derecha and not cabecera:
                texto = texto.replace(" ", "\u00a0")   # los montos no se parten en dos líneas
            out.append(Paragraph(f"<b>{texto}</b>" if es_total else texto, est))
        return out

    datos = [fila_p(encabezados, cabecera=True)] + [fila_p(f) for f in filas]
    if total:
        datos.append(fila_p(total, es_total=True))
    t = Table(datos, colWidths=anchos, repeatRows=1)
    estilo = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 0.6, AZUL),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 2),
              ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2)]
    for i in range(1, len(filas) + 1):
        if i % 2 == 0:
            estilo.append(("BACKGROUND", (0, i), (-1, i), CLARO))
    if total:
        estilo.append(("LINEABOVE", (0, -1), (-1, -1), 0.6, GRIS))
    t.setStyle(TableStyle(estilo))
    el = [*encabezado(titulo, subtitulo, logo, ancho), t]
    if not filas:
        el.append(Paragraph("Sin datos para los filtros seleccionados.", NOR))
    el += [Spacer(1, 8), Paragraph(pie or "Generado con Planilla Costa Rica · auromtec.com", SUB)]
    doc.build(el)
    return buf.getvalue()
