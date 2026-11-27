# ZEMYIPTV — Custom IPTV M3U / M3U8 Playlist Studio

A self-hosted IPTV playlist creator, editor, and live M3U hosting server built with **Python 3** (zero external dependencies required — uses built-in `http.server`, `sqlite3`, and `concurrent.futures`) and a smooth dark-gradient web UI.

## Quick Start

```powershell
python app.py
```

Then open **http://localhost:8765** in your browser.

To run on a custom port:
```powershell
$env:PORT=9000; python app.py
```

## Core Features

1. **Multi-Playlist Management**: Create, rename, duplicate, delete, and configure XMLTV EPG URLs (`x-tvg-url` / `url-tvg`).
2. **Live Hosted M3U Endpoint**: Every playlist is immediately served at `http://<your-server>:8765/m3u/<playlist-id>.m3u` and `.m3u8` for direct subscription in **TiviMate**, **VLC**, **Kodi**, or **IPTV Smarters**, plus one-click file download.
3. **Auto Channel Logo Finder**:
   - Fuzzy-matches channel names and `tvg-id` against a curated HD logo index + the live **IPTV-Org** logo database.
   - Automatically generates dark-gradient SVG monogram badges when a niche channel has no external logo.
4. **⚡ Bulk `.m3u8` Channel Implant**:
   - Paste raw `.m3u8` stream links, `Channel Name | https://.../live.m3u8 | Category`, CSV lines, or `#EXTINF` blocks.
   - Automatically extracts channel names, assigns categories, deduplicates URLs, and fetches logos in bulk.
5. **📥 Auto-Import from M3U Playlists**:
   - Import from remote M3U URLs (fetched server-side to bypass CORS), local `.m3u`/`.m3u8` files, or raw `#EXTM3U` text.
   - Preview categories and cherry-pick which groups to import into an existing or new playlist.
6. **Channel & Category Editor**:
   - Edit channel name, `.m3u8` URL, category (`group-title`), logo (`tvg-logo`), `tvg-id`, `tvg-chno`, and custom `#EXTVLCOPT` HTTP `User-Agent` / `Referrer` headers.
   - Rename categories, unassign channels from a category, or delete categories and their channels.
7. **Built-in Live `.m3u8` Player & Stream Health Checker**:
   - Preview `.m3u8` HLS streams directly in the browser using the docked video player.
   - Probe stream status and latency (`ms`) concurrently via the Python backend.
