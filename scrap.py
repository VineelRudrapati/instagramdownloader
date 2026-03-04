from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

# Public constants/functions consumed by ui.py
IG_APP_ID = "936619743392459"
IG_ASBD_ID = "129477"

_DEFAULT_TIMEOUT = 20
_MAX_PAGE_SIZE = 50
_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_POST_ROOT_QUERY_DOC_ID = "25874459848900880"
_VALID_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".heic",
    ".heif",
    ".mp4",
    ".mov",
    ".m4v",
}
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,30}$")
_AUTH_ENV_COOKIE_MAP = {
    "IG_SESSIONID": "sessionid",
    "IG_CSRFTOKEN": "csrftoken",
    "IG_DS_USER_ID": "ds_user_id",
    "IG_RUR": "rur",
    "IG_MID": "mid",
    "IG_IG_DID": "ig_did",
    "IG_SHBID": "shbid",
    "IG_SHBTS": "shbts",
}


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
            "Origin": "https://www.instagram.com",
            "X-IG-App-ID": IG_APP_ID,
            "X-ASBD-ID": IG_ASBD_ID,
            "X-Requested-With": "XMLHttpRequest",
            "Connection": "keep-alive",
        }
    )

    for env_key, cookie_name in _AUTH_ENV_COOKIE_MAP.items():
        value = str(os.getenv(env_key, "")).strip()
        if not value:
            continue
        session.cookies.set(cookie_name, value, domain=".instagram.com", path="/")

    csrf_token = str(session.cookies.get("csrftoken") or "").strip()
    if csrf_token:
        session.headers["X-CSRFToken"] = csrf_token

    return session


def _extract_lsd_token(page_html: str) -> str:
    if not page_html:
        return ""
    patterns = (
        r'"LSD",\[\],\{"token":"([^"]+)"\}',
        r'"lsd":"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html)
        if match:
            token = str(match.group(1) or "").strip()
            if token:
                return token
    return ""


def _prime_instagram_session(session: requests.Session, username: str = "") -> str:
    normalized = _normalize_username(username)
    targets = ["https://www.instagram.com/"]
    if normalized:
        targets.append(f"https://www.instagram.com/{normalized}/")

    lsd_token = ""
    for target in targets:
        try:
            response = session.get(
                target,
                timeout=14,
                allow_redirects=True,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.instagram.com/",
                },
            )
            if response.status_code >= 400:
                continue
            token = _extract_lsd_token(response.text)
            if token:
                lsd_token = token
        except Exception:
            continue

    csrf_token = str(session.cookies.get("csrftoken") or "").strip()
    if csrf_token:
        session.headers["X-CSRFToken"] = csrf_token
    if lsd_token:
        session.headers["X-FB-LSD"] = lsd_token
    return lsd_token


def _guess_extension(url: str, is_video: bool) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in _VALID_EXTENSIONS:
        if suffix == ".jpeg":
            return ".jpg"
        return suffix
    return ".mp4" if is_video else ".jpg"


def _normalize_username(username: str) -> str:
    normalized = re.sub(r"\s+", "", (username or "")).lstrip("@").strip()
    return normalized


def _extract_shortcode(post_url: str) -> str:
    raw = str(post_url or "").strip()
    if not raw:
        return ""

    if _SHORTCODE_RE.fullmatch(raw):
        return raw

    if not re.match(r"^https?://", raw, flags=re.I):
        raw = f"https://{raw.lstrip('/')}"

    parsed = urlparse(raw)
    host = str(parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host.endswith("instagram.com"):
        return ""

    parts = [segment for segment in parsed.path.split("/") if segment]
    if len(parts) >= 2 and parts[0] in {"p", "reel", "tv"}:
        shortcode = parts[1]
    elif len(parts) >= 3 and parts[0] == "share" and parts[1] in {"p", "reel", "tv"}:
        shortcode = parts[2]
    else:
        return ""

    shortcode = shortcode.strip()
    if _SHORTCODE_RE.fullmatch(shortcode):
        return shortcode
    return ""


def _parse_compact_count(raw: str) -> int | None:
    token = (raw or "").strip().replace(",", "")
    if not token:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMB])?", token, re.I)
    if not match:
        digits = re.sub(r"[^\d]", "", token)
        return int(digits) if digits else None
    value = float(match.group(1))
    unit = (match.group(2) or "").upper()
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[unit]
    return int(value * multiplier)


