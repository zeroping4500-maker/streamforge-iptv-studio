#!/usr/bin/env python3
"""
ZEMYIPTV source importers.

Non-M3U provider adapters (tvnow.st JSON API, thedaddy.li HTML catalog,
pflix.net probe) plus the GitHub m3u8 code search that feeds live web search.

Stdlib only, no app.py imports: every function is callable on its own, and the
network budget per call is bounded so weak hosts are not starved.
"""

import gzip
import json
import re
import urllib.error
import urllib.parse
import urllib.request

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

TVNOW_API_URL = "https://tvnow.st/api/channels"
TVNOW_REFERRER = "https://tvnow.st/"
TVNOW_LOGO_BASE = "https://tvnow.st"

THEDADDY_LIST_URL = "https://thedaddy.li/24-7-channels.php"
THEDADDY_STREAM_URL = "https://thedaddy.li/stream/stream-{cid}.php"
THEDADDY_REFERRER = "https://thedaddy.li/"
THEDADDY_EMBED_URL = "https://dembed.st/premiumtv/daddy.php?id={cid}"
THEDADDY_EMBED_REFERRER = "https://thedaddy.li/stream/stream-{cid}.php"

PFLIX_PROBE_URL = "https://pflix.net/live"

MAX_BODY_BYTES = 6 * 1024 * 1024


