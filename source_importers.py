#!/usr/bin/env python3
"""
ZEMYIPTV source importers.

Non-M3U provider adapters (tvnow.st JSON API, thedaddy.li HTML catalog,
pflix.net probe) plus the GitHub m3u8 code search that feeds live web search.

Stdlib only, no app.py imports: every function is callable on its own, and the
network budget per call is bounded so weak hosts are not starved.
"""

import concurrent.futures
import difflib
import gzip
import html as html_mod
import ipaddress
import json
import os
import re
import socket
import time
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


class UnsafeHostError(ValueError):
    """The target host resolves to a private, loopback or link-local address."""


def assert_public_http_url(url: str) -> str:
    """SSRF guard: only http(s) URLs whose hosts resolve to public addresses.

    Blocks loopback, RFC1918, link-local (incl. cloud metadata 169.254.169.254),
    reserved and unspecified targets. Returns the URL on success.
    """
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeHostError("Only absolute http(s) URLs are allowed")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeHostError(f"Cannot resolve host: {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise UnsafeHostError(f"Host {parsed.hostname} resolves to a non-public address")
    return url


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
    try:
        assert_public_http_url(url)
    except UnsafeHostError as exc:
        return {"ok": False, "status": 0, "body": b"", "content_type": "", "error": str(exc)}
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
# smotret.tv - RU channel catalogue. Each category page lists its channels as
# <li data-channel-id=... data-channel-name=...>; each channel page frames a
# player page that declares its sources inline:
#   var streams = ["https://.../chunklist.m3u8", "/error.m3u8"]
# Channels that play through ok.ru / rutube / 1tv.ru / smotrim.ru have no HLS
# in the player page and are reported as embed-only instead of imported.
# ---------------------------------------------------------------------------

SMOTRET_BASE = "https://smotret.tv"
SMOTRET_REFERRER = "https://smotret.tv/"
SMOTRET_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "smotret_catalog_cache.json"
)
SMOTRET_CACHE_TTL = 24 * 3600

SMOTRET_CATEGORIES = (
    ("", "General"),
    ("music", "Music"),
    ("news", "News"),
    ("sport", "Sport"),
    ("detskie", "Kids"),
    ("christian", "Christian"),
    ("razvlekatelnye", "Entertainment"),
    ("poznavatelnye", "Educational"),
    ("region", "Regional"),
    ("relax", "Relax"),
)

_SMOTRET_LI_RE = re.compile(
    r'<li[^>]*data-channel-id="([^"]+)"[^>]*data-channel-name="([^"]*)"[^>]*>(.*?)</li>',
    re.S | re.I,
)
_SMOTRET_IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']?([^"\'> ]+)', re.I)
_SMOTRET_STREAMS_RE = re.compile(r'var\s+streams\s*=\s*(\[[^\]]*\])')
_SMOTRET_M3U8_RE = re.compile(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*')
_SMOTRET_NAME_SUFFIX_RE = re.compile(r"\s*(?:[—–-]\s*)?(?:смотреть\s+онлайн|watch\s+online)\b.*$", re.I)
_SMOTRET_ONLINE_SUFFIX_RE = re.compile(r"\s*[—–-]\s*online\s*$", re.I)


def _smotret_load_cache(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    channels = data.get("channels") if isinstance(data, dict) else None
    return channels if isinstance(channels, dict) else {}


def _smotret_save_cache(path: str, cache: dict) -> None:
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"v": 1, "saved_at": int(time.time()), "channels": cache}, handle)
        os.replace(tmp, path)
    except Exception:
        pass


def parse_smotret_listing(page_html: str, category: str = "Live TV") -> list:
    """Channel cards from one category page: [{id, name, page, logo, category}]."""
    out = []
    for match in _SMOTRET_LI_RE.finditer(page_html or ""):
        cid = _clean_name(html_mod.unescape(match.group(1) or ""))
        raw_name = _clean_name(html_mod.unescape(match.group(2) or ""))
        name = _SMOTRET_ONLINE_SUFFIX_RE.sub("", _SMOTRET_NAME_SUFFIX_RE.sub("", raw_name)).strip()
        name = _clean_name(name) or raw_name
        if not cid or not name:
            continue
        block = match.group(3) or ""
        href = re.search(r'href="(/[^"]+)"', block)
        logo = re.search(r'data-(?:src|original)="([^"]+)"', block) or re.search(r'<img[^>]+src="([^"]+)"', block)
        out.append({
            "id": cid,
            "name": name,
            "page": urllib.parse.urljoin(SMOTRET_BASE + "/", href.group(1)) if href else f"{SMOTRET_BASE}/{cid}",
            "logo": urllib.parse.urljoin(SMOTRET_BASE + "/", logo.group(1)) if logo else "",
            "category": category or "Live TV",
        })
    return out


