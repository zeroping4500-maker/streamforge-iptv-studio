"""Admin panel API: authentication, authorisation and the data it manages.

Everything under /api/admin requires a valid admin session (HttpOnly cookie) or
a verified Neon Auth bearer token. Mutating calls additionally require the
same-origin check and the X-Requested-With marker, so a cross-site page cannot
ride a logged-in admin's cookie.
"""

import json
import threading
import time
import urllib.parse

import auth
import db

RATE_LOCK = threading.Lock()
RATE_STATE = {}


def _client_ip(handler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (handler.client_address[0] if handler.client_address else "")[:64]


def _send(handler, payload, status=200, cookies=None):
    raw = json.dumps(payload).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "same-origin")
        for cookie in cookies or []:
            handler.send_header("Set-Cookie", cookie)
        handler.end_headers()
        handler.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _error(handler, status, code, message):
    _send(handler, {"error": message, "code": code}, status)


def _cookie_value(handler, name):
    raw = handler.headers.get("Cookie") or ""
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def _is_https(handler) -> bool:
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return proto == "https"


def _cookie_header(sid, expires, handler) -> str:
    parts = [
        f"{auth.COOKIE_NAME}={sid}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        f"Max-Age={max(0, int(expires - time.time()))}",
    ]
    if _is_https(handler):
        parts.append("Secure")
    return "; ".join(parts)


def _clear_cookie(handler) -> str:
    parts = [f"{auth.COOKIE_NAME}=", "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=0"]
    if _is_https(handler):
        parts.append("Secure")
    return "; ".join(parts)


def _same_origin(handler) -> bool:
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return True  # non-browser client; the cookie rules still apply
    host = (handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "").strip()
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc == host


def _rate_ok(ip, bucket, limit, window=600) -> bool:
    now = time.time()
    with RATE_LOCK:
        hits = [t for t in RATE_STATE.get((ip, bucket), []) if now - t < window]
        if len(hits) >= limit:
            RATE_STATE[(ip, bucket)] = hits
            return False
        hits.append(now)
        RATE_STATE[(ip, bucket)] = hits
    return True


def current_admin(handler):
    """Resolve the caller: HttpOnly session cookie first, then bearer token."""
    sid = _cookie_value(handler, auth.COOKIE_NAME)
    if sid:
        identity = auth.load_session(sid)
        if identity:
            return identity
    header = handler.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        try:
            return auth.authorize(auth.identity_from_token(token))
        except auth.AuthError:
            return None
    return None


# ---------------------------------------------------------------------------
# Data access for the panel
# ---------------------------------------------------------------------------

def _overview():
    with db.get_db() as conn:
        counts = conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM playlists) AS playlists,
                   (SELECT COUNT(*) FROM channels) AS channels,
                   (SELECT COUNT(*) FROM channels WHERE enabled = 1) AS channels_enabled,
                   (SELECT COUNT(*) FROM provider_sources) AS providers,
                   (SELECT COUNT(*) FROM provider_sources WHERE enabled = 1) AS providers_enabled,
                   (SELECT COUNT(*) FROM admin_sessions WHERE expires_at > ?) AS sessions,
                   (SELECT COUNT(*) FROM admin_audit) AS audit_events
            """,
            (int(time.time()),),
        ).fetchone()
        last_health = conn.execute(
            "SELECT ran_at, total_checked, online_count, offline_count FROM health_logs ORDER BY ran_at DESC LIMIT 1"
        ).fetchone()
        recent_channels = conn.execute(
            "SELECT name, category, playlist_id FROM channels ORDER BY position DESC LIMIT 5"
        ).fetchall()
    stats = dict(counts)
    stats["last_health"] = dict(last_health) if last_health else None
    stats["storage"] = db.health()
    stats["recent_channels"] = [dict(r) for r in recent_channels]
    return stats


def _playlists():
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.name, p.description, p.epg_url, p.created_at, p.updated_at,
                   (SELECT COUNT(*) FROM channels c WHERE c.playlist_id = p.id) AS channel_count,
                   (SELECT COUNT(*) FROM channels c WHERE c.playlist_id = p.id AND c.enabled = 1) AS enabled_count
              FROM playlists p
             ORDER BY p.created_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _channels(query="", status="", limit=50, offset=0):
    where, params = [], []
    if query:
        where.append("(lower(c.name) LIKE lower(?) OR lower(c.category) LIKE lower(?))")
        params.extend([f"%{query}%", f"%{query}%"])
    if status == "online":
        where.append("c.status = 'online'")
    elif status == "offline":
        where.append("c.status = 'offline'")
    elif status == "disabled":
        where.append("c.enabled = 0")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db.get_db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM channels c" + clause, tuple(params)).fetchone()["n"]
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.category, c.status, c.enabled, c.latency_ms, c.playlist_id,
                   p.name AS playlist_name, c.url
              FROM channels c
              LEFT JOIN playlists p ON p.id = c.playlist_id
            """
            + clause
            + " ORDER BY lower(c.name) LIMIT ? OFFSET ?",
            tuple(params) + (int(limit), int(offset)),
        ).fetchall()
    return {"total": total, "items": [dict(r) for r in rows]}


