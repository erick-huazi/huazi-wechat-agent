import json
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import image_quality_gate as quality
import content_quality_gate as content_quality
import content_visual_brief as visual_brief
import wechat_retention_inspector as retention
import social_draft_router as router
from distribution_io import file_sha256, five_image_names
from wechat_draft_publisher import validate_preflight, verify_draft
from content_workflow_gate import asset_gate
from preview_article_layout import export


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def six_image_pack(tmp_path):
    pack = tmp_path / "2026-11-09-extra-images-huazi"
    (pack / "images").mkdir(parents=True)
    write_json(pack / "image-count.json", {"schema_version": 1, "count": 6})
    board = quality.skeleton(pack)
    board.update(character_reference="Synthetic fixture", palette="blue and white")
    article = "# A synthetic topic\n\n"
    for index, row in enumerate(board["items"]):
        row.update(section_anchor=f"Section {index}", message=f"Information {index}",
                   layout=f"layout {index}", prompt=f"Explain information {index}.")
        image = Image.new("RGB", (900, 383) if index == 0 else (1080, 720), (20 + index * 30, 200 - index * 20, 50 + index * 20))
        ImageDraw.Draw(image).rectangle((40 + index * 30, 40, 150 + index * 100, 300), fill="white")
        image.save(pack / row["image"])
        article += f"## Section {index}\n\nA substantive explanation of the fact, its limitations and practical use.\n\n![panel]({row['image']})\n\n"
    (pack / "wechat-article.md").write_text(article, encoding="utf-8")
    write_json(pack / "distribution-plan.json", {"items": [{"platform_id": "wechat"}]})
    write_json(pack / "image-storyboard.json", board)
    review = {"schema_version": 1, "reviewer": "synthetic test fixture",
              "reviewed_at": datetime.now(timezone.utc).isoformat(),
              "storyboard_sha256": file_sha256(pack / "image-storyboard.json"),
              "article_sha256": file_sha256(pack / "wechat-article.md"),
              "items": [{"image": row["image"], "sha256": file_sha256(pack / row["image"]),
                         "scores": {k: 5 for k in quality.load_standard()["review_dimensions"]},
                         "fact_checked": True, "no_engagement_bait": True, "notes": "Synthetic fixture."}
                        for row in board["items"]]}
    write_json(pack / "image-visual-review.json", review)
    return pack


def test_six_declared_images_are_all_checked_and_bound(six_image_pack):
    pack = six_image_pack
    report = quality.build_report(pack)
    assert report["passed"], report["issues"]
    assert len(report["images"]) == 6 and report["visual_score"] == 100
    assert any(row["path"] == "image-count.json" for row in report["inputs"])
    extra = pack / "images" / five_image_names(pack)[5]
    extra.write_bytes(b"broken")
    report = quality.build_report(pack)
    assert not report["passed"]
    assert "IMAGE_DECODE_OR_SIZE" in {item["code"] for item in report["issues"]}


def test_missing_or_reordered_supplement_fails(six_image_pack):
    pack = six_image_pack
    article = pack / "wechat-article.md"
    source = article.read_text(encoding="utf-8")
    article.write_text(source.replace("2026-11-09-06.png", "2026-11-09-05.png"), encoding="utf-8")
    report = quality.build_report(pack)
    assert not report["passed"]
    assert any(item["code"].startswith("IMAGE_") for item in report["issues"])


def test_six_image_asset_gate_and_plan_are_bound_to_all_images(six_image_pack):
    pack = six_image_pack
    report = quality.build_report(pack)
    write_json(pack / "image-quality-report.json", report)
    assert asset_gate(pack)["passed"]
    registry = router.load_registry(Path(__file__).resolve().parents[1] / "config/social-platforms.json")
    wechat = next(row for row in registry["platforms"] if row["id"] == "wechat")
    plan = router.plan_item(pack, "Synthetic topic", wechat)
    assert plan["required_image_count"] == 6 and plan["image_count"] == 6
    assert not any("images_found" in problem or "canonical_01" in problem for problem in plan["problems"])
    (pack / "images" / five_image_names(pack)[5]).unlink()
    assert not asset_gate(pack)["passed"]


def test_content_quality_counts_all_declared_images(six_image_pack):
    standard = content_quality.load_standard()
    result = content_quality.platform_item(six_image_pack, "wechat", standard["platforms"]["wechat"], standard)
    assert result["checks"]["image_references"]
    article = six_image_pack / "wechat-article.md"
    article.write_text(article.read_text(encoding="utf-8").replace("![panel](images/2026-11-09-06.png)", ""), encoding="utf-8")
    result = content_quality.platform_item(six_image_pack, "wechat", standard["platforms"]["wechat"], standard)
    assert not result["checks"]["image_references"]


