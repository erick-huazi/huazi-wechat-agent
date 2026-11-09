import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from image_copy_review import review_image_copy


def example(role="scenario"):
    blocks = [
        {"kind": "headline", "text": "先核对，再使用", "font_px": 72},
        {"kind": "body", "text": "仅用于示例：48%（n=25），不是实测。", "font_px": 44},
        {"kind": "boundary", "text": "数值与单位不能丢", "font_px": 44},
        {"kind": "disclosure", "text": "AI辅助绘图 · 编辑建议", "font_px": 36},
    ]
    return {"image": "images/example.png", "role": role, "prompt": "Draw a copy-led scene.",
            "on_image_text": "\n".join(b["text"] for b in blocks),
            "copy_design": {"schema_version": 1, "blocks": blocks}}


def codes(result):
    return {f["code"] for f in result["findings"]}


def sync(item):
    item["on_image_text"] = "\n".join(b["text"] for b in item["copy_design"]["blocks"])


def test_literal_unicode_conditions_and_disclosure_survive_unchanged():
    item = example()
    before = copy.deepcopy(item)
    result = review_image_copy(item, 1080, 720)
    assert item == before
    assert result["status"] == "specified_for_visual_review"
    assert [{k: b[k] for k in ("kind", "text", "font_px")} for b in result["blocks"]] == item["copy_design"]["blocks"]
    embedded = result["prompt_instruction"].split("\n", 1)[1]
    assert json.loads(embedded) == item["copy_design"]["blocks"]
    assert result["fact_verification_performed"] is False
    assert result["actual_image_review"] == "not_performed"
    assert "passed" not in result and "score" not in result


def test_absent_optional_design_never_migrates_history():
    item = {"on_image_text": "", "prompt": "No words or labels."}
    result = review_image_copy(item, 900, 383)
    assert result["status"] == "not_provided"
    assert result["blocks"] == [] and result["prompt_instruction"] == ""
    assert result["findings"] == [] and "copy_design" not in item


@pytest.mark.parametrize("value", [None, True, [], {}, {"schema_version": True, "blocks": []},
                                  {"schema_version": 1, "blocks": "text"},
                                  {"schema_version": 1, "blocks": []}])
def test_malformed_optional_design_is_not_silently_ignored(value):
    item = example()
    item["copy_design"] = value
    result = review_image_copy(item, 1080, 720)
    assert result["status"] == "invalid" and result["prompt_instruction"] == ""
    assert "COPY_DESIGN_INVALID" in codes(result)


@pytest.mark.parametrize("size", [True, False, None, "44", 0, -1, float("nan"), float("inf"), 721, 10**400])
def test_font_sizes_must_be_finite_non_boolean_and_inside_canvas(size):
    item = example()
    item["copy_design"]["blocks"][1]["font_px"] = size
    result = review_image_copy(item, 1080, 720)
    assert result["status"] == "invalid"
    assert result["blocks"] == [] and result["prompt_instruction"] == ""


@pytest.mark.parametrize("block", [None, True, {}, {"kind": "body", "text": "x"},
                                   {"kind": [], "text": "x", "font_px": 44},
                                   {"kind": "caption", "text": "x", "font_px": 44},
                                   {"kind": "body", "text": " ", "font_px": 44},
                                   {"kind": "body", "text": "x" * 3001, "font_px": 44},
                                   {"kind": "body", "text": "x", "font_px": 44, "claim": "invented"}])
def test_malformed_blocks_do_not_enter_prompt(block):
    item = example()
    item["copy_design"]["blocks"][1] = block
    result = review_image_copy(item, 1080, 720)
    assert "COPY_BLOCK_INVALID" in codes(result)
    assert result["prompt_instruction"] == ""


@pytest.mark.parametrize("mutation", ["strip_negation", "change_unit", "drop_disclosure", "change_whitespace", "reorder"])
def test_copy_block_drift_requires_explicit_author_resolution(mutation):
    item = example()
    if mutation == "strip_negation":
        item["on_image_text"] = item["on_image_text"].replace("不是", "是")
    elif mutation == "change_unit":
        item["on_image_text"] = item["on_image_text"].replace("48%", "48秒")
    elif mutation == "drop_disclosure":
        item["on_image_text"] = item["on_image_text"].split("\nAI")[0]
    elif mutation == "change_whitespace":
        item["on_image_text"] = item["on_image_text"].replace("\n", "\r\n")
    else:
        item["copy_design"]["blocks"].reverse()
    before = copy.deepcopy(item)
    result = review_image_copy(item, 1080, 720)
    assert result["status"] == "invalid" and "COPY_LABEL_MISMATCH" in codes(result)
    assert item == before and result["prompt_instruction"] == ""


