from zipfile import ZipFile

import pytest
from docx import Document
from lxml import html

from scripts.article_layout import render_article
from scripts.build_toutiao_import_docx import build
from test_build_toutiao_import_docx import make_pack


def test_html_preserves_semantics_and_escapes_raw_html(tmp_path):
    article = tmp_path / "article.md"
    article.write_text(
        '# Title\n\n## Facts\n\n### Sources\n\n'
        'Model-2 **48%** versus *0%*, n=100; only condition A.\n\n'
        '[source](https://example.com/a?q=1&v=2)\n\n'
        '3. First\n4. Second\n\n> A quote\n\n'
        '`a_b`\n\n<script>alert(1)</script>\n\nAI assisted.', encoding="utf-8")
    rendered, images = render_article(article)
    assert "<h1" not in rendered
    assert "<h2 " in rendered and "<h3 " in rendered
    assert '<ol start="3"' in rendered
    assert '<strong ' in rendered and '<em>' in rendered
    assert 'href="https://example.com/a?q=1&amp;v=2"' in rendered
    assert '<blockquote ' in rendered
    assert '&lt;script&gt;' in rendered and '<script>' not in rendered
    assert "a_b" in rendered and "n=100; only condition A." in rendered
    assert "AI assisted." in rendered and not images
    assert "letter-spacing:0" in rendered


@pytest.mark.parametrize("reference", ["../outside.png", "%2e%2e/outside.png", "https://example.com/img.png"])
def test_rejects_external_images_before_upload(tmp_path, reference):
    article = tmp_path / "article.md"
    article.write_text(f"![x]({reference})", encoding="utf-8")
    uploaded = []
    with pytest.raises(ValueError, match="LAYOUT_"):
        render_article(article, lambda path: uploaded.append(path))
    assert not uploaded


def test_docx_preserves_heading_links_formatting_numbering_and_table(tmp_path):
    pack = make_pack(tmp_path)
    article = pack / "toutiao-article.md"
    original = article.read_text(encoding="utf-8")
    article.write_text(original + '\n\n### Sources\n\n'
        '**48%** versus *0%*, model-2, condition A. [source](https://example.com/source)\n\n'
        '3. Three\n4. Four\n\nParagraph\n\n1. Restart\n\n'
        '> Quote with `a_b`\n\n| Metric | Value |\n| --- | --- |\n| n | 100 |\n\nAI assisted.', encoding="utf-8")
    before = article.read_bytes()
    output = tmp_path / "article.docx"
    build(pack, output)
    doc = Document(output)
    assert len(doc.inline_shapes) == 5
    assert any(p.text == "Sources" and p.style.name == "Heading 3" for p in doc.paragraphs)
    assert any(r.text == "48%" and r.bold for p in doc.paragraphs for r in p.runs)
    assert any(r.text == "0%" and r.italic for p in doc.paragraphs for r in p.runs)
    assert doc.tables[0].cell(1, 1).text == "100"
    assert article.read_bytes() == before
    assert "AI assisted." in [p.text for p in doc.paragraphs]
    with ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode()
        rels = archive.read("word/_rels/document.xml.rels").decode()
        numbering_xml = archive.read("word/numbering.xml").decode()
    assert "w:hyperlink" in xml and "https://example.com/source" in rels
    assert '<w:start w:val="3"' in numbering_xml
    assert "[source](" not in xml and "###" not in xml


def test_docx_rejects_path_remapping(tmp_path):
    pack = make_pack(tmp_path)
    article = pack / "toutiao-article.md"
    article.write_text(article.read_text(encoding="utf-8").replace('images/2026-08-08-01.png', '../images/2026-08-08-01.png'), encoding="utf-8")
    with pytest.raises(ValueError, match="LAYOUT_IMAGE_OUTSIDE_PACK"):
        build(pack, tmp_path / "article.docx")
    assert not (tmp_path / "article.docx").exists()


def test_docx_in_pack_export_uses_recovery_point(tmp_path):
    pack = make_pack(tmp_path)
    output = pack / "import.docx"
    build(pack, output)
    build(pack, output)
    assert len(list((pack / ".backups").glob("*/backup-manifest.json"))) == 2
    assert not (pack / ".distribution-action.lock").exists()


