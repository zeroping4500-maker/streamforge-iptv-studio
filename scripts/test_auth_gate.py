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

    # cleanup
    auth.delete_session(sid)
    with store.get_db() as conn:
        conn.execute("DELETE FROM admin_users WHERE user_id = ?", (identity["user_id"],))
finally:
    server.terminate()
    server.wait(timeout=10)

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
