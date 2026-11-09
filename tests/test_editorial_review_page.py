import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from lxml import html
from PIL import Image

from scripts import editorial_review_page as review
import source_evidence as grounding
from distribution_io import canonical_sha256


@pytest.fixture
def pack(tmp_path):
    root = tmp_path / "2026-10-28-review-huazi"
    (root / "images").mkdir(parents=True)
    Image.new("RGB", (32, 32), "green").save(root / "reference.png")
    items = []
    article = ["# 可核验的模型变化", "公告更新了说明，读者仍需核对自己的任务。"]
    for index, role in enumerate(review.ROLES, 1):
        image = f"images/2026-10-28-{index:02d}.png"
        Image.new("RGB", (900, 383) if index == 1 else (1080, 720), (index * 30, 90, 130)).save(root / image)
        article.extend([f"## 章节 {index}", f"解释 {index} 的实际内容与限制。", f"![图片 {index}]({image})"])
        items.append({"role": role, "image": image, "section_anchor": f"章节 {index}", "message": f"信息 {index}",
                      "prompt": f"Draw object {index}", "layout": "One subject", "on_image_text": "",
                      "evidence_mode": "source_fact", "claim_ids": ["c1"]})
    (root / "wechat-article.md").write_text("\n\n".join(article) + "\n\n本文由AI辅助整理。", encoding="utf-8")
    (root / "distribution-plan.json").write_text(json.dumps({"items": [{"platform_id": "wechat"}]}), encoding="utf-8")
    (root / "image-storyboard.json").write_text(json.dumps({"schema_version": 1, "character_reference": "reference.png", "palette": "white, green", "items": items}), encoding="utf-8")
    claims = {"schema_version": 3, "sources": [{"source_id": "s1", "title": "Primary source", "kind": "official", "url": "https://example.org/source"}],
              "claims": [{"claim_id": "c1", "text": "48%（n=25），仅条件X，不外推。", "verdict": "verified", "source_ids": ["s1"]}]}
    (root / "claim-verification.json").write_text(json.dumps(claims), encoding="utf-8")
    return root


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


def test_offline_export_has_five_original_images_and_no_account_or_score(pack, tmp_path):
    before = hashes(pack)
    output = tmp_path / "preview"
    manifest = review.export(pack, output)
    assert hashes(pack) == before
    assert review.verify(output)["status"] == "current_local_snapshot_only"
    assert not manifest["platform_action_performed"]
    assert not manifest["visual_review_performed"]
    assert not manifest["fact_review_performed"]
    page = (output / review.PAGE).read_text(encoding="utf-8")
    tree = html.fromstring(page)
    assert len(tree.xpath("//img")) == 5
    assert all(node.get("src").startswith("data:image/png;base64,") for node in tree.xpath("//img"))
    assert not tree.xpath("//script | //form | //iframe")
    assert "default-src 'none'" in tree.xpath('//meta[@http-equiv="Content-Security-Policy"]')[0].get("content")
    assert "48%（n=25），仅条件X，不外推。" in tree.text_content()
    assert "没有关联主张" not in page
    assert "toutiao" not in page


def test_untrusted_copy_is_escaped_not_executed(pack, tmp_path):
    path = pack / "image-storyboard.json"
    board = json.loads(path.read_text(encoding="utf-8"))
    board["items"][0]["message"] = '<script>alert("bad")</script><img src="https://invalid.test/tracker">'
    path.write_text(json.dumps(board), encoding="utf-8")
    output = tmp_path / "preview"
    review.export(pack, output)
    tree = html.fromstring((output / review.PAGE).read_text(encoding="utf-8"))
    assert not tree.xpath("//script")
    assert len(tree.xpath("//img")) == 5
    assert "<script>" in tree.text_content()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///private", "https://user:secret@example.org", "https://example.org/?token=secret", "https://[", None, {}])
def test_credentials_and_query_urls_are_not_rendered(url):
    assert review.source_url(url) == "链接未展示"


def test_missing_or_ambiguous_claim_is_displayed_not_filled(pack, tmp_path):
    path = pack / "claim-verification.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["claims"].append(copy.deepcopy(data["claims"][0]))
    path.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "preview"
    review.export(pack, output)
    page = (output / review.PAGE).read_text(encoding="utf-8")
    assert page.count("主张关联缺失或歧义") == 5
    assert "BRIEF_CLAIM_LINK_UNRESOLVED" in page


