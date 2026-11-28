"""Neon Auth integration for the ZEMYIPTV admin panel.

Sign-in is delegated to Neon Auth (email one-time code or Google). The browser
receives a token from Neon Auth, posts it to this server, and the server:

1. verifies the token (EdDSA signature against the project JWKS, expiry and
   issuer) or, for an opaque session token, checks it against
   ``neon_auth.session`` in the database;
2. confirms the account exists in ``neon_auth."user"`` and holds admin
   permission in ``public.admin_users`` (or the Neon Auth ``admin`` role);
3. issues its own HttpOnly session cookie that is validated on every request.

Nothing about a user's password or one-time code ever reaches this server.
"""

import base64
import hashlib
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import db

COOKIE_NAME = "zemy_admin"
ADMIN_ROLES = ("owner", "admin")
JWKS_TTL = 600
CLOCK_LEEWAY = 60


class AuthError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _env(name, default=""):
    db.load_env()
    return (os.environ.get(name) or default).strip()


def base_url() -> str:
    return _env("NEON_AUTH_BASE_URL", "https://ep-lingering-fire-bet1noj3.neonauth.c-9.us-east-2.aws.neon.tech/neondb/auth").rstrip("/")


def jwks_url() -> str:
    explicit = _env("NEON_AUTH_JWKS_URL")
    if explicit:
        return explicit
    return base_url() + "/.well-known/jwks.json"


def app_name() -> str:
    return _env("NEON_AUTH_APP_NAME", "ZEMYIPTV")


def session_ttl() -> int:
    try:
        return max(900, int(_env("ZEMY_ADMIN_SESSION_TTL", "43200")))
    except ValueError:
        return 43200


def enabled() -> bool:
    return bool(base_url())


def public_config() -> dict:
    # Deliberately no baseUrl: the browser never learns where Neon Auth lives;
    # every auth call goes through the server-side proxy endpoints in admin.py.
    return {
        "enabled": enabled(),
        "appName": app_name(),
        "methods": ["email_otp", "google"],
    }


# ---------------------------------------------------------------------------
# Server-side Neon Auth proxy (the browser never talks to Neon Auth directly)
# ---------------------------------------------------------------------------

def _neon_headers(extra=None):
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "zemyiptv-admin/1.0",
        "Accept": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def neon_request(path, payload=None, method="POST", cookie_header=None, timeout=10):
    """Call the Neon Auth server API. Returns (status, parsed body or None, set-cookie list)."""
    headers = _neon_headers({"Cookie": cookie_header} if cookie_header else None)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base_url() + path, headers=headers, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
            cookies = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
        cookies = exc.headers.get_all("Set-Cookie") or [] if exc.headers else []
    except Exception as exc:
        raise AuthError(503, "auth_service_unreachable", f"Authentication service unreachable: {exc}")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        body = None
    return status, body, [c for c in cookies if c]


def _session_token(payload):
    if not isinstance(payload, dict):
        return ""
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return payload.get("token") or session.get("token") or data.get("token") or ""


def _neon_error(status, body, fallback):
    message, code = fallback, "auth_failed"
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or message
        code = body.get("code") or code
    if status in (400, 401):
        return AuthError(401, code, message)
    if status == 429:
        return AuthError(429, "rate_limited", "Too many attempts, wait a few minutes")
    return AuthError(502, code, f"{fallback} (authentication service returned {status})")


def send_otp(email):
    status, body, _ = neon_request("/email-otp/send-verification-otp", {"email": email, "type": "sign-in"})
    if status != 200:
        raise _neon_error(status, body, "The code could not be sent")
    return True


def verify_otp(email, otp):
    """Exchange an emailed code for a Neon Auth session token (server-side only)."""
    status, body, cookies = neon_request("/sign-in/email-otp", {"email": email, "otp": otp})
    token = _session_token(body)
    if status == 200 and not token and cookies:
        # The session arrived as cookies instead of a JSON token: replay them
        # server-side against get-session to recover the token.
        jar = "; ".join(c.split(";")[0] for c in cookies)
        status2, body2, _ = neon_request("/get-session", method="GET", cookie_header=jar, timeout=8)
        token = _session_token(body2)
    if status != 200 or not token:
        raise _neon_error(status, body, "That code was not accepted")
    return token


