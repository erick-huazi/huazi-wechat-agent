#!/usr/bin/env python3
"""Offline cross-pack opening comparison; advisory only, never a traffic prediction."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from distribution_io import pack_date_prefix
from editorial_advisor import prose_blocks

MAX_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 200


def capture(path: Path) -> tuple[str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("UNAVAILABLE_ARTICLE")
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("UNAVAILABLE_ARTICLE")
    digest = hashlib.sha256(raw).hexdigest()
    with path.open("rb") as handle:
        second = handle.read(MAX_BYTES + 1)
    if len(second) > MAX_BYTES or hashlib.sha256(second).hexdigest() != digest:
        raise ValueError("ARTICLE_CHANGED_DURING_READ")
    return raw.decode("utf-8-sig"), digest


def fingerprint(text: str) -> tuple[str, list[str]]:
    blocks = prose_blocks(text)
    paragraphs = [b["text"] for b in blocks if b["kind"] == "paragraph"]
    headings = [b["text"] for b in blocks if b["kind"] == "heading" and b["level"] == 2]
    def normalize(value: str) -> str:
        return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()
    return normalize(paragraphs[0])[:300] if paragraphs else "", [normalize(h) for h in headings if normalize(h)]


def review(pack: Path, root: Path) -> dict:
    pack, root = pack.resolve(strict=True), root.resolve(strict=True)
    if not pack.is_relative_to(root):
        raise ValueError("PACK_OUTSIDE_PROJECT")
    dated = pack_date_prefix(pack)
    if not dated:
        raise ValueError("PACK_DATE_REQUIRED")
    today = date.fromisoformat(dated)
    article = pack / "wechat-article.md"
    if not article.resolve().is_relative_to(pack):
        raise ValueError("ARTICLE_OUTSIDE_PACK")
    current, current_hash = capture(article)
    opening, headings = fingerprint(current)
    if not opening:
        raise ValueError("OPENING_NOT_AVAILABLE")
    roots = []
    outputs = root / "outputs"
    if outputs.is_dir() and not outputs.is_symlink():
        roots.append(outputs)
    archive = root / "archive"
    if archive.is_dir() and not archive.is_symlink():
        roots.extend(p for p in sorted(archive.glob("outputs-reset-*")) if p.is_dir() and not p.is_symlink())
    candidates = []
    for folder in roots:
        for candidate in folder.iterdir():
            if not candidate.is_dir() or candidate.is_symlink() or candidate.resolve() == pack:
                continue
            candidate_date = pack_date_prefix(candidate)
            try:
                day = date.fromisoformat(candidate_date)
            except ValueError:
                continue
            if today - timedelta(days=14) <= day <= today:
                candidates.append((day, candidate / "wechat-article.md"))
    candidates.sort(key=lambda pair: (pair[0], str(pair[1])), reverse=True)
    truncated = len(candidates) > MAX_CANDIDATES
    seen, inspected, unreadable, matches, examined = {current_hash}, 0, 0, [], []
    for day, candidate in candidates[:MAX_CANDIDATES]:
        if not candidate.resolve().is_relative_to(root) or candidate.is_symlink():
            unreadable += 1
            continue
        try:
            text, digest = capture(candidate)
        except (OSError, UnicodeError, ValueError):
            unreadable += 1
            continue
        if digest in seen:
            continue
        seen.add(digest)
        previous, previous_headings = fingerprint(text)
        if not previous:
            unreadable += 1
            continue
        inspected += 1
        examined.append((candidate, digest))
        score = SequenceMatcher(None, opening, previous, autojunk=False).ratio()
        headings_equal = bool(headings and previous_headings and headings == previous_headings)
        if score >= 0.82 or headings_equal:
            matches.append({"reference": candidate.relative_to(root).as_posix(), "pack_date": day.isoformat(),
                            "article_sha256": digest, "opening_similarity": round(score, 3),
                            "headings_exact_match": headings_equal})
    matches.sort(key=lambda row: (row["opening_similarity"], row["pack_date"]), reverse=True)
    changed = []
    for path, digest in [(article, current_hash), *examined]:
        try:
            if capture(path)[1] != digest:
                changed.append("current" if path == article else path.relative_to(root).as_posix())
        except (OSError, UnicodeError, ValueError):
            changed.append("current" if path == article else path.relative_to(root).as_posix())
    status = ("inputs_changed" if changed else "incomplete_history" if truncated or unreadable or not roots
              else "insufficient_history" if not inspected else "advisory_only")
    return {"schema_version": 1, "authority": "offline_editorial_advice_only", "status": status,
            "current_article_sha256": current_hash, "window_days": 14, "pack_date": dated,
            "examined_distinct_articles": inspected, "unreadable_or_missing": unreadable,
            "candidate_limit_reached": truncated, "changed_inputs": changed, "matches": matches[:3],
            "limitations": ["Pack dates are not publication dates; drafts and archived copies may be included.",
                            "Lexical similarity is not semantic equivalence, quality, traffic or a reason to delete necessary facts.",
                            "No remote data, platform action, auto-edit, scoring or fact verification was performed."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        print(json.dumps(review(args.pack, args.root), ensure_ascii=False, indent=2))
    except (OSError, UnicodeError, ValueError):
        print(json.dumps({"status": "unavailable", "authority": "offline_editorial_advice_only"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