def test_preview_preserves_inputs_and_exports_exactly_five_platforms(tmp_path):
    from scripts.preview_article_layout import FILES, export
    pack = make_pack(tmp_path)
    source = (pack / "toutiao-article.md").read_text(encoding="utf-8")
    for platform, name in FILES.items():
        (pack / name).write_text(source if platform != "xiaohongshu" else "# Title\n\nAI assisted.\n\n- Task", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in pack.glob("*.md")}
    report = export(pack, tmp_path / "preview")
    assert set(report["previews"]) == set(FILES)
    assert report["platform_drafts_modified"] is False
    assert before == {p.name: p.read_bytes() for p in pack.glob("*.md")}
    for platform in FILES:
        page = (tmp_path / "preview" / f"{platform}.html").read_text(encoding="utf-8")
        assert page.count("<img ") == 5
        assert page.count("<h1") == 1
    with pytest.raises(ValueError, match="OUTSIDE_PACK"):
        export(pack, pack / "preview")


def test_docx_rejects_reordered_images_without_replacing_existing_output(tmp_path):
    pack = make_pack(tmp_path)
    output = tmp_path / "article.docx"
    build(pack, output)
    previous = output.read_bytes()
    article = pack / "toutiao-article.md"
    source = article.read_text(encoding="utf-8")
    source = source.replace("-01.png", "-TEMP.png").replace("-02.png", "-01.png").replace("-TEMP.png", "-02.png")
    article.write_text(source, encoding="utf-8")
    with pytest.raises(ValueError, match="in order"):
        build(pack, output)
    assert output.read_bytes() == previous


def test_wechat_only_preview_needs_no_paused_manuscripts(tmp_path):
    from scripts.preview_article_layout import export
    pack = make_pack(tmp_path)
    source = (pack / "toutiao-article.md").read_text(encoding="utf-8")
    (pack / "wechat-article.md").write_text(source, encoding="utf-8")
    (pack / "toutiao-article.md").unlink()
    (pack / "distribution-plan.json").write_text('{"items":[{"platform_id":"wechat"}]}', encoding="utf-8")
    report = export(pack, tmp_path / "wechat-preview")
    assert set(report["previews"]) == {"wechat"}
    page = (tmp_path / "wechat-preview/wechat.html").read_text(encoding="utf-8")
    assert page.count("<img ") == 5
    assert 'toutiao.html' not in page


@pytest.mark.parametrize("theme", ["huazi", "huazi-light"])
def test_huazi_theme_adds_identity_without_changing_source_text_or_images(tmp_path, theme):
    pack = make_pack(tmp_path)
    article = pack / "toutiao-article.md"
    source = article.read_text(encoding="utf-8")
    source += '\n\n## 一条判断\n\n仅在条件A下 **48%**；n=100，不能外推。\n\n### 一手资料\n\n[source](https://example.com/source)\n\nAI辅助整理。'
    article.write_text(source, encoding="utf-8")
    plain, plain_images = render_article(article, theme="plain")
    branded, branded_images = render_article(article, theme=theme)
    assert plain_images == branded_images
    assert branded.count('<img ') == 5
    assert '<svg' not in branded and 'data:image' not in branded
    assert "华子观察簿" in branded and "↗" in branded
    if theme == "huazi":
        assert "01 / 华子拆解" in branded and "◇ 华子图解 / 05" in branded
    else:
        assert "华子拆解" not in branded and "图 05" in branded
    assert "已核验" not in branded
    tree = html.fromstring(branded)
    for decoration in tree.xpath('//*[@data-huazi-decoration]'):
        if decoration.getparent() is not None:
            decoration.drop_tree()
    assert "".join(tree.text_content().split()) == "".join(html.fromstring(plain).text_content().split())
    assert article.read_text(encoding="utf-8") == source


@pytest.mark.parametrize("theme", ["plain", "huazi", "huazi-light"])
def test_huazi_theme_keeps_exactly_one_visible_title_in_preview(tmp_path, theme):
    from scripts.preview_article_layout import FILES, export
    pack = make_pack(tmp_path)
    source = (pack / "toutiao-article.md").read_text(encoding="utf-8")
    for platform, name in FILES.items():
        (pack / name).write_text(source if platform != "xiaohongshu" else "# Title\n\n正文与AI说明", encoding="utf-8")
    output = tmp_path / theme
    result = export(pack, output, theme)
    for platform in FILES:
        page = (output / f"{platform}.html").read_text(encoding="utf-8")
        assert page.count('<h1') == 1
        assert page.count('<img ') == 5
        assert ('华子观察簿' in page) == (theme != "plain")
    assert result['platform_native_styling_verified'] is False