@pytest.mark.parametrize("name", ["wechat-article.md", "claim-verification.json", "image-storyboard.json"])
def test_changed_input_invalidates_snapshot(pack, tmp_path, name):
    output = tmp_path / "preview"
    review.export(pack, output)
    path = pack / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="INPUT_CHANGED"):
        review.verify(output)


def test_changed_image_invalidates_snapshot(pack, tmp_path):
    output = tmp_path / "preview"
    review.export(pack, output)
    Image.new("RGB", (900, 383), "red").save(pack / "images/2026-10-28-01.png")
    with pytest.raises(ValueError, match="INPUT_CHANGED"):
        review.verify(output)


def test_changed_page_and_manifest_flags_are_rejected(pack, tmp_path):
    output = tmp_path / "preview"
    review.export(pack, output)
    page = output / review.PAGE
    page.write_bytes(page.read_bytes() + b" ")
    with pytest.raises(ValueError, match="PAGE_CHANGED"):
        review.verify(output)
    manifest_path = output / review.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["platform_action_performed"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="MANIFEST_INVALID"):
        review.verify(output)


def test_output_must_be_fresh_and_outside_packs(pack, tmp_path):
    for output in (pack / "review", pack.parent, review.ROOT / "outputs/test-review", review.ROOT / "archive/test-review"):
        with pytest.raises(ValueError, match="OUTSIDE_PACK"):
            review.export(pack, output)
    output = tmp_path / "preview"
    review.export(pack, output)
    original = (output / review.PAGE).read_bytes()
    with pytest.raises(ValueError, match="FRESH_OUTPUT"):
        review.export(pack, output)
    assert (output / review.PAGE).read_bytes() == original


def test_mutation_during_render_leaves_no_success_manifest(pack, tmp_path):
    original = review.render_page
    def mutate(*args):
        result = original(*args)
        path = pack / "wechat-article.md"
        path.write_bytes(path.read_bytes() + b"\n")
        return result
    output = tmp_path / "preview"
    with patch.object(review, "render_page", side_effect=mutate), pytest.raises(ValueError, match="INPUT_CHANGED"):
        review.export(pack, output)
    assert not (output / review.MANIFEST).exists()


def test_bound_read_rejects_changed_missing_and_outside_paths(pack):
    with pytest.raises(ValueError, match="INPUT_CHANGED"):
        review.read_bound(pack, "wechat-article.md", {"wechat-article.md": "0" * 64}, required=True)
    with pytest.raises(ValueError, match="MISSING"):
        review.read_bound(pack, "missing.png", {"missing.png": "0" * 64}, required=True)
    with pytest.raises(ValueError, match="OUTSIDE_PACK"):
        review.read_bound(pack, "../other.png", {"../other.png": "0" * 64}, required=True)


@pytest.mark.parametrize("kind", ["corrupt", "wrong_size", "animated"])
def test_bad_images_do_not_create_complete_preview(pack, tmp_path, kind):
    path = pack / "images/2026-10-28-01.png"
    if kind == "corrupt":
        path.write_bytes(b"not-png")
    elif kind == "wrong_size":
        Image.new("RGB", (20, 20)).save(path)
    else:
        Image.new("RGB", (900, 383), "red").save(path, save_all=True, append_images=[Image.new("RGB", (900, 383), "blue")], duration=100, loop=0)
    output = tmp_path / "preview"
    with pytest.raises((OSError, ValueError)):
        review.export(pack, output)
    assert not (output / review.MANIFEST).exists()


def test_missing_optional_authoring_plan_does_not_migrate(pack, tmp_path):
    output = tmp_path / "preview"
    review.export(pack, output)
    assert not (pack / "editorial-experiment.json").exists()
    page = (output / review.PAGE).read_text(encoding="utf-8")
    assert "DELIVERY_ANCHORS_OPTIONAL" in page
    assert "计划声明；未定位正文原句" in page


def test_cli_export_verify_and_error_are_explicit(pack, tmp_path, capsys):
    output = tmp_path / "preview"
    assert review.main(["--pack", str(pack), "--output", str(output)]) == 0
    assert "local_review_only" in capsys.readouterr().out
    assert review.main(["--verify", str(output)]) == 0
    assert "current_local_snapshot_only" in capsys.readouterr().out
    assert review.main(["--pack", str(pack), "--output", str(output)]) == 2


QUOTE = "Model A returned 48% (n=25, task X only)."