def parse_smotret_streams(player_html: str) -> list:
    """HLS URLs from a player page, best first; the placeholder /error.m3u8 is dropped."""
    candidates = []
    match = _SMOTRET_STREAMS_RE.search(player_html or "")
    if match:
        try:
            data = json.loads(match.group(1))
        except Exception:
            data = []
        for entry in data if isinstance(data, list) else []:
            if isinstance(entry, str) and entry.strip():
                candidates.append(entry.strip())
    if not candidates:
        candidates = _SMOTRET_M3U8_RE.findall(player_html or "")
    urls = []
    for url in candidates:
        if not url.startswith(("http://", "https://")):
            continue
        if url.rstrip("/").endswith("/error.m3u8"):
            continue
        if url not in urls:
            urls.append(url)
    urls.sort(key=lambda u: 0 if ".m3u8" in u else 1)
    return urls


def resolve_smotret_channel(page_url: str, timeout: float = 12.0) -> dict:
    """Channel page -> player iframe -> HLS URLs. Returns {ok, streams, player, error}."""
    if not page_url.startswith(("http://", "https://")):
        return {"ok": False, "streams": [], "player": "", "error": "invalid channel page"}
    page = http_get(page_url, referrer=SMOTRET_REFERRER, timeout=timeout, max_bytes=2 * 1024 * 1024)
    if not page["ok"]:
        return {"ok": False, "streams": [], "player": "", "error": page["error"]}
    frame = _SMOTRET_IFRAME_RE.search(page["body"].decode("utf-8", errors="ignore"))
    if not frame:
        return {"ok": False, "streams": [], "player": "", "error": "no player iframe on channel page"}
    player_url = urllib.parse.urljoin(page_url, frame.group(1))
    player = http_get(player_url, referrer=page_url, timeout=timeout, max_bytes=1024 * 1024)
    if not player["ok"]:
        return {"ok": False, "streams": [], "player": player_url, "error": player["error"]}
    streams = parse_smotret_streams(player["body"].decode("utf-8", errors="ignore"))
    return {
        "ok": bool(streams),
        "streams": streams,
        "player": player_url,
        "error": "" if streams else "embed-only channel (no HLS in player page)",
    }


