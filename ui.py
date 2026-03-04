import math
import re
import html
import io
import zipfile
import json
import base64
import time
from urllib.parse import quote_plus, parse_qs, urlsplit
from concurrent.futures import ThreadPoolExecutor

import streamlit as st
import requests

from scrap import (
    _guess_extension,
    _new_session,
    _prime_instagram_session,
    get_post_from_url_detailed,
    get_recent_posts_detailed,
)

POST_FETCH_LIMIT = 60
POST_FETCH_OPTIONS = [60, 120, 240, 500]
ITEMS_PER_PAGE = 50
PROFILE_PAGE_CONTENT_QUERY_ID = "33954869174158742"
PROFILE_POSTS_QUERY_ID = "26149520921371801"
_GENERIC_PROFILE_PIC_MARKERS = (
    "44884218_345707102882519_2446069589734326272_n",
    "/default_profile_",
    "/default_avatar/",
)


def _normalize_username(username: str) -> str:
    return re.sub(r"\s+", "", (username or "")).lstrip("@")


def _safe_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value or "")
    return cleaned or "item"


def _parse_count(value: str) -> int | None:
    token = (value or "").strip().replace(",", "")
    if not token:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([KMBkmb])?", token)
    if not m:
        digits = re.sub(r"[^\d]", "", token)
        return int(digits) if digits else None

    amount = float(m.group(1))
    suffix = (m.group(2) or "").upper()
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return int(amount * multiplier)


def _format_count(value: int | None) -> str:
    if value is None:
        return "Unknown"
    return f"{value:,}"


def _extract_profile_user_id(page_html: str) -> str:
    if not page_html:
        return ""
    m = re.search(r"profilePage_(\d+)", page_html)
    return m.group(1) if m else ""


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


def _clean_profile_pic_url(raw_url: str) -> str:
    return html.unescape(str(raw_url or "").strip()).replace("\\u0026", "&").replace("\\/", "/")


def _profile_pic_variants(url: str) -> list[str]:
    clean = _clean_profile_pic_url(url)
    if not clean:
        return []
    return [clean]


def _append_profile_pic_candidate(candidates: list[str], url: str):
    for candidate in _profile_pic_variants(url):
        candidates.append(candidate)


def _profile_pic_candidates_from_user(user: dict) -> list[str]:
    if not isinstance(user, dict):
        return []

    candidates: list[str] = []
    hd_info = user.get("hd_profile_pic_url_info")
    if isinstance(hd_info, dict):
        _append_profile_pic_candidate(candidates, str(hd_info.get("url") or ""))

    hd_versions = user.get("hd_profile_pic_versions")
    if isinstance(hd_versions, list):
        ranked: list[tuple[int, str]] = []
        for version in hd_versions:
            if not isinstance(version, dict):
                continue
            url = str(version.get("url") or "").strip()
            if not url:
                continue
            width = int(version.get("width") or 0)
            height = int(version.get("height") or 0)
            ranked.append((width * height, url))
        ranked.sort(key=lambda item: item[0], reverse=True)
        for _, url in ranked:
            _append_profile_pic_candidate(candidates, url)

    profile_hd_url = str(user.get("profile_pic_url_hd") or "").strip()
    if profile_hd_url:
        _append_profile_pic_candidate(candidates, profile_hd_url)

    profile_url_info = user.get("profile_pic_url_info")
    if isinstance(profile_url_info, dict):
        _append_profile_pic_candidate(candidates, str(profile_url_info.get("url") or ""))

    profile_url = str(user.get("profile_pic_url") or "").strip()
    if profile_url:
        _append_profile_pic_candidate(candidates, profile_url)

    return candidates


def _profile_pic_candidates_from_page_html(page_html: str) -> list[str]:
    if not page_html:
        return []

    candidates: list[str] = []
    m = re.search(
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
        page_html,
        re.I,
    )
    if m:
        _append_profile_pic_candidate(candidates, m.group(1))

    normalized = html.unescape(page_html).replace("\\/", "/").replace("\\u0026", "&")
    for pattern in (
        r'"profile_pic_url_hd"\s*:\s*"([^"]+)"',
        r'"profile_pic_url"\s*:\s*"([^"]+)"',
        r'"hd_profile_pic_url_info"\s*:\s*\{[^{}]*"url"\s*:\s*"([^"]+)"',
    ):
        for match in re.finditer(pattern, normalized):
            _append_profile_pic_candidate(candidates, match.group(1))

    return candidates


