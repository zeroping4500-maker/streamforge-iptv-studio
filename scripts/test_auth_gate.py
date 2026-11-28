#!/usr/bin/env python3
"""Regression test for the global auth gate (default-deny) on a real server."""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

os.environ["DATABASE_URL"] = "postgresql://ubuntu:ubuntu@127.0.0.1:5432/app"
os.environ["NEON_AUTH_BASE_URL"] = "http://127.0.0.1:3100"
os.environ["NEON_AUTH_JWKS_URL"] = "http://127.0.0.1:3100/jwks.json"
os.environ["NEON_AUTH_APP_NAME"] = "gate-test"
os.environ["PORT"] = "3210"
os.environ.pop("NEON_AUTH_ENABLED", None)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
BASE = "http://127.0.0.1:3210"
results = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def req(method, path, headers=None, cookie=None, origin=None):
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    if origin:
        h["Origin"] = origin
    r = urllib.request.Request(BASE + path, data=b"{}" if method == "POST" else None, headers=h, method=method)
    if method == "POST":
        h.setdefault("Content-Type", "application/json")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read(200)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(200)


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail) if detail and not ok else ""))


server = subprocess.Popen(
    [sys.executable, "app.py"], cwd=REPO,
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
)
try:
    # wait for boot
    for _ in range(60):
        try:
            req("GET", "/api/health")
            break
        except Exception:
            time.sleep(0.5)

    import auth, db as store
    identity = {"user_id": "gate-test-user", "email": "gatetest@zemy.test", "name": "Gate Test"}
    with store.get_db() as conn:
        conn.execute('CREATE SCHEMA IF NOT EXISTS neon_auth')
        conn.execute('''CREATE TABLE IF NOT EXISTS neon_auth."user" (
            id text PRIMARY KEY, email text, name text, role text, banned boolean)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS neon_auth.session (
            id text PRIMARY KEY, "userId" text, "expiresAt" timestamptz)''')
        conn.execute(
            "INSERT INTO admin_users (user_id, email, role, created_at) VALUES (?, ?, 'owner', ?) "
            "ON CONFLICT (user_id) DO NOTHING",
            (identity["user_id"], identity["email"], int(time.time())),
        )
        conn.execute(
            'INSERT INTO neon_auth."user" (id, email, name, role, banned) VALUES (?, ?, ?, NULL, false) '
            "ON CONFLICT (id) DO NOTHING",
            (identity["user_id"], identity["email"], identity["name"]),
        )
    sid, expires = auth.create_session(identity, "127.0.0.1", "gate-test")
    cookie = f"zemy_admin={sid}"

    # --- anonymous ---
    st, hd, _ = req("GET", "/")
    check("anon GET / -> 302 /admin", st == 302 and hd.get("Location") == "/admin", (st, hd.get("Location")))
    st, hd, body = req("GET", "/api/playlists")
    check("anon GET /api/playlists -> 401", st == 401 and b"not_authenticated" in body, (st, body[:80]))
    st, _, _ = req("GET", "/m3u/x.m3u")
    check("anon GET /m3u/x.m3u -> 401", st == 401, st)
    st, _, _ = req("GET", "/watch?u=https%3A%2F%2Fthedaddy.li%2Fx")
    check("anon GET /watch -> 302 /admin", st == 302, st)
    st, _, _ = req("GET", "/api/health")
    check("anon GET /api/health -> 200", st == 200, st)
    st, _, _ = req("GET", "/admin")
    check("anon GET /admin -> 200", st == 200, st)
    st, _, _ = req("GET", "/app.js")
    check("anon GET /app.js -> 200", st == 200, st)
    st, _, _ = req("GET", "/secret.env")
    check("anon GET /secret.env blocked", st in (302, 404), st)
    st, _, _ = req("HEAD", "/api/playlists")
    check("anon HEAD /api/playlists -> 401", st == 401, st)
    st, hd, _ = req("GET", "/admin")
    check("security headers on /admin", hd.get("X-Content-Type-Options") == "nosniff"
          and hd.get("X-Frame-Options") == "DENY"
          and "frame-ancestors 'none'" in (hd.get("Content-Security-Policy") or ""), {k: v for k, v in hd.items() if k.lower() in ("x-frame-options", "content-security-policy")})
    st, _, body = req("POST", "/api/sources/bulk-search", headers={"X-Requested-With": "zemy-admin"})
    check("anon POST bulk-search -> 401", st == 401, (st, body[:80]))

    # --- authenticated as admin ---
    st, _, _ = req("GET", "/api/playlists", cookie=cookie)
    check("admin GET /api/playlists -> 200", st == 200, st)
    st, _, _ = req("GET", "/", cookie=cookie)
    check("admin GET / -> 200", st == 200, st)
    st, _, body = req("POST", "/api/sources/bulk-search", cookie=cookie, headers={"X-Requested-With": "zemy-admin"})
    check("admin POST bulk-search + marker accepted", st != 403 and st != 401, (st, body[:80]))
    st, _, body = req("POST", "/api/sources/bulk-search", cookie=cookie)
    check("admin POST without marker -> 403", st == 403 and b"cross_origin" in body, (st, body[:80]))
    st, _, body = req("POST", "/api/sources/bulk-search", cookie=cookie,
                      headers={"X-Requested-With": "zemy-admin", "Origin": "https://evil.example"})
    check("admin POST foreign Origin -> 403", st == 403, (st, body[:80]))

    # --- SSRF guard ---
    st, _, body = req("GET", "/api/proxy-stream?url=" + urllib.parse.quote("http://127.0.0.1:5432/master.m3u8"), cookie=cookie)
    check("proxy to loopback -> 400", st == 400 and b"non-public" in body, (st, body[:80]))
    st, _, body = req("GET", "/api/proxy-stream?url=" + urllib.parse.quote("http://169.254.169.254/latest/meta-data/"), cookie=cookie)
    check("proxy to cloud metadata IP -> 400", st == 400, (st, body[:80]))
    st, _, body = req("GET", "/api/proxy-stream?url=" + urllib.parse.quote("http://10.0.0.1/x.m3u8"), cookie=cookie)
    check("proxy to RFC1918 -> 400", st == 400, (st, body[:80]))
    st, _, body = req("GET", "/api/proxy-stream?url=" + urllib.parse.quote("https://raw.githubusercontent.com/some/repo/master/not-a-stream.m3u8"), cookie=cookie)
    check("proxy to public host not blocked by guard", st != 400, (st, body[:80]))

    # --- auth proxy endpoints (no Neon Auth host exposed, routes wired) ---
    st, _, body = req("GET", "/api/auth/config")
    cfg = json.loads(body.decode("utf-8") or "{}")
    check("auth/config hides baseUrl", st == 200 and "baseUrl" not in cfg and "neonauth" not in body.decode("utf-8"), (st, body[:120]))
    st, _, body = req("POST", "/api/auth/otp/send", headers={"X-Requested-With": "zemy-admin"})
    check("auth/otp/send invalid email -> 400", st == 400 and b"invalid_email" in body, (st, body[:80]))
    st, _, body = req("POST", "/api/auth/otp/send", headers={"X-Requested-With": "zemy-admin", "Content-Type": "application/json"})
    check("auth/otp/send no email -> 400", st == 400, (st, body[:80]))
    st, _, body = req("POST", "/api/auth/otp/verify", headers={"X-Requested-With": "zemy-admin"})
    check("auth/otp/verify routed (not 404)", st in (400, 401, 503) and st != 404, (st, body[:80]))
    st, _, body = req("POST", "/api/auth/google/start", headers={"X-Requested-With": "zemy-admin"})
    check("auth/google/start routed (not 404)", st != 404, (st, body[:80]))
    st, hd, _ = req("GET", "/api/auth/resume")
    check("auth/resume -> 302 to configured base", st == 302 and (hd.get("Location") or "").startswith("http://127.0.0.1:3100/"), (st, hd.get("Location")))
    st, _, body = req("POST", "/api/auth/nonsense", headers={"X-Requested-With": "zemy-admin"})
    check("auth unknown endpoint -> 404", st == 404, (st, body[:80]))

    # --- admin panel must not render its shell before sign-in ---
    st, _, _ = req("GET", "/admin.css")
    with open(os.path.join(REPO, "static", "admin.css"), encoding="utf-8") as fh:
        css = " ".join(fh.read().split())
    check("admin.css forces [hidden] display:none", st == 200 and "[hidden] { display: none !important; }" in css, st)

    # cleanup
    auth.delete_session(sid)
    with store.get_db() as conn:
        conn.execute("DELETE FROM admin_users WHERE user_id = ?", (identity["user_id"],))
    # --- shared playlist links: an IPTV player has no admin cookie ---
    key_ok = auth.share_token("pl-1")
    st, _, _ = req("GET", "/m3u/pl-1.m3u")
    check("playlist link without key is refused", st == 401, st)
    st, _, _ = req("GET", "/m3u/pl-1.m3u?key=" + key_ok)
    check("playlist link with its key is not refused", st != 401, st)
    st, _, _ = req("GET", "/m3u/pl-1.m3u?key=wrong")
    check("playlist link with a wrong key is refused", st == 401, st)
    st, _, _ = req("GET", "/api/stream/ch-1/live.m3u8?key=" + auth.share_token("ch-1"))
    check("failover stream with its key is not refused", st != 401, st)
    st, _, _ = req("GET", "/api/stream/ch-1/live.m3u8?key=" + auth.share_token("other"))
    check("failover stream with another channel's key is refused", st == 401, st)

