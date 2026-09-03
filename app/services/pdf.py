"""PDF generation with fpdf2. Simple, dependency-free, good enough for invoices and signed letters."""
import os
import re
from html import unescape
from fpdf import FPDF
from flask import current_app


class DocPDF(FPDF):
    def __init__(self, firm, title=""):
        super().__init__()
        self.firm = firm
        self.doc_title = title
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 18, 18)

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.cell(0, 7, self.firm.name, new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        for line in [l for l in (self.firm.address or "").splitlines() if l.strip()]:
            self.cell(0, 4.5, line, new_x="LMARGIN", new_y="NEXT")
        contact = " | ".join([x for x in (self.firm.phone, self.firm.email, self.firm.website) if x])
        if contact:
            self.cell(0, 4.5, contact, new_x="LMARGIN", new_y="NEXT")
        self.ln(3)
        self.set_draw_color(180, 180, 180)
        self.line(18, self.get_y(), 192, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 5, f"{self.doc_title}   Page {self.page_no()}/{{nb}}", align="C")
        self.set_text_color(0, 0, 0)


def _clean(s):
    return (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"') \
        .replace("–", "-").replace("—", "-").replace("•", "-").encode("latin-1", "replace").decode("latin-1")


def html_to_pdf_body(pdf, html):
    """Very small HTML subset: p, br, h1-h3, strong/b, em/i, ul/li, ol/li. Anything else is stripped."""
    html = html or ""
    html = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    blocks = re.split(r"</?(?:p|div|h1|h2|h3|li|ul|ol|table|tr)[^>]*>", html, flags=re.I)
    heads = re.findall(r"<(h1|h2|h3)[^>]*>(.*?)</\1>", html, flags=re.I | re.S)
    head_text = {unescape(re.sub(r"<[^>]+>", "", h[1])).strip(): h[0] for h in heads}
    for b in blocks:
        text = unescape(re.sub(r"<[^>]+>", "", b)).strip()
        if not text:
            continue
        tag = head_text.get(text)
        if tag == "h1":
            pdf.set_font("Helvetica", "B", 14)
        elif tag == "h2":
            pdf.set_font("Helvetica", "B", 12)
        elif tag == "h3":
            pdf.set_font("Helvetica", "B", 11)
        else:
            pdf.set_font("Helvetica", "", 10.5)
        pdf.multi_cell(0, 5.2, _clean(text))
        pdf.ln(2.5)


def save_pdf(pdf, filename):
    out_dir = current_app.config["PDF_DIR"]
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    pdf.output(path)
    return path


def money(c):
    c = int(c or 0)
    neg = c < 0
    c = abs(c)
    s = f"${c // 100:,}.{c % 100:02d}"
    return f"({s})" if neg else s
