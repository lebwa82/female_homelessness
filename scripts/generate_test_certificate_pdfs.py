"""Generate neutral, invalid PDF certificates for end-to-end testing."""

from __future__ import annotations

import argparse
import secrets
import string
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

PAGE_W, PAGE_H = A4
ALPHABET = string.ascii_uppercase + string.digits


def _fonts() -> None:
    candidates = (
        Path("/System/Library/Fonts/Supplemental"),
        Path("/usr/share/fonts/truetype/dejavu"),
    )
    for directory in candidates:
        regular = directory / ("Arial.ttf" if "System" in str(directory) else "DejaVuSans.ttf")
        bold = directory / ("Arial Bold.ttf" if "System" in str(directory) else "DejaVuSans-Bold.ttf")
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("CertificateSans", regular))
            pdfmetrics.registerFont(TTFont("CertificateSansBold", bold))
            return
    raise RuntimeError("a Unicode TrueType font is required")


def _token(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def _label(c: canvas.Canvas, value: str, x: float, y: float, size: float, color, *, bold=False):
    c.setFont("CertificateSansBold" if bold else "CertificateSans", size)
    c.setFillColor(color)
    c.drawString(x, y, value)


def _paragraph(c, value, x, y_top, width, size, color, leading=None):
    style = ParagraphStyle(
        "certificate", fontName="CertificateSans", fontSize=size,
        leading=leading or size * 1.35, textColor=color, alignment=TA_LEFT,
    )
    paragraph = Paragraph(value, style)
    _, height = paragraph.wrap(width, PAGE_H)
    paragraph.drawOn(c, x, y_top - height)


def _box(c, x, y, width, height, fill):
    c.setFillColor(fill)
    c.roundRect(x, y, width, height, 6 * mm, stroke=0, fill=1)


def _base(c, accent, pale, provider):
    c.setTitle(f"Тестовый сертификат {provider}")
    c.setAuthor("Невидимый фонд — тестовый макет")
    c.setFillColor(HexColor("#F7F7F5"))
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    c.setFillColor(accent)
    c.rect(0, PAGE_H - 76 * mm, PAGE_W, 76 * mm, stroke=0, fill=1)
    c.setFillColor(pale)
    c.circle(PAGE_W - 24 * mm, PAGE_H - 18 * mm, 40 * mm, stroke=0, fill=1)
    c.circle(PAGE_W - 62 * mm, PAGE_H - 72 * mm, 26 * mm, stroke=0, fill=1)
    _label(c, "ТЕСТОВЫЙ СЕРТИФИКАТ", 18 * mm, PAGE_H - 22 * mm, 11, white, bold=True)
    _label(c, provider, 18 * mm, PAGE_H - 42 * mm, 30, white, bold=True)
    _box(c, 18 * mm, PAGE_H - 68 * mm, 74 * mm, 17 * mm, white)
    c.setFont("CertificateSansBold", 11)
    c.setFillColor(accent)
    c.drawCentredString(55 * mm, PAGE_H - 62 * mm, "НЕ ДЕЙСТВИТЕЛЕН")
    c.saveState()
    c.translate(PAGE_W / 2, PAGE_H / 2)
    c.rotate(31)
    c.setFillColor(Color(0.2, 0.17, 0.3, alpha=0.08))
    c.setFont("CertificateSansBold", 43)
    c.drawCentredString(0, 0, "TEST / НЕ ДЕЙСТВИТЕЛЕН")
    c.restoreState()


def _qr(c, payload, x, y, size):
    widget = qr.QrCodeWidget(payload)
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, c, x, y)


