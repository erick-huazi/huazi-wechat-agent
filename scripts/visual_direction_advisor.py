#!/usr/bin/env python3
"""Review declared image direction only; never judge pixels or change a pack."""
import argparse
import json
from collections import Counter
from pathlib import Path

ROLES = ("cover", "news_fact", "mechanism", "scenario", "action_boundary")
COMPOSITIONS = {"hero_object", "annotated_object", "sequence", "comparison",
                "spatial_scene", "decision_path", "relationship", "checklist"}
FIELDS = ("proof_object", "reader_takeaway", "visual_necessity", "focal_point",
          "hierarchy", "negative_space", "avoid_implication")


def inspect_storyboard(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("storyboard items must be a list")
    items = data["items"]
    issues = []
    roles = [i.get("role") if isinstance(i, dict) else None for i in items]
    if len(items) < 5 or not roles or roles[0] != "cover" or [role for role in roles if role != "supplement"] != list(ROLES):
        issues.append("VISUAL_ROLE_SEQUENCE_REVIEW")
    result, layouts = [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            result.append({"index": index + 1, "issues": ["VISUAL_ITEM_INVALID"]})
            continue
        direction = item.get("visual_direction")
        warnings = []
        if direction is None:
            warnings.append("VISUAL_DIRECTION_NOT_DECLARED")
            direction = {}
        elif not isinstance(direction, dict):
            warnings.append("VISUAL_DIRECTION_INVALID")
            direction = {}
        composition = direction.get("composition")
        if not isinstance(composition, str) or composition not in COMPOSITIONS:
            warnings.append("VISUAL_COMPOSITION_REVIEW")
        else:
            layouts.append(composition)
        for field in FIELDS:
            if not isinstance(direction.get(field), str) or not direction[field].strip():
                warnings.append("VISUAL_" + field.upper() + "_MISSING")
        result.append({"index": index + 1, "image": item.get("image"), "role": item.get("role"),
                       "composition": composition, "issues": warnings,
                       "manual_checks": [
                           "画面是否真实解释正文，而不是重复标题或只做装饰？",
                           "画面是否准确保留对象、限定语与假设/类比边界？",
                           "生成图在手机宽度是否清晰、人物一致且构图优美？",
                       ]})
    if any(n >= 3 for n in Counter(layouts).values()):
        issues.append("VISUAL_REPEATED_COMPOSITION_REVIEW")
    return {"schema_version": 1, "authority": "declared_direction_only",
            "issues": issues, "items": result,
            "limitations": [
                "Does not render, inspect image pixels, verify claims or score aesthetics.",
                "Declared compositions can still look alike; distinct labels do not prove visual variety.",
                "Does not approve publication, modify files or replace existing quality gates.",
            ]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="image-storyboard.json")
    args = p.parse_args()
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        print(json.dumps(inspect_storyboard(data), ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit(f"Invalid storyboard: {type(exc).__name__}") from None


if __name__ == "__main__":
    main()
