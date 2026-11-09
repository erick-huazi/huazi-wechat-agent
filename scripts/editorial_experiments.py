"""Question-led opening experiments; read-only advice, never traffic verdicts."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt


PROBLEM_FIELDS = ("verified_change", "reader", "question", "answer", "information_gain",
                  "action", "boundary", "audience_basis")
MEASUREMENT_FIELDS = ("primary_metric", "numerator", "denominator", "window",
                      "data_source", "guardrail", "stop_rule", "confounders")
GROWTH_FIELDS = ("hypothesis", "control", "signature_move", "breakout_reason",
                 "audience_signal", "kill_condition")


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def public_url(value: Any) -> bool:
    if not nonempty(value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def opening_span(article: str) -> tuple[int, int] | None:
    """Only a standalone first paragraph after H1; never guess inside rich blocks."""
    lines = article.splitlines(keepends=True)
    parse_lines = list(lines)
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
        if end is None:
            return None
        parse_lines[:end + 1] = ["\n"] * (end + 1)
    tokens = MarkdownIt("commonmark").parse("".join(parse_lines))
    title_seen = False
    for index, token in enumerate(tokens):
        if token.type in {"html_block", "fence", "code_block", "blockquote_open", "bullet_list_open", "ordered_list_open"}:
            return None
        if token.type == "heading_open" and token.tag == "h1":
            title_seen = True
        if token.type != "inline" or not token.map:
            continue
        previous = tokens[index - 1] if index else None
        if previous and previous.type == "heading_open":
            continue
        children = token.children or []
        if children and all(c.type in {"image", "softbreak", "hardbreak"} for c in children):
            continue
        if not (title_seen and previous and previous.type == "paragraph_open" and previous.level == 0):
            return None
        if any(c.type in {"html_inline", "image"} for c in children):
            return None
        start, end = token.map
        return sum(len(line) for line in lines[:start]), sum(len(line) for line in lines[:end])
    return None


def opening_context(article: str) -> dict[str, Any]:
    span = opening_span(article)
    if span is None:
        return {"available": False, "frozen_body_sha256": None}
    start, end = span
    raw = article[start:end]
    content = raw.rstrip("\r\n")
    return {"available": True, "opening": content, "start": start, "end": end,
            "line_ending": raw[len(content):],
            "frozen_body_sha256": text_hash(article[:start] + "<HUAZI_OPENING_SLOT>" + raw[len(content):] + article[end:])}


def plain_paragraph(value: Any) -> bool:
    if not nonempty(value) or "\n" in value or "\r" in value:
        return False
    tokens = MarkdownIt("commonmark").parse(value)
    return ([token.type for token in tokens] == ["paragraph_open", "inline", "paragraph_close"]
            and all(c.type == "text" for c in tokens[1].children or []))


def review_experiment(plan: Any, growth: Any, article: str,
                      frozen_inputs: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Check declared invariants. Equal hashes cannot establish semantic equivalence."""
    result: dict[str, Any] = {
        "authority": "authoring_advice_only_not_experiment_or_draft_approval",
        "status": "not_planned", "findings": [], "tactic_catalog": policy["tactics"],
        "frozen_inputs_now": frozen_inputs, "fixed_dimensions": policy["fixed_dimensions"],
        "actual_image_review": "not_performed", "fact_review": "not_performed",
        "experiment_result": "inconclusive", "traffic_collected": False,
        "causal_claim_allowed": False, "automatic_rewrite": False,
    }

    def issue(code: str, field: str, action: str) -> None:
        result["findings"].append({"code": code, "reference": "editorial-experiment.json:" + field,
                                   "severity": "advisory", "action": action})

    if plan is None:
        issue("EXP_PLAN_OPTIONAL", "root", "For new opening tests, record the reader question, one tactic and a frozen control before choosing a variant. Historical packs need no migration.")
        return result
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan.get("schema_version") != 1:
        issue("EXP_SCHEMA", "schema_version", "Use schema version 1, not a passing-score placeholder.")
        result["status"] = "needs_revision"
        return result
    problem = plan.get("problem") if isinstance(plan.get("problem"), dict) else {}
    for field in PROBLEM_FIELDS:
        if not nonempty(problem.get(field)):
            issue("EXP_PROBLEM_INCOMPLETE", "problem." + field, "Describe the verified change, specific reader problem and useful answer without inventing demand.")
    sources = problem.get("source_urls")
    if not isinstance(sources, list) or not sources or not all(public_url(url) for url in sources):
        issue("EXP_SOURCE_REQUIRED", "problem.source_urls", "Record public first-hand HTTPS sources; URL presence is not verification.")
    basis = problem.get("audience_basis")
    if not isinstance(basis, str) or basis not in {"public_evidence", "editorial_hypothesis"}:
        issue("EXP_AUDIENCE_BASIS", "problem.audience_basis", "Use public_evidence or editorial_hypothesis; do not invent reader quotes.")
    if basis == "public_evidence":
        evidence = problem.get("audience_evidence_urls")
        if not isinstance(evidence, list) or not evidence or not all(public_url(url) for url in evidence):
            issue("EXP_AUDIENCE_EVIDENCE", "problem.audience_evidence_urls", "Supply public evidence and independently check that it supports this reader problem.")
    tactic = plan.get("tactic")
    if not isinstance(tactic, str) or tactic not in policy["tactics"]:
        issue("EXP_TACTIC", "tactic", "Choose one supported opening tactic, not a bundle of tactics or channels.")
    else:
        result["selected_tactic"] = {"id": tactic, **policy["tactics"][tactic]}
    if plan.get("variable") != "opening_hook" or not isinstance(growth, dict) or growth.get("variable") != "opening_hook":
        issue("EXP_VARIABLE_MISMATCH", "variable", "This pilot supports opening_hook only and must match ranked growth-experiment.json. Other experiment axes are not certified here.")
    if not isinstance(growth, dict) or any(not nonempty(growth.get(key)) for key in GROWTH_FIELDS):
        issue("EXP_GROWTH_INCOMPLETE", "growth-experiment", "Complete the ranker's hypothesis, control, signature move, rationale, signal and stop rule.")
    context = opening_context(article)
    if not context["available"]:
        issue("EXP_OPENING_UNSUPPORTED", "opening", "Use a standalone prose opening after the title; do not guess offsets in quotes, lists or raw HTML.")
    control, treatment = plan.get("control_opening"), plan.get("treatment_opening")
    variants_valid = plain_paragraph(control) and plain_paragraph(treatment)
    if not variants_valid:
        issue("EXP_OPENING_INVALID", "openings", "Provide two single plain-text paragraphs; no new headings, images, links or hidden markup.")
    elif control == treatment:
        issue("EXP_NO_CHANGE", "openings", "The two openings are identical; there is no opening test.")
    elif sorted(re.findall(r"\d+(?:[.,]\d+)*%?", control)) != sorted(re.findall(r"\d+(?:[.,]\d+)*%?", treatment)):
        issue("EXP_NUMERIC_SCOPE_CHANGED", "openings", "Numerical tokens differ. Keep evidence scope fixed and independently review units, denominators, model versions and qualifiers even when numbers match.")
    if context["available"] and variants_valid and context["opening"] not in {control, treatment}:
        issue("EXP_OPENING_NOT_CURRENT", "openings", "The article opening is neither declared variant. Revise the plan or the intended article through the existing safe writer.")
    if any(value is None for value in frozen_inputs.values()):
        issue("EXP_FREEZE_INCOMPLETE", "frozen_inputs", "Finish article, storyboard, claims, growth experiment and all five images before freezing a comparison.")
    if plan.get("frozen_inputs") != frozen_inputs:
        issue("EXP_CONTROL_DRIFT", "frozen_inputs", "The frozen control is absent or changed. Inspect the diff; do not silently replace baseline hashes after seeing results.")
    measurement = plan.get("measurement") if isinstance(plan.get("measurement"), dict) else {}
    if measurement.get("design") != "observational":
        issue("EXP_DESIGN_UNSUPPORTED", "measurement.design", "Default to observational: separate WeChat articles are not randomized A/B traffic. Randomized testing needs a separate reviewed design.")
    for field in MEASUREMENT_FIELDS:
        if not nonempty(measurement.get(field)):
            issue("EXP_MEASUREMENT_INCOMPLETE", "measurement." + field, "Predeclare one accessible metric, its exact denominator/window, evidence source, confounders and stopping rule. Missing platform data stays unavailable.")
    result["problem"] = problem
    result["measurement_plan"] = measurement
    result["opening_context"] = context
    if context["available"] and variants_valid:
        start, end = context["start"], context["end"]
        result["variant_previews"] = [
            {"name": name, "opening": opening,
             "article_sha256": text_hash(article[:start] + opening + context["line_ending"] + article[end:])}
            for name, opening in (("control", control), ("treatment", treatment))
        ]
    result["status"] = "needs_revision" if result["findings"] else "ready_for_editor_review"
    result["semantic_review_required"] = ["same factual claims and qualifications", "same emotional intensity and point of view", "same information density", "preserved source references and AI disclosure", "question answered in body", "sources support reader problem"]
    return result
