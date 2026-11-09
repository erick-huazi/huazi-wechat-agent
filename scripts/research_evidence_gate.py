#!/usr/bin/env python3
"""Validate a separate, source-backed fact-check pass for a content pack."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import atomic_write_json, canonical_sha256, create_pack_backup, file_sha256, pack_lock  # noqa: E402


PROJECT_ROOT = SCRIPT_ROOT.parent
DEFAULT_STANDARD = PROJECT_ROOT / "config" / "research-evidence-standard.json"
NUMERIC_METRIC_RE = re.compile(
    r"(?:\$\s*\d|\d+(?:\.\d+)?\s*(?:%|倍|[xX]\b|美元|元|tokens?\b|分钟|小时))",
    re.IGNORECASE,
)
NUMERIC_CONTEXT_FIELDS = (
    "evaluation_name",
    "metric_name",
    "test_condition",
    "source_locator",
)
NUMERIC_EVIDENCE_FIELDS = (
    "evidence_source_id",
    "evidence_excerpt",
    "comparison_scope",
)
NUMERIC_VALUE_RE = re.compile(
    r"(?:[$¥￥]\s*\d+(?:,\d{3})*(?:\.\d+)?|"
    r"\d+(?:,\d{3})*(?:\.\d+)?\s*"
    r"(?:%|倍|[xX](?![A-Za-z])|(?:万|亿)?(?:美元|元)|tokens?\b|分钟|小时))",
    re.IGNORECASE,
)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_standard(path: Path = DEFAULT_STANDARD) -> dict[str, Any]:
    payload = read_json(path)
    if not payload.get("policy_id"):
        raise ValueError("research evidence standard must contain policy_id")
    return payload


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone()


def pack_date(pack: Path) -> date | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-|$)", pack.name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def source_domain(value: str) -> str:
    if not valid_url(value):
        return ""
    hostname = (urlparse(value).hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def normalize_numeric_value(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"[\s,]", "", normalized).lower()
    if normalized.endswith("倍"):
        normalized = f"{normalized[:-1]}x"
    return normalized


def numeric_values(value: str) -> list[str]:
    return [normalize_numeric_value(match.group(0)) for match in NUMERIC_VALUE_RE.finditer(unicodedata.normalize("NFKC", value))]


def validate_numeric_evidence_contract(
    claim_id: str,
    text: str,
    source_ids: list[str],
    context: dict[str, Any],
) -> list[str]:
    """Lock numeric prose to values from one explicitly cited evidence excerpt."""
    issues: list[str] = []
    evidence_source_id = str(context.get("evidence_source_id", "")).strip()
    evidence_excerpt = str(context.get("evidence_excerpt", "")).strip()
    comparison_scope = str(context.get("comparison_scope", "")).strip()

    for field in NUMERIC_EVIDENCE_FIELDS:
        if not str(context.get(field, "")).strip():
            issues.append(f"numeric_claim_evidence_missing:{claim_id}:{field}")
    if evidence_source_id and evidence_source_id not in source_ids:
        issues.append(f"numeric_claim_evidence_source_mismatch:{claim_id}:{evidence_source_id}")

    reported_values = context.get("reported_values")
    if not isinstance(reported_values, list) or not reported_values:
        issues.append(f"numeric_claim_reported_values_missing:{claim_id}")
        return issues

    claim_contract_values: list[str] = []
    source_contract_values: list[str] = []
    for index, value in enumerate(reported_values):
        if not isinstance(value, dict):
            issues.append(f"numeric_claim_reported_value_invalid:{claim_id}:{index}")
            continue
        label = str(value.get("label", "")).strip()
        claim_value = str(value.get("claim_value", "")).strip()
        source_value = str(value.get("source_value", "")).strip()
        if not label:
            issues.append(f"numeric_claim_reported_value_missing:{claim_id}:{index}:label")
        if not claim_value:
            issues.append(f"numeric_claim_reported_value_missing:{claim_id}:{index}:claim_value")
        if not source_value:
            issues.append(f"numeric_claim_reported_value_missing:{claim_id}:{index}:source_value")

        claim_tokens = numeric_values(claim_value)
        source_tokens = numeric_values(source_value)
        if claim_value and not claim_tokens:
            issues.append(f"numeric_claim_reported_value_unparseable:{claim_id}:{index}:claim_value")
        if source_value and not source_tokens:
            issues.append(f"numeric_claim_reported_value_unparseable:{claim_id}:{index}:source_value")
        claim_contract_values.extend(claim_tokens)
        source_contract_values.extend(source_tokens)

        if claim_tokens and source_tokens and set(claim_tokens) != set(source_tokens):
            immutable_units = ("%", "x")
            immutable_mismatch = any(
                token.endswith(immutable_units)
                for token in [*claim_tokens, *source_tokens]
            )
            if immutable_mismatch:
                issues.append(f"numeric_claim_nonconvertible_mismatch:{claim_id}:{index}")
            elif not str(value.get("derivation", "")).strip():
                issues.append(f"numeric_claim_derivation_missing:{claim_id}:{index}")

    uncovered_claim_values = sorted(set(numeric_values(text)) - set(claim_contract_values))
    if uncovered_claim_values:
        issues.append(
            f"numeric_claim_values_unreported:{claim_id}:{','.join(uncovered_claim_values)}"
        )
    excerpt_values = set(numeric_values(evidence_excerpt))
    missing_source_values = sorted(set(source_contract_values) - excerpt_values)
    if missing_source_values:
        issues.append(
            f"numeric_claim_source_values_absent:{claim_id}:{','.join(missing_source_values)}"
        )
    if len(reported_values) > 1 and not comparison_scope:
        issues.append(f"numeric_claim_comparison_scope_missing:{claim_id}")
    return issues


def required_article_files(pack: Path, standard: dict[str, Any]) -> list[str]:
    from workflow_policy import requires_video, inspection_platform_ids, ARTICLE_FILES
    from image_quality_gate import required as image_quality_required

    files = [str(item) for item in standard.get("required_article_files", [])]
    platforms = inspection_platform_ids(pack)
    excluded = {name for pid, name in ARTICLE_FILES.items() if pid not in platforms}
    files = [name for name in files if name not in excluded]
    if not requires_video(pack) and "visual-video-pack.md" in files:
        files = [name for name in files if name != "visual-video-pack.md"]
        if "weibo" in platforms and "weibo.md" not in files:
            files.append("weibo.md")
    if image_quality_required(pack) and "image-storyboard.json" not in files:
        files.append("image-storyboard.json")
    return files


def required_file_snapshot(pack: Path, required_files: list[str]) -> list[dict[str, str]]:
    return [
        {
            "path": name,
            "sha256": file_sha256(pack / name) if (pack / name).is_file() else "",
        }
        for name in required_files
    ]


def _recorded_files(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("verified_files") if isinstance(payload.get("verified_files"), list) else []
    return {
        str(item.get("path", "")).replace("\\", "/"): str(item.get("sha256", ""))
        for item in raw
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    }


def evaluate_pack(
    pack: Path,
    standard: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    standard = standard or load_standard()
    now = now or datetime.now().astimezone()
    verification_path = pack / "claim-verification.json"
    payload = read_json(verification_path)
    issues: list[str] = []

    if not verification_path.is_file():
        issues.append("claim_verification_missing")
    if payload.get("policy_id") != standard.get("policy_id"):
        issues.append("claim_verification_policy_mismatch")
    if payload.get("status") != "complete":
        issues.append("claim_verification_not_complete")
    if payload.get("verification_mode") != standard.get("verification_mode"):
        issues.append("verification_not_separate_post_generation")

    verified_at = parse_time(str(payload.get("verified_at", "")))
    age_hours: float | None = None
    if verified_at is None:
        issues.append("claim_verification_time_invalid")
    else:
        age_hours = max((now - verified_at).total_seconds() / 3600, 0.0)
        if verified_at > now + timedelta(minutes=5):
            issues.append("claim_verification_time_in_future")
        if age_hours > float(standard.get("maximum_age_hours", 24)):
            issues.append("claim_verification_stale")

    required_files = required_article_files(pack, standard)
    current_files = required_file_snapshot(pack, required_files)
    recorded_files = _recorded_files(payload)
    for item in current_files:
        path = item["path"]
        if not item["sha256"]:
            issues.append(f"verified_article_missing:{path}")
        elif not recorded_files.get(path):
            issues.append(f"verified_article_hash_missing:{path}")
        elif recorded_files[path] != item["sha256"]:
            issues.append(f"verified_article_changed:{path}")
    extra_files = sorted(set(recorded_files) - set(required_files))

    allowed_source_kinds = {str(item) for item in standard.get("allowed_source_kinds", [])}
    primary_source_kinds = {str(item) for item in standard.get("primary_source_kinds", [])}
    raw_sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    sources: dict[str, dict[str, Any]] = {}
    domains: set[str] = set()
    primary_count = 0
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            issues.append(f"source_invalid:{index}")
            continue
        source_id = str(item.get("source_id", "")).strip()
        url = str(item.get("url", "")).strip()
        kind = str(item.get("kind", "")).strip()
        title = str(item.get("title", "")).strip()
        excerpt = str(item.get("evidence_excerpt", "")).strip()
        checked_at = parse_time(str(item.get("checked_at", "")))
        if not source_id:
            issues.append(f"source_id_missing:{index}")
            continue
        if source_id in sources:
            issues.append(f"source_id_duplicate:{source_id}")
            continue
        if not valid_url(url):
            issues.append(f"source_url_invalid:{source_id}")
        if kind not in allowed_source_kinds:
            issues.append(f"source_kind_invalid:{source_id}:{kind}")
        if not title:
            issues.append(f"source_title_missing:{source_id}")
        if not excerpt:
            issues.append(f"source_evidence_excerpt_missing:{source_id}")
        if checked_at is None:
            issues.append(f"source_checked_at_invalid:{source_id}")
        elif checked_at > now + timedelta(minutes=5):
            issues.append(f"source_checked_at_in_future:{source_id}")
        elif verified_at and checked_at > verified_at + timedelta(minutes=5):
            issues.append(f"source_checked_after_verification:{source_id}")
        domain = source_domain(url)
        if domain:
            domains.add(domain)
        if kind in primary_source_kinds:
            primary_count += 1
        sources[source_id] = item

    minimum_domains = int(standard.get("minimum_source_domain_count", 2))
    if len(domains) < minimum_domains:
        issues.append(f"source_domain_count_below_minimum:{len(domains)}<{minimum_domains}")
    minimum_primary = int(standard.get("minimum_primary_source_count", 1))
    if primary_count < minimum_primary:
        issues.append(f"primary_source_count_below_minimum:{primary_count}<{minimum_primary}")

    allowed_verdicts = {str(item) for item in standard.get("allowed_verdicts", [])}
    allowed_importance = {str(item) for item in standard.get("allowed_importance", [])}
    raw_claims = payload.get("claims") if isinstance(payload.get("claims"), list) else []
    verification_schema = payload.get("schema_version", 0)
    if type(verification_schema) is not int or verification_schema < 1:
        issues.append("claim_verification_schema_invalid")
        verification_schema = 0
    numeric_context_schema = int(standard.get("numeric_claim_context_required_from_schema", 2))
    numeric_evidence_schema = int(standard.get("numeric_claim_evidence_required_from_schema", 3))
    minimum_schema = int(standard.get("minimum_claim_verification_schema", 1))
    schema_effective_from_raw = str(
        standard.get("minimum_claim_verification_schema_effective_from", "")
    ).strip()
    current_pack_date = pack_date(pack)
    try:
        schema_effective_from = date.fromisoformat(schema_effective_from_raw)
    except ValueError:
        schema_effective_from = None
    if (
        current_pack_date is not None
        and schema_effective_from is not None
        and current_pack_date >= schema_effective_from
        and verification_schema < minimum_schema
    ):
        issues.append(
            f"claim_verification_schema_too_old:{verification_schema}<{minimum_schema}"
        )
    claim_ids: set[str] = set()
    verdict_counts = {key: 0 for key in allowed_verdicts}
    load_bearing_count = 0
    for index, item in enumerate(raw_claims):
        if not isinstance(item, dict):
            issues.append(f"claim_invalid:{index}")
            continue
        claim_id = str(item.get("claim_id", "")).strip()
        text = str(item.get("text", "")).strip()
        importance = str(item.get("importance", "")).strip()
        verdict = str(item.get("verdict", "")).strip()
        source_ids = item.get("source_ids") if isinstance(item.get("source_ids"), list) else []
        source_ids = [str(value).strip() for value in source_ids if str(value).strip()]
        if not claim_id:
            issues.append(f"claim_id_missing:{index}")
        elif claim_id in claim_ids:
            issues.append(f"claim_id_duplicate:{claim_id}")
        else:
            claim_ids.add(claim_id)
        if not text:
            issues.append(f"claim_text_missing:{claim_id or index}")
        if importance not in allowed_importance:
            issues.append(f"claim_importance_invalid:{claim_id or index}:{importance}")
        if importance == "load_bearing":
            load_bearing_count += 1
        if verdict not in allowed_verdicts:
            issues.append(f"claim_verdict_invalid:{claim_id or index}:{verdict}")
        else:
            verdict_counts[verdict] += 1
        unknown_sources = sorted(set(source_ids) - set(sources))
        if unknown_sources:
            issues.append(f"claim_source_unknown:{claim_id or index}:{','.join(unknown_sources)}")
        if verdict in {"verified", "corrected"} and not source_ids:
            issues.append(f"claim_source_missing:{claim_id or index}")
        if verdict == "corrected" and not str(item.get("correction", "")).strip():
            issues.append(f"claim_correction_missing:{claim_id or index}")
        if verdict == "contradicted":
            issues.append(f"claim_contradicted:{claim_id or index}")
        if importance == "load_bearing" and verdict == "unverified":
            issues.append(f"load_bearing_claim_unverified:{claim_id or index}")
        if verification_schema >= numeric_context_schema and numeric_values(text):
            context = item.get("metric_context") if isinstance(item.get("metric_context"), dict) else {}
            for field in NUMERIC_CONTEXT_FIELDS:
                if not str(context.get(field, "")).strip():
                    issues.append(f"numeric_claim_context_missing:{claim_id or index}:{field}")
            if verification_schema >= numeric_evidence_schema:
                issues.extend(
                    validate_numeric_evidence_contract(
                        claim_id or str(index),
                        text,
                        source_ids,
                        context,
                    )
                )

    minimum_claims = int(standard.get("minimum_claim_count", 2))
    if len(raw_claims) < minimum_claims:
        issues.append(f"claim_count_below_minimum:{len(raw_claims)}<{minimum_claims}")
    minimum_load_bearing = int(standard.get("minimum_load_bearing_claim_count", 1))
    if load_bearing_count < minimum_load_bearing:
        issues.append(
            f"load_bearing_claim_count_below_minimum:{load_bearing_count}<{minimum_load_bearing}"
        )

    from source_evidence import policy_required, validate_grounding
    grounding = validate_grounding(pack, payload, standard, now) if policy_required(pack, payload, standard) else None
    if grounding:
        issues.extend(grounding["issues"])
    issues = sorted(set(issues))
    inputs = {
        "claim_verification_sha256": file_sha256(verification_path) if verification_path.is_file() else "",
        "verified_files": current_files,
        "verified_file_fingerprint": canonical_sha256(current_files),
    }
    if grounding:
        inputs["source_snapshots"] = grounding["files"]
    report = {
        "schema_version": 1,
        "policy_id": standard.get("policy_id", ""),
        "generated_at": now.isoformat(timespec="seconds"),
        "pack": str(pack.resolve()),
        "passed": not issues,
        "verification_mode": str(payload.get("verification_mode", "")),
        "verified_at": verified_at.isoformat(timespec="seconds") if verified_at else "",
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "source_count": len(sources),
        "source_domain_count": len(domains),
        "source_domains": sorted(domains),
        "primary_source_count": primary_count,
        "claim_count": len(raw_claims),
        "claim_verification_schema_version": verification_schema,
        "minimum_claim_verification_schema": minimum_schema,
        "minimum_schema_effective_from": schema_effective_from_raw,
        "load_bearing_claim_count": load_bearing_count,
        "verdict_counts": verdict_counts,
        "extra_verified_files": extra_files,
        "inputs": inputs,
        "issues": issues,
    }
    if grounding:
        report["source_grounding"] = grounding
    report["report_id"] = canonical_sha256({
        key: value for key, value in report.items() if key not in {"generated_at", "report_id"}
    })
    return report


def report_is_current(
    report: dict[str, Any],
    pack: Path,
    standard: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    standard = standard or load_standard()
    if not isinstance(report, dict):
        return False
    if report.get("policy_id") != standard.get("policy_id"):
        return False
    if report.get("passed") is not True:
        return False
    try:
        identity = canonical_sha256({k: v for k, v in report.items() if k not in {"generated_at", "report_id"}})
        if report.get("report_id") != identity or not (pack / "claim-verification.json").is_file():
            return False
        current = evaluate_pack(pack, standard, now)
        # Re-evaluate time and evidence contracts instead of trusting saved passed.
        volatile = {"generated_at", "report_id", "age_hours"}
        return (current["passed"] is True and
                {k: v for k, v in report.items() if k not in volatile} ==
                {k: v for k, v in current.items() if k not in volatile})
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate Huazi claim evidence")
    parser.add_argument("--pack", required=True)
    parser.add_argument("--standard", default=str(DEFAULT_STANDARD))
    parser.add_argument("--output", default="")
    parser.add_argument("--now", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    pack = Path(args.pack).resolve()
    if not pack.is_dir():
        raise SystemExit(f"pack not found: {pack}")
    standard = load_standard(Path(args.standard).resolve())
    now = parse_time(args.now) if args.now else datetime.now().astimezone()
    if now is None:
        raise SystemExit("--now must be a valid ISO timestamp")
    output = Path(args.output).resolve() if args.output else pack / "research-evidence-report.json"
    if pack not in output.parents:
        raise SystemExit("Evidence report must stay inside the publish pack")
    with pack_lock(pack, "research-evidence"):
        report = evaluate_pack(pack, standard, now)
        create_pack_backup(pack, "research-evidence", [output.relative_to(pack).as_posix()], keep=10)
        atomic_write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Wrote research evidence report: {output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