def _extract_posts_count_from_profile_html(html_text: str) -> int | None:
    if not html_text:
        return None

    # Example:  "335M Followers, 79 Following, 7,917 Posts - ..."
    meta = re.search(
        r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"',
        html_text,
        flags=re.I,
    )
    if meta:
        description = html.unescape(meta.group(1))
        posts_match = re.search(r"([\d.,KMBkmb]+)\s+Posts?", description, flags=re.I)
        if posts_match:
            return _parse_compact_count(posts_match.group(1))

    posts_match = re.search(r'"media_count"\s*:\s*(\d+)', html_text)
    if posts_match:
        return int(posts_match.group(1))

    return None


def _fetch_profile_posts_count(username: str) -> int | None:
    url = f"https://www.instagram.com/{username}/"
    try:
        response = requests.get(
            url,
            timeout=_DEFAULT_TIMEOUT,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if response.status_code >= 400:
            return None
        return _extract_posts_count_from_profile_html(response.text)
    except Exception:
        return None


def _request_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    retries: int = 2,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=timeout, allow_redirects=True)
            if response.status_code in _TRANSIENT_STATUS_CODES:
                if attempt < retries - 1:
                    time.sleep(1.3 * (attempt + 1))
                    continue
                if response.status_code == 429:
                    raise RuntimeError("Instagram rate-limited the request. Try again shortly.")
                response.raise_for_status()
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Unexpected API response type from Instagram.")
            return payload
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            break
    raise RuntimeError(f"Failed to fetch Instagram API response: {last_error}") from last_error


