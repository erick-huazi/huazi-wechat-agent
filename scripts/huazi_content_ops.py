#!/usr/bin/env python3
"""Huazi social agent scoring, prediction, and retro helper."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Iterable


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from distribution_io import atomic_write_text, five_image_names, pack_date_prefix, pack_lock  # noqa: E402


WEIGHTS = {
    "UV": 1.5,
    "HP": 1.2,
    "SK": 1.5,
    "SC": 1.0,
    "CR": 1.3,
    "RV": 1.0,
    "MA": 1.0,
}

DIM_NAMES = {
    "UV": "用户价值",
    "HP": "钩子强度",
    "SK": "Skill 可交付度",
    "SC": "场景清晰度",
    "CR": "自包含行动潜力",
    "RV": "风险控制",
    "MA": "多平台适配",
}

STATE_NAME = ".huazi-content-state.json"


def now_iso() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).replace(microsecond=0).isoformat()


def parse_scores(raw: Iterable[str]) -> Dict[str, int]:
    scores: Dict[str, int] = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"Invalid score '{item}', expected KEY=0-5")
        key, value = item.split("=", 1)
        key = key.strip().upper()
        if key not in WEIGHTS:
            raise SystemExit(f"Unknown dimension '{key}', expected {', '.join(WEIGHTS)}")
        try:
            score = int(value)
        except ValueError as exc:
            raise SystemExit(f"Invalid score for {key}: {value}") from exc
        if not 0 <= score <= 5:
            raise SystemExit(f"Score for {key} must be between 0 and 5")
        scores[key] = score
    missing = [key for key in WEIGHTS if key not in scores]
    if missing:
        raise SystemExit(f"Missing scores: {', '.join(missing)}")
    return scores


def composite(scores: Dict[str, int]) -> float:
    total = sum(scores[key] * WEIGHTS[key] for key in WEIGHTS)
    return round(total / sum(WEIGHTS.values()) * 2.0, 2)


def stable_id(title: str, source: str = "manual") -> str:
    raw = f"{source}|{title.strip().lower().replace(' ', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def state_path(root: Path) -> Path:
    return root / STATE_NAME


def load_state(root: Path) -> dict:
    path = state_path(root)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schema_version": "huazi-1.0",
        "rubric_version": "hzm-v1",
        "calibration_samples": 0,
        "calibration_samples_at_last_bump": 0,
        "pending_retros": [],
        "published_items": [],
        "last_prediction_at": None,
        "last_retro_at": None,
        "last_bump_at": None,
        "initialized_at": now_iso(),
    }


def save_state(root: Path, state: dict) -> None:
    state_path(root).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def score_table(scores: Dict[str, int]) -> str:
    lines = [
        "| 维度 | 权重 | 分数 |",
        "|---|---:|---:|",
    ]
    for key in WEIGHTS:
        lines.append(f"| {key} {DIM_NAMES[key]} | {WEIGHTS[key]} | {scores[key]} |")
    lines.append(f"| 综合分 |  | {composite(scores)} |")
    return "\n".join(lines)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def read_json_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def first_match(text: str, pattern: str, default: str = "") -> str:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else default


def prediction_title(path: Path) -> str:
    text = read_text(path)
    return first_match(text, r"^# 发布前预测：(.+)$", path.stem)


def prediction_id(path: Path) -> str:
    text = read_text(path)
    return first_match(text, r"^- id:\s*(.+)$", stable_id(path.stem, "prediction"))


def prediction_at(path: Path) -> str:
    text = read_text(path)
    return first_match(text, r"^- prediction_at:\s*(.+)$")


def canonical_prediction(path: Path) -> Path:
    text = read_text(path)
    source = first_match(text, r"^来源：`(.+)`$")
    if source:
        source_path = Path(source).resolve()
        if source_path.exists():
            return source_path
    return path.resolve()


def short_path(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


def iso_to_datetime(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def due_status(prediction: Path, now: datetime) -> str:
    dt = iso_to_datetime(prediction_at(prediction))
    if not dt:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    age_days = (now - dt).days
    if age_days >= 3:
        return f"overdue {age_days}d"
    return f"wait {3 - age_days}d"


def pack_dirs(outputs_root: Path, state_root: Path) -> list[Path]:
    if not outputs_root.exists():
        return []
    dirs = []
    for path in outputs_root.iterdir():
        if not path.is_dir() or path.resolve() == state_root.resolve():
            continue
        if any((path / name).exists() for name in ["prediction.md", "wechat-publish-result.md", "publish-checklist.md"]):
            dirs.append(path)
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)


def published_pack_rows(outputs_root: Path, state_root: Path) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for pack in pack_dirs(outputs_root, state_root):
        result = pack / "wechat-publish-result.md"
        text = read_text(result)
        if not text or "media_id" not in text:
            continue
        media = first_match(text, r"media_id[：:]\s*`?([^`\n]+)`?")
        pushed = first_match(text, r"pushed_at[：:]\s*(.+)$")
        status = "success" if "状态：成功" in text or "状态: 成功" in text else "recorded"
        if media:
            rows.append((pack.name, status, pushed or ""))
    return rows


def cmd_score(args: argparse.Namespace) -> None:
    scores = parse_scores(args.score)
    print(score_table(scores))
    print()
    print(f"composite = {composite(scores)}")


def cmd_init(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ["predictions", "retros", "published"]:
        (root / name).mkdir(exist_ok=True)
        keep = root / name / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")
    state = load_state(root)
    save_state(root, state)
    print(f"Initialized Huazi content ops at {root}")
    print(f"State: {state_path(root)}")


def cmd_predict(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    state = load_state(root)
    scores = parse_scores(args.score)
    item_id = args.id or stable_id(args.title, args.source)
    date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    short = args.short or args.title[:12].replace(" ", "-")
    out_dir = root / "predictions"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}_{item_id}_{short}.md"
    if out_path.exists() and not args.force:
        raise SystemExit(f"Prediction exists: {out_path}. Use --force to overwrite before publishing.")
    experiment_path = Path(args.experiment).resolve() if getattr(args, "experiment", None) else None
    experiment = read_json_file(experiment_path) if experiment_path else {}

    def experiment_value(key: str) -> str:
        return str(experiment.get(key, ""))

    body = f"""# 发布前预测：{args.title}