def test_brief_includes_supplement_not_only_the_first_five(six_image_pack):
    result = visual_brief.compile_brief(six_image_pack)
    assert len(result["image_prompts"]) == 6
    assert result["image_prompts"][5]["role"] == "supplement"
    assert "image 6/6" in result["image_prompts"][5]["final_prompt"]


def test_preview_contains_all_six_and_binds_declaration(six_image_pack, tmp_path):
    preview = tmp_path / "preview"
    manifest = export(six_image_pack, preview)
    assert manifest["previews"]["wechat"]["image_count"] == 6
    assert manifest["image_count_sha256"] == file_sha256(six_image_pack / "image-count.json")
    assert (preview / "wechat.html").read_text(encoding="utf-8").count("<img ") == 6


def test_supplement_requires_hash_bound_visual_review(six_image_pack):
    pack = six_image_pack
    review_path = pack / "image-visual-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["items"].pop()
    write_json(review_path, review)
    report = quality.build_report(pack)
    assert not report["passed"] and report["visual_score"] is None
    assert "IMAGE_VISUAL_REVIEW_MISSING" in {item["code"] for item in report["issues"]}


def test_supplement_can_appear_between_core_roles(six_image_pack):
    pack = six_image_pack
    board_path, review_path = pack / "image-storyboard.json", pack / "image-visual-review.json"
    board = json.loads(board_path.read_text(encoding="utf-8"))
    for row, role in zip(board["items"], ["cover", "supplement", "news_fact", "mechanism", "scenario", "action_boundary"]):
        row["role"] = role
    write_json(board_path, board)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["storyboard_sha256"] = file_sha256(board_path)
    write_json(review_path, review)
    report = quality.build_report(pack)
    assert report["passed"], report["issues"]
    brief = visual_brief.compile_brief(pack)
    assert brief["image_prompts"][1]["role"] == "supplement"
    assert brief["image_prompts"][2]["role"] == "news_fact"


def test_declared_source_image_missing_basis_blocks_image_gate(six_image_pack, monkeypatch):
    pack = six_image_pack
    board_path, article_path = pack / "image-storyboard.json", pack / "wechat-article.md"
    review_path = pack / "image-visual-review.json"
    board = json.loads(board_path.read_text(encoding="utf-8"))
    board["items"][1]["source_image"] = {
        "kind": "adapted", "source_title": "Synthetic source",
        "source_page_url": "https://example.org/article", "source_image_url": "https://example.org/figure.png",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "rights_basis": {}, "modifications": "Crop with conditions preserved", "caption": "来源：示例；裁剪。",
    }
    article = article_path.read_text(encoding="utf-8")
    article_path.write_text(article.replace("![panel](images/2026-11-09-02.png)", "![panel](images/2026-11-09-02.png)\n\n来源：示例；裁剪。"), encoding="utf-8")
    write_json(board_path, board)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["storyboard_sha256"] = file_sha256(board_path)
    review["article_sha256"] = file_sha256(article_path)
    write_json(review_path, review)
    report = quality.build_report(pack)
    assert not report["passed"]
    assert "SOURCE_IMAGE_RIGHTS_BASIS_MISSING" in {item["code"] for item in report["issues"]}
    board["items"][1]["source_image"]["rights_basis"] = {"type": "author_permission", "reference": "User confirmed"}
    write_json(board_path, board)
    review["storyboard_sha256"] = file_sha256(board_path)
    write_json(review_path, review)
    report = quality.build_report(pack)
    assert report["passed"], report["issues"]
    assert report["source_image_advisor_sha256"] == file_sha256(Path(quality.__file__).with_name("source_image_advisor.py"))
    write_json(pack / "image-quality-report.json", report)
    assert quality.gate(pack)["passed"]
    brief = visual_brief.compile_brief(pack)
    assert brief["image_prompts"][1]["source_image_review"]["status"] == "declarations_complete_manual_review_required"
    assert not brief["image_prompts"][1]["source_image_review"]["rights_verified"]
    assert "source_image_advisor_sha256" in brief["inputs"]
    real_hash = quality.file_sha256
    monkeypatch.setattr(quality, "file_sha256", lambda path: "0" * 64 if Path(path).name == "source_image_advisor.py" else real_hash(path))
    assert not quality.gate(pack)["passed"]


