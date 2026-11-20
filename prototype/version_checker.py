"""
Pure-Stdlib Version Metadata & GitHub Update Checker for AGY Roundtable.

Checks GitHub (`raw.githubusercontent.com/.../VERSION.json` with fallback to
GitHub Releases API) on a non-blocking TTL cache so older local installations
automatically pop up a clearly badged update notification whenever a new version ships.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple


PROTO_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(PROTO_DIR)
VERSION_FILE_PATH = os.path.join(REPO_ROOT, "VERSION.json")

DEFAULT_REPO_SLUG = "QUDAD-ENT/agy-roundtable"
DEFAULT_APP_VERSION = "5.5.0"
DEFAULT_SERVER_REV = 19

_CACHE_LOCK = threading.Lock()
_UPDATE_CACHE: Dict[str, Any] = {"ts": 0.0, "result": None}
_CACHE_TTL_SEC = 1800.0  # 30 minutes between remote checks unless force=True


def parse_semver(ver: Optional[str]) -> Tuple[int, ...]:
    """Parse version string like 'v5.5.0' or '5.10.2-beta' into comparable numeric tuple."""
    s = str(ver or "").strip()
    if s.lower().startswith("v"):
        s = s[1:]
    nums = [int(x) for x in re.findall(r"\d+", s.split("-", 1)[0])]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:4])


def compare_versions(v_a: Optional[str], v_b: Optional[str]) -> int:
    """Return -1 if v_a < v_b, 0 if equal, 1 if v_a > v_b."""
    ta = parse_semver(v_a)
    tb = parse_semver(v_b)
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def get_local_version_info(version_file: str = VERSION_FILE_PATH) -> Dict[str, Any]:
    """Read local VERSION.json cleanly with safe fallbacks."""
    base: Dict[str, Any] = {
        "version": DEFAULT_APP_VERSION,
        "server_rev": DEFAULT_SERVER_REV,
        "release_date": "",
        "repo": DEFAULT_REPO_SLUG,
        "release_url": f"https://github.com/{DEFAULT_REPO_SLUG}/releases/latest",
        "title_zh": f"AGY Roundtable v{DEFAULT_APP_VERSION}",
        "title_en": f"AGY Roundtable v{DEFAULT_APP_VERSION}",
        "highlights_zh": [],
        "highlights_en": [],
    }
    try:
        if os.path.isfile(version_file):
            with open(version_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                base.update({k: v for k, v in loaded.items() if v is not None})
    except Exception:
        pass
    base["version"] = str(base.get("version") or DEFAULT_APP_VERSION).lstrip("vV")
    base["server_rev"] = int(base.get("server_rev") or DEFAULT_SERVER_REV)
    base["repo"] = str(base.get("repo") or DEFAULT_REPO_SLUG).strip()
    return base


def _default_http_json_fetch(url: str, timeout_sec: float = 4.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"AGY-Roundtable/{DEFAULT_APP_VERSION}",
            "Accept": "application/json, text/plain, */*",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)


def _extract_bullets_from_release_body(body: str, limit: int = 8) -> List[str]:
    lines: List[str] = []
    for raw in str(body or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith(("- ", "* ", "• ")):
            lines.append(ln[2:].strip())
        elif re.match(r"^\d+[.)]\s+", ln):
            lines.append(re.sub(r"^\d+[.)]\s+", "", ln).strip())
        if len(lines) >= limit:
            break
    if not lines and str(body or "").strip():
        lines = [str(body).strip()[:280]]
    return lines


def check_for_updates(
    force: bool = False,
    timeout_sec: float = 4.0,
    version_file: str = VERSION_FILE_PATH,
    fetcher: Optional[Callable[[str, float], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compare local VERSION.json against remote GitHub VERSION.json (and fallback Releases API).
    Always offline-safe and never raises network exceptions to caller.
    """
    local = get_local_version_info(version_file=version_file)
    use_cache = (fetcher is None) and (version_file == VERSION_FILE_PATH) and (not force)
    now = time.time()
    if use_cache:
        with _CACHE_LOCK:
            cached = _UPDATE_CACHE.get("result")
            if cached and (now - float(_UPDATE_CACHE.get("ts") or 0.0)) < _CACHE_TTL_SEC:
                return dict(cached)

    repo = local["repo"]
    cur_ver = local["version"]
    cur_rev = int(local["server_rev"])
    http_get = fetcher or _default_http_json_fetch

    remote_data: Optional[Dict[str, Any]] = None
    source = "local_only"
    err_msg: Optional[str] = None

    raw_url = f"https://raw.githubusercontent.com/{repo}/main/VERSION.json"
    rel_api_url = f"https://api.github.com/repos/{repo}/releases/latest"

    # 1. Primary: raw VERSION.json on main (zero GitHub API unauthenticated rate-limit + rich bilingual notes)
    try:
        obj = http_get(raw_url, timeout_sec)
        if isinstance(obj, dict) and obj.get("version"):
            remote_data = {
                "latest_version": str(obj.get("version") or "").lstrip("vV"),
                "latest_server_rev": int(obj.get("server_rev") or 0),
                "release_date": str(obj.get("release_date") or ""),
                "release_url": str(obj.get("release_url") or f"https://github.com/{repo}/releases/latest"),
                "title_zh": str(obj.get("title_zh") or obj.get("title") or f"AGY Roundtable v{obj.get('version')}"),
                "title_en": str(obj.get("title_en") or obj.get("title") or f"AGY Roundtable v{obj.get('version')}"),
                "highlights_zh": list(obj.get("highlights_zh") or []),
                "highlights_en": list(obj.get("highlights_en") or []),
            }
            source = "github_raw_version_json"
    except Exception as exc:
        err_msg = str(exc)

    # 2. Fallback: GitHub Releases latest API
    if remote_data is None:
        try:
            rel = http_get(rel_api_url, timeout_sec)
            if isinstance(rel, dict) and (rel.get("tag_name") or rel.get("name")):
                tag_v = str(rel.get("tag_name") or rel.get("name") or "").lstrip("vV")
                bullets = _extract_bullets_from_release_body(str(rel.get("body") or ""))
                remote_data = {
                    "latest_version": tag_v,
                    "latest_server_rev": 0,
                    "release_date": str(rel.get("published_at") or "")[:10],
                    "release_url": str(rel.get("html_url") or f"https://github.com/{repo}/releases/latest"),
                    "title_zh": str(rel.get("name") or f"AGY Roundtable v{tag_v}"),
                    "title_en": str(rel.get("name") or f"AGY Roundtable v{tag_v}"),
                    "highlights_zh": bullets,
                    "highlights_en": bullets,
                }
                source = "github_releases_api"
                err_msg = None
        except Exception as exc2:
            err_msg = err_msg or str(exc2)

    if remote_data is not None:
        lat_ver = remote_data["latest_version"] or cur_ver
        lat_rev = int(remote_data.get("latest_server_rev") or 0)
        cmp_v = compare_versions(cur_ver, lat_ver)
        has_update = (cmp_v < 0) or (cmp_v == 0 and lat_rev > cur_rev)
        result: Dict[str, Any] = {
            "ok": True,
            "current_version": cur_ver,
            "current_server_rev": cur_rev,
            "latest_version": lat_ver,
            "latest_server_rev": lat_rev or cur_rev,
            "has_update": bool(has_update),
            "repo": repo,
            "repo_url": f"https://github.com/{repo}",
            "release_url": remote_data["release_url"],
            "release_date": remote_data["release_date"],
            "title_zh": remote_data["title_zh"],
            "title_en": remote_data["title_en"],
            "highlights_zh": remote_data["highlights_zh"],
            "highlights_en": remote_data["highlights_en"],
            "source": source,
            "checked_at": now,
            "offline_or_error": None,
        }
    else:
        result = {
            "ok": True,
            "current_version": cur_ver,
            "current_server_rev": cur_rev,
            "latest_version": cur_ver,
            "latest_server_rev": cur_rev,
            "has_update": False,
            "repo": repo,
            "repo_url": f"https://github.com/{repo}",
            "release_url": local["release_url"],
            "release_date": local["release_date"],
            "title_zh": local["title_zh"],
            "title_en": local["title_en"],
            "highlights_zh": list(local.get("highlights_zh") or []),
            "highlights_en": list(local.get("highlights_en") or []),
            "source": "local_only",
            "checked_at": now,
            "offline_or_error": (err_msg or "offline")[:200],
        }

    if fetcher is None and version_file == VERSION_FILE_PATH:
        with _CACHE_LOCK:
            _UPDATE_CACHE["ts"] = now
            _UPDATE_CACHE["result"] = dict(result)
    return result
