#!/usr/bin/env python3
"""Validate and rank Huazi topic candidates with a reproducible 100-point rubric."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import PackLockError, atomic_write_json, canonical_sha256, create_pack_backup, pack_date_prefix, pack_lock  # noqa: E402


PROJECT_ROOT = SCRIPT_ROOT.parent
DEFAULT_RUBRIC = PROJECT_ROOT / "config" / "content-workflow-rubric.json"
DEFAULT_GROWTH_STRATEGY = PROJECT_ROOT / "config" / "content-growth-strategy.json"
MAX_CANDIDATE_BYTES = 2 * 1024 * 1024


def rubric_for_pack(pack: Path, rubric: dict) -> dict:
    """Apply a project-authorized editorial scope, never scoring overrides."""
    from requested_topic import request_for_pack
    request = request_for_pack(pack)
    if request:
        effective = copy.deepcopy(rubric)
        effective["minimum_candidate_count"] = 1
        effective["maximum_candidate_count"] = 1
        effective["news_priority"]["enabled"] = False
        effective["requested_topic"] = request
        return effective
    policy_path = PROJECT_ROOT / "config" / "topic-special-issues.json"
    if not policy_path.exists():
        return rubric
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if type(policy.get("schema_version")) is not int or policy["schema_version"] != 1:
        raise ValueError("TOPIC_SPECIAL_ISSUE_POLICY_INVALID")
    if pack.resolve().parent != (PROJECT_ROOT / "outputs").resolve():
        return rubric
    entry = policy.get("packs", {}).get(pack.name)
    if entry is None:
        return rubric
    if (set(entry) != {"date", "topic_type", "user_request"}
            or entry["date"] != pack_date_prefix(pack)
            or entry["topic_type"] not in {"science_award_news", "industry_expo_observation"}
            or not isinstance(entry["user_request"], str) or not entry["user_request"].strip()):
        raise ValueError("TOPIC_SPECIAL_ISSUE_POLICY_INVALID")
    effective = copy.deepcopy(rubric)
    effective["news_priority"]["preferred_topic_types"].append(entry["topic_type"])
    effective["editorial_scope"] = {"pack": pack.name, **entry}
    return effective


def read_candidate_input(path: Path) -> tuple[bytes, dict[str, Any]]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_CANDIDATE_BYTES + 1)
    if len(raw) > MAX_CANDIDATE_BYTES:
        raise ValueError("candidate file too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("candidate input must be an object")
    return raw, payload


def parse_time(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def valid_url(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and not parsed.username and not parsed.password)
    except ValueError:
        return False


def source_domain(value: str) -> str:
    if not valid_url(value):
        return ""
    hostname = (urlparse(value).hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def normalize_source_urls(candidate: dict[str, Any]) -> list[str]:
    raw = candidate.get("source_urls", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return []
    return [item.strip() for item in raw if item.strip()]


def normalize_heat_signals(candidate: dict[str, Any]) -> list[dict[str, str]]:
    raw = candidate.get("heat_signals", [])
    if not isinstance(raw, list):
        return []
    signals: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        signals.append({
            "kind": str(item.get("kind", "")).strip(),
            "summary": str(item.get("summary", "")).strip(),
            "reference": str(item.get("reference", "")).strip(),
        })
    return signals


def validate_knowledge_layer(
    candidate: dict[str, Any],
    rubric: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    config = rubric.get("knowledge_layer") if isinstance(rubric.get("knowledge_layer"), dict) else {}
    raw = candidate.get("knowledge_layer") if isinstance(candidate.get("knowledge_layer"), dict) else {}
    knowledge = {str(key): value.strip() for key, value in raw.items() if isinstance(value, str) and value.strip()}
    blocks: list[str] = []
    if config.get("required") is not True:
        return knowledge, blocks
    for field in config.get("required_fields", []):
        if not knowledge.get(str(field)):
            blocks.append(f"knowledge_field_missing:{field}")
    return knowledge, blocks


def validate_news_priority(
    candidate: dict[str, Any],
    rubric: dict[str, Any],
) -> tuple[str, list[dict[str, str]], bool, list[str]]:
    config = rubric.get("news_priority") if isinstance(rubric.get("news_priority"), dict) else {}
    topic_type = str(candidate.get("topic_type", "")).strip()
    preferred_types = {str(item) for item in config.get("preferred_topic_types", [])}
    is_news_priority = bool(topic_type and topic_type in preferred_types)
    blocks: list[str] = []
    if config.get("enabled") is not True:
        return topic_type, normalize_heat_signals(candidate), False, blocks
    if not topic_type:
        blocks.append("topic_type_missing")
    elif topic_type not in preferred_types and topic_type != "evergreen_knowledge":
        blocks.append(f"topic_type_not_allowed:{topic_type}")

    signals = normalize_heat_signals(candidate)
    if is_news_priority:
        allowed = {str(item) for item in config.get("allowed_heat_signal_kinds", [])}
        minimum = int(config.get("minimum_heat_signal_count", 2))
        valid_signals: list[dict[str, str]] = []
        for index, signal in enumerate(signals):
            if signal["kind"] not in allowed:
                blocks.append(f"heat_signal_kind_not_allowed:{index}:{signal['kind']}")
                continue
            if not signal["summary"]:
                blocks.append(f"heat_signal_summary_missing:{index}")
                continue
            if not valid_url(signal["reference"]):
                blocks.append(f"heat_signal_reference_invalid:{index}")
                continue
            valid_signals.append(signal)
        if len(valid_signals) < minimum:
            blocks.append(f"heat_signal_count_below_minimum:{len(valid_signals)}<{minimum}")
        kinds = {item["kind"] for item in valid_signals}
        minimum_kinds = int(config.get("minimum_heat_signal_kinds", min(minimum, 2)))
        if len(kinds) < minimum_kinds:
            blocks.append(f"heat_signal_kind_count_below_minimum:{len(kinds)}<{minimum_kinds}")
        domains = {source_domain(item["reference"]) for item in valid_signals}
        domains.discard("")
        minimum_domains = int(config.get("minimum_independent_domains", 2))
        if len(domains) < minimum_domains:
            blocks.append(f"heat_signal_domain_count_below_minimum:{len(domains)}<{minimum_domains}")
        external = {str(item) for item in config.get("required_external_signal_kinds", [])}
        if not kinds.intersection(external):
            blocks.append("external_heat_signal_missing")
    return topic_type, signals, is_news_priority, blocks


def load_default_growth_strategy() -> dict[str, Any]:
    return json.loads(DEFAULT_GROWTH_STRATEGY.read_text(encoding="utf-8"))


def validate_experiment(candidate: dict[str, Any], growth: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    config = growth.get("experiment") if isinstance(growth.get("experiment"), dict) else {}
    raw = candidate.get("experiment") if isinstance(candidate.get("experiment"), dict) else {}
    experiment = {str(key): value.strip() for key, value in raw.items() if isinstance(value, str) and value.strip()}
    blocks: list[str] = []
    if config.get("required") is not True:
        return experiment, blocks
    for field in config.get("required_fields", []):
        if not experiment.get(str(field)):
            blocks.append(f"experiment_field_missing:{field}")
    variable = experiment.get("variable", "")
    allowed = {str(item) for item in config.get("allowed_variables", [])}
    if variable and variable not in allowed:
        blocks.append(f"experiment_variable_not_allowed:{variable}")
    if "," in variable or "，" in variable or "+" in variable:
        blocks.append("experiment_must_test_exactly_one_variable")
    return experiment, blocks


def score_candidate(
    candidate: dict[str, Any],
    rubric: dict[str, Any],
    now: datetime,
    growth: dict[str, Any],
) -> dict[str, Any]:
    dimensions = rubric["dimensions"]
    raw_scores = candidate.get("scores") if isinstance(candidate.get("scores"), dict) else {}
    scores: dict[str, int] = {}
    hard_blocks: list[str] = []
    warnings: list[str] = []

    title = str(candidate.get("title", "")).strip()
    if not title:
        hard_blocks.append("missing_title")

    for key, maximum in dimensions.items():
        value = raw_scores.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            hard_blocks.append(f"invalid_score:{key}")
            continue
        if not 0 <= value <= int(maximum):
            hard_blocks.append(f"score_out_of_range:{key}:{value}")
            continue
        scores[key] = value

    total = sum(scores.values()) if len(scores) == len(dimensions) else 0
    source_urls = normalize_source_urls(candidate)
    if not source_urls or any(not valid_url(url) for url in source_urls):
        hard_blocks.append("missing_or_invalid_source_url")
    if rubric.get("hard_requirements", {}).get("official_or_primary_source") and candidate.get("primary_source") is not True:
        hard_blocks.append("primary_source_not_confirmed")

    published_at = parse_time(str(candidate.get("published_at", "")))
    age_hours: float | None = None
    if not published_at:
        hard_blocks.append("missing_or_invalid_published_at")
    else:
        age_hours = max((now - published_at).total_seconds() / 3600, 0.0)
        if published_at > now + timedelta(minutes=5):
            hard_blocks.append("published_at_in_future")
        if age_hours > float(rubric["freshness_window_hours"]) and not rubric.get("requested_topic"):
            hard_blocks.append(f"outside_freshness_window:{age_hours:.1f}h")

    duplicate_days = int(rubric.get("hard_requirements", {}).get("deduplicate_within_days", 7))
    if candidate.get("duplicate_within_days") is True:
        hard_blocks.append(f"duplicate_within_{duplicate_days}_days")

    experiment, experiment_blocks = validate_experiment(candidate, growth)
    hard_blocks.extend(experiment_blocks)
    topic_type, heat_signals, is_news_priority, news_blocks = validate_news_priority(candidate, rubric)
    hard_blocks.extend(news_blocks)
    knowledge_layer, knowledge_blocks = validate_knowledge_layer(candidate, rubric)
    hard_blocks.extend(knowledge_blocks)
    heat_signal_domains = sorted({source_domain(item["reference"]) for item in heat_signals if source_domain(item["reference"])})
    heat_signal_kinds = sorted({item["kind"] for item in heat_signals if item["kind"]})

    minimum = int(rubric["minimum_score"])
    if total < minimum:
        warnings.append(f"below_minimum_score:{total}<{minimum}")

    return {
        "candidate_id": str(candidate.get("candidate_id", "")).strip() or canonical_sha256({"title": title, "source_urls": source_urls})[:12],
        "title": title,
        "angle": str(candidate.get("angle", "")).strip(),
        "published_at": published_at.isoformat(timespec="seconds") if published_at else "",
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "source_urls": source_urls,
        "primary_source": candidate.get("primary_source") is True,
        "signals": candidate.get("signals", {}),
        "topic_type": topic_type,
        "heat_signals": heat_signals,
        "heat_signal_count": len(heat_signals),
        "heat_signal_kind_count": len(heat_signal_kinds),
        "heat_signal_kinds": heat_signal_kinds,
        "heat_signal_domain_count": len(heat_signal_domains),
        "heat_signal_domains": heat_signal_domains,
        "news_priority": is_news_priority,
        "knowledge_layer": knowledge_layer,
        "experiment": experiment,
        "scores": scores,
        "total": total,
        "extreme_value_score": sum(
            scores.get(str(key), 0)
            for key in growth.get("extreme_value", {}).get("tie_break_dimensions", [])
        ),
        "hard_blocks": sorted(set(hard_blocks)),
        "warnings": warnings,
        "verified": not hard_blocks,
        "eligible": not hard_blocks and total >= minimum,
        "notes": str(candidate.get("notes", "")).strip(),
    }


def rank_payload(
    payload: dict[str, Any],
    rubric: dict[str, Any],
    now: datetime,
    growth: dict[str, Any] | None = None,
    *, input_sha256: str = "",
) -> dict[str, Any]:
    growth = growth or load_default_growth_strategy()
    if not isinstance(payload, dict):
        raise ValueError("candidate input must be an object")
    if now.tzinfo is None:
        raise ValueError("ranking time requires an explicit timezone")
    now = now.replace(microsecond=0)
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("input must contain a candidates array")
    ranked = [score_candidate(item, rubric, now, growth) for item in raw_candidates if isinstance(item, dict)]
    ranked.sort(
        key=lambda item: (
            item["eligible"],
            item["news_priority"],
            item["verified"],
            item["total"],
            item["extreme_value_score"],
            item["title"],
        ),
        reverse=True,
    )

    eligible = [item for item in ranked if item["eligible"]]
    eligible_news = [item for item in eligible if item["news_priority"]]
    verified = [item for item in ranked if item["verified"]]
    selected = eligible_news[0] if eligible_news else (eligible[0] if eligible else (verified[0] if verified else None))
    if eligible_news:
        selection_mode = "authorized_special_issue" if rubric.get("editorial_scope") else "ai_news_first"
    elif eligible:
        selection_mode = "non_news_fallback"
    else:
        selection_mode = "best_verified_below_threshold" if selected else "none"
    issues: list[str] = []
    if len(ranked) != len(raw_candidates):
        issues.append("candidate_record_invalid")
    for cid, count in Counter(row["candidate_id"] for row in ranked).items():
        if count > 1:
            issues.append(f"candidate_id_duplicate:{cid}")
    if rubric.get("growth_strategy_id") != growth.get("strategy_id"):
        issues.append("growth_strategy_mismatch")
    minimum_count = int(rubric["minimum_candidate_count"])
    if len(ranked) < minimum_count:
        issues.append(f"candidate_count_below_minimum:{len(ranked)}<{minimum_count}")
    maximum_count = int(rubric.get("maximum_candidate_count", 10))
    if len(ranked) > maximum_count:
        issues.append(f"candidate_count_above_maximum:{len(ranked)}>{maximum_count}")
    news_config = rubric.get("news_priority") if isinstance(rubric.get("news_priority"), dict) else {}
    news_count = sum(1 for item in ranked if item["news_priority"])
    news_ratio = news_count / len(ranked) if ranked else 0.0
    if news_config.get("enabled") is True:
        minimum_news = int(news_config.get("minimum_candidate_count", 3))
        minimum_ratio = float(news_config.get("minimum_candidate_ratio", 0.6))
        if news_count < minimum_news:
            issues.append(f"ai_news_candidate_count_below_minimum:{news_count}<{minimum_news}")
        if news_ratio < minimum_ratio:
            issues.append(f"ai_news_candidate_ratio_below_minimum:{news_ratio:.2f}<{minimum_ratio:.2f}")
        verified_news = [item for item in verified if item["news_priority"]]
        if not verified_news:
            issues.append("no_verified_ai_news_candidate")
    if not selected:
        issues.append("no_verified_candidate")

    result = {
        "schema_version": 2,
        "generated_at": now.isoformat(timespec="seconds"),
        "rubric_id": rubric["rubric_id"],
        "growth_strategy_id": growth.get("strategy_id", ""),
        "growth_mode": growth.get("active_mode", ""),
        "candidate_count": len(ranked),
        "eligible_count": len(eligible),
        "eligible_news_count": len(eligible_news),
        "news_candidate_count": news_count,
        "news_candidate_ratio": round(news_ratio, 3),
        "selection_mode": selection_mode,
        "selected_candidate_id": selected["candidate_id"] if selected else "",
        "selected_title": selected["title"] if selected else "",
        "selected_score": selected["total"] if selected else 0,
        "selected_topic_type": selected["topic_type"] if selected else "",
        "selected_news_priority": selected["news_priority"] if selected else False,
        "selected_heat_signals": selected["heat_signals"] if selected else [],
        "selected_heat_signal_domains": selected["heat_signal_domains"] if selected else [],
        "selected_knowledge_layer": selected["knowledge_layer"] if selected else {},
        "selected_extreme_value_score": selected["extreme_value_score"] if selected else 0,
        "selected_experiment": selected["experiment"] if selected else {},
        "passed": bool(selected) and not issues,
        "issues": issues,
        "candidates": ranked,
        "inputs": {
            "candidate_file_sha256": input_sha256,
            "candidate_payload_sha256": canonical_sha256(payload),
            "rubric_sha256": canonical_sha256(rubric),
            "growth_strategy_sha256": canonical_sha256(growth),
        },
    }
    result["ranking_id"] = ranking_identity(result)
    return result


def ranking_identity(ranking: dict[str, Any]) -> str:
    return canonical_sha256({k: v for k, v in ranking.items() if k not in {"ranking_id", "generated_at"}})


def validate_ranking(pack: Path, ranking: dict, *, now: datetime | None = None,
                     rubric: dict | None = None, growth: dict | None = None) -> dict:
    rubric = rubric if rubric is not None else rubric_for_pack(
        pack, json.loads(DEFAULT_RUBRIC.read_text(encoding="utf-8")))
    growth = growth or load_default_growth_strategy()
    now = now or datetime.now().astimezone()
    required = ((pack_date_prefix(pack) or "") >= rubric["integrity_required_from"]
                or "inputs" in ranking or ranking.get("schema_version") == 2)
    if not required:
        return {"required": False, "passed": True, "issues": []}
    issues = []
    generated = parse_time(ranking.get("generated_at"))
    if generated is None or generated > now + timedelta(minutes=5):
        issues.append("TOPIC_RANKING_TIME_INVALID")
    if type(ranking.get("schema_version")) is not int or ranking.get("schema_version") != 2:
        issues.append("TOPIC_RANKING_SCHEMA_INVALID")
    inputs = ranking.get("inputs") if isinstance(ranking.get("inputs"), dict) else {}
    if (inputs.get("rubric_sha256") != canonical_sha256(rubric)
            or inputs.get("growth_strategy_sha256") != canonical_sha256(growth)):
        issues.append("TOPIC_RANKING_POLICY_CHANGED")
    try:
        path = pack / "topic-candidates.json"
        if not path.resolve().is_relative_to(pack.resolve()):
            raise ValueError("outside pack")
        raw, payload = read_candidate_input(path)
        digest = hashlib.sha256(raw).hexdigest()
        if (inputs.get("candidate_file_sha256") != digest
                or inputs.get("candidate_payload_sha256") != canonical_sha256(payload)):
            issues.append("TOPIC_RANKING_INPUT_CHANGED")
        if generated is not None:
            expected = rank_payload(payload, rubric, generated, growth, input_sha256=digest)
            if (ranking.get("ranking_id") != ranking_identity(ranking)
                    or ranking_identity(ranking) != ranking_identity(expected) or not expected["passed"]):
                issues.append("TOPIC_RANKING_CONTENT_MISMATCH")
            selected = next((row for row in expected["candidates"]
                             if row["candidate_id"] == expected["selected_candidate_id"]), {})
            published = parse_time(selected.get("published_at"))
            if published is None or (not rubric.get("requested_topic") and (now - published).total_seconds() > 3600 * rubric["freshness_window_hours"]):
                issues.append("TOPIC_SELECTED_NEWS_EXPIRED")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        issues.append("TOPIC_RANKING_INPUT_INVALID")
    return {"required": True, "passed": not issues, "issues": sorted(set(issues))}


def growth_experiment_payload(
    ranking: dict[str, Any],
    growth: dict[str, Any],
    generated_at: datetime,
) -> dict[str, Any]:
    experiment = ranking.get("selected_experiment") if isinstance(ranking.get("selected_experiment"), dict) else {}
    payload = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "strategy_id": growth.get("strategy_id", ""),
        "mode": growth.get("active_mode", ""),
        "status": "planned" if ranking.get("passed") is True else "blocked",
        "ranking_id": ranking.get("ranking_id", ""),
        "selected_candidate_id": ranking.get("selected_candidate_id", ""),
        "selected_title": ranking.get("selected_title", ""),
        "variable": experiment.get("variable", ""),
        "hypothesis": experiment.get("hypothesis", ""),
        "control": experiment.get("control", ""),
        "signature_move": experiment.get("signature_move", ""),
        "breakout_reason": experiment.get("breakout_reason", ""),
        "audience_signal": experiment.get("audience_signal", ""),
        "kill_condition": experiment.get("kill_condition", ""),
        "hard_boundaries": growth.get("hard_boundaries", {}),
    }
    payload["experiment_id"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "generated_at"
    })
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rank Huazi topic candidates")
    parser.add_argument("--input", required=True, help="Candidate JSON file")
    parser.add_argument("--output", default="", help="Ranking JSON output")
    parser.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    parser.add_argument("--growth-strategy", default=str(DEFAULT_GROWTH_STRATEGY))
    parser.add_argument("--experiment-output", default="", help="Selected growth experiment JSON output")
    parser.add_argument("--now", default="", help="ISO timestamp override for reproducible tests")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input).resolve()
    rubric_path = Path(args.rubric).resolve()
    growth_path = Path(args.growth_strategy).resolve()
    now = parse_time(args.now) if args.now else datetime.now().astimezone()
    if now is None:
        raise SystemExit("--now must be a valid ISO timestamp")
    output = Path(args.output).resolve() if args.output else input_path.with_name("topic-ranking.json")
    experiment_output = (
        Path(args.experiment_output).resolve()
        if args.experiment_output
        else input_path.with_name("growth-experiment.json")
    )
    pack = input_path.parent
    if (output.parent != pack or experiment_output.parent != pack
            or len({input_path, output, experiment_output}) != 3):
        raise SystemExit("TOPIC_OUTPUT_PATH_INVALID: distinct input, ranking and experiment files must share one pack")
    try:
        with pack_lock(pack, "topic-ranking"):
            try:
                raw, payload = read_candidate_input(input_path)
                rubric = rubric_for_pack(pack, json.loads(rubric_path.read_text(encoding="utf-8")))
                growth = json.loads(growth_path.read_text(encoding="utf-8"))
                result = rank_payload(payload, rubric, now, growth, input_sha256=hashlib.sha256(raw).hexdigest())
                experiment = growth_experiment_payload(result, growth, now)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
                raise SystemExit("TOPIC_RANKING_INPUT_INVALID: check candidate input and scoring policies") from exc
            create_pack_backup(pack, "topic-ranking", [output.name, experiment_output.name], keep=10)
            atomic_write_json(output, result)
            atomic_write_json(experiment_output, experiment)
    except PackLockError as exc:
        raise SystemExit("TOPIC_PACK_BUSY: another task is processing this pack; no files rewritten") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Wrote ranking: {output}")
    print(f"Wrote experiment: {experiment_output}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