@pytest.mark.parametrize("headline_count", [0, 2])
def test_one_headline_block_required_not_one_line(headline_count):
    item = example()
    item["copy_design"]["blocks"][0]["kind"] = "body"
    for block in item["copy_design"]["blocks"][:headline_count]:
        block["kind"] = "headline"
    assert "COPY_HEADLINE_REQUIRED" in codes(review_image_copy(item, 1080, 720))


def test_multiline_headline_is_not_forced_into_checklist():
    item = example("mechanism")
    item["copy_design"]["blocks"] = [{"kind": "headline", "text": "输入\n检查\n输出", "font_px": 72}]
    sync(item)
    result = review_image_copy(item, 1080, 720)
    assert result["status"] == "specified_for_visual_review"
    assert result["blocks"][0]["text"] == "输入\n检查\n输出"


@pytest.mark.parametrize("prompt", ["No words, letters, digits.", "Text-free illustration", "Do not render any labels.", "无字插画", "不要显示文字"])
def test_legacy_no_text_prompt_conflicts_are_diagnostic_not_rewrites(prompt):
    item = example()
    item["prompt"] = prompt
    result = review_image_copy(item, 1080, 720)
    assert "COPY_PROMPT_TEXT_CONFLICT_REVIEW" in codes(result)
    assert result["status"] == "needs_review" and item["prompt"] == prompt


def test_no_text_hint_does_not_claim_full_prompt_semantic_detection():
    item = example()
    item["prompt"] = "No text on the depicted book; place the headline beside it."
    result = review_image_copy(item, 1080, 720)
    assert result["findings"][0]["severity"] == "advisory"


def test_phone_projection_uses_target_width_not_reference_width():
    item = example()
    item["copy_design"]["blocks"][1]["font_px"] = 36
    result = review_image_copy(item, 1080, 720)
    assert result["blocks"][1]["estimated_phone_font_px"] == {"320": 10.67, "375": 12.5}
    assert "COPY_PHONE_SIZE_REVIEW" in codes(result)


@pytest.mark.parametrize("value", ["核对\n，不能省略", "核对（\n包括单位）", "  ，核对", "先看《"])
def test_explicit_punctuation_breaks_are_located_without_editing(value):
    item = example()
    item["copy_design"]["blocks"][1]["text"] = value
    sync(item)
    result = review_image_copy(item, 1080, 720)
    findings = [f for f in result["findings"] if f["code"] == "COPY_LINE_BREAK_REVIEW"]
    assert findings and ":line" in findings[0]["reference"]
    assert result["blocks"][1]["text"] == value


def test_unbroken_sentence_not_claimed_to_have_safe_rendered_wrap():
    item = example()
    item["copy_design"]["blocks"][1]["text"] = "先核对（包括单位），再使用。"
    sync(item)
    result = review_image_copy(item, 1080, 720)
    assert "COPY_LINE_BREAK_REVIEW" not in codes(result)
    assert result["actual_image_review"] == "not_performed"


def test_cover_density_and_hierarchy_review_never_delete_text():
    item = example("cover")
    item["copy_design"]["blocks"][1].update(text="带着条件阅读。" * 20, font_px=74)
    sync(item)
    result = review_image_copy(item, 900, 383)
    assert {"COPY_COVER_DENSITY_REVIEW", "COPY_HIERARCHY_REVIEW", "COPY_BLOCK_DENSITY_REVIEW"} <= codes(result)
    assert len(result["blocks"][1]["text"]) == 140


def test_typography_projection_is_unrounded_for_warning_threshold():
    item = example()
    item["copy_design"]["blocks"][1]["font_px"] = 40.49
    result = review_image_copy(item, 1080, 720)
    assert result["blocks"][1]["estimated_phone_font_px"]["320"] == 12.0
    assert "COPY_PHONE_SIZE_REVIEW" in codes(result)


def test_unknown_design_keys_and_excessive_blocks_are_rejected():
    item = example()
    item["copy_design"]["auto_approve"] = True
    assert "COPY_DESIGN_INVALID" in codes(review_image_copy(item, 1080, 720))
    del item["copy_design"]["auto_approve"]
    item["copy_design"]["blocks"] *= 7
    assert "COPY_DESIGN_INVALID" in codes(review_image_copy(item, 1080, 720))


@pytest.mark.parametrize("width,height", [(True, 383), (0, 720), (1080, -1), (1080.0, 720)])
def test_invalid_canvas_dimensions_raise(width, height):
    with pytest.raises(ValueError, match="COPY_IMAGE_SIZE_INVALID"):
        review_image_copy(example(), width, height)