def _choose_best_image(image_versions2: dict[str, Any] | None) -> str:
    if not isinstance(image_versions2, dict):
        return ""
    candidates = image_versions2.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    ranked: list[tuple[int, str]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        url = str(candidate.get("url") or "").strip()
        if not url:
            continue
        width = int(candidate.get("width") or 0)
        height = int(candidate.get("height") or 0)
        ranked.append((width * height, url))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _choose_best_video(video_versions: list[dict[str, Any]] | None) -> str:
    if not isinstance(video_versions, list) or not video_versions:
        return ""
    ranked: list[tuple[int, str]] = []
    for version in video_versions:
        if not isinstance(version, dict):
            continue
        url = str(version.get("url") or "").strip()
        if not url:
            continue
        width = int(version.get("width") or 0)
        height = int(version.get("height") or 0)
        bitrate = int(version.get("bandwidth") or 0)
        ranked.append((width * height + bitrate, url))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _media_from_item(item: dict[str, Any], post_type: str) -> dict[str, Any]:
    is_video = bool(item.get("media_type") == 2 or item.get("video_versions"))
    media_url = _choose_best_video(item.get("video_versions")) if is_video else ""
    if not media_url:
        media_url = _choose_best_image(item.get("image_versions2"))

    thumbnail = _choose_best_image(item.get("image_versions2"))
    if not thumbnail:
        thumbnail = str(item.get("display_uri") or "").strip() or media_url

    return {
        "post_type": post_type,
        "url": media_url,
        "thumbnail_url": thumbnail or media_url,
        "is_video": is_video,
    }


def _post_type_for_item(item: dict[str, Any]) -> str:
    media_type = int(item.get("media_type") or 0)
    product_type = str(item.get("product_type") or "").strip().lower()
    if media_type == 8:
        return "carousel"
    if media_type == 2 and product_type == "clips":
        return "reel"
    if media_type == 2:
        return "video"
    return "photo"


def _to_post_dict(item: dict[str, Any]) -> dict[str, Any]:
    post_type = _post_type_for_item(item)
    post_id = str(item.get("code") or item.get("id") or item.get("pk") or "").strip()
    if not post_id:
        post_id = "unknown_post"

    media_items: list[dict[str, Any]] = []
    if int(item.get("media_type") or 0) == 8:
        for child in item.get("carousel_media") or []:
            if not isinstance(child, dict):
                continue
            media = _media_from_item(child, post_type)
            if media["url"]:
                media_items.append(
                    {
                        "url": media["url"],
                        "thumbnail_url": media["thumbnail_url"],
                        "is_video": media["is_video"],
                    }
                )
    else:
        media = _media_from_item(item, post_type)
        if media["url"]:
            media_items.append(
                {
                    "url": media["url"],
                    "thumbnail_url": media["thumbnail_url"],
                    "is_video": media["is_video"],
                }
            )

    return {"post_id": post_id, "post_type": post_type, "media_items": media_items}


def get_post_from_url_detailed(post_url: str) -> dict[str, Any]:
    shortcode = _extract_shortcode(post_url)
    if not shortcode:
        raise ValueError("Enter a valid Instagram post/reel URL (or shortcode).")

    session = _new_session()
    _prime_instagram_session(session)

    payload = _request_json(
        session,
        "https://www.instagram.com/graphql/query/",
        params={
            "doc_id": _POST_ROOT_QUERY_DOC_ID,
            "variables": json.dumps({"shortcode": shortcode}, separators=(",", ":")),
        },
    )

    data = payload.get("data") if isinstance(payload, dict) else {}
    media_root = data.get("xdt_api__v1__media__shortcode__web_info") if isinstance(data, dict) else {}
    items = media_root.get("items") if isinstance(media_root, dict) else []
    if not isinstance(items, list) or not items:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise RuntimeError(
                "Instagram did not return media details for this URL. "
                "The post may be private, unavailable, or temporarily blocked."
            )
        raise RuntimeError("Could not resolve media details for this Instagram URL.")

    media_item = items[0] if isinstance(items[0], dict) else {}
    post = _to_post_dict(media_item)
    if not post["media_items"]:
        raise RuntimeError("No downloadable media found for this Instagram URL.")

    owner = media_item.get("user") if isinstance(media_item, dict) else {}
    owner_username = ""
    if isinstance(owner, dict):
        owner_username = _normalize_username(str(owner.get("username") or ""))

    return {
        "shortcode": shortcode,
        "owner_username": owner_username,
        "post": post,
    }


def get_recent_posts_detailed(username: str, post_limit: int = 100) -> tuple[int | None, list[dict[str, Any]]]:
    normalized = _normalize_username(username)
    if not normalized:
        raise ValueError("Username is required.")
    if not _USERNAME_RE.fullmatch(normalized):
        raise ValueError("Invalid Instagram username format.")

    post_limit = max(1, int(post_limit))
    session = _new_session()
    _prime_instagram_session(session, normalized)
    profile_count: int | None = None

    posts: list[dict[str, Any]] = []
    seen_post_ids: set[str] = set()
    next_max_id: str | None = None

    while len(posts) < post_limit:
        per_page = min(_MAX_PAGE_SIZE, post_limit - len(posts))
        params: dict[str, Any] = {"count": per_page}
        if next_max_id:
            params["max_id"] = next_max_id

        payload = _request_json(
            session,
            f"https://www.instagram.com/api/v1/feed/user/{normalized}/username/",
            params=params,
        )

        items = payload.get("items")
        if not isinstance(items, list):
            items = []
        if not posts and not items and not isinstance(payload.get("user"), dict):
            raise RuntimeError(f"Instagram user @{normalized} was not found or is not publicly accessible.")

        for item in items:
            if not isinstance(item, dict):
                continue
            post = _to_post_dict(item)
            if not post["media_items"]:
                continue
            post_id = str(post["post_id"])
            if post_id in seen_post_ids:
                continue
            seen_post_ids.add(post_id)
            posts.append(post)
            if len(posts) >= post_limit:
                break

        more_available = bool(payload.get("more_available"))
        new_cursor = str(payload.get("next_max_id") or "").strip() or None
        if not more_available or not new_cursor or new_cursor == next_max_id:
            break
        next_max_id = new_cursor

    return profile_count, posts