def _ozon(path: Path, code: str, serial: str, nominal: int) -> None:
    navy, blue, pale = HexColor("#162A4A"), HexColor("#356AF2"), HexColor("#8EAAFF")
    ink, muted = HexColor("#202631"), HexColor("#626A78")
    c = canvas.Canvas(str(path), pagesize=A4)
    _base(c, navy, pale, "Ozon")
    _label(c, "НОМИНАЛ", 18 * mm, PAGE_H - 96 * mm, 10, muted, bold=True)
    _label(c, f"{nominal} руб.", 18 * mm, PAGE_H - 116 * mm, 34, blue, bold=True)
    _box(c, 18 * mm, PAGE_H - 176 * mm, PAGE_W - 36 * mm, 47 * mm, white)
    _label(c, "КОД АКТИВАЦИИ", 27 * mm, PAGE_H - 145 * mm, 9, muted, bold=True)
    _label(c, code, 27 * mm, PAGE_H - 159 * mm, 20, ink, bold=True)
    _label(c, "АКТИВИРОВАТЬ ДО", 122 * mm, PAGE_H - 145 * mm, 9, muted, bold=True)
    _label(c, "31.12.2030", 122 * mm, PAGE_H - 159 * mm, 15, ink, bold=True)
    _label(c, "СЕРИЙНЫЙ НОМЕР", 18 * mm, PAGE_H - 198 * mm, 9, muted, bold=True)
    _label(c, serial, 18 * mm, PAGE_H - 211 * mm, 15, ink, bold=True)
    _label(c, "Как проверить сценарий", 18 * mm, PAGE_H - 239 * mm, 17, navy, bold=True)
    _paragraph(c, "1. Откройте приложение Ozon и перейдите в раздел кодов и сертификатов.<br/>2. Введите тестовый код без пробелов.<br/>3. Этот макет не активируется и предназначен только для проверки приложения.", 18 * mm, PAGE_H - 247 * mm, PAGE_W - 36 * mm, 10.5, ink, 16)
    _label(c, "Создано для тестирования выдачи PDF. Не является платёжным средством.", 18 * mm, 13 * mm, 8.5, muted)
    c.save()


def _pyaterochka(path: Path, number: str, nominal: int) -> None:
    plum, amber, pale = HexColor("#402A5C"), HexColor("#F4B63D"), HexColor("#E0C8FF")
    ink, muted = HexColor("#26212C"), HexColor("#6A6170")
    c = canvas.Canvas(str(path), pagesize=A4)
    _base(c, plum, pale, "Пятёрочка")
    _label(c, "НОМИНАЛ", 18 * mm, PAGE_H - 96 * mm, 10, muted, bold=True)
    _label(c, f"{nominal} руб.", 18 * mm, PAGE_H - 116 * mm, 34, amber, bold=True)
    _box(c, 18 * mm, PAGE_H - 211 * mm, PAGE_W - 36 * mm, 80 * mm, white)
    _qr(c, number, 26 * mm, PAGE_H - 203 * mm, 58 * mm)
    _label(c, "НОМЕР И QR-PAYLOAD", 108 * mm, PAGE_H - 150 * mm, 9, muted, bold=True)
    _paragraph(c, number, 108 * mm, PAGE_H - 158 * mm, 76 * mm, 15, ink, 19)
    _label(c, "СРОК ДЕЙСТВИЯ", 108 * mm, PAGE_H - 185 * mm, 9, muted, bold=True)
    _paragraph(c, "01.10.2026 - 30.09.2027", 108 * mm, PAGE_H - 193 * mm, 76 * mm, 12, ink, 16)
    _label(c, "Как проверить сценарий", 18 * mm, PAGE_H - 232 * mm, 17, plum, bold=True)
    _paragraph(c, "1. Добавьте тестовые товары в чек.<br/>2. Выберите оплату подарочным сертификатом.<br/>3. Покажите QR-код на экране или откройте этот PDF.<br/>4. QR содержит тот же тестовый номер, который напечатан рядом.", 18 * mm, PAGE_H - 240 * mm, PAGE_W - 36 * mm, 10.5, ink, 15.5)
    _label(c, "Создано для тестирования выдачи PDF. Не является платёжным средством.", 18 * mm, 13 * mm, 8.5, muted)
    c.save()


def generate(output: Path, count: int) -> tuple[Path, ...]:
    _fonts()
    output.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(1, count + 1):
        code = f"TEST-{_token(4)}-{_token(4)}"
        serial = f"TEST-OZ-{index:06d}-{_token(4)}"
        path = output / f"ozon-test-{index:02d}.pdf"
        _ozon(path, code, serial, secrets.choice((300, 500, 1000)))
        paths.append(path)
        number = f"DC00250{_token(13)}"
        path = output / f"pyaterochka-test-{index:02d}.pdf"
        _pyaterochka(path, number, secrets.choice((250, 500, 1000)))
        paths.append(path)
    return tuple(paths)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--count", type=int, default=15)
    args = parser.parse_args()
    if not 1 <= args.count <= 100:
        raise SystemExit("count must be between 1 and 100")
    paths = generate(args.output, args.count)
    print(f"Generated {len(paths)} test PDF certificates in {args.output}")


if __name__ == "__main__":
    main()