@pytest.mark.parametrize("payload", [
    {"schema_version": 1, "count": 4}, {"schema_version": 1, "count": 21},
    {"schema_version": 1, "count": True}, {"schema_version": 1, "count": 6, "extra": 1},
])
def test_invalid_count_is_rejected(six_image_pack, payload):
    write_json(six_image_pack / "image-count.json", payload)
    with pytest.raises(ValueError, match="IMAGE_COUNT_INVALID"):
        five_image_names(six_image_pack)


def test_old_pack_cannot_opt_into_new_contract(tmp_path):
    pack = tmp_path / "2026-11-08-existing-huazi"
    pack.mkdir()
    write_json(pack / "image-count.json", {"schema_version": 1, "count": 6})
    with pytest.raises(ValueError, match="IMAGE_COUNT_SCOPE_INVALID"):
        five_image_names(pack)


def test_official_draft_preflight_and_readback_use_declared_count(six_image_pack):
    pack = six_image_pack
    _, paths = validate_preflight(pack / "wechat-article.md", pack / "images" / five_image_names(pack)[0], expected_images=6)
    assert len(paths) == 6
    result = {"news_item": [{"title": "T", "content": "".join(f'<img src=\"https://example.com/{i}\">' for i in range(6)), "thumb_url": "https://example.com/cover"}]}
    probe = lambda _: {"status": 200, "content_type": "image/png", "bytes": 1}
    assert verify_draft(result, "T", 6, False, probe)["passed"]
    with pytest.raises(RuntimeError, match="expected 5"):
        verify_draft(result, "T", 5, False, probe)


def test_router_passes_plan_count_to_official_publisher(monkeypatch):
    captured = []
    def execute(command, env):
        captured.extend(command)
        return {"returncode": 0, "stdout": json.dumps({"media_id": "fixture", "verification": {"passed": True}})}
    monkeypatch.setattr(router, "run_command", execute)
    item = {"platform_id": "wechat", "platform": "微信公众号", "delivery": "official_draft_api", "content": "article.md",
            "cover": "cover.png", "title": "Title", "image_count": 6, "required_image_count": 6,
            "video_count": 0, "status": "ready", "problems": []}
    args = SimpleNamespace(dry_run=False, confirm_drafts=True, author="", digest="")
    result = router.dispatch_item(item, args)
    assert result["image_count"] == 6
    assert captured[captured.index("--expected-images") + 1] == "6"
    assert result["status"] == "partial"
    assert not result["image_verified"] and not result["cover_verified"] and not result["draft_verified"]
    assert router.resume_decision(result, {"quality": "partial"})["action"] == "verify_existing"


def test_router_retains_id_after_failed_readback(monkeypatch, tmp_path):
    response = {"media_id": "fixture-draft", "verification": {"passed": False}, "verification_error_type": "TimeoutError"}
    monkeypatch.setattr(router, "run_command", lambda command, env: {"returncode": 1, "stdout": json.dumps(response), "stderr": ""})
    item = {"platform_id": "wechat", "platform": "微信公众号", "delivery": "official_draft_api", "content": "article.md",
            "cover": "cover.png", "title": "Title", "image_count": 6, "required_image_count": 6,
            "video_count": 0, "status": "ready", "problems": []}
    result = router.dispatch_item(item, SimpleNamespace(dry_run=False, confirm_drafts=True, author="", digest=""))
    assert result["status"] == "partial" and result["media_id"] == "fixture-draft"
    assert result["evidence"]["verified_at"] == "" and not result["draft_verified"]
    assert router.resume_decision(result, {"quality": "partial"}, retry=True)["action"] == "verify_existing"
    plan = {"plan_id": "fixture", "items": [{"platform_id": "wechat", "required_image_count": 6}]}
    audit = router.audit_distribution(tmp_path, plan, {"result_id": "fixture", "items": [result]})
    assert not audit["complete"] and audit["summary"]["partial"] == 1


def test_timeout_after_identity_keeps_id_and_requires_reverification(monkeypatch):
    identity = json.dumps({"record_type": "draft_identity", "media_id": "fixture-draft"}).encode()
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="fixture", timeout=180, output=identity)
    monkeypatch.setattr(router.subprocess, "run", timeout)
    item = {"platform_id": "wechat", "platform": "微信公众号", "delivery": "official_draft_api", "content": "article.md",
            "cover": "cover.png", "title": "Title", "image_count": 6, "required_image_count": 6,
            "video_count": 0, "status": "ready", "problems": []}
    result = router.dispatch_item(item, SimpleNamespace(dry_run=False, confirm_drafts=True, author="", digest=""))
    assert result["execution"]["timed_out"] is True
    assert result["status"] == "partial" and result["media_id"] == "fixture-draft"
    assert router.resume_decision(result, {"quality": "partial"}, retry=True)["action"] == "verify_existing"


