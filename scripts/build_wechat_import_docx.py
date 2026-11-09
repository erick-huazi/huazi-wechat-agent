#!/usr/bin/env python3
"""Build a DOCX that WeChat's document importer can preserve reliably."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


IMAGE_RE = re.compile(r"^!\[(.*?)\]\((.*?)\)$")
ORDERED_RE = re.compile(r"^\d+\.\s+(.*)$")
INLINE_RE = re.compile(r"(\*\*.*?\*\*|`.*?`)")


def set_font(run, name: str = "Microsoft YaHei", size: float = 11.5, bold: bool = False) -> None:
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    run.font.size = Pt(size)
    run.bold = bold


def add_inline(paragraph, text: str, size: float = 11.5) -> None:
    for part in INLINE_RE.split(text):
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**")
        code = part.startswith("`") and part.endswith("`")
        content = part[2:-2] if bold else part[1:-1] if code else part
        run = paragraph.add_run(content)
        set_font(run, "Consolas" if code else "Microsoft YaHei", size, bold)


def configure_document(doc: Document) -> None:
    section = doc.sections[0]
    section.top_margin = Inches(0.65)
    section.bottom_margin = Inches(0.65)
    section.left_margin = Inches(0.72)
    section.right_margin = Inches(0.72)

    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(11.5)
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.line_spacing = 1.35

    for style_name, size, color in (
        ("Heading 1", 18, "111111"),
        ("Heading 2", 15, "1F4E5F"),
        ("Heading 3", 12.5, "1F4E5F"),
    ):
        style = doc.styles[style_name]
        style.font.name = "Microsoft YaHei"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor.from_string(color)
        style.paragraph_format.space_before = Pt(12)
        style.paragraph_format.space_after = Pt(6)


def build(markdown_path: Path, output_path: Path) -> int:
    doc = Document()
    configure_document(doc)
    image_count = 0

    for raw_line in markdown_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# "):
            # The editor title is filled separately; do not duplicate it in the body.
            continue
        match = IMAGE_RE.match(line)
        if match:
            alt, relative = match.groups()
            image_path = (markdown_path.parent / relative).resolve()
            if not image_path.is_file():
                raise FileNotFoundError(image_path)
            paragraph = doc.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = paragraph.add_run()
            run.add_picture(str(image_path), width=Inches(6.15))
            paragraph.paragraph_format.space_after = Pt(8)
            image_count += 1
            continue
        if line.startswith("## "):
            paragraph = doc.add_paragraph(style="Heading 2")
            add_inline(paragraph, line[3:], 15)
            continue
        if line.startswith("### "):
            paragraph = doc.add_paragraph(style="Heading 3")
            add_inline(paragraph, line[4:], 12.5)
            continue
        if line.startswith("> "):
            paragraph = doc.add_paragraph()
            paragraph.paragraph_format.left_indent = Inches(0.22)
            paragraph.paragraph_format.right_indent = Inches(0.12)
            paragraph.paragraph_format.space_before = Pt(4)
            paragraph.paragraph_format.space_after = Pt(9)
            add_inline(paragraph, line[2:], 10.5)
            for run in paragraph.runs:
                run.font.color.rgb = RGBColor(90, 90, 90)
            continue
        if line.startswith("- "):
            paragraph = doc.add_paragraph(style="List Bullet")
            add_inline(paragraph, line[2:])
            continue
        ordered = ORDERED_RE.match(line)
        if ordered:
            paragraph = doc.add_paragraph(style="List Number")
            add_inline(paragraph, ordered.group(1))
            continue
        paragraph = doc.add_paragraph()
        add_inline(paragraph, line)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    return image_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("markdown")
    parser.add_argument("output")
    args = parser.parse_args()
    images = build(Path(args.markdown).resolve(), Path(args.output).resolve())
    if images != 5:
        raise SystemExit(f"expected 5 images, found {images}")
    print(f"images={images}")
    print(f"output={Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