def _providers():
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, url, badge, provider_type, channel_count, enabled, is_system, last_synced_at
              FROM provider_sources ORDER BY position, name
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _set_channel_enabled(channel_id, enabled):
    with db.get_db() as conn:
        cur = conn.execute("UPDATE channels SET enabled = ? WHERE id = ?", (1 if enabled else 0, channel_id))
        return cur.rowcount


def _delete_channel(channel_id):
    with db.get_db() as conn:
        cur = conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        return cur.rowcount


def _delete_playlist(playlist_id):
    with db.get_db() as conn:
        cur = conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        return cur.rowcount


def _set_provider_enabled(provider_id, enabled):
    with db.get_db() as conn:
        cur = conn.execute("UPDATE provider_sources SET enabled = ? WHERE id = ?", (1 if enabled else 0, provider_id))
        return cur.rowcount


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def handle(handler, method, path, query) -> bool:
    """Handle /api/auth/config and /api/admin/*. Returns True when handled."""
    if path == "/api/auth/config" and method == "GET":
        _send(handler, auth.public_config())
        return True
    if not path.startswith("/api/admin"):
        return False

    ip = _client_ip(handler)
    try:
        if method == "GET":
            return _handle_get(handler, path, query, ip)
        if method == "POST":
            return _handle_post(handler, path, ip)
        _error(handler, 405, "method_not_allowed", "Method not allowed")
        return True
    except auth.AuthError as exc:
        _error(handler, exc.status, exc.code, exc.message)
        return True


def _handle_get(handler, path, query, ip):
    if path == "/api/admin/session":
        identity = current_admin(handler)
        if not identity:
            _send(handler, {"authenticated": False}, 200)
            return True
        _send(handler, {"authenticated": True, "admin": identity})
        return True

    identity = current_admin(handler)
    if not identity:
        _error(handler, 401, "not_authenticated", "Sign in to the admin panel first")
        return True

    params = urllib.parse.parse_qs(query or "")
    if path == "/api/admin/overview":
        _send(handler, {"admin": identity, **_overview()})
        return True
    if path == "/api/admin/playlists":
        _send(handler, {"playlists": _playlists()})
        return True
    if path == "/api/admin/channels":
        q = (params.get("q") or [""])[0][:80]
        status = (params.get("status") or [""])[0]
        try:
            limit = min(200, max(10, int((params.get("limit") or ["50"])[0])))
            offset = max(0, int((params.get("offset") or ["0"])[0]))
        except ValueError:
            limit, offset = 50, 0
        _send(handler, _channels(q, status, limit, offset))
        return True
    if path == "/api/admin/providers":
        _send(handler, {"providers": _providers()})
        return True
    if path == "/api/admin/admins":
        _send(handler, {"admins": auth.list_admins()})
        return True
    if path == "/api/admin/users":
        q = (params.get("q") or [""])[0][:80]
        _send(handler, {"users": auth.neon_users(q)})
        return True
    if path == "/api/admin/audit":
        _send(handler, {"events": auth.recent_audit(50)})
        return True
    _error(handler, 404, "not_found", "Unknown admin endpoint")
    return True


