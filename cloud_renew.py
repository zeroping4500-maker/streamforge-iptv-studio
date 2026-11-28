"""
ZEMYIPTV IPTV Studio - Cloud Stream Auto-Renew & M3U Export Engine
Runs automatically in GitHub Actions on a 5-hour cron schedule (or on-demand).

1. Extracts fresh live session tokens for Televizo / tvcdnpotok streams via Playwright.
2. Runs Fallback Guard to test channels and promote working backup sources.
3. Exports high-compatibility .m3u playlists to the output/ directory and root.
"""

import os
import re
import shutil
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app import (
    init_db,
    get_db,
    sync_televizo_playlist_streams,
    run_global_health_check,
    build_m3u_export,
    DATA_DIR,
)


def export_all_playlists_to_disk(output_dir: Path) -> list:
    output_dir.mkdir(parents=True, exist_ok=True)
    exported = []

    with get_db() as conn:
        playlists = conn.execute("SELECT * FROM playlists ORDER BY updated_at DESC").fetchall()

    if not playlists:
        print("[Export] No playlists found in database.")
        return exported

    master_playlist_content = None
    master_playlist_name = ""

    for pl_row in playlists:
        pl = dict(pl_row)
        pid = pl["id"]
        safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", pl["name"]).strip("_") or f"playlist_{pid}"

        with get_db() as conn:
            ch_rows = conn.execute(
                "SELECT * FROM channels WHERE playlist_id = ? ORDER BY position ASC, rowid ASC",
                (pid,),
            ).fetchall()
        channels = [dict(c) for c in ch_rows]

        # Expired televizo tokens cannot play, so their lines point at the public
        # endpoint that re-resolves the token when a viewer opens the channel.
        base_url = os.environ.get("RENDER_EXTERNAL_URL") or "https://zemyiptv.onrender.com"
        m3u_text = build_m3u_export(pl, channels, base_url=base_url)

        # Write named file and id-based file
        named_path = output_dir / f"{safe_name}.m3u"
        id_path = output_dir / f"{pid}.m3u"

        named_path.write_text(m3u_text, encoding="utf-8")
        id_path.write_text(m3u_text, encoding="utf-8")

        exported.append({
            "id": pid,
            "name": pl["name"],
            "filename": f"{safe_name}.m3u",
            "channels_count": len(channels),
            "size_bytes": len(m3u_text.encode("utf-8")),
        })
        print(f"[Export] Saved: {named_path.name} ({len(channels)} channels, {named_path.stat().st_size} bytes)")

        if master_playlist_content is None or len(channels) > (exported[0]["channels_count"] if exported else 0):
            master_playlist_content = m3u_text
            master_playlist_name = safe_name

    # Write master channels.m3u to output/ and repository root
    if master_playlist_content:
        master_output_path = output_dir / "channels.m3u"
        master_output_path.write_text(master_playlist_content, encoding="utf-8")

        root_channels_path = BASE_DIR / "channels.m3u"
        root_channels_path.write_text(master_playlist_content, encoding="utf-8")
        print(f"[Export] Generated master channels.m3u from '{master_playlist_name}'")

    return exported


def main():
    print("=" * 65)
    print("🚀 ZEMYIPTV IPTV Studio - 24/7 Cloud Auto-Renew & Export")
    print("=" * 65)
    t0 = time.time()

    init_db()

    # Step 1: Televizo & Expiring CDN Token Renewal
    print("\n[Step 1/3] 📺 Refreshing Televizo & Expiring Session Tokens...")
    try:
        tv_result = sync_televizo_playlist_streams(playlist_id=None, background=False)
        refreshed = tv_result.get("refreshed_count", 0)
        matched = tv_result.get("total_matched", 0)
        print(f"  -> Renewed {refreshed} of {matched} Televizo/CDN stream tokens.")
    except Exception as e:
        print(f"  -> Televizo renewal note: {e}")

    # Step 2: Fallback Guard & Stream Failover Check
    print("\n[Step 2/3] 🛡️ Running Fallback Guard (Testing & Auto-Failover)...")
    try:
        guard_result = run_global_health_check(trigger_type="github_actions_5h_cron")
        print(f"  -> Total checked: {guard_result.get('total', 0)}")
        print(f"  -> Online: {guard_result.get('online', 0)} | Offline: {guard_result.get('offline', 0)}")
        print(f"  -> Auto-Failovers promoted: {guard_result.get('failovers', 0)}")
    except Exception as e:
        print(f"  -> Fallback Guard note: {e}")

    # Step 3: Export Fresh M3U Playlists to output/ and root
    print("\n[Step 3/3] 📁 Exporting Fresh M3U Playlists...")
    output_dir = BASE_DIR / "output"
    exported = export_all_playlists_to_disk(output_dir)

    elapsed = round(time.time() - t0, 2)
    print("\n" + "=" * 65)
    print(f"✅ Renewal and export completed in {elapsed} seconds!")
    print(f"Generated {len(exported)} playlist(s). Ready to publish.")
    print("=" * 65)


if __name__ == "__main__":
    main()
