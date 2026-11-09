from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches, Pt, RGBColor

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from article_layout import article_images, atomic_write_bytes, local_image, parse_article, safe_link
from distribution_io import create_pack_backup, pack_lock


DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-|$)")


def pack_date(pack: Path) -> str:
    match = DATE_RE.match(pack.name)
    if not match:
        raise ValueError(f"pack directory must start with YYYY-MM-DD: {pack.name}")
    return match.group(1)


def configure_document(doc: Document) -> None:
    section = doc.sections[0]
    section.top_margin = Inches(0.75)
    section.bottom_margin = Inches(0.75)
    section.left_margin = Inches(0.8)
    section.right_margin = Inches(0.8)

    for name in ["Normal", "Quote", "List Bullet", "List Number"] + [f"Heading {i}" for i in range(1, 7)]:
        style = doc.styles[name]
        style.font.name = "Microsoft YaHei"
        style.font.size = Pt(11.5)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.italic = False
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        style.paragraph_format.space_after = Pt(8)
        style.paragraph_format.line_spacing = 1.6
        style.paragraph_format.widow_control = True
        if name.startswith("Heading"):
            level = int(name[-1])
            style.font.size = Pt(16 if level <= 2 else 13)
            style.paragraph_format.space_before = Pt(18)
            style.paragraph_format.space_after = Pt(10)
            style.paragraph_format.keep_with_next = True


def numbering(doc: Document, start: int, depth: int, ordered: bool) -> int:
    root = doc.part.numbering_part.element
    abstract_id = max((int(x.get(qn("w:abstractNumId"))) for x in root.findall(qn("w:abstractNum"))), default=-1) + 1
    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), str(abstract_id))
    level = OxmlElement("w:lvl")
    level.set(qn("w:ilvl"), "0")
    for tag, val in [("start", str(start)), ("numFmt", "decimal" if ordered else "bullet"), ("lvlText", "%1." if ordered else "\u2022")]:
        node = OxmlElement(f"w:{tag}")
        node.set(qn("w:val"), val)
        level.append(node)
    props = OxmlElement("w:pPr")
    indent = OxmlElement("w:ind")
    indent.set(qn("w:left"), str(360 * depth))
    indent.set(qn("w:hanging"), "240")
    props.append(indent)
    level.append(props)
    abstract.append(level)
    root.append(abstract)
    return root.add_num(abstract_id).numId


def append_inline(paragraph, tokens, base: Path, *, header: bool = False) -> None:
    bold = emphasis = 0
    link = None
    for token in tokens:
        kind = token.type
        if kind in {"strong_open", "strong_close"}:
            bold += token.nesting
        elif kind in {"em_open", "em_close"}:
            emphasis += token.nesting
        elif kind == "link_open":
            href = safe_link(token.attrGet("href") or "")
            link = OxmlElement("w:hyperlink")
            if href.startswith("#"):
                link.set(qn("w:anchor"), href[1:])
            else:
                link.set(qn("r:id"), paragraph.part.relate_to(href, RT.HYPERLINK, is_external=True))
            paragraph._p.append(link)
        elif kind == "link_close":
            link = None
        elif kind == "image":
            paragraph.add_run().add_picture(str(local_image(base, token.attrGet("src") or "")), width=Inches(6.1))
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.space_after = Pt(12)
        elif kind in {"text", "code_inline", "softbreak", "hardbreak"}:
            value = " " if kind == "softbreak" else "\n" if kind == "hardbreak" else token.content
            run = paragraph.add_run(value)
            if bold or header:
                run.bold = True
            if emphasis:
                run.italic = True
            if kind == "code_inline":
                run.font.name = "Consolas"
                run.font.size = Pt(10.5)
            if link is not None:
                run.font.color.rgb = RGBColor.from_string("176B62")
                run.underline = True
                link.append(run._r)
        else:
            raise ValueError(f"LAYOUT_UNSUPPORTED_INLINE: {kind}")


