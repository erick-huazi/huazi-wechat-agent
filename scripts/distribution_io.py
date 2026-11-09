#!/usr/bin/env python3
"""Reliable local file operations for Huazi distribution packs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from PIL import Image, UnidentifiedImageError


PACK_LOCK_NAME = ".distribution-action.lock"
PACK_LOCK_ENV = "HUAZI_PACK_LOCK_HELD"
LEDGER_CHAIN_VERSION = 1
FIVE_IMAGE_COUNT = 5
LEGACY_FIVE_IMAGE_NAMES = (
    "00-cover.png",
    "01-context.png",
    "02-user-scene.png",
    "03-structure.png",
    "04-risk-or-action.png",
)


class PackLockError(RuntimeError):
    """Raised when another process is already mutating a distribution pack."""


def pack_date_prefix(pack: Path) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-|$)", pack.name)
    return match.group(1) if match else ""


def validate_image_count(pack: Path, value: Any) -> int:
    image_date = pack_date_prefix(pack)
    if not image_date or image_date < "2026-11-09":
        raise ValueError("IMAGE_COUNT_SCOPE_INVALID")
    if (not isinstance(value, dict) or set(value) != {"schema_version", "count"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["count"]) is not int or not 5 <= value["count"] <= 20):
        raise ValueError("IMAGE_COUNT_INVALID")
    return value["count"]


def five_image_names(pack: Path) -> list[str]:
    """Return the declared dated inventory; old packs retain their five-image contract."""
    image_date = pack_date_prefix(pack)
    count = FIVE_IMAGE_COUNT
    declaration = pack / "image-count.json"
    if declaration.exists() or declaration.is_symlink():
        if (not image_date or image_date < "2026-11-09" or declaration.is_symlink()
                or not declaration.resolve().is_relative_to(pack.resolve())):
            raise ValueError("IMAGE_COUNT_SCOPE_INVALID")
        try:
            if declaration.stat().st_size > 1024:
                raise ValueError("IMAGE_COUNT_INVALID")
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("IMAGE_COUNT_INVALID")
                    result[key] = value
                return result
            value = json.loads(declaration.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        except (OSError, ValueError) as exc:
            raise ValueError("IMAGE_COUNT_INVALID") from exc
        count = validate_image_count(pack, value)
    if image_date:
        return [f"{image_date}-{index:02d}.png" for index in range(1, count + 1)]
    return list(LEGACY_FIVE_IMAGE_NAMES)


def five_image_paths(pack: Path) -> list[Path]:
    image_root = pack / "images"
    return [image_root / name for name in five_image_names(pack)]


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Replace a text file atomically after flushing it to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def atomic_append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    added = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    atomic_write_text(path, existing + added)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_bundle_snapshot(root: Path, paths: Iterable[Path | str]) -> dict[str, Any]:
    """Fingerprint a bounded group of files without trusting timestamps."""
    resolved_root = root.resolve()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = resolved_root / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(resolved_root).as_posix()
            allowed = True
        except ValueError:
            relative = f"external:{resolved.name}"
            allowed = False
        if relative in seen:
            continue
        seen.add(relative)
        exists = allowed and resolved.is_file()
        rows.append({
            "path": relative,
            "allowed": allowed,
            "exists": exists,
            "bytes": resolved.stat().st_size if exists else 0,
            "sha256": file_sha256(resolved) if exists else "",
        })
    rows.sort(key=lambda item: item["path"])
    return {
        "valid": bool(rows) and all(item["allowed"] and item["exists"] for item in rows),
        "files": rows,
        "fingerprint": canonical_sha256(rows),
    }


def image_file_probe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "decoded": False, "format": "", "width": None, "height": None, "bytes": 0, "sha256": "", "error": "missing"}
    try:
        with Image.open(path) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
            image.verify()
        # verify() checks the container; load() also decodes every pixel.
        with Image.open(path) as image:
            image.load()
        return {
            "exists": True,
            "decoded": True,
            "format": image_format,
            "width": width,
            "height": height,
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
            "error": "",
        }
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        return {
            "exists": True,
            "decoded": False,
            "format": "",
            "width": None,
            "height": None,
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
            "error": str(exc),
        }


def video_file_probe(path: Path) -> dict[str, Any]:
    """Inspect a local video with ffprobe and return stable validation metadata."""
    empty = {
        "exists": False,
        "decoded": False,
        "format": "",
        "width": None,
        "height": None,
        "duration": None,
        "fps": None,
        "video_codec": "",
        "audio_codec": "",
        "has_audio": False,
        "bytes": 0,
        "sha256": "",
        "error": "missing",
    }
    if not path.is_file():
        return empty

    ffprobe = os.environ.get("FFPROBE_PATH") or shutil.which("ffprobe")
    if not ffprobe:
        return {
            **empty,
            "exists": True,
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
            "error": "ffprobe_unavailable",
        }
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries",
                "format=format_name,duration:stream=codec_type,codec_name,width,height,r_frame_rate",
                "-of", "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "ffprobe_failed")
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), {})
        audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
        rate = str(video.get("r_frame_rate", "0/1"))
        numerator, denominator = (rate.split("/", 1) + ["1"])[:2]
        fps = float(numerator) / float(denominator) if float(denominator or 1) else 0.0
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        duration = float(payload.get("format", {}).get("duration") or 0)
        return {
            "exists": True,
            "decoded": bool(video and width > 0 and height > 0 and duration > 0),
            "format": str(payload.get("format", {}).get("format_name", "")),
            "width": width or None,
            "height": height or None,
            "duration": round(duration, 3),
            "fps": round(fps, 3),
            "video_codec": str(video.get("codec_name", "")),
            "audio_codec": str(audio.get("codec_name", "")),
            "has_audio": bool(audio),
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
            "error": "",
        }
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return {
            **empty,
            "exists": True,
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
            "error": str(exc),
        }


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except (TypeError, ValueError):
        return None


def read_pack_lock(pack: Path, stale_seconds: int = 1800) -> dict[str, Any]:
    path = pack / PACK_LOCK_NAME
    if not path.is_file():
        return {"active": False, "stale": False, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"action": "unknown", "started_at": "", "pid": None, "token": ""}
    started = _parse_time(str(payload.get("started_at", "")))
    age_seconds = max(int((datetime.now().astimezone() - started).total_seconds()), 0) if started else None
    return {
        **payload,
        "active": True,
        "stale": age_seconds is None or age_seconds > stale_seconds,
        "age_seconds": age_seconds,
        "path": str(path),
    }


@contextmanager
def pack_lock(pack: Path, action: str, stale_seconds: int = 1800) -> Iterator[dict[str, Any]]:
    """Acquire a cross-process lock, with one stale-lock recovery attempt."""
    resolved = str(pack.resolve())
    if os.environ.get(PACK_LOCK_ENV) == resolved:
        yield {"active": True, "inherited": True, "action": action, "path": str(pack / PACK_LOCK_NAME)}
        return

    lock_path = pack / PACK_LOCK_NAME
    token = uuid.uuid4().hex
    payload = {
        "schema_version": 1,
        "token": token,
        "pid": os.getpid(),
        "action": action,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    for attempt in range(2):
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            break
        except FileExistsError as exc:
            current = read_pack_lock(pack, stale_seconds)
            if attempt == 0 and current.get("stale"):
                stale_path = pack / f".distribution-action.stale-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
                try:
                    os.replace(lock_path, stale_path)
                    continue
                except OSError:
                    pass
            owner = current.get("action") or "unknown"
            raise PackLockError(f"发布包正由另一项任务处理：{owner}") from exc

    try:
        yield {**payload, "active": True, "inherited": False, "path": str(lock_path)}
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else {}
            if current.get("token") == token:
                lock_path.unlink()
        except (OSError, json.JSONDecodeError):
            pass


def _safe_backup_relative_name(raw: str) -> Path | None:
    relative = Path(str(raw).replace("\\", "/"))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return None
    return relative


def create_pack_backup(pack: Path, action: str, names: list[str], keep: int = 10) -> Path:
    root = pack / ".backups"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = root / f"{stamp}-{action}"
    destination.mkdir()
    copied: list[dict[str, Any]] = []
    tracked_names: list[str] = []
    for raw_name in dict.fromkeys(names):
        relative = _safe_backup_relative_name(raw_name)
        if relative is None:
            continue
        name = relative.as_posix()
        tracked_names.append(name)
        source = pack / relative
        if source.is_file():
            copied_path = destination / relative
            copied_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, copied_path)
            copied.append({"name": name, "bytes": copied_path.stat().st_size, "sha256": file_sha256(copied_path)})
    atomic_write_json(destination / "backup-manifest.json", {
        "schema_version": 2,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": action,
        "pack": str(pack),
        "tracked_files": tracked_names,
        "files": copied,
    })
    backups = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)
    for expired in backups[max(keep, 1):]:
        shutil.rmtree(expired)
    return destination


def verify_pack_backup(path: Path) -> dict[str, Any]:
    manifest_path = path / "backup-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"valid": False, "verified_files": 0, "total_files": 0, "issues": ["备份清单缺失或损坏"]}
    raw_files = manifest.get("files", [])
    if not isinstance(raw_files, list):
        return {"valid": False, "verified_files": 0, "total_files": 0, "issues": ["备份文件列表格式错误"]}
    issues: list[str] = []
    verified = 0
    for raw in raw_files:
        item = raw if isinstance(raw, dict) else {"name": str(raw)}
        relative = _safe_backup_relative_name(str(item.get("name", "")))
        name = relative.as_posix() if relative is not None else ""
        candidate = path / relative if relative is not None else path / "__invalid__"
        if not name or not candidate.is_file():
            issues.append(f"缺少备份文件：{name or '?'}")
            continue
        if item.get("bytes") is not None and candidate.stat().st_size != item.get("bytes"):
            issues.append(f"备份大小不匹配：{name}")
            continue
        if item.get("sha256") and file_sha256(candidate) != item.get("sha256"):
            issues.append(f"备份哈希不匹配：{name}")
            continue
        if not item.get("sha256"):
            issues.append(f"旧版备份缺少哈希：{name}")
            continue
        verified += 1
    return {
        "valid": not issues and verified == len(raw_files),
        "verified_files": verified,
        "total_files": len(raw_files),
        "issues": issues,
    }


def compare_pack_backup(pack: Path, path: Path) -> dict[str, Any]:
    """Preview how current mutable files differ from a restore point."""
    manifest_path = path / "backup-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"valid": False, "unchanged": 0, "changed": 0, "missing": 0, "added": 0, "files": []}
    raw_files = manifest.get("files", []) if isinstance(manifest.get("files"), list) else []
    backup_index: dict[str, dict[str, Any]] = {}
    for item in raw_files:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        relative = _safe_backup_relative_name(str(item["name"]))
        if relative is not None:
            backup_index[relative.as_posix()] = item
    tracked = manifest.get("tracked_files") if isinstance(manifest.get("tracked_files"), list) else list(backup_index)
    rows: list[dict[str, Any]] = []
    counts = {"unchanged": 0, "changed": 0, "missing": 0, "added": 0}
    for raw_name in dict.fromkeys(str(item) for item in tracked):
        relative = _safe_backup_relative_name(raw_name)
        if relative is None:
            continue
        name = relative.as_posix()
        current = pack / relative
        backup_item = backup_index.get(name)
        if backup_item is None and current.is_file():
            state = "added"
        elif backup_item is not None and not current.is_file():
            state = "missing"
        elif backup_item is None:
            continue
        else:
            state = "unchanged" if file_sha256(current) == backup_item.get("sha256") else "changed"
        counts[state] += 1
        rows.append({
            "name": name,
            "state": state,
            "current_sha256": file_sha256(current) if current.is_file() else "",
            "backup_sha256": str((backup_item or {}).get("sha256", "")),
        })
    return {"valid": True, **counts, "files": rows}


def list_pack_backups(pack: Path, limit: int = 10, verify: bool = False) -> list[dict[str, Any]]:
    root = pack / ".backups"
    if not root.is_dir():
        return []
    rows = []
    for path in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name, reverse=True)[:limit]:
        manifest_path = path / "backup-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {"created_at": "", "action": "unknown", "files": []}
        row = {"name": path.name, "path": str(path), **manifest}
        if verify:
            row["verification"] = verify_pack_backup(path)
            row["comparison"] = compare_pack_backup(pack, path)
        rows.append(row)
    return rows


def canonical_sha256(payload: Any) -> str:
    stable = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def ledger_record_hash(record: dict[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in record.items() if key != "record_hash"})


def chain_ledger_records(records: list[dict[str, Any]], previous_hash: str = "") -> list[dict[str, Any]]:
    chained: list[dict[str, Any]] = []
    for source in records:
        record = {
            key: value for key, value in source.items()
            if key not in {"chain_version", "previous_hash", "record_hash"}
        }
        record["chain_version"] = LEDGER_CHAIN_VERSION
        record["previous_hash"] = previous_hash
        record["record_hash"] = ledger_record_hash(record)
        chained.append(record)
        previous_hash = record["record_hash"]
    return chained


def validate_ledger_chain(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"valid": True, "entries": 0, "legacy_entries": 0, "broken_at": None, "head_hash": ""}
    legacy = sum("record_hash" not in record for record in records)
    if legacy:
        return {
            "valid": False,
            "entries": len(records),
            "legacy_entries": legacy,
            "broken_at": 1 if legacy != len(records) else None,
            "head_hash": "",
        }
    previous_hash = ""
    for index, record in enumerate(records, start=1):
        if (
            record.get("chain_version") != LEDGER_CHAIN_VERSION
            or record.get("previous_hash") != previous_hash
            or record.get("record_hash") != ledger_record_hash(record)
        ):
            return {
                "valid": False,
                "entries": len(records),
                "legacy_entries": 0,
                "broken_at": index,
                "head_hash": previous_hash,
            }
        previous_hash = str(record["record_hash"])
    return {
        "valid": True,
        "entries": len(records),
        "legacy_entries": 0,
        "broken_at": None,
        "head_hash": previous_hash,
    }


def inspect_chained_jsonl(path: Path, anchor_path: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    invalid_lines = 0
    read_error = False
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
            read_error = True
        for raw in lines:
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise ValueError("row must be an object")
                records.append(row)
            except (json.JSONDecodeError, ValueError):
                invalid_lines += 1
    chain = validate_ledger_chain(records) if not invalid_lines else {
        "valid": False, "entries": len(records), "legacy_entries": 0, "broken_at": None, "head_hash": "",
    }
    anchor: dict[str, Any] = {}
    anchor_valid = not path.exists() and not anchor_path.exists()
    anchor_shape_valid = False
    if anchor_path.is_file():
        try:
            raw_anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
            anchor_shape_valid = (
                isinstance(raw_anchor, dict)
                and type(raw_anchor.get("schema_version")) is int and raw_anchor["schema_version"] == 1
                and type(raw_anchor.get("entries")) is int and raw_anchor["entries"] >= 0
                and isinstance(raw_anchor.get("head_hash"), str)
                and (raw_anchor["head_hash"] == "" if raw_anchor["entries"] == 0
                     else bool(re.fullmatch(r"[0-9a-f]{64}", raw_anchor["head_hash"])))
            )
            anchor = raw_anchor if isinstance(raw_anchor, dict) else {}
            anchor_valid = (
                path.is_file() and anchor_shape_valid
                and anchor.get("entries") == chain.get("entries")
                and anchor.get("head_hash") == chain.get("head_hash")
            )
        except (OSError, ValueError, UnicodeError):
            anchor_valid = False
    issue_code = ""
    if read_error or (path.exists() and not path.is_file()) or (anchor_path.exists() and not anchor_path.is_file()):
        issue_code = "LEDGER_READ_ERROR"
    elif invalid_lines:
        issue_code = "LEDGER_RECORD_INVALID"
    elif chain.get("legacy_entries"):
        issue_code = "LEDGER_LEGACY_REVIEW_REQUIRED" if chain["legacy_entries"] == len(records) else "LEDGER_MIXED_CHAIN"
    elif not chain.get("valid"):
        issue_code = "LEDGER_HASH_CHAIN_BROKEN"
    elif anchor_path.exists() and not path.exists():
        issue_code = "LEDGER_FILE_MISSING"
    elif path.exists() and not anchor_path.exists():
        issue_code = "LEDGER_ANCHOR_MISSING"
    elif anchor_path.exists() and not anchor_shape_valid:
        issue_code = "LEDGER_ANCHOR_INVALID"
    elif not anchor_valid:
        issue_code = "LEDGER_ANCHOR_MISMATCH"
    return {
        "valid": not issue_code,
        "issue_code": issue_code,
        "records": records,
        "invalid_lines": invalid_lines,
        "chain": chain,
        "anchor": {**anchor, "exists": anchor_path.is_file(), "valid": anchor_valid},
    }


def require_valid_chained_jsonl(path: Path, anchor_path: Path) -> dict[str, Any]:
    inspection = inspect_chained_jsonl(path, anchor_path)
    if not inspection["valid"]:
        raise ValueError(f"{inspection['issue_code']}: {path.name} hash chain or anchor is invalid; preserve files and reconcile a verified backup")
    return inspection


def append_chained_jsonl(path: Path, anchor_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Caller holds the pack lock. Never rebuild history to repair an invalid pair."""
    inspection = require_valid_chained_jsonl(path, anchor_path)
    if not rows:
        return {"appended": 0, "chain": inspection["chain"]}
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("LEDGER_RECORD_INVALID: new rows must be objects")
    chained = chain_ledger_records(rows, previous_hash=inspection["chain"]["head_hash"])
    # newline='' preserves existing CRLF/spacing as evidence, not just its parsed value.
    existing_text = ""
    if path.is_file():
        with path.open("r", encoding="utf-8", newline="") as handle:
            existing_text = handle.read()
    separator = "\n" if existing_text and not existing_text.endswith(("\n", "\r")) else ""
    added = "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in chained)
    atomic_write_text(path, existing_text + separator + added)
    chain = validate_ledger_chain(inspection["records"] + chained)
    atomic_write_json(anchor_path, {
        "schema_version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "entries": chain["entries"],
        "head_hash": chain["head_hash"],
    })
    return {"appended": len(rows), "chain": chain}
