"""Truthful channel liveness.

A stream that answers HTTP 200 is not a channel that plays. Two things make a
"live" badge lie in practice:

* the URL is a short-lived signed link (``exp=``/``expire=``/``expires=``/
  ``hdnts=``/JWT ``token=``) that was valid when it was probed and is dead by
  the time a player opens the playlist;
* the last probe is hours old, so "online" only describes the past.

This module turns those two facts into a status a viewer can trust, plus the
media sniffing the probe needs to tell a real segment from an HTML error page.
"""

import base64
import json
import time
import urllib.parse

# How long a probe result may claim "online". Signed links rotate within
# minutes, plain ones are stable for much longer.
FRESH_SIGNED = 20 * 60
FRESH_UNSIGNED = 6 * 3600

EXPIRY_KEYS = ("exp", "expires", "expire", "expiry", "expiration", "valid_until", "end", "e")


def _epoch(value):
    try:
        num = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
    if num > 10**11:  # milliseconds
        num //= 1000
    if num < 946684800 or num > 4102444800:  # < 2000 or > 2100: not a timestamp
        return 0
    return num


def _jwt_exp(token):
    parts = (token or "").split(".")
    if len(parts) < 2:
        return 0
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
    except Exception:
        return 0
    return _epoch(claims.get("exp")) if isinstance(claims, dict) else 0


def token_expiry(url):
    """Epoch second at which the URL's signature stops working, or 0 when it has none."""
    try:
        parsed = urllib.parse.urlparse(url or "")
    except Exception:
        return 0
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    found = []
    for key, value in params:
        key_l = key.lower()
        if key_l in EXPIRY_KEYS:
            found.append(_epoch(value))
        elif key_l == "hdnts":
            for part in value.replace("~", "&").split("&"):
                name, _, raw = part.partition("=")
                if name.strip().lower() in EXPIRY_KEYS:
                    found.append(_epoch(raw))
        elif key_l in ("token", "jwt", "auth"):
            found.append(_jwt_exp(value))
        elif key_l == "wmsauthsign":
            # Flussonic/nginx secure link: <hash>-<expiry epoch>-<policy>
            for part in value.split("-"):
                if part.isdigit():
                    found.append(_epoch(part))
    # Path tokens such as /<nonce>/<channel>/<expiry epoch>/index.m3u8
    for segment in parsed.path.split("/"):
        if segment.isdigit() and len(segment) == 10:
            found.append(_epoch(segment))
    found = [f for f in found if f]
    return max(found) if found else 0


def is_signed(url):
    return token_expiry(url) > 0 or any(
        marker in (url or "").lower()
        for marker in ("sign=", "sig=", "token=", "hdnts=", "policy=", "key-pair-id=", "wmsauthsign=")
    )


def effective_status(status, url="", checked_at=0, now=None):
    """Return (status, note) as a player would experience it right now.

    ``online`` only survives while the link is unexpired and the probe is fresh;
    anything older becomes ``unknown`` so the panel never promises a dead stream.
    """
    now = int(now or time.time())
    status = (status or "unknown").lower()
    expiry = token_expiry(url)
    if expiry and expiry <= now:
        return "offline", "link expired"
    if status != "online":
        return status, ""
    checked_at = int(checked_at or 0)
    if not checked_at:
        return "unknown", "never verified"
    age = max(0, now - checked_at)
    limit = FRESH_SIGNED if is_signed(url) else FRESH_UNSIGNED
    if age > limit:
        if expiry and expiry - now < FRESH_SIGNED:
            return "offline", "link expired"
        return "unknown", f"last checked {_age_label(age)} ago"
    if expiry and expiry - now < FRESH_SIGNED:
        return "warning", "link expires in minutes"
    return "online", ""


def _age_label(seconds):
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def looks_like_media(chunk, content_type=""):
    """True when the first bytes are real transport-stream / fMP4 / audio data."""
    ct = (content_type or "").lower()
    if any(kind in ct for kind in ("video/", "audio/", "mp2t", "mpegurl", "dash+xml", "octet-stream")):
        return True
    data = chunk or b""
    if not data:
        return False
    if data[0] == 0x47:  # MPEG-TS sync byte
        return True
    if len(data) >= 8 and data[4:8] in (b"ftyp", b"styp", b"moof", b"moov", b"mdat", b"sidx"):
        return True
    if data[:3] == b"ID3":
        return True
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0:
        return True
    return False


def playlist_is_finished(text):
    """A live playlist never carries #EXT-X-ENDLIST; a finished VOD does."""
    return "#EXT-X-ENDLIST" in (text or "")


def media_playlist_segments(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip() and not line.strip().startswith("#")]
