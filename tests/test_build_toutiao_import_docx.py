from pathlib import Path

from docx import Document
from PIL import Image

from scripts.build_toutiao_import_docx import build, pack_date


def make_pack(tmp_path: Path) -> Path:
    pack = tmp_path / "2026-08-08-test-huazi"
    images = pack / "images"
    images.mkdir(parents=True)
    for index in range(1, 6):
        Image.new("RGB", (40 + index, 30 + index), (index * 20, 80, 120)).save(
            images / f"2026-08-08-{index:02d}.png"
        )

    lines = ["# 测试标题"]
    for index in range(1, 6):
        lines.extend(
            [
                f"第 {index} 段正文。",
                f"![图 {index}](images/2026-08-08-{index:02d}.png)",
            ]
        )
    (pack / "toutiao-article.md").write_text("\n\n".join(lines), encoding="utf-8")
    return pack


def test_build_uses_pack_date_and_embeds_markdown_images(tmp_path: Path) -> None:
    pack = make_pack(tmp_path)
    output = tmp_path / "toutiao.docx"

    build(pack, output)

    doc = Document(output)
    assert pack_date(pack) == "2026-08-08"
    assert doc.core_properties.title == "测试标题"
    assert doc.core_properties.author == "华子社长"
    assert len(doc.inline_shapes) == 5
    assert "第 5 段正文。" in [paragraph.text for paragraph in doc.paragraphs]


def test_pack_date_rejects_legacy_undated_pack(tmp_path: Path) -> None:
    pack = tmp_path / "legacy-pack"

    try:
        pack_date(pack)
    except ValueError as error:
        assert "YYYY-MM-DD" in str(error)
    else:
        raise AssertionError("undated packs must be rejected")