def fetch_smotret_channels(timeout: float = 12.0, workers: int = 8, max_resolve: int = 0,
                           cache_path: str = "", cache_ttl: int = SMOTRET_CACHE_TTL) -> dict:
    """
    Every channel in smotret.tv's ten categories, resolved to its HLS URL.

    Two requests per channel (channel page -> player page) the first time; the
    resolved URLs are cached per channel id, so a later sync only fetches the
    ten category pages until entries expire (cache_ttl, default 24 h).
    max_resolve > 0 caps how many channels a single run resolves.
    Returns {ok, status, channels, error, discovered, resolved, embed_only, pending}.
    """
    cache_file = cache_path or SMOTRET_CACHE_FILE
    discovered = {}
    listing_errors = []
    status = 0
    for slug, label in SMOTRET_CATEGORIES:
        url = SMOTRET_BASE + (f"/{slug}/" if slug else "/")
        res = http_get(url, referrer=SMOTRET_REFERRER, timeout=timeout, max_bytes=2 * 1024 * 1024)
        status = status or res["status"]
        if not res["ok"] or _is_cloudflare_challenge(res):
            listing_errors.append(f"{label}: {res['error'] or 'challenge'}")
            continue
        for channel in parse_smotret_listing(res["body"].decode("utf-8", errors="ignore"), label):
            known = discovered.get(channel["id"])
            if known is None:
                discovered[channel["id"]] = channel
            elif known["category"] == "General" and channel["category"] != "General":
                known["category"] = channel["category"]
                known["logo"] = known["logo"] or channel["logo"]
    if not discovered:
        return {"ok": False, "status": status, "channels": [], "discovered": 0, "resolved": 0,
                "embed_only": 0, "pending": 0,
                "error": "; ".join(listing_errors) or "no channel cards matched (page layout changed?)"}

    cache = _smotret_load_cache(cache_file)
    now = int(time.time())
    todo = []
    for channel in discovered.values():
        entry = cache.get(channel["id"]) or {}
        if entry and now - int(entry.get("ts") or 0) < cache_ttl:
            continue
        todo.append(channel)
    if max_resolve and max_resolve > 0:
        todo = todo[:max_resolve]

    resolved_now = 0
    if todo:
        pool_size = max(1, min(int(workers or 1), 16))
        with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as pool:
            futures = {pool.submit(resolve_smotret_channel, ch["page"], timeout): ch for ch in todo}
            for future in concurrent.futures.as_completed(futures):
                channel = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"ok": False, "streams": [], "player": "", "error": str(exc)}
                base = {"name": channel["name"], "category": channel["category"],
                        "logo": channel["logo"], "ts": now}
                if result["ok"]:
                    base.update({"url": result["streams"][0], "streams": result["streams"][:4],
                                 "player": result["player"]})
                    resolved_now += 1
                elif result.get("player"):
                    base["embed"] = True
                    base["player"] = result["player"]
                else:
                    continue
                cache[channel["id"]] = base
        _smotret_save_cache(cache_file, cache)

    channels = []
    embed_only = 0
    pending = 0
    for channel in discovered.values():
        entry = cache.get(channel["id"]) or {}
        url = entry.get("url") or ""
        if url.startswith(("http://", "https://")):
            channels.append({
                "name": entry.get("name") or channel["name"],
                "url": url,
                "category": entry.get("category") or channel["category"] or "Live TV",
                "logo": entry.get("logo") or channel["logo"],
                "referrer": entry.get("player") or SMOTRET_REFERRER,
                "user_agent": BROWSER_UA,
                "quality": "HD",
            })
        elif entry.get("embed"):
            embed_only += 1
        else:
            pending += 1
    channels.sort(key=lambda ch: ch["name"].lower())
    return {
        "ok": bool(channels),
        "status": status,
        "channels": channels,
        "error": "" if channels else ("; ".join(listing_errors) or "no HLS channels resolved yet"),
        "discovered": len(discovered),
        "resolved": resolved_now,
        "embed_only": embed_only,
        "pending": pending,
    }


# ---------------------------------------------------------------------------
# smotret.tv name search - the bulk search needs "does this channel exist"
# without downloading and resolving all 700 cards, so the ten listing pages
# are cached as an index and only the matched channel page is resolved.
# ---------------------------------------------------------------------------

SMOTRET_INDEX_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "smotret_index_cache.json"
)
SMOTRET_RESOLVE_TTL = 6 * 3600

_SMOTRET_QUOTES_RE = re.compile(r"[\"'`«»„“”]")
_SMOTRET_NONWORD_RE = re.compile(r"[^0-9a-zа-яё]+")

# Card names use digits ("1 канал") while people search in words ("Первый канал").
_SMOTRET_NUMERALS = {
    "первый": "1", "первая": "1", "второй": "2", "третий": "3", "четвертый": "4",
    "пятый": "5", "шестой": "6", "седьмой": "7", "восьмой": "8", "девятый": "9",
    "десятый": "10", "одиннадцатый": "11", "двенадцатый": "12",
}


def _smotret_norm(text: str) -> str:
    s = (text or "").lower().replace("ё", "е")
    s = _SMOTRET_NONWORD_RE.sub(" ", s)
    words = [_SMOTRET_NUMERALS.get(word, word) for word in s.split()]
    return re.sub(r"\s+", " ", " ".join(words)).strip()