def _maybe_gunzip(raw: bytes, content_encoding: str = "") -> bytes:
    if (content_encoding or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    return raw


def http_get(url: str, referrer: str = "", user_agent: str = BROWSER_UA,
             timeout: float = 15.0, max_bytes: int = MAX_BODY_BYTES,
             extra_headers: dict = None) -> dict:
    """Single bounded GET. Returns {ok, status, body, error} and never raises."""
    headers = {
        "User-Agent": user_agent or BROWSER_UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
    }
    if referrer:
        headers["Referer"] = referrer
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes)
            raw = _maybe_gunzip(raw, resp.headers.get("Content-Encoding") or "")
            return {"ok": True, "status": resp.getcode(), "body": raw,
                    "content_type": resp.headers.get("Content-Type", ""), "error": ""}
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = _maybe_gunzip(e.read(min(max_bytes, 256 * 1024)),
                                 (e.headers or {}).get("Content-Encoding") or "")
        except Exception:
            pass
        return {"ok": False, "status": e.code, "body": body,
                "content_type": (e.headers or {}).get("Content-Type", ""), "error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        return {"ok": False, "status": 0, "body": b"", "content_type": "", "error": str(e)}


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def _is_cloudflare_challenge(res: dict) -> bool:
    body = (res.get("body") or b"").decode("utf-8", errors="ignore").lower()
    return (
        "just a moment" in body
        or "cf-chl" in body
        or "/cdn-cgi/challenge-platform" in body
        or "attention required" in body
    )


# ---------------------------------------------------------------------------
# tvnow.st - public JSON channel API, m3u8 playback needs a Referer header
# ---------------------------------------------------------------------------

def fetch_tvnow_channels(timeout: float = 15.0) -> dict:
    """
    Returns {ok, status, channels, error}. Each channel is catalog ready:
    {name, url, category, logo, referrer, user_agent, quality}.
    """
    res = http_get(TVNOW_API_URL, referrer=TVNOW_REFERRER, timeout=timeout,
                   extra_headers={"Accept": "application/json"})
    if not res["ok"]:
        return {"ok": False, "status": res["status"], "channels": [], "error": res["error"]}
    try:
        data = json.loads(res["body"].decode("utf-8", errors="ignore"))
    except Exception as e:
        return {"ok": False, "status": res["status"], "channels": [], "error": f"Invalid JSON: {e}"}

    raw_channels = data.get("channels") if isinstance(data, dict) else data
    if not isinstance(raw_channels, list):
        return {"ok": False, "status": res["status"], "channels": [], "error": "No channels array in response"}

    out = []
    for entry in raw_channels:
        if not isinstance(entry, dict):
            continue
        playback = (entry.get("playback") or "").strip()
        name = _clean_name(entry.get("name") or entry.get("mark") or entry.get("slug") or "")
        if not playback.startswith(("http://", "https://")) or not name:
            continue
        logo = (entry.get("logo") or "").strip()
        if logo.startswith("/"):
            logo = TVNOW_LOGO_BASE + logo
        out.append({
            "name": name,
            "url": playback,
            "category": _clean_name(entry.get("category") or "Live TV") or "Live TV",
            "logo": logo,
            "referrer": TVNOW_REFERRER,
            "user_agent": BROWSER_UA,
            "quality": "HD",
        })
    return {"ok": True, "status": res["status"], "channels": out, "error": ""}


# ---------------------------------------------------------------------------
# thedaddy.li - HTML channel catalog; playback resolves through a player embed
# ---------------------------------------------------------------------------

_CARD_RE = re.compile(
    r'<a\s+class="card"[^>]*?href="/watch\.php\?id=(\d+)"[^>]*?data-title="([^"]*)"',
    re.S | re.I,
)


def fetch_thedaddy_channels(timeout: float = 20.0) -> dict:
    """
    Parses the 24/7 channel grid. Streams are not in the grid: each entry points
    at the player embed page, which the proxy resolves to the real HLS URL on
    play (one request per channel viewed, not per channel imported).
    """
    res = http_get(THEDADDY_LIST_URL, referrer=THEDADDY_REFERRER, timeout=timeout)
    if not res["ok"]:
        return {"ok": False, "status": res["status"], "channels": [], "error": res["error"]}
    html = res["body"].decode("utf-8", errors="ignore")
    if _is_cloudflare_challenge(res):
        return {"ok": False, "status": res["status"], "channels": [], "error": "Cloudflare challenge on channel list"}

    out = []
    seen = set()
    for cid, title in _CARD_RE.findall(html):
        if cid in seen:
            continue
        name = _clean_name(title)
        if not name:
            continue
        seen.add(cid)
        out.append({
            "name": name,
            "url": THEDADDY_EMBED_URL.format(cid=cid),
            "category": "24/7",
            "logo": "",
            "referrer": THEDADDY_EMBED_REFERRER.format(cid=cid),
            "user_agent": BROWSER_UA,
            "quality": "HD",
            "embed": True,
            "play_mode": "iframe",
        })
    if not out:
        return {"ok": False, "status": res["status"], "channels": [], "error": "No channel cards matched (page layout changed?)"}
    return {"ok": True, "status": res["status"], "channels": out, "error": ""}


_SRC_RE = re.compile(r'const\s+SRC\s*=\s*"([^"]+)"')


def resolve_embed_stream(page_url: str, referrer: str = "", timeout: float = 12.0) -> str:
    """Extracts the HLS URL ('const SRC = ...') from a player embed page."""
    if not page_url.startswith(("http://", "https://")):
        return ""
    res = http_get(page_url, referrer=referrer or THEDADDY_REFERRER, timeout=timeout, max_bytes=1024 * 1024)
    if not res["ok"]:
        return ""
    html = res["body"].decode("utf-8", errors="ignore")
    m = _SRC_RE.search(html)
    if m:
        return m.group(1).strip()
    m = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)
    return m.group(0) if m else ""


def embed_referrer_for(page_url: str) -> str:
    """Referer a player embed expects: the wrapping watch page for the same id."""
    m = re.search(r"[?&]id=(\d+)", page_url or "")
    if m and "dembed.st" in page_url:
        return THEDADDY_EMBED_REFERRER.format(cid=m.group(1))
    return ""


# ---------------------------------------------------------------------------
# Source probe - one cheap request that says whether a source is usable
# ---------------------------------------------------------------------------

def probe_source(url: str, timeout: float = 10.0) -> dict:
    """
    {status: ok|blocked|unreachable, http_status, detail}
    blocked == the host answered but hides content from non-browser clients
    (Cloudflare JS challenge), so importing needs a headless browser.
    """
    res = http_get(url, referrer=urllib.parse.urljoin(url, "/"), timeout=timeout, max_bytes=256 * 1024)
    if res["status"] == 0:
        return {"status": "unreachable", "http_status": 0, "detail": res["error"]}
    if res["status"] in (401, 403, 429, 503) and _is_cloudflare_challenge(res):
        return {"status": "blocked", "http_status": res["status"],
                "detail": "Cloudflare JS challenge: needs a real browser, not a plain HTTP fetch"}
    if res["status"] >= 400:
        return {"status": "unreachable", "http_status": res["status"], "detail": res["error"]}
    return {"status": "ok", "http_status": res["status"], "detail": "reachable"}


