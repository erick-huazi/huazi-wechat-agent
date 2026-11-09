from datetime import datetime, timezone

import pytest

from scripts.source_image_advisor import public_reference, review_source_image


def source_item(kind="adapted"):
    return {"image": "images/2026-11-09-02.png", "source_image": {
        "kind": kind,
        "source_title": "Official research",
        "source_page_url": "https://example.org/research",
        "source_image_url": "https://example.org/assets/figure.png",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "rights_basis": {"type": "author_permission", "reference": "User confirmed permission"},
        "modifications": "Cropped with original conditions retained",
        "caption": "来源：示例研究；原图经过裁剪。",
    }}


def test_declared_details_are_not_treated_as_verified_rights():
    item = source_item()
    article = "# Title\n\n![图](images/2026-11-09-02.png)\n\n来源：示例研究；原图经过裁剪。\n"
    result = review_source_image(item, article)
    assert result["status"] == "declarations_complete_manual_review_required"
    assert result["issues"] == []
    assert not result["rights_verified"] and not result["facts_verified"] and not result["visual_review_performed"]


def test_missing_declaration_does_not_invent_an_origin():
    result = review_source_image({"image": "image.png"}, "")
    assert result["status"] == "not_declared" and not result["rights_verified"]


def test_explicit_null_is_an_invalid_declaration():
    result = review_source_image({"image": "image.png", "source_image": None}, "")
    assert result["status"] == "needs_review"
    assert "SOURCE_IMAGE_DECLARATION_INVALID" in result["issues"]


def test_caption_must_follow_its_image_and_not_be_hidden_as_alt_text():
    item = source_item()
    article = "来源：示例研究；原图经过裁剪。\n\n![来源：示例研究；原图经过裁剪。](images/2026-11-09-02.png)\n\n## 另一节\n"
    result = review_source_image(item, article)
    assert "SOURCE_IMAGE_CAPTION_NOT_ADJACENT" in result["issues"]


def test_missing_use_basis_and_naive_time_are_flagged():
    item = source_item()
    item["source_image"]["rights_basis"] = {}
    item["source_image"]["observed_at"] = "2026-11-08T12:00:00"
    result = review_source_image(item, "")
    assert "SOURCE_IMAGE_RIGHTS_BASIS_MISSING" in result["issues"]
    assert "SOURCE_IMAGE_OBSERVED_TIME_INVALID" in result["issues"]


def test_ai_redrawn_requires_an_explicit_caption_label():
    item = source_item("ai_redrawn")
    article = "![图](images/2026-11-09-02.png)\n\n来源：示例研究；原图经过裁剪。\n"
    assert "SOURCE_IMAGE_AI_LABEL_MISSING" in review_source_image(item, article)["issues"]
    item["source_image"]["caption"] = "来源：示例研究；AI重绘示意。"
    article = "![图](images/2026-11-09-02.png)\n\n来源：示例研究；AI重绘示意。\n"
    assert review_source_image(item, article)["issues"] == []


def test_adapted_caption_identifies_source_and_modification():
    item = source_item()
    item["source_image"]["caption"] = "示例图片"
    article = "![图](images/2026-11-09-02.png)\n\n示例图片\n"
    issues = review_source_image(item, article)["issues"]
    assert "SOURCE_IMAGE_CAPTION_SOURCE_MISSING" in issues
    assert "SOURCE_IMAGE_MODIFICATION_LABEL_MISSING" in issues


@pytest.mark.parametrize("url", [
    "http://example.org/a", "https://localhost/a", "https://127.0.0.1/a",
    "https://user:password@example.org/a", "https://example.org/a?access_token=hidden",
    "https://example.org/a#token=hidden", "https://example.org/a?signature=hidden",
])
def test_private_or_credential_bearing_urls_are_rejected(url):
    assert not public_reference(url)


def test_license_reference_requires_public_url():
    item = source_item()
    item["source_image"]["rights_basis"] = {"type": "public_license", "reference": "license asserted"}
    assert "SOURCE_IMAGE_LICENSE_URL_REQUIRED" in review_source_image(item, "")["issues"]