finally:
    server.terminate()
    server.wait(timeout=10)

# --- channel liveness: HTTP 200 is not proof a channel plays ---
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LIVE_MEDIA = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg.ts\n"
_TS_PACKET = b"\x47" + b"\x00" * 187


class _StreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/live.m3u8":
            body, ctype = _LIVE_MEDIA, "application/vnd.apple.mpegurl"
        elif path == "/ended.m3u8":
            body, ctype = _LIVE_MEDIA + b"#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl"
        elif path == "/htmseg.m3u8":
            body, ctype = _LIVE_MEDIA.replace(b"seg.ts", b"bad.ts"), "application/vnd.apple.mpegurl"
        elif path == "/seg.ts":
            body, ctype = _TS_PACKET, "video/mp2t"
        else:  # a segment that answers 200 with an HTML error page
            body, ctype = b"<html><body>blocked</body></html>", "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_streams = ThreadingHTTPServer(("127.0.0.1", 0), _StreamHandler)
threading.Thread(target=_streams.serve_forever, daemon=True).start()
_stream_base = f"http://127.0.0.1:{_streams.server_address[1]}"
try:
    import app as app_module

    result = app_module.probe_stream({"id": "live", "url": _stream_base + "/live.m3u8"})
    check("probe: HLS with a real TS segment is online", result["status"] == "online", result)
    result = app_module.probe_stream({"id": "ended", "url": _stream_base + "/ended.m3u8"})
    check("probe: EXT-X-ENDLIST playlist is offline", result["status"] == "offline" and "ENDLIST" in result["error"], result)
    result = app_module.probe_stream({"id": "html", "url": _stream_base + "/htmseg.m3u8"})
    check("probe: HTML instead of a media segment is offline", result["status"] == "offline", result)
    result = app_module.probe_stream({"id": "expired", "url": _stream_base + "/live.m3u8?exp=" + str(int(time.time()) - 60)})
    check("probe: expired signed link is offline", result["status"] == "offline" and "expired" in result["error"].lower(), result)
    result = app_module.probe_stream({"id": "future", "url": _stream_base + "/live.m3u8?exp=" + str(int(time.time()) + 3600)})
    check("probe: signed link with time left is online", result["status"] == "online", result)