- id: {item_id}
- source: {args.source}
- prediction_at: {now_iso()}
- rubric_version: {state.get("rubric_version", "hzm-v1")}
- main_platform: {args.platform}
- blind_status: no_post_publish_data_seen

## 稿件打分

{score_table(scores)}

公式：

```text
composite = (UV×1.5 + HP×1.2 + SK×1.5 + SC + CR×1.3 + RV + MA) / 8.5 × 2.0
```

## 预测

主平台预期：{args.expectation or ""}

核心赌注：{args.bet or ""}

最大风险：{args.risk or ""}

## 启动期极值实验

- 模式：{experiment_value("mode") or "survival"}
- 唯一变量：{experiment_value("variable")}
- 假设：{experiment_value("hypothesis")}
- 对照：{experiment_value("control")}
- 本条绝活：{experiment_value("signature_move")}
- 击穿理由：{experiment_value("breakout_reason")}
- 观察信号：{experiment_value("audience_signal")}
- 停止条件：{experiment_value("kill_condition")}

要验证的问题：

1. 读者是否能直接按正文完成一个真实动作。
2. 完读、收藏或分享是否来自内容本身，而非互动诱导。
3. 多平台里哪个版本最能带来高质量阅读与实际应用。

## 发布记录

- published_at:
- published_urls:

## 复盘入口

