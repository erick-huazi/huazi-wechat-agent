#!/usr/bin/env python3
"""Offline source snapshots and exact quotation spans, not a truth classifier.

Imports only explicitly selected public source text/HTML. No network, credentials,
browser storage or upstream agent execution is involved in this module.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from distribution_io import atomic_write_json, canonical_sha256, create_pack_backup, pack_lock, PackLockError

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 500_000
STANDARD = Path(__file__).resolve().parents[1] / "config/research-evidence-standard.json"
PROVENANCE = "operator_supplied_public_source_not_authenticated_by_importer"


def valid_source_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value) is not None


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("SOURCE_SNAPSHOT_INVALID")
        result[key] = value
    return result


def reject_nonfinite(value: str) -> None:
    raise ValueError("SOURCE_SNAPSHOT_INVALID")


def timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def public_url(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme != "https" or not host or url.username or url.password
                or url.query or url.port not in {None, 443} or "\\" in value
                or re.search(r"[\s\x00-\x1f\x7f]", value)
                or host == "localhost" or host.endswith((".local", ".internal", ".localhost"))):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except (ValueError, TypeError):
        return False


def policy_required(pack: Path, payload: dict, standard: dict | None = None) -> bool:
    standard = standard or json.loads(STANDARD.read_text(encoding="utf-8"))
    try:
        required = date.fromisoformat(pack.name[:10]) >= date.fromisoformat(standard["source_snapshot_required_from"])
    except (KeyError, ValueError):
        required = False
    sources = payload.get("sources", [])
    return required or (isinstance(sources, list) and any(
        isinstance(row, dict) and "source_snapshot" in row for row in sources
    ))


def snapshot_path(pack: Path, name: Any) -> Path:
    if (not isinstance(name, str) or "\\" in name or ":" in name
            or PureWindowsPath(name).is_absolute() or Path(name).is_absolute()):
        raise ValueError("SOURCE_SNAPSHOT_PATH_INVALID")
    parts = Path(name).parts
    if len(parts) != 2 or parts[0] != "sources" or not name.endswith(".json"):
        raise ValueError("SOURCE_SNAPSHOT_PATH_INVALID")
    path = pack / name
    if not path.resolve().is_relative_to(pack.resolve()):
        raise ValueError("SOURCE_SNAPSHOT_PATH_INVALID")
    return path


def snapshot_paths(pack: Path, payload: dict | None = None) -> list[str]:
    try:
        if payload is None:
            path = pack / "claim-verification.json"
            if not path.resolve().is_relative_to(pack.resolve()) or path.stat().st_size > MAX_BYTES:
                return []
            payload = json.loads(path.read_text(encoding="utf-8"))
        sources = payload.get("sources", [])
        return sorted({row["source_snapshot"]["path"] for row in sources
                       if isinstance(row, dict) and isinstance(row.get("source_snapshot"), dict)
                       and isinstance(row["source_snapshot"].get("path"), str)})
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def read_snapshot(pack: Path, reference: Any) -> tuple[dict, str]:
    if not isinstance(reference, dict):
        raise ValueError("SOURCE_SNAPSHOT_REQUIRED")
    path = snapshot_path(pack, reference.get("path"))
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("SOURCE_SNAPSHOT_TOO_LARGE")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != reference.get("sha256"):
        raise ValueError("SOURCE_SNAPSHOT_HASH_MISMATCH")
    try:
        document = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_nonfinite)
    except (ValueError, RecursionError) as exc:
        raise ValueError("SOURCE_SNAPSHOT_INVALID") from exc
    if (not isinstance(document, dict) or type(document.get("schema_version")) is not int or document.get("schema_version") != 1
            or document.get("kind") != "public_source_snapshot"
            or document.get("snapshot_id") != canonical_sha256({
                k: v for k, v in document.items() if k != "snapshot_id"})):
        raise ValueError("SOURCE_SNAPSHOT_INVALID")
    if (not isinstance(document.get("text"), str) or not 40 <= len(document["text"]) <= MAX_TEXT
            or not public_url(document.get("url", "")) or not valid_source_id(document.get("source_id"))
            or any(not isinstance(document.get(key), str) or not document[key].strip()
                   for key in ("title", "extractor", "input_sha256"))
            or re.fullmatch(r"[0-9a-f]{64}", document["input_sha256"]) is None
            or document.get("provenance") != PROVENANCE):
        raise ValueError("SOURCE_SNAPSHOT_INVALID")
    return document, digest


def checked_span(text: str, span: Any) -> bool:
    if not isinstance(span, dict):
        return False
    start, end, quote = span.get("start"), span.get("end"), span.get("quote")
    return (type(start) is int and type(end) is int and isinstance(quote, str)
            and len(quote.strip()) >= 12 and 0 <= start < end <= len(text)
            and text[start:end] == quote)


def locate(text: str, quote: str, start: int | None = None) -> dict:
    positions, cursor = [], 0
    while quote and (found := text.find(quote, cursor)) >= 0:
        positions.append(found)
        cursor = found + 1
    if not positions or (start is not None and start not in positions):
        raise ValueError("SOURCE_QUOTE_NOT_FOUND")
    if start is None and len(positions) != 1:
        raise ValueError("SOURCE_QUOTE_AMBIGUOUS: choose --start or a longer quote")
    start = positions[0] if start is None else start
    span = {"start": start, "end": start + len(quote), "quote": quote}
    if not checked_span(text, span):
        raise ValueError("SOURCE_QUOTE_TOO_SHORT")
    return span


def import_source(pack: Path, input_file: Path, *, source_id: str, url: str,
                  title: str, captured_at: str, input_format: str = "text",
                  now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    captured = timestamp(captured_at)
    if not pack.is_dir() or not valid_source_id(source_id):
        raise ValueError("SOURCE_IMPORT_TARGET_INVALID")
    if not public_url(url) or not isinstance(title, str) or not title.strip():
        raise ValueError("SOURCE_IMPORT_METADATA_INVALID: public HTTPS URL without query credentials required")
    if captured is None or captured > now + timedelta(minutes=5):
        raise ValueError("SOURCE_CAPTURE_TIME_INVALID")
    with input_file.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("SOURCE_INPUT_TOO_LARGE")
    text = raw.decode("utf-8-sig")
    extractor = "verbatim_public_text"
    if input_format == "html":
        import trafilatura

        text = trafilatura.extract(text, url=url, include_comments=False,
                                   include_tables=True, favor_precision=True) or ""
        extractor = f"trafilatura:{trafilatura.__version__}"
    elif input_format != "text":
        raise ValueError("SOURCE_INPUT_FORMAT_INVALID")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not 40 <= len(text) <= MAX_TEXT:
        raise ValueError("SOURCE_TEXT_EMPTY_OR_TOO_LARGE")
    document = {
        "schema_version": 1, "kind": "public_source_snapshot", "source_id": source_id,
        "url": url, "title": title.strip(), "captured_at": captured.isoformat(timespec="seconds"),
        "extractor": extractor, "input_sha256": hashlib.sha256(raw).hexdigest(), "text": text,
        "provenance": PROVENANCE,
    }
    document["snapshot_id"] = canonical_sha256(document)
    name = f"sources/{source_id}-{document['snapshot_id']}.json"
    encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("SOURCE_SNAPSHOT_TOO_LARGE")
    with pack_lock(pack, "source-evidence-import"):
        path = snapshot_path(pack, name)
        if path.exists():
            if path.read_bytes() != encoded:
                raise ValueError("SOURCE_SNAPSHOT_IMMUTABLE_CONFLICT")
        else:
            create_pack_backup(pack, "source-evidence-import", [name, "claim-verification.json"], keep=10)
            atomic_write_json(path, document)
    return {"path": name, "sha256": hashlib.sha256(encoded).hexdigest()}


def validate_grounding(pack: Path, payload: dict, standard: dict,
                       now: datetime) -> dict:
    issues, files, documents = [], [], {}
    sources = payload.get("sources", [])
    sources = sources if isinstance(sources, list) else []
    for source in sources:
        if not isinstance(source, dict):
            continue  # Structural source validation belongs to the research gate.
        sid = str(source.get("source_id", ""))
        reference = source.get("source_snapshot")
        row = {"source_id": sid, "path": reference.get("path", "") if isinstance(reference, dict) else "", "sha256": ""}
        files.append(row)
        try:
            doc, row["sha256"] = read_snapshot(pack, reference)
        except (OSError, ValueError, TypeError) as exc:
            code = str(exc) if str(exc).startswith("SOURCE_") else "SOURCE_SNAPSHOT_UNREADABLE"
            issues.append(f"{code}:{sid}")
            continue
        issue_count = len(issues)
        if doc.get("source_id") != sid or doc.get("url") != source.get("url"):
            issues.append(f"SOURCE_SNAPSHOT_IDENTITY_MISMATCH:{sid}")
        captured, checked = timestamp(doc.get("captured_at")), timestamp(source.get("checked_at"))
        verified = timestamp(payload.get("verified_at"))
        if (captured is None or captured > now + timedelta(minutes=5)
                or (checked and captured > checked + timedelta(minutes=5))
                or (verified and captured > verified + timedelta(minutes=5))):
            issues.append(f"SOURCE_CAPTURE_TIME_INVALID:{sid}")
        elif (now - captured).total_seconds() > 3600 * standard.get("source_snapshot_maximum_age_hours", 72):
            issues.append(f"SOURCE_SNAPSHOT_STALE:{sid}")
        excerpt = source.get("evidence_excerpt")
        if not isinstance(excerpt, str) or len(excerpt.strip()) < 12 or excerpt not in doc["text"]:
            issues.append(f"SOURCE_EXCERPT_NOT_FOUND:{sid}")
        if len(issues) == issue_count:
            documents[sid] = doc

    grounded = 0
    claims = payload.get("claims", [])
    for claim in claims if isinstance(claims, list) else []:
        if (not isinstance(claim, dict) or not isinstance(claim.get("verdict"), str)
                or claim["verdict"].strip() not in {"verified", "corrected"}):
            continue
        cid = str(claim.get("claim_id", ""))
        spans = claim.get("evidence_spans")
        source_ids = claim.get("source_ids", [])
        source_ids = source_ids if isinstance(source_ids, list) else []
        if not isinstance(spans, list) or not spans:
            issues.append(f"CLAIM_EVIDENCE_SPAN_REQUIRED:{cid}")
            continue
        valid, span_error = [], False
        for span in spans:
            sid = span.get("source_id") if isinstance(span, dict) else None
            doc = documents.get(sid) if isinstance(sid, str) else None
            if sid not in source_ids or doc is None or not checked_span(doc["text"], span):
                issues.append(f"CLAIM_EVIDENCE_SPAN_INVALID:{cid}")
                span_error = True
            else:
                valid.append(span)
        if set(str(s) for s in source_ids) - {s["source_id"] for s in valid}:
            issues.append(f"CLAIM_CITED_SOURCE_UNGROUNDED:{cid}")
            span_error = True
        context = claim.get("metric_context")
        if isinstance(context, dict) and context.get("evidence_excerpt"):
            if not any(s["source_id"] == context.get("evidence_source_id")
                       and isinstance(context["evidence_excerpt"], str)
                       and context["evidence_excerpt"] in s["quote"] for s in valid):
                issues.append(f"NUMERIC_EXCERPT_NOT_GROUNDED:{cid}")
                span_error = True
        if valid and not span_error:
            grounded += 1
    return {"issues": sorted(set(issues)), "files": sorted(files, key=lambda x: x["source_id"]),
            "grounded_claim_count": grounded,
            "assurance": "exact_text_integrity_only_not_semantic_truth_or_source_authentication"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("import", help="Import an explicitly selected public source, never browser state")
    capture.add_argument("--pack", required=True)
    capture.add_argument("--input", required=True)
    capture.add_argument("--format", choices=["text", "html"], default="text")
    for name in ("source-id", "url", "title", "captured-at"):
        capture.add_argument(f"--{name}", required=True)
    capture.add_argument("--public-source-confirmed", action="store_true", required=True)
    find = sub.add_parser("locate", help="Read-only exact span lookup; never marks a claim verified")
    find.add_argument("--pack", required=True)
    find.add_argument("--snapshot", required=True)
    find.add_argument("--sha256", required=True)
    find.add_argument("--quote", required=True)
    find.add_argument("--start", type=int)
    args = parser.parse_args()
    try:
        if args.command == "import":
            result = import_source(Path(args.pack).resolve(), Path(args.input), source_id=args.source_id,
                                   url=args.url, title=args.title, captured_at=args.captured_at, input_format=args.format)
        else:
            doc, _ = read_snapshot(Path(args.pack).resolve(), {"path": args.snapshot, "sha256": args.sha256})
            result = {"source_id": doc["source_id"], **locate(doc["text"], args.quote, args.start)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PackLockError:
        print("SOURCE_PACK_BUSY: another action owns this pack; no source written")
        return 1
    except (OSError, ValueError, ImportError) as exc:
        print(str(exc) if str(exc).startswith(("SOURCE_", "CLAIM_")) else "SOURCE_IMPORT_FAILED: check UTF-8 input and installed extractor")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