finally:
    _streams.shutdown()

import liveness

check(
    "liveness: signed online goes unknown when the probe is old",
    liveness.effective_status("online", "http://h/a.m3u8", checked_at=int(time.time()) - 9 * 3600)[0] == "unknown",
)
check(
    "liveness: expired signature is offline whatever the stored status",
    liveness.effective_status("online", "http://h/a.m3u8?expire=%d" % (int(time.time()) - 5), checked_at=int(time.time()))[0] == "offline",
)
check(
    "liveness: hdnts expiry is understood",
    liveness.token_expiry("http://h/a.m3u8?hdnts=st=1~exp=1900000000~acl=%2f*") == 1900000000,
)

import source_importers as ext

check(
    "liveness: wmsAuthSign expiry is understood",
    liveness.token_expiry("http://h/157/index.m3u8?wmsAuthSign=abc-1900000000-321") == 1900000000,
)
check(
    "liveness: path-token expiry is understood",
    liveness.token_expiry("http://h/nonce/157/1900000000/index.m3u8") == 1900000000,
)
check(
    "liveness: plain path numbers are not mistaken for expiry",
    liveness.token_expiry("http://h/live/1234/index.m3u8") == 0,
)
check(
    "televizo: player iframe is found",
    ext.parse_televizo_players('<iframe src="//cdn.example/p.php?sp=2"></iframe>') == ["https://cdn.example/p.php?sp=2"],
)
televizo_player_html = (
    'var p={file:"https://tvcdnpotok.com/157/index.m3u8?wmsAuthSign=a-1900000000-1",'
    'source:"https://bl.rutube.ru/route/x.m3u8"};'
)
check(
    "televizo: signed HLS is parsed and ad edges are dropped",
    ext.parse_televizo_streams(televizo_player_html) == ["https://tvcdnpotok.com/157/index.m3u8?wmsAuthSign=a-1900000000-1"],
)

