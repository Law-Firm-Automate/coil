"""PDF generation with fpdf2. Simple, dependency-free, good enough for invoices and signed letters."""
import os
import re
import threading
from html import unescape
from fpdf import FPDF
from flask import current_app

# Helvetica is one of the PDF core fonts and cannot represent anything outside cp1252, so
# every builder used to force text through a lossy encode. A client called Nadia in Arabic,
# or any name in Greek, Cyrillic or Chinese, came out of an invoice as a row of question
# marks. DejaVu Sans is bundled (see LICENSE-DejaVu.txt) and covers Latin, Greek, Cyrillic
# and most European punctuation.
#
# It is only switched on for a document that actually needs it, so an ordinary invoice keeps
# the metrics it has always had and does not silently restyle. The flag is per thread because
# a builder sets it once at the top of a render and every helper below reads it; requests are
# handled one per thread under gunicorn's sync worker, so there is nothing to share.
FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "fonts")
UNICODE_FAMILY = "DejaVu"
_UNICODE_FILES = {"": "DejaVuSans.ttf", "B": "DejaVuSans-Bold.ttf", "I": "DejaVuSans-Oblique.ttf"}
_state = threading.local()


def unicode_on():
    return getattr(_state, "unicode", False)


def needs_unicode(*parts):
    """True when any of this text cannot survive the core-font encoding."""
    for part in parts:
        if not part:
            continue
        try:
            str(part).encode("cp1252")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return True
    return False


def enable_unicode(pdf, *parts):
    """Switch a document to the bundled Unicode font when its text needs one.

    Returns True when it did. Safe to call with everything the document will print; the
    scan is cheap next to rendering. Falls back silently to the core font if the bundled
    files are missing, because a slightly mangled invoice beats a 500 on the download.
    """
    if not needs_unicode(*parts):
        return False
    try:
        for style, fname in _UNICODE_FILES.items():
            pdf.add_font(UNICODE_FAMILY, style, os.path.join(FONT_DIR, fname))
    except Exception:  # noqa: BLE001 - missing or unreadable font files
        return False
    _state.unicode = True
    return True


def reset_unicode():
    _state.unicode = False


def font_family(requested="Helvetica"):
    """The family to actually ask fpdf for."""
    return UNICODE_FAMILY if unicode_on() else requested


class DocPDF(FPDF):
    def __init__(self, firm, title=""):
        super().__init__()
        self.firm = firm
        self.doc_title = title
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 18, 18)

    def set_font(self, family=None, style="", size=0):
        # Every builder asks for Helvetica by name. When the document has been switched to
        # the bundled Unicode font, honour that instead of silently dropping the glyphs.
        if family and family.lower() in ("helvetica", "arial"):
            family = font_family(family)
        return super().set_font(family, style, size)

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
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"') \
        .replace("–", "-").replace("—", "-").replace("•", "-")
    if unicode_on():
        return mark_unsupported(s)
    return s.encode("latin-1", "replace").decode("latin-1")


def _parse_table_rows(table_html):
    """Cell text for each <tr> in a <table> block, tags stripped, in document order."""
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.I | re.S):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.I | re.S)
        rows.append([unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells])
    return [r for r in rows if r]


def _render_table(pdf, rows):
    """A real bordered grid via fpdf2's table(), not a text block, so cells stay visually separated."""
    if not rows:
        return
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    pdf.set_font("Helvetica", "", 9.5)
    with pdf.table(text_align="LEFT", line_height=5.5, borders_layout="ALL") as table:
        for r in rows:
            row = table.row()
            for cell in r:
                row.cell(_clean(cell))
    pdf.set_font("Helvetica", "", 10.5)
    pdf.ln(2.5)


def _render_text_blocks(pdf, html):
    blocks = re.split(r"</?(?:p|div|h1|h2|h3|li|ul|ol)[^>]*>", html, flags=re.I)
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


def html_to_pdf_body(pdf, html):
    """Very small HTML subset: p, br, h1-h3, strong/b, em/i, ul/li, ol/li, table/tr/td/th.

    Tables are pulled out and rendered as a real bordered grid; everything else is stripped
    to plain paragraphs.
    """
    html = html or ""
    html = re.sub(r"<\s*br\s*/?>", "\n", html, flags=re.I)
    for part in re.split(r"(<table[^>]*>.*?</table>)", html, flags=re.I | re.S):
        if re.match(r"\s*<table", part, flags=re.I):
            _render_table(pdf, _parse_table_rows(part))
        else:
            _render_text_blocks(pdf, part)


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

_coverage = None


def _font_covers():
    """The set of codepoints the bundled font can actually draw, read once from its cmap."""
    global _coverage
    if _coverage is None:
        try:
            from fontTools.ttLib import TTFont
            f = TTFont(os.path.join(FONT_DIR, _UNICODE_FILES[""]), lazy=True)
            _coverage = set(f.getBestCmap().keys())
            f.close()
        except Exception:  # noqa: BLE001
            _coverage = set()
    return _coverage


def mark_unsupported(s, marker="?"):
    """Replace characters the font cannot draw, so they cannot silently disappear.

    DejaVu covers Latin, Greek, Cyrillic and most European punctuation, but not Arabic,
    CJK or emoji. fpdf drops a glyph it has no outline for, which on an invoice means a
    client's name is simply absent and nobody notices. A visible marker is not good
    output, but it is output somebody will query rather than sign and post.
    """
    cov = _font_covers()
    if not cov:
        return s
    return "".join(ch if (ord(ch) in cov or ch in "\n\r\t") else marker for ch in s)

