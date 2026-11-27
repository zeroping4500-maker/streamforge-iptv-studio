# Architecture

ZEMYIPTV is a single Python 3 stdlib HTTP app (`app.py`, roughly 6k lines) serving `static/` plus a JSON API. It runs as one process: Render web service `zemyiptv`, or the Docker image.

## Data

- All state lives in Neon Postgres. `db.py` opens a small psycopg2 pool and exposes `get_db()` with a sqlite3-compatible facade (`?` placeholders, `rowid` → `id`, `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`, dict-like rows), so existing query code kept working through the migration.
- `init_db()` at boot calls `store.init_schema()` and seeds defaults.
- No SQLite file is used at runtime; the old SQLite snapshot/state-sync code was removed. `scripts/migrate_sqlite_to_pg.py` copies an old SQLite file into Postgres once, idempotently.
- Schema and seed live in `db.py`; never create tables from request handlers.

## Auth and admin

- Identity provider is Neon Auth (Better Auth hosted at `NEON_AUTH_BASE_URL`, app name `holy-dawn-31979762`). The browser talks to it directly for email OTP and Google sign-in; this server never sees a password.
- `auth.py` verifies a Neon Auth token locally: EdDSA/Ed25519 JWT checked against the project JWKS (cached, kid-aware, 60 s leeway, issuer allowlist), with a fallback that validates opaque tokens against `neon_auth.session`. It then issues its own session: random id stored hashed (sha256) in `admin_sessions`, delivered as an HttpOnly SameSite=Strict cookie.
- Permission is server-side only: a row in `public.admin_users` (`user_id` = Neon Auth user id, `role` owner/admin), with the Neon `admin` role accepted as a second source. Granting requires the account to already exist in `neon_auth."user"` (`scripts/grant_admin.py grant <email>`).
- `admin.py` owns every rule for the panel: `/api/auth/config`, `/api/admin/{session,logout,overview,playlists,channels,providers,admins,users,audit}`, sign-in rate limiting, same-origin plus `X-Requested-With: zemy-admin` checks on writes, and the audit trail. The UI never decides authorisation.

## UI

- `/` is the viewer app (`static/index.html`, `app.js`, `styles.css`).
- `/admin` serves `static/admin.html` with `Cache-Control: no-store` and `X-Frame-Options: DENY`; the panel uses the Midnight Stream tokens (blue on near-black) and reads/writes only through `/api/admin/*`.
- The panel signs in silently at boot by exchanging a token from Neon Auth; a failed exchange just shows the sign-in card.

## Environment

`DATABASE_URL` (pooled Neon string), `NEON_AUTH_BASE_URL`, `NEON_AUTH_JWKS_URL`, `NEON_AUTH_APP_NAME`. Local values go in a gitignored `.env`; on Render they are service env vars. `NEON_AUTH_ENABLED=0` turns the panel off.