_real_http_get = ext.http_get


def _fake_page_only(url, referrer="", **kwargs):
    if "player" in url:
        return {"ok": False, "status": 403, "body": b"", "error": "HTTP 403 Forbidden"}
    return {"ok": True, "status": 200, "body": b'<iframe src="https://player.test/p.php"></iframe>', "error": ""}


ext.http_get = _fake_page_only
try:
    res = ext.resolve_televizo_channel("https://televizo.live/channel.html")
finally:
    ext.http_get = _real_http_get
check(
    "televizo: browser-only player is reported instead of crashing",
    res["ok"] is False and res["browser_required"] is True,
    res,
)


def _fake_full(url, referrer="", **kwargs):
    if "player" in url:
        return {"ok": True, "status": 200, "body": televizo_player_html.encode(), "error": ""}
    return {"ok": True, "status": 200, "body": b'<iframe src="https://player.test/p.php"></iframe>', "error": ""}


ext.http_get = _fake_full
try:
    res = ext.resolve_televizo_channel("https://televizo.live/channel.html")
finally:
    ext.http_get = _real_http_get
check(
    "televizo: resolver returns the signed HLS over plain HTTP",
    res["ok"] and res["streams"][0].startswith("https://tvcdnpotok.com/157/"),
    res,
)

_real_resolve_tv = ext.resolve_televizo_channel
ext.resolve_televizo_channel = lambda page, timeout=12: {
    "ok": True, "streams": ["http://127.0.0.1:9/x.m3u8"], "player": "https://player.test/p.php",
    "referrer": "https://player.test/p.php", "error": "", "browser_required": False,
}
try:
    res = app_module.extract_televizo_stream_sync("https://televizo.live/channel.html", timeout_sec=5)
finally:
    ext.resolve_televizo_channel = _real_resolve_tv
check(
    "televizo: sync extraction works without Chromium",
    res.get("found") is True and res.get("via") == "http",
    res,
)

# An expired televizo line must not be dropped from the export: its channel still
# exists, so the line points at the endpoint that re-resolves the token.
expired_tv = "https://tvcdnpotok.com/157/index.m3u8?wmsAuthSign=a-%d-1" % (int(time.time()) - 60)
expired_plain = "https://other.invalid/x.m3u8?exp=%d" % (int(time.time()) - 60)
export = app_module.build_m3u_export(
    {"name": "Test", "id": "pl1"},
    [
        {"id": "c1", "name": "Россия 1", "url": expired_tv, "tvg_id": "ru1", "tvg_name": "Россия 1", "logo": ""},
        {"id": "c2", "name": "Zeta Not A Televizo Channel", "url": expired_plain, "tvg_id": "x1", "tvg_name": "Zeta", "logo": ""},
    ],
    smart_failover=False,
    base_url="https://zemyiptv.onrender.com",
)
check(
    "export: expired televizo line becomes the self-refreshing endpoint",
    "/api/stream/c1/live.m3u8?key=" in export,
    export,
)
check(
    "export: an expired non-televizo line is still left out",
    "other.invalid" not in export,
    export,
)

check("smotret: Russian numeral matches the card name", ext._smotret_score("Первый канал", "1 канал") == 1.0)

_real_resolve_sm = ext.resolve_smotret_matches
ext.resolve_smotret_matches = lambda q, limit=2, **kw: [{
    "name": "Россия 1", "url": "https://stream.test/rossiya1/index.m3u8",
    "streams": ["https://stream.test/rossiya1/index.m3u8"],
    "page": "https://smotret.tv/rossiya1", "player": "https://smotret.tv/live/rossiya_1.html",
    "logo": "", "category": "Общие", "embed": False, "score": 1.0,
}]
try:
    hits = app_module.search_smotret_live_streams_fast("Россия 1", check_live=False)
    found = app_module.find_stream_sources_by_name(
        "Россия 1", check_live=False, include_web=False, include_yt=False,
        include_rutube=False, include_televizo=False, include_openindex=False,
        include_smotret=True, strict=False,
    )
finally:
    ext.resolve_smotret_matches = _real_resolve_sm
check(
    "smotret: candidate keeps the player page as referrer",
    bool(hits) and hits[0]["referrer"] == "https://smotret.tv/live/rossiya_1.html",
    hits,
)
check(
    "smotret: bulk source search includes the RU catalogue",
    any(c.get("source") == "📺 Smotret.tv" for c in found),
    found,
)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