发布后三天创建对应 `retros/{date}_{item_id}_{short}.md`。
不要修改本文件 `## 预测` 段，只能在复盘文件里对账。
"""
    out_path.write_text(body, encoding="utf-8")
    state["last_prediction_at"] = now_iso()
    pending = state.setdefault("pending_retros", [])
    if str(out_path) not in pending:
        pending.append(str(out_path))
    save_state(root, state)
    print(f"Wrote prediction: {out_path}")
    print(f"Composite: {composite(scores)}")


def resolve_prediction(args: argparse.Namespace) -> Path:
    if args.prediction:
        prediction = Path(args.prediction).resolve()
    elif args.pack:
        pack = Path(args.pack).resolve()
        prediction = pack / "prediction.md"
    else:
        raise SystemExit("retro requires --prediction or --pack")
    if not prediction.exists():
        raise SystemExit(f"Prediction not found: {prediction}")
    return prediction


def cmd_retro(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    state = load_state(root)
    prediction = resolve_prediction(args)
    item_id = prediction_id(prediction)
    out_dir = root / "retros"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.output).resolve() if args.output else out_dir / f"{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')}_{item_id}_retro.md"
    metrics = args.metric or []
    metric_lines = "\n".join(f"- {m}" for m in metrics) if metrics else "- "
    complete = bool(args.complete or metrics)
    calibration_text = "是" if complete else "否，等待真实数据录入"
    status_text = "complete" if complete else "template"
    experiment = read_json_file(Path(args.pack).resolve() / "growth-experiment.json") if args.pack else {}
    body = f"""# T+3d 复盘：{args.title or prediction_title(prediction)}

- retro_at: {now_iso()}
- prediction_file: {prediction}
- retro_status: {status_text}
- rubric_version: {state.get("rubric_version", "hzm-v1")}

## 实际数据

| 平台 | 发布时间 | 阅读/播放 | 近10条中位数 | 爆款倍数 | 完读/完播 | 收藏 | 分享 | 完成动作 | 备注 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 微信公众号 |  |  |  |  |  |  |  |  |  |
| 今日头条 |  |  |  |  |  |  |  |  |  |
| 小红书 |  |  |  |  |  |  |  |  |  |
| 知乎 |  |  |  |  |  |  |  |  |  |
| 微博头条文章 |  |  |  |  |  |  |  |  |  |
| 抖音视频 |  |  |  |  |  |  |  |  |  |
| 微信视频号 |  |  |  |  |  |  |  |  |  |
| 快手视频 |  |  |  |  |  |  |  |  |  |

## 手动录入指标

{metric_lines}

## 和预测对账

预测命中：

预测偏差：

高估的维度：

低估的维度：

## 极值实验验收

- 实验 ID：{experiment.get("experiment_id", "")}
- 唯一变量：{experiment.get("variable", "")}
- 原假设：{experiment.get("hypothesis", "")}
- 对照基线：{experiment.get("control", "")}
- 本条绝活：{experiment.get("signature_move", "")}
- 最大爆款倍数：
- 实验结果：win / loss / inconclusive
- 只保留的变化：
- 下一轮不得同时改变的变量：

爆款判定参考：任一主平台阅读或播放达到该平台近 10 条中位数的 3 倍。观众反馈只作为实验数据，不作为人格评价。

## 用户信号

读者实际行动：

收藏/分享原因：

负反馈：

## Rubric 观察

是否计入校准池：{calibration_text}

可能需要调高的维度：

可能需要调低的维度：

证据：

## 结论

- 复盘等级：
- 是否做二创：
- 是否进入系列：
- 下次同类选题保留：
- 下次同类选题删除：

## 阶段检查

- 完整复盘是否达到 20 篇：
- 爆款实验是否至少 3 次：
- 可重复系列是否至少 2 个：
- 是否建议人工评估转入长期品牌模式：
"""
    out_path.write_text(body, encoding="utf-8")

    if args.pack:
        pack_retro = Path(args.pack).resolve() / "retro.md"
        pack_retro.write_text(body, encoding="utf-8")

    state["last_retro_at"] = now_iso()
    if complete:
        state["calibration_samples"] = int(state.get("calibration_samples", 0)) + 1
        pending = state.setdefault("pending_retros", [])
        state["pending_retros"] = [p for p in pending if Path(p).resolve() != prediction]
    save_state(root, state)
    print(f"Wrote retro: {out_path}")
    if args.pack:
        print(f"Wrote pack retro: {Path(args.pack).resolve() / 'retro.md'}")
    print(f"Retro status: {status_text}")
    print(f"Calibration samples: {state.get('calibration_samples', 0)}")


def write_if_missing(path: Path, body: str) -> None:
    if not path.exists():
        atomic_write_text(path, body)


def cmd_pack(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    pack_root = Path(args.out).resolve()
    image_date = pack_date_prefix(pack_root)
    if not image_date:
        raise SystemExit("Pack directory name must start with YYYY-MM-DD")
    image_names = five_image_names(pack_root)
    pack_root.mkdir(parents=True, exist_ok=True)
    (pack_root / "images").mkdir(exist_ok=True)
    (pack_root / "data").mkdir(exist_ok=True)

    scores = parse_scores(args.score)
    title = args.title
    pack_name = pack_root.name

    with pack_lock(pack_root, "pack-create"):
        from workflow_policy import ACTIVE_PLATFORMS, ARTICLE_FILES
        copy_files = {ARTICLE_FILES[pid] for pid in ACTIVE_PLATFORMS}
        if "weibo" in ACTIVE_PLATFORMS:
            copy_files.add("weibo.md")
        result_files = {pid + "-publish-result.md" for pid in ACTIVE_PLATFORMS}
        platform_file_list = "\n".join(f"- `{name}`" for name in sorted(copy_files | result_files))
        from image_quality_gate import required as image_quality_required, skeleton as image_storyboard_skeleton
        if image_quality_required(pack_root):
            write_if_missing(pack_root / "image-storyboard.json", json.dumps(
                image_storyboard_skeleton(pack_root), ensure_ascii=False, indent=2) + "\n")
        write_if_missing(pack_root / "README.md", f"""# {title}