def _iter_numeric_values(value):
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield int(value)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_numeric_values(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_numeric_values(item)


def _profile_pic_quality_hint(url: str) -> int:
    if not url:
        return 0

    split = urlsplit(url)
    query_map = parse_qs(split.query)
    raw_efg = ""
    if "efg" in query_map and query_map["efg"]:
        raw_efg = str(query_map["efg"][0] or "").strip()

    if raw_efg:
        payload = raw_efg
        if len(payload) % 4:
            payload += "=" * (4 - len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8", errors="ignore")
            parsed = json.loads(decoded)
            values = [v for v in _iter_numeric_values(parsed) if 0 < v <= 8192]
            if values:
                return max(values)
            decoded_hints = [
                int(m.group(1))
                for m in re.finditer(r"profile_pic(?:\.[A-Za-z0-9_-]+)*\.(\d{2,4})", decoded)
            ]
            if decoded_hints:
                return max(decoded_hints)
        except Exception:
            pass

    hints: list[int] = []
    for m in re.finditer(r"(\d{2,4})x(\d{2,4})", url):
        w = int(m.group(1))
        h = int(m.group(2))
        hints.append(max(w, h))
    for m in re.finditer(r"profile_pic(?:\.[A-Za-z0-9_-]+)*\.(\d{2,4})", url):
        hints.append(int(m.group(1)))
    return max(hints) if hints else 0


def _profile_pic_score(url: str) -> int:
    if not url:
        return -10_000

    score = _profile_pic_quality_hint(url) * 10
    lowered = url.lower()
    if any(marker in lowered for marker in _GENERIC_PROFILE_PIC_MARKERS):
        score -= 5_000
    if "static.cdninstagram.com/rsrc.php" in lowered:
        score -= 7_000
    if "profile_pic" in url:
        score += 500
    if re.search(r"s150x150", url):
        score -= 700
    elif re.search(r"s320x320", url):
        score -= 350
    elif re.search(r"s640x640", url):
        score -= 120
    if "stp=" not in url:
        score += 100
    else:
        score -= 60
    return score


def _pick_best_profile_pic_url(candidates: list[str]) -> str:
    seen: set[str] = set()
    unique: list[str] = []
    for url in candidates:
        clean = str(url or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        unique.append(clean)
    if not unique:
        return ""
    unique.sort(key=_profile_pic_score, reverse=True)
    return unique[0]


def _merge_profile_from_user(profile: dict, user: dict, pic_candidates: list[str]):
    if not isinstance(user, dict) or not user:
        return

    pic_candidates.extend(_profile_pic_candidates_from_user(user))

    if not profile.get("user_id"):
        profile["user_id"] = str(user.get("pk") or user.get("id") or "").strip()

    if profile.get("is_private") is None and user.get("is_private") is not None:
        try:
            profile["is_private"] = bool(user.get("is_private"))
        except Exception:
            pass

    if profile.get("followers") is None:
        followers = user.get("follower_count")
        if followers is None:
            edge_follow = user.get("edge_followed_by")
            if isinstance(edge_follow, dict):
                followers = edge_follow.get("count")
        if followers is not None:
            try:
                profile["followers"] = int(followers)
            except Exception:
                pass

    if profile.get("posts") is None:
        posts = user.get("media_count")
        if posts is None:
            edge_posts = user.get("edge_owner_to_timeline_media")
            if isinstance(edge_posts, dict):
                posts = edge_posts.get("count")
        if posts is not None:
            try:
                profile["posts"] = int(posts)
            except Exception:
                pass


def _graphql_profile_user(session: requests.Session, user_id: str, referer_username: str) -> dict:
    clean_user_id = str(user_id or "").strip()
    if not clean_user_id:
        return {}

    normalized = _normalize_username(referer_username)
    referer = f"https://www.instagram.com/{normalized}/" if normalized else "https://www.instagram.com/"
    relay_provider_keys = (
        "__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider",
        "__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider",
        "__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider",
        "__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider",
    )

    for provider_value in (False, True):
        variables = {
            "id": clean_user_id,
            "render_surface": "PROFILE",
            "enable_integrity_filters": True,
        }
        for key in relay_provider_keys:
            variables[key] = provider_value
        try:
            r = session.get(
                "https://www.instagram.com/graphql/query/",
                params={
                    "query_id": PROFILE_PAGE_CONTENT_QUERY_ID,
                    "variables": json.dumps(variables, separators=(",", ":")),
                },
                headers={"Referer": referer},
                timeout=18,
                allow_redirects=True,
            )
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data") if isinstance(payload, dict) else {}
            user = data.get("user") if isinstance(data, dict) else {}
            if isinstance(user, dict) and user:
                return user
        except Exception:
            continue
    return {}


def _graphql_first_post_user(session: requests.Session, username: str) -> dict:
    normalized = _normalize_username(username)
    if not normalized:
        return {}

    variables = {"username": normalized, "first": 12, "data": {}}
    try:
        r = session.get(
            "https://www.instagram.com/graphql/query/",
            params={
                "query_id": PROFILE_POSTS_QUERY_ID,
                "variables": json.dumps(variables, separators=(",", ":")),
            },
            headers={"Referer": f"https://www.instagram.com/{normalized}/"},
            timeout=18,
            allow_redirects=True,
        )
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data") if isinstance(payload, dict) else {}
        connection = (
            data.get("xdt_api__v1__feed__user_timeline_graphql_connection")
            if isinstance(data, dict)
            else {}
        )
        if not isinstance(connection, dict):
            return {}
        edges = connection.get("edges")
        if not isinstance(edges, list):
            return {}
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            node = edge.get("node")
            if not isinstance(node, dict):
                continue
            user = node.get("user")
            if isinstance(user, dict) and user:
                return user
    except Exception:
        pass
    return {}


def _web_profile_info_user(session: requests.Session, username: str, lsd_token: str = "") -> dict:
    normalized = _normalize_username(username)
    if not normalized:
        return {}

    endpoints = (
        "https://www.instagram.com/api/v1/users/web_profile_info/",
        "https://i.instagram.com/api/v1/users/web_profile_info/",
    )
    referer = f"https://www.instagram.com/{normalized}/"

    for endpoint in endpoints:
        for attempt in range(2):
            try:
                headers = {
                    "Referer": referer,
                    "Accept": "*/*",
                    "X-ASBD-ID": "129477",
                }
                if lsd_token:
                    headers["X-FB-LSD"] = lsd_token

                r = session.get(
                    endpoint,
                    params={"username": normalized},
                    headers=headers,
                    timeout=14,
                    allow_redirects=True,
                )
                if r.status_code == 429:
                    time.sleep(0.9 * (attempt + 1))
                    continue
                r.raise_for_status()

                payload = r.json()
                data = payload.get("data") if isinstance(payload, dict) else {}
                user = data.get("user") if isinstance(data, dict) else {}
                if isinstance(user, dict) and user:
                    return user
            except Exception:
                continue

    return {}


def _flatten_posts(posts: list[dict]) -> list[dict]:
    out: list[dict] = []
    for post_idx, post in enumerate(posts, start=1):
        post_id = str(post.get("post_id") or f"post_{post_idx}")
        post_type = str(post.get("post_type") or "").strip().lower()
        media_items = post.get("media_items") or []
        for media_idx, media in enumerate(media_items, start=1):
            url = str(media.get("url") or "").strip()
            if not url:
                continue
            is_video = bool(media.get("is_video"))
            thumbnail_url = str(media.get("thumbnail_url") or "").strip() or url
            if is_video and post_type == "reel":
                group = "reels"
            elif is_video:
                group = "videos"
            else:
                group = "photos"
            out.append(
                {
                    "group": group,
                    "post_id": post_id,
                    "media_idx": media_idx,
                    "url": url,
                    "is_video": is_video,
                    "thumbnail_url": thumbnail_url,
                }
            )
    return out


def _build_filename(username: str, item: dict) -> str:
    group = _safe_token(str(item.get("group") or "media"))
    post_id = _safe_token(str(item.get("post_id") or "item"))
    media_idx = int(item.get("media_idx") or 1)
    ext = _guess_extension(str(item["url"]), bool(item["is_video"]))
    return f"{username}_{group}_{post_id}_{media_idx}{ext}"


def _fetch_media_bytes(session: requests.Session, media_url: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    with session.get(media_url, stream=True, timeout=45, allow_redirects=True) as r:
        r.raise_for_status()
        content_type = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in content_type:
            raise RuntimeError("Received HTML page instead of media.")
        for chunk in r.iter_content(1024 * 64):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
    if total <= 0:
        raise RuntimeError("Downloaded file is empty.")
    return b"".join(chunks)


def _build_zip_bytes(username: str, items: list[dict]) -> tuple[bytes, list[str]]:
    if not items:
        return b"", []

    session = _new_session()
    errors: list[str] = []
    progress = st.progress(0.0)
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        total = len(items)
        for idx, item in enumerate(items, start=1):
            filename = _build_filename(username, item)
            try:
                payload = _fetch_media_bytes(session, str(item["url"]))
                zf.writestr(filename, payload)
            except Exception as e:
                errors.append(f"{filename}: {e}")
            progress.progress(idx / total)

    progress.empty()
    return buffer.getvalue(), errors


def _reset_loaded_state():
    st.session_state["loaded_username"] = ""
    st.session_state["profile_count"] = None
    st.session_state["profile_info"] = {}
    st.session_state["post_items"] = []
    st.session_state["load_error"] = ""
    st.session_state["load_notice"] = ""
    st.session_state["page_all"] = 1
    st.session_state["page_reels"] = 1
    st.session_state["page_videos"] = 1
    st.session_state["page_photos"] = 1


def _ensure_session_state():
    defaults = {
        "loaded_username": "",
        "profile_count": None,
        "profile_info": {},
        "post_items": [],
        "load_error": "",
        "load_notice": "",
        "url_load_error": "",
        "url_media_result": {},
        "post_fetch_limit": POST_FETCH_LIMIT,
        "page_all": 1,
        "page_reels": 1,
        "page_videos": 1,
        "page_photos": 1,
        "favorites": [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _has_instagram_auth_session() -> bool:
    session = _new_session()
    return bool(str(session.cookies.get("sessionid") or "").strip())


@st.cache_data(show_spinner=False, ttl=900)
def _cached_recent_posts(username: str, post_limit: int) -> tuple[int | None, list[dict]]:
    return get_recent_posts_detailed(username, post_limit=post_limit)


@st.cache_data(show_spinner=False, ttl=900)
def _cached_post_from_url(post_url: str) -> dict:
    return get_post_from_url_detailed(post_url)


def _load_media_from_url(post_url: str):
    cleaned = str(post_url or "").strip()
    st.session_state["url_media_result"] = {}
    st.session_state["url_load_error"] = ""

    if not cleaned:
        st.warning("Enter a valid Instagram post/reel URL.")
        return

    with st.spinner("Loading media from URL..."):
        try:
            st.session_state["url_media_result"] = _cached_post_from_url(cleaned)
        except Exception as e:
            st.session_state["url_load_error"] = str(e)


def _load_profile_media(username: str):
    normalized = _normalize_username(username)
    if not normalized:
        _reset_loaded_state()
        st.warning("Enter a valid username.")
        return

    post_limit = int(st.session_state.get("post_fetch_limit", POST_FETCH_LIMIT))
    st.session_state["page_all"] = 1
    st.session_state["page_reels"] = 1
    st.session_state["page_videos"] = 1
    st.session_state["page_photos"] = 1
    with st.spinner(f"Loading media for @{normalized} (up to {post_limit} posts)..."):
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                overview_future = executor.submit(_profile_overview, normalized)
                posts_future = executor.submit(_cached_recent_posts, normalized, post_limit)
                profile_info = overview_future.result()
                posts_error: Exception | None = None
                try:
                    profile_count, posts = posts_future.result()
                except Exception as e:
                    posts_error = e
                    profile_count, posts = None, []

            if profile_count is None and profile_info.get("posts") is not None:
                profile_count = profile_info.get("posts")

            if posts_error is not None:
                has_profile_signal = bool(
                    profile_info.get("profile_pic_url")
                    or profile_info.get("user_id")
                    or profile_info.get("followers") is not None
                    or profile_info.get("posts") is not None
                )
                if not has_profile_signal:
                    raise posts_error
                load_notice = (
                    "Profile media is not publicly accessible. Showing available profile details and best-available DP."
                )
                if bool(profile_info.get("is_private")) and not _has_instagram_auth_session():
                    load_notice += (
                        " To improve private-account DP accuracy on hosted Streamlit, set secrets/env vars "
                        "`IG_SESSIONID` and `IG_CSRFTOKEN` from a logged-in Instagram session."
                    )
                st.session_state["load_notice"] = load_notice
            else:
                st.session_state["load_notice"] = ""

            st.session_state["loaded_username"] = normalized
            st.session_state["profile_count"] = profile_count
            st.session_state["profile_info"] = profile_info
            st.session_state["post_items"] = _flatten_posts(posts)
            st.session_state["load_error"] = ""
        except Exception as e:
            _reset_loaded_state()
            st.session_state["load_error"] = str(e)


def _add_favorite(username: str) -> bool:
    normalized = _normalize_username(username)
    if not normalized:
        return False
    favorites = st.session_state.get("favorites", [])
    if normalized in favorites:
        return False
    st.session_state["favorites"] = [*favorites, normalized]
    return True


def _favorite_link(username: str) -> str:
    return f"?fav={quote_plus(username)}"


def _selected_favorite_from_query() -> str:
    raw = st.query_params.get("fav", "")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return _normalize_username(str(raw))


def _clear_favorite_query_param():
    try:
        if "fav" in st.query_params:
            del st.query_params["fav"]
    except Exception:
        pass


@st.cache_data(show_spinner=False, ttl=1800)
def _profile_overview(username: str) -> dict:
    normalized = _normalize_username(username)
    if not normalized:
        return {}

    profile = {
        "username": normalized,
        "user_id": "",
        "profile_pic_url": "",
        "followers": None,
        "posts": None,
        "is_private": None,
    }

    session = _new_session()
    lsd_token = _prime_instagram_session(session, normalized)
    pic_candidates: list[str] = []

    web_user = _web_profile_info_user(session, normalized, lsd_token=lsd_token)
    _merge_profile_from_user(profile, web_user, pic_candidates)

    try:
        r = session.get(
            f"https://www.instagram.com/api/v1/feed/user/{normalized}/username/",
            params={"count": 1},
            timeout=18,
            allow_redirects=True,
            headers={"Referer": f"https://www.instagram.com/{normalized}/"},
        )
        r.raise_for_status()
        payload = r.json()
        top_user = payload.get("user") if isinstance(payload, dict) else {}
        if not isinstance(top_user, dict):
            top_user = {}
        items = payload.get("items") if isinstance(payload, dict) else []
        first_item_user = {}
        if isinstance(items, list) and items:
            first_item = items[0] if isinstance(items[0], dict) else {}
            first_item_user = first_item.get("user") if isinstance(first_item, dict) else {}
            if not isinstance(first_item_user, dict):
                first_item_user = {}

        _merge_profile_from_user(profile, first_item_user, pic_candidates)
        _merge_profile_from_user(profile, top_user, pic_candidates)
    except Exception:
        pass

    first_post_user = _graphql_first_post_user(session, normalized)
    _merge_profile_from_user(profile, first_post_user, pic_candidates)

    page_html = ""
    try:
        r = session.get(
            f"https://www.instagram.com/{normalized}/",
            timeout=18,
            allow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        r.raise_for_status()
        page_html = r.text
        if not lsd_token:
            lsd_token = _extract_lsd_token(page_html)
        html_user_id = _extract_profile_user_id(page_html)
        if html_user_id and not profile["user_id"]:
            profile["user_id"] = html_user_id

        pic_candidates.extend(_profile_pic_candidates_from_page_html(page_html))

        d = re.search(
            r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"',
            page_html,
            re.I,
        )
        if d:
            desc = html.unescape(d.group(1))
            if profile["followers"] is None:
                followers_match = re.search(r"([\d.,KMBkmb]+)\s+Followers?", desc, re.I)
                if followers_match:
                    profile["followers"] = _parse_count(followers_match.group(1))
            if profile["posts"] is None:
                posts_match = re.search(r"([\d.,KMBkmb]+)\s+Posts?", desc, re.I)
                if posts_match:
                    profile["posts"] = _parse_count(posts_match.group(1))
    except Exception:
        pass

    web_user = _web_profile_info_user(session, normalized, lsd_token=lsd_token)
    _merge_profile_from_user(profile, web_user, pic_candidates)

    user_id = str(profile.get("user_id") or "").strip()
    if user_id:
        graphql_user = _graphql_profile_user(session, user_id, normalized)
        _merge_profile_from_user(profile, graphql_user, pic_candidates)

        try:
            r = session.get(
                f"https://www.instagram.com/api/v1/users/{user_id}/info/",
                timeout=14,
                allow_redirects=True,
            )
            r.raise_for_status()
            user = (r.json().get("user") or {})
            _merge_profile_from_user(profile, user, pic_candidates)
        except Exception:
            pass

    profile["profile_pic_url"] = _pick_best_profile_pic_url(pic_candidates)
    return profile


@st.cache_data(show_spinner=False, ttl=1800)
def _profile_picture_url(username: str) -> str:
    overview = _profile_overview(username)
    return str(overview.get("profile_pic_url") or "").strip()


def _render_favorites_sidebar():
    st.sidebar.subheader("Favorites")
    favorites: list[str] = st.session_state.get("favorites", [])

    if not favorites:
        st.sidebar.caption("No favorites yet.")
        return

    for username in favorites:
        url = _favorite_link(username)
        dp_url = _profile_picture_url(username)
        if dp_url:
            st.sidebar.markdown(
                f'<a href="{url}"><img src="{dp_url}" width="64" style="border-radius:50%; object-fit:cover;" /></a>',
                unsafe_allow_html=True,
            )
        st.sidebar.markdown(f"[@{username}]({url})")
        st.sidebar.caption("Click profile image or username to load media.")
        st.sidebar.divider()


def _bundle_state_key(username: str, tab_key: str, scope: str, page: int | None = None) -> str:
    safe_user = _safe_token(username)
    if page is None:
        return f"zip_{safe_user}_{tab_key}_{scope}"
    return f"zip_{safe_user}_{tab_key}_{scope}_{page}"


def _render_zip_download_block(state_key: str, label: str):
    bundle = st.session_state.get(state_key)
    if not isinstance(bundle, dict):
        return

    data = bundle.get("data")
    filename = str(bundle.get("filename") or "media.zip")
    errors = bundle.get("errors") or []

    if isinstance(data, (bytes, bytearray)) and len(data) > 0:
        st.download_button(
            label=label,
            data=data,
            file_name=filename,
            mime="application/zip",
            key=f"{state_key}_save",
        )
    else:
        st.error("Could not build ZIP for download.")

    if errors:
        st.warning(f"Some items failed while preparing ZIP ({len(errors)} error(s)).")
        for err in errors[:6]:
            st.text(err)
        if len(errors) > 6:
            st.text(f"... and {len(errors) - 6} more errors")


def _render_media_tab(tab_key: str, username: str, items: list[dict]):
    page_state_key = f"page_{tab_key}"
    jump_input_key = f"{tab_key}_jump_page_input"
    jump_sync_key = f"{tab_key}_jump_page_sync"

    if not items:
        st.info("No media available in this tab for the current username.")
        return

    total_items = len(items)
    total_pages = max(1, math.ceil(total_items / ITEMS_PER_PAGE))
    current_page = int(st.session_state.get(page_state_key, 1))
    current_page = max(1, min(current_page, total_pages))
    st.session_state[page_state_key] = current_page

    if jump_input_key not in st.session_state:
        st.session_state[jump_input_key] = current_page
        st.session_state[jump_sync_key] = current_page
    elif int(st.session_state.get(jump_sync_key, current_page)) != current_page:
        st.session_state[jump_input_key] = current_page
        st.session_state[jump_sync_key] = current_page

    nav1, nav2, nav3, nav4, nav5, nav6 = st.columns([1, 1, 2, 2, 1.4, 1])
    with nav1:
        if st.button("Previous", key=f"{tab_key}_prev", disabled=current_page <= 1):
            st.session_state[page_state_key] = current_page - 1
            st.session_state[jump_sync_key] = current_page - 1
            st.rerun()
    with nav2:
        if st.button("Next", key=f"{tab_key}_next", disabled=current_page >= total_pages):
            st.session_state[page_state_key] = current_page + 1
            st.session_state[jump_sync_key] = current_page + 1
            st.rerun()
    with nav3:
        st.write(f"Page `{current_page}` / `{total_pages}`")
    with nav4:
        st.write(f"Items: `{total_items}` | `50` per page")
    with nav5:
        st.number_input(
            "Go to page",
            min_value=1,
            max_value=total_pages,
            step=1,
            key=jump_input_key,
        )
    with nav6:
        st.write("")
        if st.button("Go", key=f"{tab_key}_go_to_page"):
            target_page = int(st.session_state.get(jump_input_key, current_page))
            target_page = max(1, min(target_page, total_pages))
            if target_page != current_page:
                st.session_state[page_state_key] = target_page
                st.session_state[jump_sync_key] = target_page
                st.rerun()

    start = (current_page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = items[start:end]

    page_bundle_key = _bundle_state_key(username, tab_key, "page", current_page)
    all_bundle_key = _bundle_state_key(username, tab_key, "all")

    dl1, dl2 = st.columns(2)
    with dl1:
        if st.button("Prepare Current Page ZIP", key=f"{tab_key}_prepare_page_zip"):
            with st.spinner("Preparing current page ZIP..."):
                zip_bytes, errors = _build_zip_bytes(username, page_items)
            st.session_state[page_bundle_key] = {
                "data": zip_bytes,
                "filename": f"{username}_{tab_key}_page_{current_page}.zip",
                "errors": errors,
            }
        _render_zip_download_block(page_bundle_key, "Save Current Page ZIP")
    with dl2:
        if st.button("Prepare All ZIP", key=f"{tab_key}_prepare_all_zip"):
            with st.spinner("Preparing all media ZIP..."):
                zip_bytes, errors = _build_zip_bytes(username, items)
            st.session_state[all_bundle_key] = {
                "data": zip_bytes,
                "filename": f"{username}_{tab_key}_all.zip",
                "errors": errors,
            }
        _render_zip_download_block(all_bundle_key, "Save All ZIP")

    st.caption("Downloads are browser-based. Your browser controls where files are saved.")

    grid = st.columns(3)
    for idx, item in enumerate(page_items):
        with grid[idx % 3]:
            media_kind = "Video" if item["is_video"] else "Photo"
            caption = f"{item['post_id']} - {media_kind}"
            try:
                st.image(item["thumbnail_url"], use_container_width=True, caption=caption)
            except Exception:
                if item["is_video"]:
                    st.video(item["url"])
                else:
                    st.image(item["url"], use_container_width=True, caption=caption)

            if item["is_video"]:
                with st.expander("Play Video"):
                    st.video(item["url"])

            st.markdown(f"[Open Media URL]({item['url']})")
            filename = _build_filename(username, item)
            st.markdown(
                f'<a href="{item["url"]}" download="{filename}" target="_blank" rel="noopener noreferrer">Download This Item</a>',
                unsafe_allow_html=True,
            )


def _render_post_url_downloader():
    st.subheader("Download By Post URL")
    st.caption("Paste a post/reel URL and download the highest-quality media available.")

    with st.form("url_form"):
        post_url_input = st.text_input(
            "Instagram Post/Reel URL",
            placeholder="https://www.instagram.com/p/XXXXXXXXXXX/",
        )
        url_submitted = st.form_submit_button("Load URL Media")

    if url_submitted:
        _load_media_from_url(post_url_input)

    if st.session_state.get("url_load_error"):
        st.error(str(st.session_state["url_load_error"]))
        return

    media_result = st.session_state.get("url_media_result")
    if not isinstance(media_result, dict) or not media_result:
        st.info("No URL media loaded yet.")
        return

    post = media_result.get("post")
    if not isinstance(post, dict):
        st.error("Invalid media response from Instagram.")
        return

    owner_username = _normalize_username(str(media_result.get("owner_username") or ""))
    shortcode = _safe_token(str(media_result.get("shortcode") or post.get("post_id") or "post"))
    display_owner = owner_username or "instagram"

    post_items = _flatten_posts([post])
    st.caption(
        f"Loaded shortcode `{shortcode}`"
        + (f" from `@{owner_username}`." if owner_username else ".")
    )
    _render_media_tab(f"url_{shortcode}", display_owner, post_items)


st.set_page_config(page_title="Instagram Media Downloader", layout="wide")
_ensure_session_state()

st.title("Instagram Media Downloader")
st.caption("Enter a username. Pagination is fixed at 50 items per page.")
_render_post_url_downloader()
st.divider()
st.sidebar.subheader("Settings")
selected_limit = st.sidebar.selectbox(
    "Max posts to fetch per load",
    options=POST_FETCH_OPTIONS,
    index=POST_FETCH_OPTIONS.index(int(st.session_state.get("post_fetch_limit", POST_FETCH_LIMIT)))
    if int(st.session_state.get("post_fetch_limit", POST_FETCH_LIMIT)) in POST_FETCH_OPTIONS
    else 0,
)
st.session_state["post_fetch_limit"] = int(selected_limit)
_render_favorites_sidebar()

with st.form("search_form"):
    username_input = st.text_input("Instagram Username", placeholder="e.g. natgeo")
    submitted = st.form_submit_button("Load Media")

if submitted:
    _load_profile_media(username_input)
else:
    favorite_username = _selected_favorite_from_query()
    if favorite_username and favorite_username != st.session_state.get("loaded_username", ""):
        _load_profile_media(favorite_username)
        _clear_favorite_query_param()

if st.session_state["load_error"]:
    st.error(st.session_state["load_error"])
    st.stop()

if st.session_state.get("load_notice"):
    st.warning(str(st.session_state["load_notice"]))

active_username = st.session_state["loaded_username"]
if not active_username:
    st.info("Enter a username and click `Load Media`.")
    st.stop()

all_items = st.session_state["post_items"]
reel_items = [item for item in all_items if item.get("group") == "reels"]
video_items = [item for item in all_items if item.get("group") == "videos"]
photo_items = [item for item in all_items if item.get("group") == "photos"]
profile_info = st.session_state.get("profile_info", {})
dp_url = str(profile_info.get("profile_pic_url") or "").strip()
profile_followers = profile_info.get("followers")
profile_posts_count = profile_info.get("posts")
if profile_posts_count is None:
    profile_posts_count = st.session_state["profile_count"]

dp_col, followers_col, posts_col, media_col = st.columns([1.2, 1, 1, 1])
with dp_col:
    st.markdown(f"### @{active_username}")
    if dp_url:
        st.image(dp_url, use_container_width=True, caption="HD profile picture")
        st.markdown(f"[Open Full-Resolution DP]({dp_url})")
    else:
        st.info("Profile picture unavailable.")
with followers_col:
    st.metric("Followers", _format_count(profile_followers))
with posts_col:
    st.metric("Posts", _format_count(profile_posts_count))
with media_col:
    st.metric("Media Loaded", f"{len(all_items):,}")

st.write(
    f"Reels: `{len(reel_items)}` | Videos: `{len(video_items)}` | Photos: `{len(photo_items)}`"
)
st.caption("Use the download controls below to save media directly from your browser.")

if active_username in st.session_state.get("favorites", []):
    st.caption(f"`@{active_username}` is already in favorites.")
else:
    if st.button(f"Add @{active_username} to Favorites", key=f"add_favorite_{active_username}"):
        _add_favorite(active_username)
        st.rerun()

tab_all, tab_reels, tab_videos, tab_photos = st.tabs(["All Posts", "Reels", "Videos", "Photos"])

with tab_all:
    _render_media_tab("all", active_username, all_items)

with tab_reels:
    _render_media_tab("reels", active_username, reel_items)

with tab_videos:
    _render_media_tab("videos", active_username, video_items)

with tab_photos:
    _render_media_tab("photos", active_username, photo_items)
