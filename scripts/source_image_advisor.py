"""Check declared source-image provenance and nearby captions, not rights or facts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from markdown_it import MarkdownIt

KINDS = {"original", "adapted", "ai_redrawn"}
RIGHTS_TYPES = {"public_license", "author_permission", "other"}
SENSITIVE_KEYS = {"access_token", "token", "cookie", "secret", "api_key", "apikey", "signature", "sig", "authorization"}


def public_reference(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if not host or "." not in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            if not ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return (url.scheme == "https" and not url.username and not url.password and url.port in (None, 443)
                and not any(key.lower() in SENSITIVE_KEYS for key, _ in parse_qsl(url.query, keep_blank_values=True))
                and not re.search(r"(?i)(?:token|cookie|secret|api[_-]?key|signature)\s*[:=]", url.fragment))
    except ValueError:
        return False


def caption_after_image(article: str, image: str) -> list[str]:
    """Only return visible text directly following an image in the same/next paragraph."""
    tokens = MarkdownIt("commonmark").parse(article)
    candidates = []
    for index, token in enumerate(tokens):
        if token.type != "inline":
            continue
        children = token.children or []
        for offset, child in enumerate(children):
            if child.type != "image" or (child.attrGet("src") or "").removeprefix("./") != image:
                continue
            after = "".join(c.content for c in children[offset + 1:] if c.type in {"text", "code_inline"}).strip()
            if after:
                candidates.append(after)
            if (index + 3 < len(tokens) and tokens[index + 1].type == "paragraph_close"
                    and tokens[index + 2].type == "paragraph_open" and tokens[index + 3].type == "inline"):
                next_children = tokens[index + 3].children or []
                if not any(c.type == "image" for c in next_children):
                    candidates.append("".join(c.content for c in next_children if c.type in {"text", "code_inline"}).strip())
    return candidates


def review_source_image(item: dict[str, Any], article: str) -> dict[str, Any]:
    """No network, no legal determination, no visual or factual certification."""
    value = item.get("source_image")
    result: dict[str, Any] = {"status": "not_declared", "issues": [],
                              "rights_verified": False, "facts_verified": False,
                              "visual_review_performed": False}
    if "source_image" not in item:
        return result
    result["status"] = "needs_review"
    if not isinstance(value, dict):
        result["issues"].append("SOURCE_IMAGE_DECLARATION_INVALID")
        return result
    if value.get("kind") not in KINDS:
        result["issues"].append("SOURCE_IMAGE_KIND_INVALID")
    if not all(isinstance(value.get(k), str) and value[k].strip() for k in ("source_title", "modifications", "caption")):
        result["issues"].append("SOURCE_IMAGE_DETAILS_MISSING")
    if not all(public_reference(value.get(k)) for k in ("source_page_url", "source_image_url")):
        result["issues"].append("SOURCE_IMAGE_PUBLIC_URL_REQUIRED")
    try:
        observed = datetime.fromisoformat(value.get("observed_at", ""))
        if observed.tzinfo is None or observed > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("invalid observed time")
    except (TypeError, ValueError):
        result["issues"].append("SOURCE_IMAGE_OBSERVED_TIME_INVALID")
    basis = value.get("rights_basis")
    if (not isinstance(basis, dict) or basis.get("type") not in RIGHTS_TYPES
            or not isinstance(basis.get("reference"), str) or not basis["reference"].strip()):
        result["issues"].append("SOURCE_IMAGE_RIGHTS_BASIS_MISSING")
    elif re.search(r"(?i)(?:token|cookie|secret|api[_-]?key|signature)\s*[:=]", basis["reference"]):
        result["issues"].append("SOURCE_IMAGE_SENSITIVE_REFERENCE")
    elif basis["type"] == "public_license" and not public_reference(basis.get("license_url")):
        result["issues"].append("SOURCE_IMAGE_LICENSE_URL_REQUIRED")
    caption = value.get("caption")
    image = item.get("image")
    if not isinstance(caption, str) or not caption.strip() or not isinstance(image, str) or not any(
            caption.strip() in text for text in caption_after_image(article, image)):
        result["issues"].append("SOURCE_IMAGE_CAPTION_NOT_ADJACENT")
    if isinstance(caption, str) and caption.strip():
        if not any(marker in caption.lower() for marker in ("来源", "图源", "出自", "source", "credit")):
            result["issues"].append("SOURCE_IMAGE_CAPTION_SOURCE_MISSING")
        if value.get("kind") == "adapted" and not any(
                marker in caption.lower() for marker in ("裁剪", "调整", "加工", "编辑", "改编", "标注", "改制", "adapted", "edited", "crop")):
            result["issues"].append("SOURCE_IMAGE_MODIFICATION_LABEL_MISSING")
    if value.get("kind") == "ai_redrawn" and isinstance(caption, str) and "AI" not in caption.upper() and "人工智能" not in caption:
        result["issues"].append("SOURCE_IMAGE_AI_LABEL_MISSING")
    if not result["issues"]:
        result["status"] = "declarations_complete_manual_review_required"
    return result