状态：发布包骨架已创建

## 文件

{platform_file_list}
- `fact-check-notes.md`
- `claim-verification.json`
- `research-evidence-report.json`
- `publish-checklist.md`
- `prediction.md`
- `topic-candidates.json`
- `topic-ranking.json`
- `growth-experiment.json`
- `content-workflow.json`
- `content-quality-report.json`
- `distribution-plan.json`
- `distribution-results.json`
- `distribution-audit.json`
- `revision-notes.md`
- `reader-action-checklist.md`
- `image-spec.md`
- `image-storyboard.json` (new visual policy packs)
- `content-visual-brief.json` (authoring cards and prompts, not an approval)
- `image-visual-review.json` (written only after actual visual inspection)
- `image-quality-report.json` (generated by image_quality_gate.py)
- `images/{image_names[0]}`
- `images/{image_names[1]}`
- `images/{image_names[2]}`
- `images/{image_names[3]}`
- `images/{image_names[4]}`
""")
        write_if_missing(pack_root / "image-spec.md", f"""# Image specification

- `{image_names[0]}`: 900x383 PNG cover
- `{image_names[1]}`: 1080x720 PNG body image
- `{image_names[2]}`: 1080x720 PNG body image
- `{image_names[3]}`: 1080x720 PNG body image
- `{image_names[4]}`: 1080x720 PNG body image