def google_signin_url(origin):
    status, body, _ = neon_request("/sign-in/social", {"provider": "google", "callbackURL": origin + "/admin"})
    url = body.get("url") if isinstance(body, dict) else None
    if status != 200 or not url:
        raise _neon_error(status, body, "Google sign-in is not available right now")
    return url


def get_session_url():
    """The auth host's session endpoint: what the panel reads after Google returns."""
    return base_url() + "/get-session"


# ---------------------------------------------------------------------------
# JWKS / token verification
# ---------------------------------------------------------------------------

_JWKS_LOCK = threading.Lock()
_JWKS_CACHE = {"keys": {}, "fetched_at": 0.0}


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _fetch_jwks(force: bool = False) -> dict:
    with _JWKS_LOCK:
        fresh = (time.time() - _JWKS_CACHE["fetched_at"]) < JWKS_TTL
        if _JWKS_CACHE["keys"] and fresh and not force:
            return dict(_JWKS_CACHE["keys"])
        req = urllib.request.Request(jwks_url(), headers={"User-Agent": "zemyiptv-admin/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            if _JWKS_CACHE["keys"]:
                return dict(_JWKS_CACHE["keys"])
            raise AuthError(503, "jwks_unavailable", f"Cannot reach the Neon Auth key set: {exc}")
        keys = {}
        for jwk in payload.get("keys") or []:
            if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
                continue
            try:
                key = Ed25519PublicKey.from_public_bytes(_b64url_decode(jwk["x"]))
            except Exception:
                continue
            keys[jwk.get("kid") or ""] = key
        _JWKS_CACHE["keys"] = keys
        _JWKS_CACHE["fetched_at"] = time.time()
        return dict(keys)


def _allowed_issuers() -> set:
    base = base_url()
    issuers = {base, base.rsplit("/auth", 1)[0] if base.endswith("/auth") else base}
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme and parsed.netloc:
        issuers.add(f"{parsed.scheme}://{parsed.netloc}")
    extra = _env("NEON_AUTH_ISSUER")
    if extra:
        issuers.update(part.strip().rstrip("/") for part in extra.split(",") if part.strip())
    return {i for i in issuers if i}


def verify_jwt(token: str) -> dict:
    """Verify a Neon Auth JWT and return its claims, or raise AuthError."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise AuthError(401, "invalid_token", "Malformed token")
    alg = header.get("alg")
    if alg != "EdDSA":
        raise AuthError(401, "invalid_token", "Unsupported token algorithm")
    kid = header.get("kid") or ""
    keys = _fetch_jwks()
    key = keys.get(kid)
    if key is None:
        keys = _fetch_jwks(force=True)
        key = keys.get(kid)
    if key is None:
        raise AuthError(401, "invalid_token", "Token was not signed by this project")
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["EdDSA"],
            leeway=CLOCK_LEEWAY,
            options={"verify_aud": False, "require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "token_expired", "Sign-in token expired, please sign in again")
    except jwt.InvalidTokenError as exc:
        raise AuthError(401, "invalid_token", f"Token rejected: {exc}")
    issuer = (claims.get("iss") or "").rstrip("/")
    if issuer and issuer not in _allowed_issuers():
        raise AuthError(401, "invalid_token", "Token issuer is not this Neon Auth project")
    return claims


def looks_like_jwt(token: str) -> bool:
    return token.count(".") == 2 and len(token) > 40


def _user_from_opaque_token(token: str) -> dict:
    """Validate a Neon Auth session token directly in the database."""
    with db.get_db() as conn:
        row = conn.execute(
            """
            SELECT s."userId" AS user_id, u.email, u.name, u.role AS neon_role, u.banned
              FROM neon_auth.session s
              JOIN neon_auth."user" u ON u.id = s."userId"
             WHERE s.token = ? AND s."expiresAt" > now()
             LIMIT 1
            """,
            (token,),
        ).fetchone()
    if not row:
        raise AuthError(401, "invalid_token", "Session token is unknown or expired")
    if row["banned"]:
        raise AuthError(403, "account_suspended", "This account is suspended")
    return {"user_id": str(row["user_id"]), "email": row["email"], "name": row["name"] or "", "neon_role": row["neon_role"] or ""}


def identity_from_token(token: str) -> dict:
    token = (token or "").strip()
    if not token:
        raise AuthError(401, "missing_token", "No sign-in token supplied")
    if looks_like_jwt(token):
        claims = verify_jwt(token)
        user_id = str(claims.get("sub") or "")
        if not user_id:
            raise AuthError(401, "invalid_token", "Token has no subject")
        with db.get_db() as conn:
            row = conn.execute(
                'SELECT email, name, role AS neon_role, banned FROM neon_auth."user" WHERE id = ?',
                (user_id,),
            ).fetchone()
        if not row:
            raise AuthError(403, "no_account", "No Neon Auth account matches this token")
        if row["banned"]:
            raise AuthError(403, "account_suspended", "This account is suspended")
        return {"user_id": user_id, "email": row["email"], "name": row["name"] or "", "neon_role": row["neon_role"] or ""}
    return _user_from_opaque_token(token)


# ---------------------------------------------------------------------------
# Permission model
# ---------------------------------------------------------------------------

def admin_permission(user_id: str, email: str, neon_role: str):
    """Return the granted role, or None when the account may not use the panel."""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT role FROM admin_users WHERE user_id = ? OR lower(email) = lower(?) ORDER BY created_at LIMIT 1",
            (user_id, email),
        ).fetchone()
    if row and row["role"] in ADMIN_ROLES:
        return row["role"]
    if (neon_role or "").lower() in ADMIN_ROLES:
        return (neon_role or "").lower()
    return None


def authorize(identity: dict) -> dict:
    role = admin_permission(identity["user_id"], identity.get("email", ""), identity.get("neon_role", ""))
    if not role:
        raise AuthError(403, "not_admin", "This account does not have admin access to ZEMYIPTV")
    identity = dict(identity)
    identity["role"] = role
    return identity


# ---------------------------------------------------------------------------
# Server-side admin sessions
# ---------------------------------------------------------------------------

def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode("utf-8")).hexdigest()


def create_session(identity: dict, ip: str = "", user_agent: str = ""):
    sid = secrets.token_urlsafe(32)
    now = int(time.time())
    expires = now + session_ttl()
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO admin_sessions (id, user_id, email, created_at, expires_at, last_seen_at, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (_hash(sid), identity["user_id"], identity.get("email", ""), now, expires, now, ip[:64], (user_agent or "")[:300]),
        )
        conn.execute(
            "UPDATE admin_users SET last_seen_at = ? WHERE user_id = ? OR lower(email) = lower(?)",
            (now, identity["user_id"], identity.get("email", "")),
        )
        conn.execute(
            "DELETE FROM admin_sessions WHERE expires_at < ?",
            (now,),
        )
    return sid, expires


def load_session(sid: str):
    if not sid:
        return None
    now = int(time.time())
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id, user_id, email, expires_at, last_seen_at FROM admin_sessions WHERE id = ?",
            (_hash(sid),),
        ).fetchone()
        if not row or int(row["expires_at"]) <= now:
            return None
        if now - int(row["last_seen_at"]) > 300:
            conn.execute("UPDATE admin_sessions SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
        user = conn.execute(
            'SELECT email, name, role AS neon_role, banned FROM neon_auth."user" WHERE id = ?',
            (row["user_id"],),
        ).fetchone()
    if row:
        identity = {
            "user_id": str(row["user_id"]),
            "email": (user["email"] if user else row["email"]),
            "name": (user["name"] if user else "") or "",
            "neon_role": (user["neon_role"] if user else "") or "",
            "session_expires_at": int(row["expires_at"]),
        }
    else:
        return None
    if not user:
        return None
    if user["banned"]:
        return None
    role = admin_permission(identity["user_id"], identity["email"], identity["neon_role"])
    if not role:
        return None
    identity["role"] = role
    return identity


def delete_session(sid: str) -> None:
    if not sid:
        return
    with db.get_db() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE id = ?", (_hash(sid),))


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def audit(action: str, detail: str = "", actor: dict = None, ip: str = "") -> None:
    actor = actor or {}
    try:
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO admin_audit (at, actor_user_id, actor_email, action, detail, ip) VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), actor.get("user_id", ""), actor.get("email", ""), action[:60], detail[:400], ip[:64]),
            )
    except Exception:
        pass


def recent_audit(limit: int = 40) -> list:
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT at, actor_email, action, detail, ip FROM admin_audit ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Admin allowlist management
# ---------------------------------------------------------------------------

def list_admins() -> list:
    with db.get_db() as conn:
        rows = conn.execute(
            """
            SELECT a.user_id, a.email, a.role, a.created_at, a.last_seen_at, u.name, u.role AS neon_role
              FROM admin_users a
              LEFT JOIN neon_auth."user" u ON u.id::text = a.user_id
             ORDER BY lower(a.email)
            """
        ).fetchall()
    return [dict(r) for r in rows]


def neon_users(query: str = "", limit: int = 25) -> list:
    term = (query or "").strip()
    sql = """
        SELECT u.id, u.email, u.name, u.role, u."emailVerified" AS verified, u.banned,
               (a.user_id IS NOT NULL) AS is_admin
          FROM neon_auth."user" u
          LEFT JOIN admin_users a ON a.user_id = u.id::text
    """
    params = []
    if term:
        sql += " WHERE lower(u.email) LIKE lower(?) OR lower(u.name) LIKE lower(?)"
        like = f"%{term}%"
        params = [like, like]
    sql += ' ORDER BY u."createdAt" DESC LIMIT ?'
    params.append(int(limit))
    with db.get_db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def grant_admin(email: str, role: str = "admin", actor: dict = None, ip: str = "") -> dict:
    email = (email or "").strip().lower()
    role = (role or "admin").strip().lower()
    if "@" not in email or len(email) > 254:
        raise AuthError(400, "invalid_email", "Enter the email address of an existing Neon Auth account")
    if role not in ("owner", "admin"):
        raise AuthError(400, "invalid_role", "Role must be owner or admin")
    with db.get_db() as conn:
        user = conn.execute('SELECT id, email FROM neon_auth."user" WHERE lower(email) = ?', (email,)).fetchone()
        if not user:
            raise AuthError(404, "unknown_account", "No Neon Auth account uses that email. Sign up first, then grant access.")
        existing = conn.execute("SELECT user_id, role FROM admin_users WHERE user_id = ?", (str(user["id"]),)).fetchone()
        if existing:
            conn.execute("UPDATE admin_users SET role = ? WHERE user_id = ?", (role, str(user["id"])))
        else:
            conn.execute(
                "INSERT INTO admin_users (user_id, email, role, granted_by, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(user["id"]), user["email"], role, (actor or {}).get("email", ""), int(time.time()), 0),
            )
    audit("admin.grant", f"{email} -> {role}", actor, ip)
    return {"user_id": str(user["id"]), "email": user["email"], "role": role}


def revoke_admin(user_id: str, actor: dict = None, ip: str = "") -> bool:
    user_id = (user_id or "").strip()
    if not user_id:
        raise AuthError(400, "invalid_user", "Missing account id")
    if actor and actor.get("user_id") == user_id:
        raise AuthError(400, "self_revoke", "You cannot revoke your own admin access")
    with db.get_db() as conn:
        row = conn.execute("SELECT email FROM admin_users WHERE user_id = ?", (user_id,)).fetchone()
        conn.execute("DELETE FROM admin_sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM admin_users WHERE user_id = ?", (user_id,))
        neon = conn.execute('SELECT role FROM neon_auth."user" WHERE id = ?', (user_id,)).fetchone()
    if not row and not (neon and (neon["role"] or "") in ADMIN_ROLES):
        return False
    audit("admin.revoke", (row["email"] if row else user_id), actor, ip)
    return True
