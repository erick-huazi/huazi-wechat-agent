from __future__ import annotations

import argparse
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt


ORDERED_RE = re.compile(r"^\d+\.\s+(.*)$")
IMAGE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")


def configure_document(doc: Document) -> None:
    section = doc.sections[0]
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.78)
    section.right_margin = Inches(0.78)

    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(11.5)
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.line_spacing = 1.42


def content_lines(markdown_path: Path) -> tuple[str, list[tuple[str, str]]]:
    title = ""
    lines: list[tuple[str, str]] = []
    for raw in markdown_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# "):
            title = title or line[2:].strip()
            continue
        if IMAGE_RE.match(line):
            continue
        if line.startswith("## "):
            lines.append(("heading", line[3:].strip()))
        elif line.startswith("- "):
            lines.append(("bullet", line[2:].strip()))
        else:
            ordered = ORDERED_RE.match(line)
            if ordered:
                lines.append(("number", ordered.group(1).strip()))
            else:
                lines.append(("paragraph", line.replace("**", "").replace("`", "")))
    return title, lines


def insertion_points(line_count: int) -> list[int]:
    if line_count < 5:
        raise ValueError("at least five content blocks are required")
    return sorted({
        max(1, round(line_count * 0.14)),
        max(2, round(line_count * 0.32)),
        max(3, round(line_count * 0.50)),
        max(4, round(line_count * 0.70)),
        line_count,
    })


def add_image(doc: Document, path: Path) -> None:
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.add_run().add_picture(str(path), width=Inches(6.1))
    paragraph.paragraph_format.space_after = Pt(9)


def build(markdown: Path, images_dir: Path, output: Path, date: str) -> None:
    title, lines = content_lines(markdown)
    images = [images_dir / f"{date}-{index:02d}.png" for index in range(1, 6)]
    missing = [str(path) for path in images if not path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))

    doc = Document()
    configure_document(doc)
    points = insertion_points(len(lines))
    point_to_image = {point: images[index] for index, point in enumerate(points)}

    for index, (kind, text) in enumerate(lines, start=1):
        if kind == "heading":
            paragraph = doc.add_paragraph(text, style="Heading 2")
        elif kind == "bullet":
            paragraph = doc.add_paragraph(text, style="List Bullet")
        elif kind == "number":
            paragraph = doc.add_paragraph(text, style="List Number")
        else:
            paragraph = doc.add_paragraph(text)
        for run in paragraph.runs:
            run.font.name = "Microsoft YaHei"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        if index in point_to_image:
            add_image(doc, point_to_image[index])

    doc.core_properties.title = title
    doc.core_properties.author = "华子社长"
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--images-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    build(
        args.markdown.resolve(),
        args.images_dir.resolve(),
        args.output.resolve(),
        args.date,
    )
    print(f"output={args.output.resolve()}")


if __name__ == "__main__":
    main()