def probe_pflix(timeout: float = 10.0) -> dict:
    out = probe_source(PFLIX_PROBE_URL, timeout=timeout)
    out["source"] = "pflix.net"
    if out["status"] == "blocked":
        out["detail"] = (
            "pflix.net hides its channel list behind a Cloudflare JS challenge, so nothing can be "
            "imported server-side. Play it through the embedded player instead: paste any pflix page "
            "URL into the player's Load box and it opens in the dock from your own IP."
        )
    return out


# ---------------------------------------------------------------------------
# GitHub code search - finds .m3u8 playlists across public repos
# ---------------------------------------------------------------------------

GITHUB_CODE_SEARCH_URL = "https://api.github.com/search/code"
GITHUB_RAW_URL = "https://raw.githubusercontent.com/{repo}/HEAD/{path}"


def github_code_search_m3u8(query: str, token: str = "", limit: int = 4,
                            timeout: float = 10.0) -> dict:
    """
    Searches file contents for '<query> extension:m3u8'.
    With a token the API allows 30 requests/min; without one it is 10/min and
    often rate-limited, so callers should treat an error as 'no results'.
    Returns {ok, files: [{repo, path, raw_url, html_url}], total, error}.
    """
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", query or "").strip()
    if not clean:
        return {"ok": False, "files": [], "total": 0, "error": "Empty query"}
    per_page = max(1, min(limit, 10))
    api_url = (
        f"{GITHUB_CODE_SEARCH_URL}?q={urllib.parse.quote_plus(clean + ' extension:m3u8')}"
        f"&per_page={per_page}"
    )
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ZEMYIPTV/4.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    res = http_get(api_url, timeout=timeout, max_bytes=1024 * 1024, extra_headers=headers)
    if not res["ok"]:
        return {"ok": False, "files": [], "total": 0, "error": res["error"]}
    try:
        data = json.loads(res["body"].decode("utf-8", errors="ignore"))
    except Exception as e:
        return {"ok": False, "files": [], "total": 0, "error": f"Invalid JSON: {e}"}
    if data.get("message"):
        return {"ok": False, "files": [], "total": 0, "error": data["message"]}

    files = []
    for item in data.get("items", [])[:per_page]:
        repo = (item.get("repository") or {}).get("full_name") or ""
        path = item.get("path") or ""
        if not repo or not path:
            continue
        files.append({
            "repo": repo,
            "path": path,
            "raw_url": GITHUB_RAW_URL.format(repo=repo, path=urllib.parse.quote(path)),
            "html_url": item.get("html_url", ""),
        })
    return {"ok": True, "files": files, "total": int(data.get("total_count") or 0), "error": ""}


_EXTINF_RE = re.compile(r"#EXTINF:-?\d+(?:\.\d+)?\s*(.*?),(.*)", re.I)


def streams_from_playlist_text(text: str, max_streams: int = 400) -> list:
    """Parses m3u/m3u8 text into [{name, url, referrer, user_agent}] pairs."""
    out = []
    pending = None
    referrer = ""
    user_agent = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTVLCOPT:"):
            opt = line.split(":", 1)[1]
            if opt.lower().startswith("http-referrer="):
                referrer = opt.split("=", 1)[1]
            elif opt.lower().startswith("http-user-agent="):
                user_agent = opt.split("=", 1)[1]
            continue
        if line.startswith("#EXTINF"):
            m = _EXTINF_RE.match(line)
            name = (m.group(2) if m else "").strip()
            pending = _clean_name(name)
            continue
        if line.startswith("#"):
            if "URI=\"" in line:
                pass
            continue
        if line.startswith(("http://", "https://")):
            out.append({
                "name": pending or "",
                "url": line,
                "referrer": referrer,
                "user_agent": user_agent,
            })
            pending = None
            referrer = ""
            user_agent = ""
            if len(out) >= max_streams:
                break
    return out