def _smotret_index_load(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return []
    items = data.get("channels") if isinstance(data, dict) else None
    return [c for c in items or [] if isinstance(c, dict) and c.get("name")]


def _smotret_index_save(path: str, channels: list) -> None:
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"v": 1, "saved_at": int(time.time()), "channels": channels}, handle,
                      ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def smotret_channel_index(timeout: float = 12.0, cache_path: str = "",
                          ttl: int = SMOTRET_CACHE_TTL, refresh: bool = False) -> list:
    """
    Every card from the ten smotret.tv category pages: [{id, name, page, logo, category}].

    Cached for ``ttl`` seconds so bulk search costs one file read; ``refresh``
    forces ten fresh listing requests.
    """
    index_file = cache_path or SMOTRET_INDEX_FILE
    cached = _smotret_index_load(index_file)
    fresh = False
    try:
        with open(index_file, "r", encoding="utf-8") as handle:
            fresh = int(json.load(handle).get("saved_at") or 0) + ttl > time.time()
    except Exception:
        fresh = False
    if cached and fresh and not refresh:
        return cached

    discovered = {}
    for slug, label in SMOTRET_CATEGORIES:
        url = SMOTRET_BASE + (f"/{slug}/" if slug else "/")
        res = http_get(url, referrer=SMOTRET_REFERRER, timeout=timeout, max_bytes=2 * 1024 * 1024)
        if not res["ok"] or _is_cloudflare_challenge(res):
            continue
        for channel in parse_smotret_listing(res["body"].decode("utf-8", errors="ignore"), label):
            known = discovered.get(channel["id"])
            if known is None:
                discovered[channel["id"]] = channel
            elif known["category"] == "General" and channel["category"] != "General":
                known["category"] = channel["category"]
                known["logo"] = known["logo"] or channel["logo"]
    if discovered:
        channels = sorted(discovered.values(), key=lambda c: c["name"].lower())
        _smotret_index_save(index_file, channels)
        return channels
    return cached


def _smotret_score(query: str, name: str) -> float:
    """0..1 name similarity that tolerates the channel-card decorations."""
    q = _smotret_norm(query)
    n = _smotret_norm(_SMOTRET_QUOTES_RE.sub(" ", name))
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    alts = {n}
    for suffix in (" hd", " sd", " fhd", " uhd", " 4k", " tv", " online"):
        if n.endswith(suffix):
            alts.add(n[: -len(suffix)].strip())
    if q in alts:
        return 1.0
    if any(a.startswith(q) or q.startswith(a) for a in alts):
        return 0.9
    if any(q in a for a in alts):
        return 0.85
    q_tokens, n_tokens = set(q.split()), set(n.split())
    if q_tokens and q_tokens <= n_tokens:
        return 0.82
    overlap = len(q_tokens & n_tokens) / max(1, len(q_tokens))
    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    return max(round(overlap * 0.8, 3), round(ratio * 0.8, 3))


def search_smotret_channels(query: str, limit: int = 4, timeout: float = 12.0,
                            cache_path: str = "", min_score: float = 0.6) -> list:
    """Best name matches from the smotret.tv index, each with a ``score``."""
    if not (query or "").strip():
        return []
    ranked = []
    for channel in smotret_channel_index(timeout=timeout, cache_path=cache_path):
        score = _smotret_score(query, channel.get("name", ""))
        if score >= min_score:
            ranked.append(dict(channel, score=score))
    ranked.sort(key=lambda c: (-c["score"], c["name"].lower()))
    return ranked[:limit]


def resolve_smotret_matches(query: str, limit: int = 2, timeout: float = 12.0,
                            min_score: float = 0.82, cache_path: str = "",
                            resolve_ttl: int = SMOTRET_RESOLVE_TTL) -> list:
    """
    Name -> playable HLS: the matched channel pages only, cached per channel id.

    Returns [{name, url, streams, page, player, logo, category, score, embed}].
    The player page is the referrer some edges require, so it travels with the URL.
    """
    cache_file = cache_path or SMOTRET_CACHE_FILE
    cache = _smotret_load_cache(cache_file)
    now = int(time.time())
    out = []
    changed = False
    for channel in search_smotret_channels(query, limit=limit, timeout=timeout,
                                           cache_path=cache_path, min_score=min_score):
        entry = cache.get(channel["id"]) or {}
        url = entry.get("url") or ""
        if entry.get("embed") and entry.get("player"):
            url = entry["player"]
        if not (url.startswith("http") and now - int(entry.get("ts") or 0) < resolve_ttl):
            result = resolve_smotret_channel(channel["page"], timeout=timeout)
            base = {"name": channel["name"], "category": channel.get("category", "Live TV"),
                    "logo": channel.get("logo", ""), "ts": now}
            if result["ok"]:
                base.update({"url": result["streams"][0], "streams": result["streams"][:4],
                             "player": result["player"]})
            elif result.get("player"):
                base.update({"embed": True, "player": result["player"]})
            else:
                continue
            cache[channel["id"]] = base
            changed = True
            entry = base
            url = base.get("url") or base.get("player") or ""
        if not url:
            continue
        out.append({
            "name": channel["name"],
            "url": url,
            "streams": entry.get("streams") or ([] if entry.get("embed") else [url]),
            "page": channel["page"],
            "player": entry.get("player") or SMOTRET_REFERRER,
            "logo": entry.get("logo") or channel.get("logo", ""),
            "category": entry.get("category") or channel.get("category", "Live TV"),
            "embed": bool(entry.get("embed")),
            "score": channel["score"],
        })
    if changed:
        _smotret_save_cache(cache_file, cache)
    return out