@pytest.mark.parametrize("theme", ["huazi", "huazi-light"])
def test_source_region_is_explicit_scoped_and_preserves_list_numbering(tmp_path, theme):
    article = tmp_path / "article.md"
    article.write_text(
        '# Title\n\n## Context\n\n- Ordinary item\n\n'
        '> ### Sources\n>\n> - Not a top-level bibliography\n\n'
        '## Sources\n\n- [First](https://example.com/a)\n\n'
        '3. [Third](https://example.com/b)\n4. Fourth\n\n'
        '## Next\n\n- Ordinary again\n', encoding="utf-8")
    rendered, _ = render_article(article, theme=theme)
    tree = html.fromstring(rendered)
    sources = tree.xpath('//*[@data-huazi-sources]')
    assert len(sources) == 2
    assert sources[0].tag == "ul" and "list-style:none" in sources[0].get("style")
    assert sources[1].tag == "ol" and sources[1].get("start") == "3"
    assert "list-style:none" not in sources[1].get("style")
    assert not tree.xpath('//blockquote//*[@data-huazi-sources]')
    headings = tree.xpath('//h2')
    assert headings[1].text_content().strip() == "↗Sources"
    if theme == "huazi":
        assert "02 / 华子拆解" in rendered and "03 / 华子拆解" not in rendered
    else:
        assert "华子拆解" not in rendered
        assert headings[0].text_content() == "01Context"
        assert headings[2].text_content() == "02Next"
    assert not tree.xpath('//ul[last()][@data-huazi-sources]')


def test_link_icons_only_mark_external_links(tmp_path):
    article = tmp_path / "article.md"
    article.write_text('[local](#part) [one](https://example.com) [two](http://example.com)\n', encoding="utf-8")
    rendered, _ = render_article(article)
    links = html.fromstring(rendered).xpath('//a')
    assert not links[0].xpath('.//*[@data-huazi-decoration]')
    assert [link.text_content() for link in links] == ["local", "one↗", "two↗"]


@pytest.mark.parametrize("theme", ["plain", "huazi", "huazi-light"])
def test_image_dimensions_and_caption_spacing_are_stable(tmp_path, theme):
    pack = make_pack(tmp_path)
    rendered, _ = render_article(pack / "toutiao-article.md", theme=theme)
    tree = html.fromstring(rendered)
    for image in tree.xpath('//img'):
        assert int(image.get("width")) > 0 and int(image.get("height")) > 0
        assert "height:auto" in image.get("style")
    assert "margin:-" not in rendered
    if theme == "huazi-light":
        assert "图 01" in rendered and "图 05" in rendered
        assert "华子图解" not in rendered


def test_lead_follows_cover_without_changing_image_or_list(tmp_path):
    pack = make_pack(tmp_path)
    article = pack / "toutiao-article.md"
    article.write_text(
        '# Title\n\n![cover](images/2026-08-08-01.png)\n\n'
        '首段结论，限于条件A。\n\n- 列表内容\n\n## 小节\n\n后文。',
        encoding='utf-8',
    )
    rendered, images = render_article(article)
    tree = html.fromstring(rendered)
    cover = tree.xpath('//img')[0].getparent()
    lead = next(p for p in tree.xpath('//p') if '首段结论' in p.text_content())
    assert 'background:#f1f6fa' not in cover.get('style', '')
    assert 'background:#f1f6fa' in lead.get('style', '')
    assert 'background:#f1f6fa' not in tree.xpath('//li')[0].get('style', '')
    assert len(images) == 1


def test_no_lead_is_invented_after_first_section(tmp_path):
    article = tmp_path / 'article.md'
    article.write_text('# Title\n\n## 标题\n\n内容。', encoding='utf-8')
    rendered, _ = render_article(article)
    paragraph = html.fromstring(rendered).xpath('//p')[0]
    assert 'background:#f1f6fa' not in paragraph.get('style', '')