def snapshot_fixture(pack, original=None, quote=QUOTE):
    original = original or "Test fixture, not a real study.\n" + quote + "\nDo not extrapolate to other tasks."
    source = {"schema_version": 1, "kind": "public_source_snapshot", "source_id": "s1",
              "url": "https://example.org/source", "title": "Synthetic fixture",
              "captured_at": "2026-10-28T05:00:00+00:00", "extractor": "fixture-text",
              "input_sha256": hashlib.sha256(original.encode()).hexdigest(),
              "provenance": grounding.PROVENANCE, "text": original}
    source["snapshot_id"] = canonical_sha256(source)
    path = pack / "sources/s1-fixture.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    claim_path = pack / "claim-verification.json"
    payload = json.loads(claim_path.read_text(encoding="utf-8"))
    payload["sources"][0]["source_snapshot"] = {"path": "sources/s1-fixture.json", "sha256": review.file_sha256(path)}
    payload["claims"][0]["evidence_spans"] = [{"source_id": "s1", **grounding.locate(original, quote)}]
    claim_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, payload


def test_quote_context_preserves_full_conditions_and_original_unicode_offsets(pack, tmp_path):
    _, payload = snapshot_fixture(pack, "前提：仅示例。\U0001f4c4" + QUOTE + "\n不能外推，也不是实测结果。")
    citations = review.citation_context(pack, payload)
    excerpt = citations["claims"][0][0]
    assert excerpt["quote"] == QUOTE and excerpt["before"] == "前提：仅示例。\U0001f4c4"
    assert "不能外推" in excerpt["after"]
    output = tmp_path / "preview"
    manifest = review.export(pack, output)
    assert manifest["schema_version"] == 2
    assert len(manifest["source_inputs"]["files"]) == 1
    page = (output / review.PAGE).read_text(encoding="utf-8")
    assert QUOTE in page and "不代表事实、语义或时效已复核" in page
    assert review.verify(output)["status"] == "current_local_snapshot_only"


def test_quote_integrity_never_claims_semantic_truth(pack, tmp_path):
    _, payload = snapshot_fixture(pack)
    payload["claims"][0]["text"] = "Unjustified claim: perfect on all tasks."
    (pack / "claim-verification.json").write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "preview"
    manifest = review.export(pack, output)
    assert manifest["fact_review_performed"] is False
    assert manifest["visual_review_performed"] is False
    assert manifest["platform_action_performed"] is False
    assert "perfect on all tasks" in (output / review.PAGE).read_text(encoding="utf-8")


def test_quote_and_context_html_is_escaped(pack, tmp_path):
    quote = '<script>fake("48%")</script> is untrusted text.'
    snapshot_fixture(pack, "<img src=https://invalid.test>Before. " + quote + " End.", quote)
    output = tmp_path / "preview"
    review.export(pack, output)
    tree = html.fromstring((output / review.PAGE).read_text(encoding="utf-8"))
    assert not tree.xpath("//script") and len(tree.xpath("//img")) == 5
    assert quote in tree.text_content()


@pytest.mark.parametrize("field,value", [("start", True), ("start", -1), ("end", "12"), ("end", 999999),
                                        ("quote", "Invented matching words"), ("source_id", "unknown"), ("source_id", [])])
def test_invalid_span_is_diagnostic_never_matched(pack, field, value):
    _, payload = snapshot_fixture(pack)
    payload["claims"][0]["evidence_spans"][0][field] = value
    citations = review.citation_context(pack, payload)
    assert all("issue" in item for item in citations["claims"][0])


def test_duplicate_source_and_uncited_quote_are_not_resolved(pack):
    _, payload = snapshot_fixture(pack)
    duplicate = copy.deepcopy(payload)
    duplicate["sources"].append(copy.deepcopy(duplicate["sources"][0]))
    assert review.citation_context(pack, duplicate)["claims"][0][0]["issue"] == "REVIEW_QUOTE_SOURCE_UNRESOLVED"
    payload["claims"][0]["source_ids"] = []
    assert review.citation_context(pack, payload)["claims"][0][0]["issue"] == "REVIEW_QUOTE_SOURCE_UNRESOLVED"


def test_each_cited_source_needs_a_displayed_excerpt(pack):
    _, payload = snapshot_fixture(pack)
    payload["claims"][0]["source_ids"].append("missing")
    excerpts = review.citation_context(pack, payload)["claims"][0]
    assert excerpts[0]["quote"] == QUOTE
    assert excerpts[-1]["issue"] == "REVIEW_CITED_SOURCE_NOT_SHOWN"