The five files must be distinct, decodable PNG images. New packs must not use legacy names.
For packs dated 2026-10-05 onward, complete image-storyboard.json before generation.
Use five distinct information roles: cover, news fact, mechanism, scenario, action/boundary.
Run scripts/content_visual_brief.py --pack <pack> to assemble writing cards and five prompts.
Read docs/editorial-design-playbook.md; prepared is not factual or visual approval.
Review line-located editorial advice for the current plan's manuscripts only.
Optional visual_explanation records subject, visible relationship, reader takeaway and implication to avoid.
It expresses author intent only; never fill factual or visual passing scores from these fields.
Inspect all five images before writing hash-bound image-visual-review.json.
Run scripts/image_quality_gate.py before dry-run; technical metrics do not prove visual quality.
""")
        write_if_missing(
            pack_root / "topic-candidates.json",
            json.dumps({
                "schema_version": 2,
                "generated_at": now_iso(),
                "growth_strategy_id": "huazi-survival-extreme-v1",
                "editorial_strategy_id": "ai-news-plus-knowledge-v1",
                "candidate_rules": {
                    "minimum_ai_news_count": 3,
                    "minimum_ai_news_ratio": 0.6,
                    "minimum_heat_signal_count": 2,
                    "minimum_heat_signal_kinds": 2,
                    "minimum_independent_heat_signal_domains": 2,
                    "required_knowledge_fields": [
                        "why_it_matters",
                        "mental_model",
                        "reader_action",
                        "fact_boundary"
                    ]
                },
                "experiment_fields": [
                    "variable",
                    "hypothesis",
                    "control",
                    "signature_move",
                    "breakout_reason",
                    "audience_signal",
                    "kill_condition"
                ],
                "candidates": [],
            }, ensure_ascii=False, indent=2) + "\n",
        )
        write_if_missing(
            pack_root / "content-approval.json",
            json.dumps({
                "schema_version": 1,
                "decision": "policy_auto",
                "reviewer": "huazi-workflow",
                "reviewed_at": now_iso(),
                "note": "Deterministic ranking, one-variable growth experiment, compliance, asset and dry-run gates apply before draft actions.",
            }, ensure_ascii=False, indent=2) + "\n",
        )
        write_if_missing(
            pack_root / "claim-verification.json",
            json.dumps({
                "schema_version": 3,
                "policy_id": "huazi-research-evidence-v1",
                "status": "pending",
                "verification_mode": "separate_post_generation",
                "verified_at": "",
                "verified_files": [],
                "sources": [],
                "claims": [],
            }, ensure_ascii=False, indent=2) + "\n",
        )
        for filename, heading in [
        ("wechat-article.md", "公众号正文"),
        ("toutiao-article.md", "今日头条正文"),
        ("zhihu-article.md", "知乎正文"),
        ("xiaohongshu.md", "小红书文案"),
        ("weibo-article.md", "微博头条文章"),
        ("weibo.md", "微博文案"),
        ("multi-platform.md", "多平台分发"),
        ("fact-check-notes.md", "事实核查"),
        ("wechat-publish-result.md", "微信公众号草稿推送结果"),
        ("toutiao-publish-result.md", "今日头条草稿推送结果"),
        ("xiaohongshu-publish-result.md", "小红书草稿结果"),
        ("zhihu-publish-result.md", "知乎草稿结果"),
        ("weibo-publish-result.md", "微博草稿结果"),
        ("revision-notes.md", "修订记录"),
        ("reader-action-checklist.md", "读者行动清单"),
        ]:
            if filename in {*ARTICLE_FILES.values(), "weibo.md"} and filename not in copy_files:
                continue
            if filename.endswith("-publish-result.md") and filename not in result_files:
                continue
            if filename == "multi-platform.md" and len(ACTIVE_PLATFORMS) == 1:
                continue
            write_if_missing(pack_root / filename, f"# {heading}\n\n标题：{title}\n\n")

        write_if_missing(pack_root / "publish-checklist.md", f"""# 发布前检查

标题：{title}

