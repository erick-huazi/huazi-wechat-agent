import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from wechat_opening_novelty import capture, fingerprint, review


def add_article(folder: Path, name: str, content: str) -> Path:
    pack = folder / name
    pack.mkdir(parents=True)
    (pack / "wechat-article.md").write_text(content, encoding="utf-8")
    return pack


def article(title: str, opening: str, later: str) -> str:
    return f"# {title}\n\n{opening}\n\n## 有用信息\n\n{later}\n"


def test_cross_pack_review_uses_14_day_window_and_never_emits_article_text(tmp_path):
    outputs = tmp_path / "outputs"
    current = add_article(outputs, "2026-11-08-current-huazi", article("Today", "这次更新让模型记忆可以跨任务使用，但重要信息仍须人工核对。", "今日观点"))
    other = add_article(outputs, "2026-11-07-previous-huazi", article("Yesterday", "这次更新让模型记忆可以跨任务使用，但重要信息仍须人工核对。", "另一个事实"))
    add_article(outputs, "2026-10-20-old-huazi", article("Old", "这次更新让模型记忆可以跨任务使用，但重要信息仍须人工核对。", "旧内容"))
    archive = tmp_path / "archive" / "outputs-reset-example"
    add_article(archive, "2026-11-07-duplicate-huazi", (other / "wechat-article.md").read_text(encoding="utf-8"))
    report = review(current, tmp_path)
    assert report["status"] == "advisory_only"
    assert report["examined_distinct_articles"] == 1
    assert report["matches"][0]["opening_similarity"] == 1.0
    assert "这次更新" not in json.dumps(report, ensure_ascii=False)
    assert "2026-10-20" not in json.dumps(report)


def test_absent_history_is_not_a_proven_originality_result(tmp_path):
    pack = add_article(tmp_path / "outputs", "2026-11-08-current-huazi", article("Title", "这是一段可读的开头。", "内容"))
    report = review(pack, tmp_path)
    assert report["status"] == "insufficient_history" and not report["matches"]


def test_missing_candidate_is_reported_not_counted_as_zero_similarity(tmp_path):
    outputs = tmp_path / "outputs"
    pack = add_article(outputs, "2026-11-08-current-huazi", article("Title", "可读的真实开头。", "内容"))
    (outputs / "2026-11-07-unfinished-huazi").mkdir()
    report = review(pack, tmp_path)
    assert report["status"] == "incomplete_history" and report["unreadable_or_missing"] == 1


def test_excludes_quotes_code_and_source_section_from_opening():
    text = '# Title\n\n> 引用的句子\n\n```text\n代码\n```\n\n这才是真实开头。\n\n## 资料来源\n\n来源内容。'
    opening, headings = fingerprint(text)
    assert opening == "这才是真实开头" and not headings


def test_invalid_scope_and_broken_current_article_fail_closed(tmp_path):
    outside = tmp_path / "outside"
    pack = add_article(outside, "2026-11-08-current-huazi", article("Title", "开头。", "内容"))
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="PACK_OUTSIDE_PROJECT"):
        review(pack, project)
    (pack / "wechat-article.md").write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        capture(pack / "wechat-article.md")
