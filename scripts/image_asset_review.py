"""Inspect captured image bytes before replacement; never certify visual quality."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any
import warnings

from PIL import Image, ImageChops, ImageStat, __version__ as pillow_version

from content_revision_review import CONTEXT_FILES, checked_path, strict_json
from distribution_io import canonical_sha256, file_sha256, five_image_names, pack_date_prefix


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config/image-quality-standard.json"
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_BATCH_BYTES = 160 * 1024 * 1024
MAX_PIXELS = 4_000_000


class AssetInputError(ValueError):
    pass


def captured(path: Path, limit: int = MAX_IMAGE_BYTES) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise AssetInputError(f"ASSET_FILE_TOO_LARGE: {path.name}")
    return raw


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_png(raw: bytes, expected: tuple[int, int] | None = None) -> tuple[dict, Image.Image]:
    # Inspect and decode the same captured bytes, never a later source-file read.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as image:
            if image.format != "PNG":
                raise AssetInputError("ASSET_NOT_PNG")
            if image.width * image.height > MAX_PIXELS:
                raise AssetInputError("ASSET_PIXEL_LIMIT")
            if expected and image.size != expected:
                raise AssetInputError("ASSET_WRONG_DIMENSIONS")
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            if getattr(image, "n_frames", 1) != 1 or getattr(image, "is_animated", False):
                raise AssetInputError("ASSET_ANIMATED_PNG")
            image.load()
            rgba = image.convert("RGBA")
    visible = Image.new("RGBA", rgba.size, "white")
    visible.alpha_composite(rgba)
    rgb = visible.convert("RGB")
    thumb = rgb.resize((64, 64), Image.Resampling.LANCZOS)
    return {"format": "PNG", "size": list(rgba.size), "frame_count": 1, "decoded": True,
            "rgba_sha256": sha(str(rgba.size).encode() + rgba.tobytes()),
            "white_composited_pixel_sha256": sha(str(rgb.size).encode() + rgb.tobytes()),
            "pixel_stddev": max(ImageStat.Stat(thumb).stddev)}, rgba


def pixel_change(before: Image.Image, after: Image.Image) -> dict:
    if before.size != after.size:
        return {"kind": "dimensions_changed", "before_size": list(before.size), "after_size": list(after.size)}
    channels = ImageChops.difference(before, after).split()
    mask = channels[0]
    for channel in channels[1:]:
        mask = ImageChops.lighter(mask, channel)
    count = after.width * after.height - mask.histogram()[0]
    return {"kind": "pixels_changed" if count else "same_decoded_pixels",
            "different_rgba_pixels": count, "total_pixels": after.width * after.height,
            "bounding_box": list(mask.getbbox()) if mask.getbbox() else None,
            "interpretation": "Exact decoded RGBA difference, not perceptual, semantic or quality scoring"}


def staged_files(source: Path) -> list[Path]:
    files = []
    for path in source.iterdir():
        checked_path(source, path.name)
        if path.name.startswith(".") or ":" in path.name or not path.is_file() or path.suffix.lower() != ".png":
            raise AssetInputError(f"ASSET_UNEXPECTED_STAGING_ENTRY: {path.name}")
        files.append(path)
        if len(files) > 32:
            raise AssetInputError("ASSET_TOO_MANY_FILES")
    return sorted(files)


def resolve_mapping(pack: Path, source: Path, files: list[Path], subdir: str,
                    mapping: Path | None, strict: bool) -> tuple[list[tuple[Path, Path]], dict]:
    target = checked_path(pack, subdir).resolve()
    if target == pack or (target.exists() and not target.is_dir()):
        raise AssetInputError("ASSET_TARGET_DIRECTORY_INVALID")
    five = target == pack / "images"
    names = five_image_names(pack) if five else [p.name for p in files]
    if not files or (five and len(files) != len(names)):
        raise AssetInputError("ASSET_EXACTLY_FIVE_REQUIRED" if five else "ASSET_NO_FILES")
    by_name = {p.name: p for p in files}
    if len({p.name.casefold() for p in files}) != len(files):
        raise AssetInputError("ASSET_CASE_COLLISION")
    binding: dict[str, Any] = {"mode": "canonical_names"}
    if mapping:
        if not five:
            raise AssetInputError("ASSET_MAPPING_REQUIRES_FIVE_IMAGE_DIRECTORY")
        checked_path(mapping.parent, mapping.name)
        raw = captured(mapping, 64 * 1024)
        value = strict_json(raw.decode("utf-8"))
        rows = value.get("images") if isinstance(value, dict) else None
        if (not isinstance(rows, list) or len(rows) != len(names)
                or any(not isinstance(r, dict) or set(r) != {"source", "target"}
                       or not isinstance(r["source"], str) or not isinstance(r["target"], str) for r in rows)):
            raise AssetInputError("ASSET_MAPPING_INVALID")
        destinations = [f"images/{n}" for n in names]
        if (sorted(r["source"] for r in rows) != sorted(by_name)
                or sorted(r["target"] for r in rows) != sorted(destinations)):
            raise AssetInputError("ASSET_MAPPING_MUST_BE_BIJECTIVE")
        lookup = {r["target"]: r["source"] for r in rows}
        ordered = [by_name[lookup[n]] for n in destinations]
        binding = {"mode": "explicit_mapping", "path": str(mapping), "sha256": sha(raw)}
    elif set(by_name) == set(names):
        ordered = [by_name[n] for n in names]
    elif five and strict:
        raise AssetInputError("ASSET_ORDER_REQUIRED: use dated names or --mapping; arbitrary lexical order is unsafe")
    else:
        ordered = files
        binding = {"mode": "legacy_lexical_order", "warning": "Inspect source-to-target order; names do not prove roles"}
    return [(p, checked_path(pack, (target / name).relative_to(pack).as_posix()))
            for p, name in zip(ordered, names)], binding


def prepare_assets(pack: Path, source: Path, subdir: str = "images", mapping: Path | None = None) -> tuple[dict, dict[str, bytes]]:
    pack, source = pack.absolute(), source.absolute()
    checked_path(pack, ".")
    checked_path(source, ".")
    pack, source = pack.resolve(), source.resolve()
    if not pack.is_dir() or not source.is_dir():
        raise AssetInputError("ASSET_DIRECTORIES_REQUIRED")
    if source.resolve().is_relative_to(pack.resolve()) or pack.resolve().is_relative_to(source.resolve()):
        raise AssetInputError("ASSET_DISJOINT_STAGING_REQUIRED")
    policy_raw = captured(POLICY, 64 * 1024)
    policy = strict_json(policy_raw.decode("utf-8"))
    strict = (pack_date_prefix(pack) >= policy["effective_from"] or (pack / "image-storyboard.json").exists())
    pairs, binding = resolve_mapping(pack, source, staged_files(source), subdir, mapping, strict)
    five = pairs[0][1].parent == pack / "images"
    strict = strict and five
    context = {}
    board = {}
    for name in (*CONTEXT_FILES, "image-visual-review.json", "image-quality-report.json", "image-count.json"):
        path = checked_path(pack, name)
        if path.exists() and not path.is_file():
            raise AssetInputError(f"ASSET_CONTEXT_NOT_FILE: {name}")
        context[name] = file_sha256(path) if path.is_file() else None
        if name == "image-storyboard.json" and path.is_file():
            raw = captured(path, 2 * 1024 * 1024)
            context[name] = sha(raw)
            board = strict_json(raw.decode("utf-8"))
            if not isinstance(board, dict):
                raise AssetInputError("ASSET_STORYBOARD_NOT_OBJECT")
    items = board.get("items", [])
    if not isinstance(items, list):
        raise AssetInputError("ASSET_STORYBOARD_ITEMS_INVALID")
    board_rows = {r.get("image"): r for r in items if isinstance(r, dict) and isinstance(r.get("image"), str)}
    staged, rows, blockers, pixel_hashes, total = {}, [], [], {}, 0
    inventory = []
    target_dir = pairs[0][1].parent
    if target_dir.exists():
        for path in sorted(target_dir.rglob("*")):
            name = path.relative_to(pack).as_posix()
            checked_path(pack, name)
            if path.is_file():
                inventory.append(name)
    expected = [p.relative_to(pack).as_posix() for _, p in pairs]
    extras = sorted(set(inventory) - set(expected))
    if strict and extras:
        blockers.append({"code": "ASSET_TARGET_EXTRA_FILES", "files": extras})
    for index, (path, destination) in enumerate(pairs):
        name = destination.relative_to(pack).as_posix()
        if destination.exists() and not destination.is_file():
            raise AssetInputError(f"ASSET_DESTINATION_NOT_FILE: {name}")
        raw = captured(path)
        old = captured(destination) if destination.is_file() else None
        total += len(raw) + len(old or b"")
        if total > MAX_BATCH_BYTES:
            raise AssetInputError("ASSET_BATCH_TOO_LARGE")
        staged[name] = raw
        row: dict[str, Any] = {"source": path.name, "target": name, "bytes": len(raw),
            "before_sha256": sha(old) if old is not None else None, "staged_sha256": sha(raw),
            "changed": old != raw, "expected_role": policy["roles"][index] if five else None,
            "author_intent_not_verified": {k: board_rows.get(name, {}).get(k) for k in (
                "role", "section_anchor", "message", "on_image_text", "visual_explanation")},
            "actual_visual_review": "not_performed"}
        rows.append(row)
        try:
            size = (900, 383) if index == 0 else (1080, 720)
            probe, image = decode_png(raw, size if strict else None)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            code = str(exc) if isinstance(exc, AssetInputError) else "ASSET_DECODE_FAILED"
            blockers.append({"code": code, "file": name})
            continue
        row["probe"] = probe
        if strict:
            pixel_hash = probe["white_composited_pixel_sha256"]
            if pixel_hash in pixel_hashes:
                blockers.append({"code": "ASSET_PIXEL_DUPLICATE", "file": name, "same_as": pixel_hashes[pixel_hash]})
            pixel_hashes[pixel_hash] = name
            if probe["pixel_stddev"] < policy["minimum_image_stddev"]:
                blockers.append({"code": "ASSET_BLANK_OR_FLAT", "file": name})
        row["comparison"] = {"kind": "new_image"}
        if old is not None:
            try:
                _, previous = decode_png(old)
                row["comparison"] = pixel_change(previous, image)
                if old == raw:
                    row["comparison"]["kind"] = "identical_file"
                elif row["comparison"]["kind"] == "same_decoded_pixels":
                    row["comparison"]["kind"] = "encoded_bytes_only_changed"
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
                row["comparison"] = {"kind": "old_image_unreadable", "action": "Inspect replacement; no baseline comparison available"}
    report = {"schema_version": 1, "authority": "asset_preflight_not_visual_approval",
        "pack": str(pack), "source": str(source), "subdir": subdir, "strict_five_image_policy": strict,
        "mapping": binding, "files": rows, "blockers": blockers, "target_inventory": inventory,
        "context_sha256": context, "policy_sha256": sha(policy_raw), "pillow_version": pillow_version,
        "implementation_sha256": {p: file_sha256(ROOT / "scripts" / p) for p in (
            "image_asset_review.py", "pack_asset_writer.py", "content_revision_review.py", "distribution_io.py")},
        "technical_preflight_passed": not blockers,
        "requires_visual_review": any(r["changed"] for r in rows),
        "visual_review_performed": False, "fact_verification_performed": False, "draft_actions_performed": False,
        "limits": ["No OCR, semantic verification, aesthetics score or publication approval.",
                   "Roles and labels are author-provided intent, not observed image contents.",
                   "Exact pixel differences do not show whether an edit is better.",
                   "Matching review ID binds inputs, not human approval; rerun existing gates after changes."]}
    return {**report, "review_id": "asset-review-" + canonical_sha256(report)[:24]}, staged


def assets_are_current(pack: Path, source: Path, subdir: str, mapping: Path | None, review: dict) -> bool:
    current, _ = prepare_assets(pack, source, subdir, mapping)
    return current == review
