"""Shared WMS operational document chrome for Pick Ticket and Order Closure PDFs."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

TEXT = HexColor("#182230")
MUTED = HexColor("#6B7280")
BLUE = HexColor("#1F5F8B")
BORDER = HexColor("#E3E7EB")
SECTION = HexColor("#F4F6F8")
PAGE_W, PAGE_H = letter
LEFT = 0.6 * inch
RIGHT = 0.6 * inch
TOP = 0.7 * inch
BOTTOM = 0.7 * inch


def display(value) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    return escape(text) if text else "—"


def format_ts(value, *, with_time=True) -> str:
    if value is None:
        return "—"
    if with_time:
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return value.strftime("%Y-%m-%d")


def format_time(value) -> str:
    if value is None:
        return "—"
    return value.strftime("%H:%M:%S UTC")


def styles():
    return {
        "brand": ParagraphStyle(
            "wms_brand",
            fontName="Helvetica-Bold",
            fontSize=8,
            textColor=BLUE,
            leading=10,
            tracking=0.6,
        ),
        "title": ParagraphStyle(
            "wms_title",
            fontName="Helvetica-Bold",
            fontSize=13,
            textColor=TEXT,
            leading=16,
        ),
        "number": ParagraphStyle(
            "wms_number",
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=BLUE,
            leading=15,
        ),
        "label": ParagraphStyle(
            "wms_label",
            fontName="Helvetica",
            fontSize=6.5,
            textColor=MUTED,
            leading=8,
        ),
        "value": ParagraphStyle(
            "wms_value",
            fontName="Helvetica-Bold",
            fontSize=8,
            textColor=TEXT,
            leading=10,
        ),
        "section": ParagraphStyle(
            "wms_section",
            fontName="Helvetica-Bold",
            fontSize=8,
            textColor=BLUE,
            leading=11,
        ),
        "body": ParagraphStyle(
            "wms_body",
            fontName="Helvetica",
            fontSize=7.5,
            textColor=TEXT,
            leading=9.5,
        ),
        "cell": ParagraphStyle(
            "wms_cell",
            fontName="Helvetica",
            fontSize=7,
            textColor=TEXT,
            leading=9,
        ),
        "head": ParagraphStyle(
            "wms_head",
            fontName="Helvetica-Bold",
            fontSize=7,
            textColor=BLUE,
            leading=9,
        ),
        "pass": ParagraphStyle(
            "wms_pass",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=BLUE,
            leading=12,
            alignment=TA_LEFT,
        ),
        "right": ParagraphStyle(
            "wms_right",
            fontName="Helvetica-Bold",
            fontSize=8,
            textColor=TEXT,
            leading=10,
            alignment=TA_RIGHT,
        ),
    }


def pick_ticket_styles():
    """Warehouse-readable typography used only by Pick Ticket PDFs."""
    return {
        "brand": ParagraphStyle(
            "pick_brand",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=BLUE,
            leading=11,
            tracking=0.6,
        ),
        "title": ParagraphStyle(
            "pick_title",
            fontName="Helvetica-Bold",
            fontSize=17,
            textColor=TEXT,
            leading=21,
        ),
        "number": ParagraphStyle(
            "pick_number",
            fontName="Helvetica-Bold",
            fontSize=12,
            textColor=BLUE,
            leading=16,
        ),
        "label": ParagraphStyle(
            "pick_label",
            fontName="Helvetica",
            fontSize=9,
            textColor=MUTED,
            leading=11,
        ),
        "value": ParagraphStyle(
            "pick_value",
            fontName="Helvetica-Bold",
            fontSize=10,
            textColor=TEXT,
            leading=13,
        ),
        "section": ParagraphStyle(
            "pick_section",
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=BLUE,
            leading=14,
        ),
        "body": ParagraphStyle(
            "pick_body",
            fontName="Helvetica",
            fontSize=9.5,
            textColor=TEXT,
            leading=12,
        ),
        "cell": ParagraphStyle(
            "pick_cell",
            fontName="Helvetica",
            fontSize=9.5,
            textColor=TEXT,
            leading=12,
        ),
        "cell_right": ParagraphStyle(
            "pick_cell_right",
            fontName="Helvetica-Bold",
            fontSize=9.5,
            textColor=TEXT,
            leading=12,
            alignment=TA_RIGHT,
        ),
        "head": ParagraphStyle(
            "pick_head",
            fontName="Helvetica-Bold",
            fontSize=9.5,
            textColor=BLUE,
            leading=12,
        ),
        "head_right": ParagraphStyle(
            "pick_head_right",
            fontName="Helvetica-Bold",
            fontSize=9.5,
            textColor=BLUE,
            leading=12,
            alignment=TA_RIGHT,
        ),
        "pass": ParagraphStyle(
            "pick_pass",
            fontName="Helvetica-Bold",
            fontSize=11,
            textColor=BLUE,
            leading=14,
            alignment=TA_LEFT,
        ),
        "right": ParagraphStyle(
            "pick_right",
            fontName="Helvetica-Bold",
            fontSize=10,
            textColor=TEXT,
            leading=13,
            alignment=TA_RIGHT,
        ),
    }


class NumberedCanvas(canvas.Canvas):
    def __init__(
        self,
        *args,
        footer=None,
        later_header=None,
        page_footers=None,
        chrome_font_size=7,
        **kwargs,
    ):
        kwargs.setdefault("pageCompression", 0)
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self._footer = footer or {}
        self._later_header = later_header or {}
        self._page_footers = page_footers or []
        self._chrome_font_size = float(chrome_font_size or 7)

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        page_count = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._draw_chrome(page_count)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def _draw_chrome(self, page_count):
        page = self._pageNumber
        chrome = self._chrome_font_size
        self.saveState()
        if page > 1 and self._later_header:
            bar_bottom = 0.52 * inch if chrome >= 8 else 0.48 * inch
            bar_height = 0.32 * inch if chrome >= 8 else 0.28 * inch
            text_y = PAGE_H - (0.40 * inch if chrome >= 8 else 0.38 * inch)
            title_x = LEFT + (86 if chrome >= 8 else 72)
            self.setFillColor(SECTION)
            self.rect(LEFT, PAGE_H - bar_bottom, PAGE_W - LEFT - RIGHT, bar_height, stroke=0, fill=1)
            self.setStrokeColor(BORDER)
            self.setLineWidth(0.4)
            self.line(LEFT, PAGE_H - bar_bottom, PAGE_W - RIGHT, PAGE_H - bar_bottom)
            self.setFillColor(BLUE)
            self.setFont("Helvetica-Bold", chrome)
            self.drawString(LEFT + 6, text_y, "WMS SYSTEM")
            self.setFillColor(TEXT)
            self.setFont("Helvetica", chrome)
            title = self._later_header.get("title", "")
            ident = self._later_header.get("ident", "")
            status = self._later_header.get("status", "")
            self.drawString(title_x, text_y, title)
            self.setFont("Helvetica-Bold", chrome)
            self.drawRightString(PAGE_W - RIGHT - 78, text_y, ident)
            self.setFillColor(BLUE)
            self.drawRightString(PAGE_W - RIGHT - 6, text_y, status)
        self.setStrokeColor(BORDER)
        self.setLineWidth(0.4)
        self.line(LEFT, 0.48 * inch, PAGE_W - RIGHT, 0.48 * inch)
        self.setFillColor(MUTED)
        self.setFont("Helvetica", chrome)
        left = self._footer.get("left", "WMS SYSTEM")
        mid = self._footer.get("mid", "")
        generated = self._footer.get("generated", "")
        if self._page_footers and 0 <= (page - 1) < len(self._page_footers):
            extra = self._page_footers[page - 1] or {}
            left = extra.get("left", left)
            mid = extra.get("mid", mid)
            generated = extra.get("generated", generated)
        self.drawString(LEFT, 0.32 * inch, left)
        if mid:
            self.drawCentredString(PAGE_W / 2, 0.32 * inch, mid)
        self.drawRightString(PAGE_W - RIGHT, 0.32 * inch, f"Page {page} of {page_count}")
        if generated:
            self.drawString(LEFT, 0.18 * inch, generated)
        self.restoreState()


def build_pdf(story, *, footer, later_header=None, page_footers=None, chrome_font_size=7) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=LEFT,
        rightMargin=RIGHT,
        topMargin=TOP,
        bottomMargin=BOTTOM,
        title=footer.get("title") or "WMS SYSTEM",
        author="WMS SYSTEM",
    )

    def canvasmaker(*args, **kwargs):
        return NumberedCanvas(
            *args,
            footer=footer,
            later_header=later_header,
            page_footers=page_footers,
            chrome_font_size=chrome_font_size,
            **kwargs,
        )

    doc.build(story, canvasmaker=canvasmaker)
    return buffer.getvalue()


def header_block(s, *, brand="WMS SYSTEM", title, ident, status, padding=1):
    status_label = f"STATUS: {status}"
    header = Table(
        [
            [
                Paragraph(brand, s["brand"]),
                Paragraph(status_label, s["right"]),
            ],
            [Paragraph(title, s["title"]), ""],
            [Paragraph(display(ident), s["number"]), ""],
        ],
        colWidths=[4.6 * inch, 2.4 * inch],
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("SPAN", (0, 1), (1, 1)),
                ("SPAN", (0, 2), (1, 2)),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), padding),
                ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
                ("LINEBELOW", (0, 2), (-1, 2), 1, BLUE),
            ]
        )
    )
    return header


def section_title(s, text):
    return Paragraph(text, s["section"])


def kv_table(s, pairs, cols=4, pad_x=6, pad_y_top=4, pad_y_bottom=3):
    stacked = [
        [Paragraph(str(label).upper(), s["label"]), Paragraph(display(value), s["value"])]
        for label, value in pairs
    ]
    while len(stacked) % cols:
        stacked.append([Paragraph("", s["label"]), Paragraph("", s["value"])])
    flow_rows = []
    for i in range(0, len(stacked), cols):
        labels = [stacked[i + j][0] for j in range(cols)]
        values = [stacked[i + j][1] for j in range(cols)]
        flow_rows.append(labels)
        flow_rows.append(values)
    usable = PAGE_W - LEFT - RIGHT
    col_w = usable / cols
    table = Table(flow_rows, colWidths=[col_w] * cols)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SECTION),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), pad_x),
                ("RIGHTPADDING", (0, 0), (-1, -1), pad_x),
                ("TOPPADDING", (0, 0), (-1, -1), pad_y_top),
                ("BOTTOMPADDING", (0, 0), (-1, -1), pad_y_bottom),
                ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
                ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER),
            ]
        )
    )
    return table


def summary_row(s, items, pad_x=6, pad_y=5):
    usable = PAGE_W - LEFT - RIGHT
    col_w = usable / max(len(items), 1)
    labels = [Paragraph(str(k).upper(), s["label"]) for k, _ in items]
    values = [Paragraph(display(v), s["value"]) for _, v in items]
    table = Table([labels, values], colWidths=[col_w] * len(items))
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), SECTION),
                ("BOX", (0, 0), (-1, -1), 0.4, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), pad_x),
                ("RIGHTPADDING", (0, 0), (-1, -1), pad_x),
                ("TOPPADDING", (0, 0), (-1, -1), pad_y),
                ("BOTTOMPADDING", (0, 0), (-1, -1), pad_y),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    return table


def data_table(s, headers, rows, col_widths=None, numeric_last=False, pad_x=4, pad_y=3):
    last = len(headers) - 1
    head_right = s.get("head_right") or s["head"]
    cell_right = s.get("cell_right") or s["cell"]
    head = [
        Paragraph(h, head_right if numeric_last and i == last else s["head"])
        for i, h in enumerate(headers)
    ]
    body = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            style = cell_right if numeric_last and i == last else s["cell"]
            cells.append(Paragraph(display(cell), style))
        body.append(cells)
    if not body:
        body.append([Paragraph("—", s["cell"])] + [Paragraph("", s["cell"])] * last)
    table = Table([head] + body, colWidths=col_widths, repeatRows=1, splitByRow=1)
    table.hAlign = "LEFT"
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), SECTION),
        ("TEXTCOLOR", (0, 0), (-1, 0), BLUE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), pad_x),
        ("RIGHTPADDING", (0, 0), (-1, -1), pad_x),
        ("TOPPADDING", (0, 0), (-1, -1), pad_y),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad_y),
        ("BACKGROUND", (0, 1), (-1, -1), white),
    ]
    if numeric_last:
        cmds.append(("ALIGN", (-1, 1), (-1, -1), "RIGHT"))
    table.setStyle(TableStyle(cmds))
    return table


def generated_now() -> datetime:
    return datetime.utcnow()