def _handle_post(handler, path, ip):
    if not _same_origin(handler):
        _error(handler, 403, "cross_origin", "Cross-origin admin request refused")
        return True
    if (handler.headers.get("X-Requested-With") or "") != "zemy-admin":
        _error(handler, 403, "missing_marker", "Missing X-Requested-With: zemy-admin header")
        return True
    body = handler.read_json()

    if path == "/api/admin/session":
        if not _rate_ok(ip, "signin", 15):
            _error(handler, 429, "rate_limited", "Too many sign-in attempts. Wait a few minutes.")
            return True
        token = str(body.get("token") or "").strip()
        try:
            identity = auth.identity_from_token(token)
            identity = auth.authorize(identity)
        except auth.AuthError as exc:
            auth.audit("admin.signin_denied", f"{exc.code}: {exc.message}", None, ip)
            _error(handler, exc.status, exc.code, exc.message)
            return True
        sid, expires = auth.create_session(identity, ip, handler.headers.get("User-Agent", ""))
        auth.audit("admin.signin", identity.get("email", ""), identity, ip)
        _send(handler, {"authenticated": True, "admin": identity, "expiresAt": expires},
              200, cookies=[_cookie_header(sid, expires, handler)])
        return True

    identity = current_admin(handler)
    if not identity:
        _error(handler, 401, "not_authenticated", "Sign in to the admin panel first")
        return True

    if path == "/api/admin/logout":
        sid = _cookie_value(handler, auth.COOKIE_NAME)
        auth.delete_session(sid)
        auth.audit("admin.signout", identity.get("email", ""), identity, ip)
        _send(handler, {"ok": True}, 200, cookies=[_clear_cookie(handler)])
        return True

    if path == "/api/admin/admins/grant":
        result = auth.grant_admin(body.get("email", ""), body.get("role", "admin"), identity, ip)
        _send(handler, {"ok": True, "admin": result})
        return True

    if path == "/api/admin/admins/revoke":
        removed = auth.revoke_admin(body.get("user_id", ""), identity, ip)
        _send(handler, {"ok": removed, "message": "Admin access revoked" if removed else "No admin access to revoke"})
        return True

    if path == "/api/admin/providers/toggle":
        provider_id = str(body.get("id") or "")
        changed = _set_provider_enabled(provider_id, bool(body.get("enabled")))
        auth.audit("provider.toggle", f"{provider_id} -> {bool(body.get('enabled'))}", identity, ip)
        _send(handler, {"ok": bool(changed), "changed": changed})
        return True

    parts = [p for p in path.strip("/").split("/") if p]
    # /api/admin/channels/<id>/enabled|delete
    if len(parts) == 5 and parts[0] == "api" and parts[1] == "admin":
        section, item_id, action = parts[2], parts[3], parts[4]
        if section == "channels" and action == "enabled":
            changed = _set_channel_enabled(item_id, bool(body.get("enabled")))
            auth.audit("channel.toggle", f"{item_id} -> {bool(body.get('enabled'))}", identity, ip)
            _send(handler, {"ok": bool(changed), "changed": changed})
            return True
        if section == "channels" and action == "delete":
            changed = _delete_channel(item_id)
            auth.audit("channel.delete", item_id, identity, ip)
            _send(handler, {"ok": bool(changed), "changed": changed})
            return True
        if section == "playlists" and action == "delete":
            changed = _delete_playlist(item_id)
            auth.audit("playlist.delete", item_id, identity, ip)
            _send(handler, {"ok": bool(changed), "changed": changed})
            return True

    _error(handler, 404, "not_found", "Unknown admin endpoint")
    return True