@pytest.mark.parametrize("kind", ["path", "hash", "missing", "identity", "url", "schema"])
def test_declared_bad_snapshot_prevents_export(pack, tmp_path, kind):
    path, payload = snapshot_fixture(pack)
    source = payload["sources"][0]
    if kind == "path":
        source["source_snapshot"]["path"] = "../outside.json"
    elif kind == "hash":
        source["source_snapshot"]["sha256"] = "0" * 64
    elif kind == "missing":
        path.unlink()
    elif kind == "identity":
        source["source_id"] = "other"
    elif kind == "url":
        source["url"] = "https://example.org/different"
    elif kind == "schema":
        path.write_text("{}", encoding="utf-8")
        source["source_snapshot"]["sha256"] = review.file_sha256(path)
    (pack / "claim-verification.json").write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "preview"
    with pytest.raises((OSError, ValueError)):
        review.export(pack, output)
    assert not (output / review.MANIFEST).exists()


def test_snapshot_only_change_invalidates_review(pack, tmp_path):
    path, _ = snapshot_fixture(pack)
    output = tmp_path / "preview"
    review.export(pack, output)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        review.verify(output)


def test_snapshot_reader_change_invalidates_review(pack, tmp_path):
    snapshot_fixture(pack)
    output = tmp_path / "preview"
    review.export(pack, output)
    original = review.file_sha256
    with patch.object(review, "file_sha256", side_effect=lambda p: "0" * 64 if p.name == "source_evidence.py" else original(p)):
        with pytest.raises(ValueError, match="SOURCE_INPUT_CHANGED"):
            review.verify(output)


def test_snapshot_mutation_during_render_leaves_no_manifest(pack, tmp_path):
    path, _ = snapshot_fixture(pack)
    original = review.render_page
    def mutate(*args):
        rendered = original(*args)
        path.write_bytes(path.read_bytes() + b"\n")
        return rendered
    output = tmp_path / "preview"
    with patch.object(review, "render_page", side_effect=mutate), pytest.raises(ValueError, match="HASH_MISMATCH"):
        review.export(pack, output)
    assert not (output / review.MANIFEST).exists()


def test_long_quotes_are_not_silently_shortened_and_context_is_labeled(pack):
    long_quote = "Quoted fixture. " * 300
    _, payload = snapshot_fixture(pack, "Start. " + long_quote + " End.", long_quote)
    assert review.citation_context(pack, payload)["claims"][0][0]["issue"] == "REVIEW_QUOTE_TOO_LONG_FOR_PREVIEW"
    _, payload = snapshot_fixture(pack, "a" * 300 + QUOTE + "b" * 300)
    excerpt = review.citation_context(pack, payload)["claims"][0][0]
    assert excerpt["left_clipped"] and excerpt["right_clipped"]
    assert len(excerpt["before"]) == len(excerpt["after"]) == 180
    assert "上下文为节选" in review.render_excerpts([excerpt])


def test_image_section_context_is_actual_prose_not_just_anchor(pack, tmp_path):
    output = tmp_path / "preview"
    review.export(pack, output)
    tree = html.fromstring((output / review.PAGE).read_text(encoding="utf-8"))
    contexts = tree.xpath('//div[contains(@class,"article-context")]')
    assert len(contexts) == 5
    assert "解释 1 的实际内容与限制。" in contexts[0].text_content()
    assert "解释 2 的实际内容与限制。" not in contexts[0].text_content()


def test_missing_optional_snapshots_remain_explicit_without_migration(pack, tmp_path):
    payload = json.loads((pack / "claim-verification.json").read_text(encoding="utf-8"))
    payload["claims"][0]["evidence_spans"] = [{"source_id": "s1", "start": 0, "end": 40, "quote": QUOTE}]
    citations = review.citation_context(pack, payload)
    assert citations["inputs"]["files"] == []
    assert citations["claims"][0][0]["issue"] == "REVIEW_QUOTE_SNAPSHOT_MISSING"
    assert not (pack / "sources").exists()


def test_v1_preview_is_preserved_but_requires_fresh_export(pack, tmp_path):
    output = tmp_path / "preview"
    review.export(pack, output)
    path = output / review.MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    path.write_text(json.dumps(manifest), encoding="utf-8")
    before = hashes(output)
    with pytest.raises(ValueError, match="MANIFEST_INVALID"):
        review.verify(output)
    assert hashes(output) == before