# ---------------------------------------------------------------------------
# televizo.live - channel page -> player iframe -> signed HLS URL
# The channel page only frames a player, and that player mints a token that
# lives from about one to five hours. A resolved URL is therefore a snapshot:
# callers cache it briefly and resolve again once it expires.
# ---------------------------------------------------------------------------

TELEVIZO_REFERRER = "https://televizo.live/"

TELEVIZO_AD_MARKERS = (
    "bl.rutube.ru",
    "rtbcdn.ru",
    "adfinity",
    "/hls-vod/",
    "rutube.ru/play/slim",
    "yandex.ru/ads",
    "ads.",
)

_TELEVIZO_IFRAME_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)', re.I)
_TELEVIZO_FILE_RE = re.compile(
    r'["\'](?:file|source|src|hls|url)["\']\s*[:=]\s*["\']([^"\']+\.m3u8[^"\']*)', re.I
)
_TELEVIZO_M3U8_RE = re.compile(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*')


def parse_televizo_players(page_html: str) -> list:
    """Player iframe URLs from a televizo.live channel page, in page order."""
    out = []
    for raw in _TELEVIZO_IFRAME_RE.findall(page_html or ""):
        url = (raw or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http") and url not in out:
            out.append(url)
    return out


def parse_televizo_streams(player_html: str) -> list:
    """Signed HLS URLs a televizo player page declares, longest lived first."""
    found = []
    for raw in _TELEVIZO_FILE_RE.findall(player_html or "") + _TELEVIZO_M3U8_RE.findall(player_html or ""):
        url = (raw or "").strip().replace("\\/", "/")
        if not url.startswith("http") or url in found:
            continue
        low = url.lower()
        if any(marker in low for marker in TELEVIZO_AD_MARKERS):
            continue
        found.append(url)
    # The path form (/<nonce>/<ch>/<exp>/index.m3u8) outlives the query form.
    found.sort(key=lambda u: (0 if "?" not in u else 1, u))
    return found


def resolve_televizo_channel(page_url: str, timeout: float = 12.0) -> dict:
    """
    Channel page -> player iframe -> HLS, without a browser.

    Providers that only answer a real browser (403 to plain HTTP) report
    ``browser_required`` so callers can fall back to Playwright where it exists.
    Returns {ok, streams, player, referrer, error, browser_required}.
    """
    if not page_url.startswith(("http://", "https://")):
        return {"ok": False, "streams": [], "player": "", "referrer": TELEVIZO_REFERRER,
                "error": "invalid channel page", "browser_required": False}
    page = http_get(page_url, referrer=TELEVIZO_REFERRER, timeout=timeout,
                    max_bytes=2 * 1024 * 1024)
    if not page["ok"]:
        return {"ok": False, "streams": [], "player": "", "referrer": TELEVIZO_REFERRER,
                "error": page["error"], "browser_required": page["status"] in (403, 429, 503)}

    html_text = page["body"].decode("utf-8", errors="ignore")
    streams = parse_televizo_streams(html_text)
    player_url = ""
    for candidate in parse_televizo_players(html_text):
        player_url = urllib.parse.urljoin(page_url, candidate)
        player = http_get(player_url, referrer=page_url, timeout=timeout,
                          max_bytes=1024 * 1024)
        if not player["ok"]:
            if player["status"] in (403, 429, 503):
                return {"ok": False, "streams": [], "player": player_url, "referrer": page_url,
                        "error": f"player page {player['error']}", "browser_required": True}
            continue
        streams.extend(parse_televizo_streams(player["body"].decode("utf-8", errors="ignore")))
        if streams:
            break

    unique = []
    for url in streams:
        if url not in unique:
            unique.append(url)
    return {
        "ok": bool(unique),
        "streams": unique[:4],
        "player": player_url,
        "referrer": player_url or page_url,
        "error": "" if unique else "no HLS in channel page or player",
        "browser_required": False,
    }


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