def test_conflicting_identity_records_do_not_create_a_verified_draft(monkeypatch):
    output = '\n'.join([json.dumps({"record_type": "draft_identity", "media_id": "one"}),
                        json.dumps({"media_id": "other", "verification": {"passed": True}})])
    monkeypatch.setattr(router, "run_command", lambda command, env: {"returncode": 0, "stdout": output, "stderr": ""})
    item = {"platform_id": "wechat", "platform": "微信公众号", "delivery": "official_draft_api", "content": "article.md",
            "cover": "cover.png", "title": "Title", "image_count": 6, "required_image_count": 6,
            "video_count": 0, "status": "ready", "problems": []}
    result = router.dispatch_item(item, SimpleNamespace(dry_run=False, confirm_drafts=True, author="", digest=""))
    assert result["status"] == "failed" and not result["media_id"] and not result["draft_verified"]
    assert router.resume_decision(result, {"quality": "unverified"}, retry=True)["action"] == "verify_existing"


def test_six_image_retention_comparison_is_read_only_and_not_visual_approval(six_image_pack):
    pack = six_image_pack
    urls = {name: f"https://mmbiz.qpic.cn/{name}" for name in five_image_names(pack)}
    body, _ = retention.render_article(pack / "wechat-article.md", lambda p: urls[p.name])
    assets = {urls[name]: (pack / "images" / name).read_bytes() for name in urls}
    assets["https://mmbiz.qpic.cn/cover"] = (pack / "images" / five_image_names(pack)[0]).read_bytes()
    response = {"news_item": [{"title": "A synthetic topic", "content": body, "thumb_url": "https://mmbiz.qpic.cn/cover"}]}
    report = retention.inspect_response(pack, response, "A synthetic topic", fetch=assets.__getitem__)
    assert report["title_matches"] and report["body_text_matches"]
    assert report["image_count"] == 6 and report["all_images_match_in_order"]
    assert not report["platform_draft_verified"] and report["cover_visual_review"] == "not_performed"
    assert "mmbiz.qpic.cn" not in json.dumps(report)
    first, second = list(urls.values())[0:2]
    response["news_item"][0]["content"] = body.replace(first, "PLACEHOLDER").replace(second, first).replace("PLACEHOLDER", second)
    changed = retention.inspect_response(pack, response, "A synthetic topic", fetch=assets.__getitem__)
    assert not changed["all_images_match_in_order"]


def test_retention_structure_detects_lost_or_rewritten_links_without_leaking_them():
    expected = '<h2>证据</h2><p><a href="https://example.org/private?token=secret">原文</a></p><ol start="3"><li>三</li></ol>'
    actual = '<h2>证据</h2><p><a href="https://example.org/other">原文</a></p><ol start="1"><li>三</li></ol>'
    report = retention.compare_structure(expected, actual)
    assert report["headings_match"] and not report["links_match_in_order"]
    assert not report["ordered_list_starts_match"]
    assert "secret" not in json.dumps(report) and "example.org" not in json.dumps(report)
    assert report["authority"] == "structural_comparison_only_not_native_clickability"


@pytest.mark.parametrize("url", ["http://mmbiz.qpic.cn/a", "https://127.0.0.1/a", "https://mmbiz.qpic.cn.attacker.test/a", "https://name:password@mmbiz.qpic.cn/a"])
def test_remote_image_origin_is_checked_before_network(url):
    with pytest.raises(ValueError, match="REMOTE_IMAGE_ORIGIN_NOT_ALLOWED"):
        retention.fetch_image(url)


def test_count_writer_rejects_invalid_and_preserves_existing_results(tmp_path):
    root = Path(__file__).resolve().parents[1]
    pack = tmp_path / "2026-11-09-writer-huazi"
    stage = tmp_path / "stage"
    pack.mkdir()
    stage.mkdir()
    command = [sys.executable, str(root / "scripts/pack_content_writer.py"), "--pack", str(pack), "--source", str(stage)]
    write_json(stage / "image-count.json", {"schema_version": 1, "count": 21})
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0 and not (pack / "image-count.json").exists()
    write_json(stage / "image-count.json", {"schema_version": 1, "count": 6})
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert len(five_image_names(pack)) == 6
    write_json(pack / "distribution-results.json", {"items": []})
    write_json(stage / "image-count.json", {"schema_version": 1, "count": 7})
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0 and "IMAGE_COUNT_RESULTS_EXIST" in result.stderr
    assert len(five_image_names(pack)) == 6