- [ ] 标题短、清楚，呈现已核验的新变化，不强造冲突。
- [ ] 首屏有事实来源句。
- [ ] `fact-check-notes.md` 有来源链接。
- [ ] 正文完成后单独填写 schema v3 `claim-verification.json`，逐条核验承重事实，不与写作使用同一遍推理。
- [ ] 每个数字主张都记录同一来源的证据原句、来源 ID、逐值清单和比较范围；换算值写明推导，禁止跨评测替换。
- [ ] 2026-10-07 起每个来源导入公开原文快照，每条已核验主张记录 evidence_spans；原句在所引来源的区间中逐字存在。定位通过不等于事实或语义通过，独立核验仍须完成。操作见 docs/source-grounding.md。
- [ ] `research-evidence-report.json` 通过，来源跨至少两个独立域名，正文哈希与核验时一致。
- [ ] 候选池至少 60% 且不少于 3 个为 48 小时内 AI 新闻，热度信号可核验。
- [ ] 每个新闻候选至少有两类、两个独立域名的热度信号，不能用同一来源自证热度。
- [ ] 最终内容不是新闻搬运：已解释普通人影响、知识模型、当天行动和事实边界。
- [ ] 正文至少一个真实使用场景。
- [ ] 正文至少一个可执行流程。
- [ ] 方法、清单或模板已在正文内完整交付，不要求互动换取。
- [ ] 五张图存在且正文全部引用。
- [ ] 封面为 `900x383`。
- [ ] 新图文质量策略：五图分镜完整、信息各不相同、长文按序穿插，逐张目视评分后运行 `image_quality_gate.py`；不自动填写视觉通过。
- [ ] `growth-experiment.json` 与当前 `ranking_id` 一致，只测试一个变量，并写明本条绝活与停止条件。
- [ ] 五个平台合规扫描通过，未出现诱导关注、点赞、评论、收藏或转发。
- [ ] `content-quality-report.json` 通过：开头事实、具体场景、平台差异和自包含行动均合格。
- [ ] `prediction.md` 已写在发布前。
- [ ] 微信 dry-run 通过。
- [ ] 正式草稿结果写入 `wechat-publish-result.md`。
- [ ] 多平台路由 dry-run 通过。
- [ ] 多平台结果写入 `distribution-results.json`。
- [ ] 多平台结果审计已生成，未把部分完成或阻塞误报为成功。
- [ ] 仅公众号、头条、知乎、小红书、微博保存图文草稿；逐平台重开验收五图、封面和正文留存。
- [ ] 不生成或推送抖音、微信视频号、快手及 B 站稿件，不生成视频、配音或视频质检占位文件。
""")

    pred_args = argparse.Namespace(
        root=str(root),
        title=title,
        platform=args.platform,
        source=args.source,
        id=args.id,
        short=args.short or pack_name,
        score=args.score,
        expectation=args.expectation,
        bet=args.bet,
        risk=args.risk,
        experiment=getattr(args, "experiment", None),
        force=args.force,
    )
    cmd_predict(pred_args)
    latest = sorted((root / "predictions").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[0]
    with pack_lock(pack_root, "prediction-copy"):
        atomic_write_text(
            pack_root / "prediction.md",
            f"# 发布前预测\n\n来源：`{latest}`\n\n" + latest.read_text(encoding="utf-8"),
        )
    print(f"Wrote pack skeleton: {pack_root}")


def cmd_publish(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    state = load_state(root)
    pack = Path(args.pack).resolve() if args.pack else None
    title = args.title
    prediction = None
    if pack:
        title = title or first_match(read_text(pack / "README.md"), r"^#\s+(.+)$", pack.name)
        if (pack / "prediction.md").exists():
            prediction = canonical_prediction(pack / "prediction.md")
    item_id = args.id or stable_id(title or args.platform, args.platform)
    out_dir = root / "published"
    out_dir.mkdir(parents=True, exist_ok=True)
    published_at = args.published_at or now_iso()
    record = {
        "id": item_id,
        "title": title or "",
        "platform": args.platform,
        "published_at": published_at,
        "url": args.url or "",
        "media_id": args.media_id or "",
        "pack": str(pack) if pack else "",
        "prediction": str(prediction) if prediction else "",
    }
    out_path = out_dir / f"{published_at[:10]}_{item_id}_{args.platform}.md"
    out_path.write_text(
        "# 发布登记\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in record.items())
        + "\n\n## 复盘提醒\n\n发布后三天运行：\n\n"
        + f"```powershell\npython scripts/huazi_content_ops.py retro --root {root} "
        + (f"--pack {pack}" if pack else f"--prediction {prediction}")
        + "\n```\n",
        encoding="utf-8",
    )
    items = state.setdefault("published_items", [])
    items.append(record)
    if prediction:
        pending = state.setdefault("pending_retros", [])
        if str(prediction) not in pending:
            pending.append(str(prediction))
    save_state(root, state)
    print(f"Wrote published record: {out_path}")
    if prediction:
        print(f"Pending retro: {prediction}")


def cmd_status(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    state = load_state(root)
    outputs_root = Path(args.outputs_root).resolve() if args.outputs_root else root.parent
    now = datetime.now(timezone(timedelta(hours=8)))
    predictions = sorted((root / "predictions").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    retros = sorted((root / "retros").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    pending_raw = [Path(p) for p in state.get("pending_retros", [])]
    pending = [p for p in pending_raw if p.exists()]
    overdue = [p for p in pending if due_status(p, now).startswith("overdue")]
    published_rows = published_pack_rows(outputs_root, root)
    samples = int(state.get("calibration_samples", 0))
    samples_at_bump = int(state.get("calibration_samples_at_last_bump", 0))
    since_bump = samples - samples_at_bump

    lines = [
        "# 华子社长内容运营状态",
        "",
        f"- generated_at: {now_iso()}",
        f"- root: `{root}`",
        f"- rubric_version: {state.get('rubric_version', 'hzm-v1')}",
        f"- predictions: {len(predictions)}",
        f"- retros: {len(retros)}",
        f"- calibration_samples: {samples}",
        f"- samples_since_last_bump: {since_bump}",
        f"- pending_retros: {len(pending)}",
        f"- overdue_retros: {len(overdue)}",
        "",
        "## 待复盘",
        "",
    ]
    if pending:
        lines.extend(["| 预测 | 状态 | 文件 |", "|---|---|---|"])
        for p in pending[:12]:
            lines.append(f"| {prediction_title(p)} | {due_status(p, now)} | `{short_path(p, root)}` |")
    else:
        lines.append("暂无待复盘预测。")

    lines.extend(["", "## 最近预测", ""])
    if predictions:
        lines.extend(["| 预测 | 时间 | 文件 |", "|---|---|---|"])
        for p in predictions[:8]:
            lines.append(f"| {prediction_title(p)} | {prediction_at(p) or '-'} | `{short_path(p, root)}` |")
    else:
        lines.append("暂无预测文件。")

    lines.extend(["", "## 已推草稿/发布包", ""])
    if published_rows:
        lines.extend(["| 发布包 | 状态 | 时间 |", "|---|---|---|"])
        for pack_name, status, pushed in published_rows[:12]:
            lines.append(f"| `{pack_name}` | {status} | {pushed or '-'} |")
    else:
        lines.append("暂无可识别的草稿推送记录。")

    lines.extend(["", "## 下一步", ""])
    if overdue:
        lines.append("- 优先处理 overdue 复盘，补齐真实数据后再考虑升级公式。")
    elif since_bump >= 5:
        lines.append("- 校准样本已满 5 篇，运行 `bump-report` 检查是否升级 rubric。")
    elif pending:
        lines.append("- 等待 T+3d 数据，先不要修改预测段。")
    else:
        lines.append("- 当前没有待复盘项，可以继续选题或生成发布包。")

    body = "\n".join(lines) + "\n"
    print(body)
    if not args.no_write:
        root.mkdir(parents=True, exist_ok=True)
        out = root / "STATUS.md"
        out.write_text(body, encoding="utf-8")
        print(f"Wrote status: {out}")


def cmd_bump_report(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    state = load_state(root)
    retros = sorted((root / "retros").glob("*.md"))
    out = Path(args.output).resolve() if args.output else root / "rubric-bump-report.md"
    min_samples = int(args.min_samples)
    complete_retros = []
    template_retros = []
    for p in retros:
        text = read_text(p)
        if "retro_status: complete" in text or "是否计入校准池：是" in text:
            complete_retros.append(p)
        else:
            template_retros.append(p)
    samples = int(state.get("calibration_samples", len(complete_retros)))
    samples_at_bump = int(state.get("calibration_samples_at_last_bump", 0))
    since_bump = samples - samples_at_bump
    body = [
        "# Rubric 升级报告",
        "",
        f"- generated_at: {now_iso()}",
        f"- rubric_version: {state.get('rubric_version')}",
        f"- calibration_samples: {samples}",
        f"- calibration_samples_at_last_bump: {samples_at_bump}",
        f"- samples_since_last_bump: {since_bump}",
        f"- retro_files_found: {len(retros)}",
        f"- complete_retros_found: {len(complete_retros)}",
        f"- template_retros_found: {len(template_retros)}",
        f"- min_samples_for_bump: {min_samples}",
        "",
        "## 当前判断",
        "",
    ]
    if since_bump < min_samples:
        body.append(f"当前距离上次升级后的完整复盘样本少于 {min_samples} 篇，建议继续积累，不升级公式。")
    else:
        body.append("样本数已达到升级门槛。可以人工检查连续高估/低估，再决定是否创建新 rubric 版本。")
    body.extend([
        "",
        "## Cheat-on-content 升级规则",
        "",
        "- 不因为单篇异常表现改权重。",
        "- 新公式必须解释至少 4/5 个校准样本的真实排序。",
        "- 被数据推翻的观察要删掉或改写，不把 rubric 写成历史博物馆。",
        "- 如果只生成了复盘模板但没有真实数据，不计入校准池。",
        "",
        "## 完整复盘文件",
        "",
    ])
    for p in complete_retros:
        body.append(f"- `{p}`")
    if not complete_retros:
        body.append("- 暂无完整复盘。")
    body.extend(["", "## 模板/未完成复盘", ""])
    for p in template_retros:
        body.append(f"- `{p}`")
    if not template_retros:
        body.append("- 无。")
    body.extend([
        "",
        "## 升级检查问题",
        "",
        "- 哪些维度持续高估？",
        "- 哪些维度持续低估？",
        "- `SK Skill 可交付度` 是否比 `HP 钩子强度` 更能预测收藏？",
        "- `CR 自包含行动潜力` 是否更能预测完读、收藏与实际应用？",
        "- 不同平台是否需要不同权重？",
        "",
        "## 建议动作",
        "",
    ])
    if since_bump >= min_samples:
        body.extend([
            "1. 把完整复盘按实际表现从高到低排序。",
            "2. 对照旧 composite 排序，标出错位样本。",
            "3. 只调整连续错位的维度，不调整偶然样本。",
            "4. 生成 `rubric-notes.md` 新版本前，保留旧公式和理由。",
        ])
    else:
        body.append(f"继续补齐 T+3d 复盘；还差 {min_samples - since_bump} 个完整样本。")
    out.write_text("\n".join(body) + "\n", encoding="utf-8")
    print(f"Wrote bump report: {out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Huazi content ops helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init")
    init.add_argument("--root", default="outputs/huazi-social-agent")
    init.set_defaults(func=cmd_init)

    score = sub.add_parser("score")
    score.add_argument("--score", action="append", required=True, help="Dimension score, e.g. UV=5")
    score.set_defaults(func=cmd_score)

    predict = sub.add_parser("predict")
    predict.add_argument("--root", default="outputs/huazi-social-agent")
    predict.add_argument("--title", required=True)
    predict.add_argument("--platform", default="微信公众号")
    predict.add_argument("--source", default="manual")
    predict.add_argument("--id")
    predict.add_argument("--short")
    predict.add_argument("--score", action="append", required=True)
    predict.add_argument("--expectation")
    predict.add_argument("--bet")
    predict.add_argument("--risk")
    predict.add_argument("--experiment", help="Path to growth-experiment.json")
    predict.add_argument("--force", action="store_true")
    predict.set_defaults(func=cmd_predict)

    retro = sub.add_parser("retro")
    retro.add_argument("--root", default="outputs/huazi-social-agent")
    retro.add_argument("--prediction")
    retro.add_argument("--pack")
    retro.add_argument("--output")
    retro.add_argument("--title")
    retro.add_argument("--metric", action="append")
    retro.add_argument("--complete", action="store_true")
    retro.set_defaults(func=cmd_retro)

    publish = sub.add_parser("publish")
    publish.add_argument("--root", default="outputs/huazi-social-agent")
    publish.add_argument("--pack")
    publish.add_argument("--platform", default="微信公众号")
    publish.add_argument("--title")
    publish.add_argument("--url")
    publish.add_argument("--media-id")
    publish.add_argument("--published-at")
    publish.add_argument("--id")
    publish.set_defaults(func=cmd_publish)

    status = sub.add_parser("status")
    status.add_argument("--root", default="outputs/huazi-social-agent")
    status.add_argument("--outputs-root")
    status.add_argument("--no-write", action="store_true")
    status.set_defaults(func=cmd_status)

    pack = sub.add_parser("pack")
    pack.add_argument("--root", default="outputs/huazi-social-agent")
    pack.add_argument("--out", required=True)
    pack.add_argument("--title", required=True)
    pack.add_argument("--platform", default="微信公众号")
    pack.add_argument("--source", default="manual")
    pack.add_argument("--id")
    pack.add_argument("--short")
    pack.add_argument("--score", action="append", required=True)
    pack.add_argument("--expectation")
    pack.add_argument("--bet")
    pack.add_argument("--risk")
    pack.add_argument("--experiment", help="Path to growth-experiment.json")
    pack.add_argument("--force", action="store_true")
    pack.set_defaults(func=cmd_pack)

    bump = sub.add_parser("bump-report")
    bump.add_argument("--root", default="outputs/huazi-social-agent")
    bump.add_argument("--output")
    bump.add_argument("--min-samples", default=5)
    bump.set_defaults(func=cmd_bump_report)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