def _build(pack: Path, output: Path, content_name: str) -> None:
    markdown = (pack / content_name).resolve()
    if not markdown.is_relative_to(pack.resolve()):
        raise ValueError("LAYOUT_ARTICLE_OUTSIDE_PACK")
    title, tokens = parse_article(markdown.read_text(encoding="utf-8"))
    if not title:
        raise ValueError(f"missing H1 title in {markdown}")
    expected = [(pack / "images" / f"{pack_date(pack)}-{i:02d}.png").resolve() for i in range(1, 6)]
    if article_images(tokens, markdown.parent) != expected:
        raise ValueError(f"{content_name} must reference the five standard images once and in order")

    doc = Document()
    configure_document(doc)
    paragraph = None
    lists = []
    quote_depth = 0
    table = row = cell = None
    cell_index = 0
    cell_header = False
    for token in tokens:
        kind = token.type
        if kind in {"bullet_list_open", "ordered_list_open"}:
            ordered = kind == "ordered_list_open"
            lists.append({"id": numbering(doc, int(token.attrGet("start") or 1), len(lists) + 1, ordered), "first": False})
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif kind == "list_item_open":
            lists[-1]["first"] = True
        elif kind == "blockquote_open":
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
        elif kind in {"heading_open", "paragraph_open"}:
            style = f"Heading {token.tag[1:]}" if kind == "heading_open" else "Quote" if quote_depth else "Normal"
            paragraph = doc.add_paragraph(style=style)
            if lists:
                if lists[-1]["first"]:
                    props = paragraph._p.get_or_add_pPr().get_or_add_numPr()
                    props.get_or_add_ilvl().val = 0
                    props.get_or_add_numId().val = lists[-1]["id"]
                    lists[-1]["first"] = False
                else:
                    paragraph.paragraph_format.left_indent = Inches(0.25 * len(lists))
        elif kind == "inline":
            target = cell.paragraphs[0] if cell is not None else paragraph
            if target is None:
                raise ValueError("LAYOUT_INLINE_WITHOUT_BLOCK")
            append_inline(target, token.children or [], markdown.parent, header=cell_header)
        elif kind in {"fence", "code_block"}:
            paragraph = doc.add_paragraph()
            paragraph.add_run(token.content.rstrip("\n")).font.name = "Consolas"
        elif kind == "hr":
            paragraph = doc.add_paragraph()
            borders = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            for name, val in [("val", "single"), ("sz", "4"), ("color", "D6DDDA")]:
                bottom.set(qn(f"w:{name}"), val)
            borders.append(bottom)
            paragraph._p.get_or_add_pPr().append(borders)
        elif kind == "table_open":
            table = doc.add_table(rows=0, cols=0)
            table.style = "Table Grid"
        elif kind == "tr_open":
            row = table.add_row()
            cell_index = 0
        elif kind in {"td_open", "th_open"}:
            if cell_index >= len(table.columns):
                table.add_column(Inches(1))
            cell = row.cells[cell_index]
            cell_header = kind == "th_open"
            cell_index += 1
        elif kind in {"td_close", "th_close"}:
            cell = None
            cell_header = False
        elif kind == "table_close":
            for column in table.columns:
                column.width = Inches(6.1 / len(table.columns))
            table = None
        elif kind in {"heading_close", "paragraph_close", "list_item_close", "thead_open", "thead_close", "tbody_open", "tbody_close", "tr_close"}:
            pass
        else:
            raise ValueError(f"LAYOUT_UNSUPPORTED_BLOCK: {kind}")

    doc.core_properties.title = title
    doc.core_properties.author = "华子社长"
    buffer = io.BytesIO()
    doc.save(buffer)
    if output.is_relative_to(pack.resolve()):
        create_pack_backup(pack, "article-docx-export", [output.relative_to(pack.resolve()).as_posix()])
    atomic_write_bytes(output, buffer.getvalue())


def build(pack: Path, output: Path, content_name: str = "toutiao-article.md") -> None:
    output = output.resolve()
    if output.suffix.lower() != ".docx":
        raise ValueError("LAYOUT_OUTPUT_MUST_BE_DOCX")
    with pack_lock(pack, "article-docx-export"):
        _build(pack, output, content_name)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--content", default="toutiao-article.md")
    args = parser.parse_args()
    build(args.pack.resolve(), args.output.resolve(), args.content)
    print(f"output={args.output.resolve()}")


if __name__ == "__main__":
    main()
