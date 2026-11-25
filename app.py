#!/usr/bin/env python3
"""
StreamForge IPTV Studio - Custom M3U / M3U8 Playlist Creator & Manager (v3)
Fixes & Features:
- WAL-mode SQLite + zero DB locks during network I/O (fixes all 500 database locked errors)
- Full JSON error handling on all GET/POST endpoints
- Fast indexed 13,000+ stream catalog (IPTV-Org streams.json + channels.json + Curated HD streams)
- Bulk Find Source by Channel Name List (up to 100 channels at once) for BOTH Main Panel & Non-Working Channels Center
- Parallel multi-candidate live stream probing & auto-healing
- Public Accessible URL (SSH Tunnel + LAN IPv4), 3x/Day Health Monitor, Xtream Codes Importer with Dual Deduplication
"""

import concurrent.futures
import difflib
import gc
import io
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "iptv_studio.db"

DATA_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

SERVER_PORT = int(os.environ.get("PORT", "8765"))
IS_LIGHTWEIGHT_PANEL = bool(
    os.environ.get("RENDER")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or os.environ.get("OFFLOAD_HEAVY_TASKS") == "1"
)
PANEL_MAX_WORKERS = 5 if IS_LIGHTWEIGHT_PANEL else 20

# ---------------------------------------------------------------------------
# Public Accessible URL & Tunnel State
# ---------------------------------------------------------------------------

TUNNEL_LOCK = threading.Lock()
TUNNEL_STATE = {
    "status": "stopped",  # "stopped" | "starting" | "online" | "error"
    "public_url": "",
    "provider": "localhost.run",
    "custom_base_url": "",
    "error": "",
    "proc": None,
}


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _tunnel_reader_thread(proc, provider: str):
    url_regex = re.compile(
        r"https://[a-zA-Z0-9.-]+\.(?:lhr\.life|localhost\.run|pinggy\.link|a\.pinggy\.io|trycloudflare\.com)"
    )
    try:
        for raw_line in iter(proc.stdout.readline, b""):
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="ignore").strip()
            m = url_regex.search(line)
            if m:
                pub_url = m.group(0).rstrip("/")
                with TUNNEL_LOCK:
                    TUNNEL_STATE["public_url"] = pub_url
                    TUNNEL_STATE["status"] = "online"
                    TUNNEL_STATE["error"] = ""
                print(f"[Public Tunnel] Live at {pub_url}")
    except Exception as e:
        with TUNNEL_LOCK:
            TUNNEL_STATE["error"] = str(e)
    finally:
        with TUNNEL_LOCK:
            if TUNNEL_STATE["proc"] == proc:
                TUNNEL_STATE["status"] = "stopped"
                TUNNEL_STATE["proc"] = None


def start_public_tunnel(provider: str = "localhost.run") -> dict:
    stop_public_tunnel()
    with TUNNEL_LOCK:
        TUNNEL_STATE["status"] = "starting"
        TUNNEL_STATE["public_url"] = ""
        TUNNEL_STATE["provider"] = provider
        TUNNEL_STATE["error"] = ""

    null_hosts = "NUL" if os.name == "nt" else "/dev/null"
    if provider == "pinggy":
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={null_hosts}",
            "-o", "ServerAliveInterval=30",
            "-p", "443",
            f"-R0:127.0.0.1:{SERVER_PORT}",
            "a.pinggy.io",
        ]
    else:
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", f"UserKnownHostsFile={null_hosts}",
            "-o", "ServerAliveInterval=30",
            "-R", f"80:127.0.0.1:{SERVER_PORT}",
            "nokey@localhost.run",
        ]

    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            creationflags=creationflags,
        )
        with TUNNEL_LOCK:
            TUNNEL_STATE["proc"] = proc

        t = threading.Thread(target=_tunnel_reader_thread, args=(proc, provider), daemon=True)
        t.start()

        deadline = time.time() + 5.5
        while time.time() < deadline:
            with TUNNEL_LOCK:
                if TUNNEL_STATE["public_url"] or TUNNEL_STATE["status"] == "stopped":
                    break
            time.sleep(0.2)

        with TUNNEL_LOCK:
            if not TUNNEL_STATE["public_url"] and TUNNEL_STATE["status"] == "starting":
                TUNNEL_STATE["error"] = "Tunnel connecting in background..."
    except Exception as e:
        with TUNNEL_LOCK:
            TUNNEL_STATE["status"] = "error"
            TUNNEL_STATE["error"] = f"Failed to launch SSH tunnel: {e}"

    return get_network_info()


def stop_public_tunnel():
    with TUNNEL_LOCK:
        proc = TUNNEL_STATE.get("proc")
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        TUNNEL_STATE["proc"] = None
        TUNNEL_STATE["status"] = "stopped"
        TUNNEL_STATE["public_url"] = ""


def get_network_info() -> dict:
    lan_ip = get_lan_ip()
    render_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL")
    with TUNNEL_LOCK:
        pub = render_url or TUNNEL_STATE["custom_base_url"] or TUNNEL_STATE["public_url"]
        return {
            "port": SERVER_PORT,
            "localhost_url": f"http://localhost:{SERVER_PORT}",
            "lan_url": f"http://{lan_ip}:{SERVER_PORT}",
            "public_url": pub.rstrip("/") if pub else "",
            "tunnel_status": "online" if render_url else TUNNEL_STATE["status"],
            "tunnel_provider": "Render Cloud" if render_url else TUNNEL_STATE["provider"],
            "custom_base_url": TUNNEL_STATE["custom_base_url"],
            "tunnel_error": "" if render_url else TUNNEL_STATE["error"],
        }


# ---------------------------------------------------------------------------
# Global Caches: Logos Catalog + Indexed Stream Sources Catalog
# ---------------------------------------------------------------------------

CATALOG_LOCK = threading.Lock()
LOGO_CATALOG_CACHE = {"loaded": False, "items": [], "by_id": {}, "by_clean": {}}
STREAM_SOURCES_CACHE = {
    "loaded": False,
    "items": [],
    "by_clean": {},     # exact clean_name -> list of items
    "token_index": {},  # word_token -> list of item indices
}

BUILTIN_LOGOS = [
    {"name": "BBC One", "id": "BBCOne.uk", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8b/BBC_One_logo_2021.svg/512px-BBC_One_logo_2021.svg.png"},
    {"name": "BBC Two", "id": "BBCTwo.uk", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/15/BBC_Two_logo_2021.svg/512px-BBC_Two_logo_2021.svg.png"},
    {"name": "BBC News", "id": "BBCNews.uk", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/62/BBC_News_2019.svg/512px-BBC_News_2019.svg.png"},
    {"name": "CNN", "id": "CNN.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b1/CNN.svg/512px-CNN.svg.png"},
    {"name": "Sky News", "id": "SkyNews.uk", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/de/Sky-news-logo.svg/512px-Sky-news-logo.svg.png"},
    {"name": "Sky Sports Main Event", "id": "SkySportsMainEvent.uk", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c5/Sky_Sports_logo_2020.svg/512px-Sky_Sports_logo_2020.svg.png"},
    {"name": "Sky Sports F1", "id": "SkySportsF1.uk", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c5/Sky_Sports_logo_2020.svg/512px-Sky_Sports_logo_2020.svg.png"},
    {"name": "ESPN", "id": "ESPN.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2f/ESPN_wordmark.svg/512px-ESPN_wordmark.svg.png"},
    {"name": "Fox Sports 1", "id": "FS1.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/37/FS1_logo_2015.svg/512px-FS1_logo_2015.svg.png"},
    {"name": "Fox News", "id": "FoxNews.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/67/Fox_News_Channel_logo.svg/512px-Fox_News_Channel_logo.svg.png"},
    {"name": "NBC", "id": "NBC.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3f/NBC_logo.svg/512px-NBC_logo.svg.png"},
    {"name": "CBS", "id": "CBS.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4e/CBS_logo.svg/512px-CBS_logo.svg.png"},
    {"name": "ABC", "id": "ABC.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2f/ABC-2021-LOGO.svg/512px-ABC-2021-LOGO.svg.png"},
    {"name": "HBO", "id": "HBO.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/de/HBO_logo.svg/512px-HBO_logo.svg.png"},
    {"name": "Discovery Channel", "id": "Discovery.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/f/f1/2019_Discovery_logo.svg/512px-2019_Discovery_logo.svg.png"},
    {"name": "National Geographic", "id": "NatGeo.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/f/fc/Natgeologo.svg/512px-Natgeologo.svg.png"},
    {"name": "Cartoon Network", "id": "CartoonNetwork.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/80/Cartoon_Network_2010_logo.svg/512px-Cartoon_Network_2010_logo.svg.png"},
    {"name": "Nickelodeon", "id": "Nickelodeon.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7a/Nickelodeon_2023_logo.svg/512px-Nickelodeon_2023_logo.svg.png"},
    {"name": "Disney Channel", "id": "DisneyChannel.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d2/2019_Disney_Channel_logo.svg/512px-2019_Disney_Channel_logo.svg.png"},
    {"name": "MTV", "id": "MTV.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/68/MTV_2021_%28brand_version%29.svg/512px-MTV_2021_%28brand_version%29.svg.png"},
    {"name": "Eurosport 1", "id": "Eurosport1.fr", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/17/Eurosport_1_2015.svg/512px-Eurosport_1_2015.svg.png"},
    {"name": "Eurosport 2", "id": "Eurosport2.fr", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/6f/Eurosport_2_2015.svg/512px-Eurosport_2_2015.svg.png"},
    {"name": "DAZN 1", "id": "DAZN1.uk", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/71/DAZN_logo.svg/512px-DAZN_logo.svg.png"},
    {"name": "beIN Sports", "id": "beINSports.qa", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8c/BeIN_Sports_logo.svg/512px-BeIN_Sports_logo.svg.png"},
    {"name": "TNT Sports 1", "id": "TNTSports1.uk", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d3/TNT_Sports_2023_logo.svg/512px-TNT_Sports_2023_logo.svg.png"},
    {"name": "Al Jazeera English", "id": "AlJazeera.qa", "url": "https://upload.wikimedia.org/wikipedia/en/thumb/f/f2/Aljazeera_eng.svg/512px-Aljazeera_eng.svg.png"},
    {"name": "France 24", "id": "France24.fr", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/65/FRANCE_24_logo.svg/512px-FRANCE_24_logo.svg.png"},
    {"name": "DW English", "id": "DW.de", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/75/Deutsche_Welle_symbol_2012.svg/512px-Deutsche_Welle_symbol_2012.svg.png"},
    {"name": "Bloomberg TV", "id": "Bloomberg.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5d/New_Bloomberg_Logo.svg/512px-New_Bloomberg_Logo.svg.png"},
    {"name": "CNBC", "id": "CNBC.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e3/CNBC_logo.svg/512px-CNBC_logo.svg.png"},
    {"name": "LRT Televizija", "id": "LRT.lt", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/43/LRT_logo_2022.svg/512px-LRT_logo_2022.svg.png"},
    {"name": "LRT Plius", "id": "LRTPlius.lt", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a9/LRT_Plius_logo_2022.svg/512px-LRT_Plius_logo_2022.svg.png"},
    {"name": "TV3 Lithuania", "id": "TV3.lt", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/29/TV3_Lithuania_2019_logo.svg/512px-TV3_Lithuania_2019_logo.svg.png"},
    {"name": "Red Bull TV", "id": "RedBullTV.at", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/91/Red_Bull_TV_logo.svg/512px-Red_Bull_TV_logo.svg.png"},
    {"name": "NASA TV", "id": "NASATV.us", "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e5/NASA_logo.svg/512px-NASA_logo.svg.png"},
]

BUILTIN_STREAM_SOURCES = [
    # Top Worldwide News
    {"name": "BBC News", "tvg_id": "BBCNews.uk", "category": "News", "url": "https://vs-hls-push-ww-live.akamaized.net/x=4/i=urn:bbc:pips:service:bbc_news_channel_hd/t=3840/v=pv14/b=5070016/main.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "BBC News Backup", "tvg_id": "BBCNews.uk", "category": "News", "url": "https://vs-hls-push-ww-live.akamaized.net/x=4/i=urn:bbc:pips:service:bbc_news_channel_hd/t=3840/v=pv14/b=2812032/main.m3u8", "quality": "720p", "source": "Verified CDN"},
    {"name": "Sky News", "tvg_id": "SkyNews.uk", "category": "News", "url": "https://skynews2-plutolive-vo.akamaized.net/cdnhls/2112/master.m3u8", "quality": "720p", "source": "Verified CDN"},
    {"name": "Sky News HD", "tvg_id": "SkyNews.uk", "category": "News", "url": "https://linear-417.frequency.stream/mt/studio/417/hls/master/playlist.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "CNN International", "tvg_id": "CNN.us", "category": "News", "url": "https://turnerlive.warnermediacdn.com/hls/live/586495/cnngo/cnn_slate/VIDEO_0_3564000.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Bloomberg TV", "tvg_id": "Bloomberg.us", "category": "Business", "url": "https://liveproduseast.global.ssl.fastly.net/us/Channel-USTV-AWS-virginia-1/Source-USTV-1000-1_live.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "CBS News Live", "tvg_id": "CBSNews.us", "category": "News", "url": "https://cbsn-us.cbsnstream.cbsnews.com/out/v1/55a8648e8f134e82a470f83d562deeca/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "ABC News Live", "tvg_id": "ABCNewsLive.us", "category": "News", "url": "https://content.uplynk.com/channel/3324f2467c414329b3b0cc5cd987b6be.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "NBC News NOW", "tvg_id": "NBCNewsNow.us", "category": "News", "url": "https://nbcnews-lh.akamaihd.net/i/nbcnews_now@439618/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "DW English", "tvg_id": "DW.de", "category": "News", "url": "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/index.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "DW Deutsch", "tvg_id": "DWDeutsch.de", "category": "News", "url": "https://dwamdstream104.akamaized.net/hls/live/2015527/dwstream104/index.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "France 24 English", "tvg_id": "France24.fr", "category": "News", "url": "https://ample-live.akamaized.net/hls/live/2034725/F24_EN_HI_HLS/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "France 24 Français", "tvg_id": "France24Fr.fr", "category": "News", "url": "https://ample-live.akamaized.net/hls/live/2034726/F24_FR_HI_HLS/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Euronews English", "tvg_id": "Euronews.fr", "category": "News", "url": "https://rakuten-euronews-1-gb.samsung.wurl.tv/playlist.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Al Jazeera English", "tvg_id": "AlJazeera.qa", "category": "News", "url": "https://live-hls-web-aje.getaj.net/AJE/index.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Al Jazeera Arabic", "tvg_id": "AlJazeeraAr.qa", "category": "News", "url": "https://live-hls-web-aja.getaj.net/AJA/index.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "NHK World Japan", "tvg_id": "NHKWorld.jp", "category": "News", "url": "https://nhkwlive-ojp.akamaized.net/hls/live/2003459/nhkwlive-ojp-en/index.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "TRT World", "tvg_id": "TRTWorld.tr", "category": "News", "url": "https://tv-trtworld.medya.trt.com.tr/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "CGTN English", "tvg_id": "CGTN.cn", "category": "News", "url": "https://news.cgtn.com/resource/live/english/cgtn-news.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "CNA Asia Live", "tvg_id": "CNA.sg", "category": "News", "url": "https://d2e1asnsl7brco.cloudfront.net/v1/master/613d059b21008d6690fa25e0037a3c7e4368b556/cna_linear/cna_linear_720.m3u8", "quality": "720p", "source": "Verified CDN"},
    {"name": "RTVE 24 Horas", "tvg_id": "24h.es", "category": "News", "url": "https://rtvelivestream.akamaized.net/rtvesec/24h/24h_main.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "TVP World", "tvg_id": "TVPWorld.pl", "category": "News", "url": "https://stream.tvp.pl/live/world/live.m3u8", "quality": "1080p", "source": "Verified CDN"},
    # Sports & Action
    {"name": "Red Bull TV", "tvg_id": "RedBullTV.at", "category": "Sports", "url": "https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Red Bull TV 720p", "tvg_id": "RedBullTV.at", "category": "Sports", "url": "https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/BoRB-AT_720p.m3u8", "quality": "720p", "source": "Verified CDN"},
    {"name": "Outdoor Channel Action", "tvg_id": "OutdoorChannel.us", "category": "Sports", "url": "https://linear-419.frequency.stream/dist/outdoor/419/hls/master/playlist.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "World Poker Tour Live", "tvg_id": "WPT.us", "category": "Sports", "url": "https://linear-406.frequency.stream/dist/wpt/406/hls/master/playlist.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "SportsGrid HD", "tvg_id": "SportsGrid.us", "category": "Sports", "url": "https://linear-418.frequency.stream/mt/studio/418/hls/master/playlist.m3u8", "quality": "1080p", "source": "Verified CDN"},
    # Science & Documentaries
    {"name": "NASA TV Public", "tvg_id": "NASATV.us", "category": "Documentary", "url": "https://ntv1.akamaized.net/hls/live/2014075/NASA-NTV1-HLS/master.m3u8", "quality": "720p", "source": "Verified CDN"},
    {"name": "NASA TV Media HD", "tvg_id": "NASAMedia.us", "category": "Documentary", "url": "https://ntv2.akamaized.net/hls/live/2014076/NASA-NTV2-HLS/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Space Coast Live", "tvg_id": "SpaceCoast.us", "category": "Documentary", "url": "https://stream.spacecoastlive.com/live/stream.m3u8", "quality": "1080p", "source": "Verified CDN"},
    # Entertainment, Movies & Cinema
    {"name": "Apple Tears of Steel Cinema", "tvg_id": "AppleHLS.us", "category": "Entertainment", "url": "https://devstreaming-cdn.apple.com/videos/streaming/examples/img_bipbop_adv_example_fmp4/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Big Buck Bunny 4K Showcase", "tvg_id": "MuxTest.us", "category": "Entertainment", "url": "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Filmstream Cinema HD", "tvg_id": "Filmstream.us", "category": "Movies", "url": "https://linear-428.frequency.stream/dist/filmstream/428/hls/master/playlist.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "Retro TV Classics", "tvg_id": "RetroTV.us", "category": "Entertainment", "url": "https://linear-430.frequency.stream/dist/retrotv/430/hls/master/playlist.m3u8", "quality": "720p", "source": "Verified CDN"},
    # Regional Broadcasters
    {"name": "LRT Televizija", "tvg_id": "LRT.lt", "category": "General", "url": "https://lrt-live.akamaized.net/hls/live/2009276/LRT_TELEVIZIJA/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "LRT Plius", "tvg_id": "LRTPlius.lt", "category": "General", "url": "https://lrt-live.akamaized.net/hls/live/2009277/LRT_PLIUS/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
    {"name": "TV5Monde Info", "tvg_id": "TV5Monde.fr", "category": "General", "url": "https://tv5monde-live.akamaized.net/hls/live/2034720/TV5_INFO_HLS/master.m3u8", "quality": "1080p", "source": "Verified CDN"},
]


# ---------------------------------------------------------------------------
# High-Accuracy Channel Matching, Country Extraction, and Disambiguation Engine
# ---------------------------------------------------------------------------

COUNTRY_MAP = {
    "uk": "GB", "gb": "GB", "united kingdom": "GB", "great britain": "GB", "england": "GB", "british": "GB",
    "us": "US", "usa": "US", "united states": "US", "america": "US", "american": "US",
    "lt": "LT", "lithuania": "LT", "lietuva": "LT", "lithuanian": "LT", "lietuviska": "LT",
    "de": "DE", "germany": "DE", "deutschland": "DE", "german": "DE",
    "pl": "PL", "poland": "PL", "polska": "PL", "polish": "PL",
    "fr": "FR", "france": "FR", "french": "FR", "francais": "FR",
    "es": "ES", "spain": "ES", "espana": "ES", "spanish": "ES", "espanol": "ES",
    "it": "IT", "italy": "IT", "italia": "IT", "italian": "IT", "italiano": "IT",
    "ca": "CA", "canada": "CA", "canadian": "CA",
    "au": "AU", "australia": "AU", "aussie": "AU",
    "ie": "IE", "ireland": "IE", "irish": "IE",
    "pt": "PT", "portugal": "PT", "portuguese": "PT",
    "br": "BR", "brazil": "BR", "brasil": "BR", "brazilian": "BR",
    "ru": "RU", "russia": "RU", "russian": "RU",
    "ua": "UA", "ukraine": "UA", "ukrainian": "UA",
    "tr": "TR", "turkey": "TR", "turkish": "TR", "turkce": "TR",
    "gr": "GR", "greece": "GR", "greek": "GR",
    "nl": "NL", "netherlands": "NL", "dutch": "NL", "holland": "NL",
    "se": "SE", "sweden": "SE", "swedish": "SE",
    "no": "NO", "norway": "NO", "norwegian": "NO",
    "dk": "DK", "denmark": "DK", "danish": "DK",
    "fi": "FI", "finland": "FI", "finnish": "FI",
    "ro": "RO", "romania": "RO", "romanian": "RO",
    "cz": "CZ", "czech": "CZ", "czechia": "CZ",
    "sk": "SK", "slovakia": "SK", "slovak": "SK",
    "hu": "HU", "hungary": "HU", "hungarian": "HU",
    "bg": "BG", "bulgaria": "BG", "bulgarian": "BG",
    "hr": "HR", "croatia": "HR", "croatian": "HR",
    "rs": "RS", "serbia": "RS", "serbian": "RS",
    "si": "SI", "slovenia": "SI", "slovenian": "SI",
    "lv": "LV", "latvia": "LV", "latvija": "LV", "latvian": "LV",
    "ee": "EE", "estonia": "EE", "eesti": "EE", "estonian": "EE",
    "be": "BE", "belgium": "BE", "belgie": "BE", "belgique": "BE", "belgian": "BE",
    "ch": "CH", "switzerland": "CH", "schweiz": "CH", "suisse": "CH", "swiss": "CH",
    "at": "AT", "austria": "AT", "osterreich": "AT", "austrian": "AT",
    "in": "IN", "india": "IN", "indian": "IN", "hindi": "IN",
    "pk": "PK", "pakistan": "PK", "pakistani": "PK", "urdu": "PK",
    "ar": "AR", "argentina": "AR",
    "mx": "MX", "mexico": "MX",
    "cl": "CL", "chile": "CL",
    "co": "CO", "colombia": "CO",
    "pe": "PE", "peru": "PE", "peruvian": "PE",
    "za": "ZA", "south africa": "ZA",
    "nz": "NZ", "new zealand": "NZ", "kiwi": "NZ",
    "eg": "EG", "egypt": "EG", "egyptian": "EG", "misr": "EG",
    "sa": "SA", "saudi arabia": "SA", "saudi": "SA", "ksa": "SA",
    "ae": "AE", "uae": "AE", "united arab emirates": "AE", "emirates": "AE", "dubai": "AE",
    "ma": "MA", "morocco": "MA", "moroccan": "MA",
    "ng": "NG", "nigeria": "NG", "nigerian": "NG",
    "il": "IL", "israel": "IL", "hebrew": "IL",
    "jp": "JP", "japan": "JP", "japanese": "JP",
    "kr": "KR", "korea": "KR", "korean": "KR",
    "cn": "CN", "china": "CN", "chinese": "CN",
    "tw": "TW", "taiwan": "TW",
    "hk": "HK", "hong kong": "HK",
    "ph": "PH", "philippines": "PH", "filipino": "PH",
    "vn": "VN", "vietnam": "VN", "vietnamese": "VN",
    "th": "TH", "thailand": "TH", "thai": "TH",
    "id": "ID", "indonesia": "ID", "indonesian": "ID",
    "my": "MY", "malaysia": "MY", "malaysian": "MY",
    "is": "IS", "iceland": "IS", "island": "IS", "icelandic": "IS",
    "lu": "LU", "luxembourg": "LU", "luxembourgish": "LU",
    "cy": "CY", "cyprus": "CY", "cypriot": "CY",
    "mt": "MT", "malta": "MT", "maltese": "MT",
    "md": "MD", "moldova": "MD", "moldovan": "MD",
    "ba": "BA", "bosnia": "BA", "bosnian": "BA",
    "me": "ME", "montenegro": "ME", "montenegrin": "ME",
    "mk": "MK", "north macedonia": "MK", "macedonia": "MK", "macedonian": "MK",
    "al": "AL", "albania": "AL", "albanian": "AL", "shqip": "AL",
    "ge": "GE", "georgia": "GE", "georgian": "GE",
    "am": "AM", "armenia": "AM", "armenian": "AM",
    "az": "AZ", "azerbaijan": "AZ", "azerbaijani": "AZ",
    "kz": "KZ", "kazakhstan": "KZ", "kazakh": "KZ",
    "uz": "UZ", "uzbekistan": "UZ", "uzbek": "UZ",
    "ve": "VE", "venezuela": "VE", "venezuelan": "VE",
    "ec": "EC", "ecuador": "EC", "ecuadorian": "EC",
    "bo": "BO", "bolivia": "BO", "bolivian": "BO",
    "py": "PY", "paraguay": "PY", "paraguayan": "PY",
    "uy": "UY", "uruguay": "UY", "uruguayan": "UY",
    "cr": "CR", "costa rica": "CR", "costa rican": "CR",
    "pa": "PA", "panama": "PA", "panamanian": "PA",
    "gt": "GT", "guatemala": "GT", "guatemalan": "GT",
    "hn": "HN", "honduras": "HN", "honduran": "HN",
    "sv": "SV", "el salvador": "SV", "salvadoran": "SV",
    "ni": "NI", "nicaragua": "NI", "nicaraguan": "NI",
    "cu": "CU", "cuba": "CU", "cuban": "CU",
    "do": "DO", "dominican republic": "DO", "dominican": "DO",
    "pr": "PR", "puerto rico": "PR", "puerto rican": "PR",
    "jm": "JM", "jamaica": "JM", "jamaican": "JM",
    "qa": "QA", "qatar": "QA", "qatari": "QA",
    "kw": "KW", "kuwait": "KW", "kuwaiti": "KW",
    "bh": "BH", "bahrain": "BH", "bahraini": "BH",
    "om": "OM", "oman": "OM", "omani": "OM",
    "jo": "JO", "jordan": "JO", "jordanian": "JO",
    "lb": "LB", "lebanon": "LB", "lebanese": "LB",
    "iq": "IQ", "iraq": "IQ", "iraqi": "IQ",
    "dz": "DZ", "algeria": "DZ", "algerian": "DZ",
    "tn": "TN", "tunisia": "TN", "tunisian": "TN",
    "ke": "KE", "kenya": "KE", "kenyan": "KE",
    "gh": "GH", "ghana": "GH", "ghanaian": "GH",
    "sn": "SN", "senegal": "SN", "senegalese": "SN",
    "bd": "BD", "bangladesh": "BD", "bengali": "BD", "bangla": "BD",
    "lk": "LK", "sri lanka": "LK", "sri lankan": "LK", "sinhala": "LK",
    "np": "NP", "nepal": "NP", "nepali": "NP",
    "sg": "SG", "singapore": "SG", "singaporean": "SG",
    "latam": "LATAM", "latino": "LATAM", "latin": "LATAM",
    "arab": "ARAB", "arabic": "ARAB",
}

KNOWN_2LETTER_COUNTRIES = {
    "uk", "gb", "us", "lt", "lv", "ee", "de", "pl", "fr", "es", "it", "ca", "au", "ie", "pt", "br",
    "ru", "ua", "tr", "gr", "nl", "be", "ch", "at", "se", "no", "dk", "fi", "ro", "cz", "sk", "hu", "bg",
    "hr", "rs", "si", "in", "pk", "ar", "mx", "cl", "co", "pe", "za", "nz", "eg", "sa", "ae", "ma", "ng",
    "il", "jp", "kr", "cn", "tw", "hk", "ph", "vn", "th", "id", "my", "is", "lu", "cy", "mt", "md", "ba",
    "me", "mk", "al", "ge", "am", "az", "kz", "uz", "ve", "ec", "bo", "py", "uy", "cr", "pa", "gt", "hn",
    "sv", "ni", "cu", "do", "pr", "jm", "qa", "kw", "bh", "om", "jo", "lb", "iq", "dz", "tn", "ke", "gh",
    "sn", "bd", "lk", "np", "sg"
}

COUNTRY_FLAGS = {
    "GB": {"flag": "🇬🇧", "name": "United Kingdom", "code": "UK"},
    "US": {"flag": "🇺🇸", "name": "United States", "code": "USA"},
    "LT": {"flag": "🇱🇹", "name": "Lithuania", "code": "LT"},
    "LV": {"flag": "🇱🇻", "name": "Latvia", "code": "LV"},
    "EE": {"flag": "🇪🇪", "name": "Estonia", "code": "EE"},
    "DE": {"flag": "🇩🇪", "name": "Germany", "code": "DE"},
    "PL": {"flag": "🇵🇱", "name": "Poland", "code": "PL"},
    "FR": {"flag": "🇫🇷", "name": "France", "code": "FR"},
    "ES": {"flag": "🇪🇸", "name": "Spain", "code": "ES"},
    "IT": {"flag": "🇮🇹", "name": "Italy", "code": "IT"},
    "CA": {"flag": "🇨🇦", "name": "Canada", "code": "CA"},
    "AU": {"flag": "🇦🇺", "name": "Australia", "code": "AU"},
    "NZ": {"flag": "🇳🇿", "name": "New Zealand", "code": "NZ"},
    "IE": {"flag": "🇮🇪", "name": "Ireland", "code": "IE"},
    "NL": {"flag": "🇳🇱", "name": "Netherlands", "code": "NL"},
    "BE": {"flag": "🇧🇪", "name": "Belgium", "code": "BE"},
    "CH": {"flag": "🇨🇭", "name": "Switzerland", "code": "CH"},
    "AT": {"flag": "🇦🇹", "name": "Austria", "code": "AT"},
    "PT": {"flag": "🇵🇹", "name": "Portugal", "code": "PT"},
    "BR": {"flag": "🇧🇷", "name": "Brazil", "code": "BR"},
    "RU": {"flag": "🇷🇺", "name": "Russia", "code": "RU"},
    "UA": {"flag": "🇺🇦", "name": "Ukraine", "code": "UA"},
    "TR": {"flag": "🇹🇷", "name": "Turkey", "code": "TR"},
    "GR": {"flag": "🇬🇷", "name": "Greece", "code": "GR"},
    "SE": {"flag": "🇸🇪", "name": "Sweden", "code": "SE"},
    "NO": {"flag": "🇳🇴", "name": "Norway", "code": "NO"},
    "DK": {"flag": "🇩🇰", "name": "Denmark", "code": "DK"},
    "FI": {"flag": "🇫🇮", "name": "Finland", "code": "FI"},
    "IS": {"flag": "🇮🇸", "name": "Iceland", "code": "IS"},
    "LU": {"flag": "🇱🇺", "name": "Luxembourg", "code": "LU"},
    "RO": {"flag": "🇷🇴", "name": "Romania", "code": "RO"},
    "CZ": {"flag": "🇨🇿", "name": "Czechia", "code": "CZ"},
    "SK": {"flag": "🇸🇰", "name": "Slovakia", "code": "SK"},
    "HU": {"flag": "🇭🇺", "name": "Hungary", "code": "HU"},
    "BG": {"flag": "🇧🇬", "name": "Bulgaria", "code": "BG"},
    "HR": {"flag": "🇭🇷", "name": "Croatia", "code": "HR"},
    "RS": {"flag": "🇷🇸", "name": "Serbia", "code": "RS"},
    "SI": {"flag": "🇸🇮", "name": "Slovenia", "code": "SI"},
    "BA": {"flag": "🇧🇦", "name": "Bosnia & Herzegovina", "code": "BA"},
    "ME": {"flag": "🇲🇪", "name": "Montenegro", "code": "ME"},
    "MK": {"flag": "🇲🇰", "name": "North Macedonia", "code": "MK"},
    "AL": {"flag": "🇦🇱", "name": "Albania", "code": "AL"},
    "MD": {"flag": "🇲🇩", "name": "Moldova", "code": "MD"},
    "CY": {"flag": "🇨🇾", "name": "Cyprus", "code": "CY"},
    "MT": {"flag": "🇲🇹", "name": "Malta", "code": "MT"},
    "GE": {"flag": "🇬🇪", "name": "Georgia", "code": "GE"},
    "AM": {"flag": "🇦🇲", "name": "Armenia", "code": "AM"},
    "AZ": {"flag": "🇦🇿", "name": "Azerbaijan", "code": "AZ"},
    "KZ": {"flag": "🇰🇿", "name": "Kazakhstan", "code": "KZ"},
    "UZ": {"flag": "🇺🇿", "name": "Uzbekistan", "code": "UZ"},
    "IN": {"flag": "🇮🇳", "name": "India", "code": "IN"},
    "PK": {"flag": "🇵🇰", "name": "Pakistan", "code": "PK"},
    "BD": {"flag": "🇧🇩", "name": "Bangladesh", "code": "BD"},
    "LK": {"flag": "🇱🇰", "name": "Sri Lanka", "code": "LK"},
    "NP": {"flag": "🇳🇵", "name": "Nepal", "code": "NP"},
    "AR": {"flag": "🇦🇷", "name": "Argentina", "code": "AR"},
    "MX": {"flag": "🇲🇽", "name": "Mexico", "code": "MX"},
    "CL": {"flag": "🇨🇱", "name": "Chile", "code": "CL"},
    "CO": {"flag": "🇨🇴", "name": "Colombia", "code": "CO"},
    "PE": {"flag": "🇵🇪", "name": "Peru", "code": "PE"},
    "VE": {"flag": "🇻🇪", "name": "Venezuela", "code": "VE"},
    "EC": {"flag": "🇪🇨", "name": "Ecuador", "code": "EC"},
    "BO": {"flag": "🇧🇴", "name": "Bolivia", "code": "BO"},
    "PY": {"flag": "🇵🇾", "name": "Paraguay", "code": "PY"},
    "UY": {"flag": "🇺🇾", "name": "Uruguay", "code": "UY"},
    "CR": {"flag": "🇨🇷", "name": "Costa Rica", "code": "CR"},
    "PA": {"flag": "🇵🇦", "name": "Panama", "code": "PA"},
    "GT": {"flag": "🇬🇹", "name": "Guatemala", "code": "GT"},
    "HN": {"flag": "🇭🇳", "name": "Honduras", "code": "HN"},
    "SV": {"flag": "🇸🇻", "name": "El Salvador", "code": "SV"},
    "NI": {"flag": "🇳🇮", "name": "Nicaragua", "code": "NI"},
    "CU": {"flag": "🇨🇺", "name": "Cuba", "code": "CU"},
    "DO": {"flag": "🇩🇴", "name": "Dominican Republic", "code": "DO"},
    "PR": {"flag": "🇵🇷", "name": "Puerto Rico", "code": "PR"},
    "JM": {"flag": "🇯🇲", "name": "Jamaica", "code": "JM"},
    "ZA": {"flag": "🇿🇦", "name": "South Africa", "code": "ZA"},
    "EG": {"flag": "🇪🇬", "name": "Egypt", "code": "EG"},
    "SA": {"flag": "🇸🇦", "name": "Saudi Arabia", "code": "SA"},
    "AE": {"flag": "🇦🇪", "name": "United Arab Emirates", "code": "AE"},
    "QA": {"flag": "🇶🇦", "name": "Qatar", "code": "QA"},
    "KW": {"flag": "🇰🇼", "name": "Kuwait", "code": "KW"},
    "BH": {"flag": "🇧🇭", "name": "Bahrain", "code": "BH"},
    "OM": {"flag": "🇴🇲", "name": "Oman", "code": "OM"},
    "JO": {"flag": "🇯🇴", "name": "Jordan", "code": "JO"},
    "LB": {"flag": "🇱🇧", "name": "Lebanon", "code": "LB"},
    "IQ": {"flag": "🇮🇶", "name": "Iraq", "code": "IQ"},
    "MA": {"flag": "🇲🇦", "name": "Morocco", "code": "MA"},
    "DZ": {"flag": "🇩🇿", "name": "Algeria", "code": "DZ"},
    "TN": {"flag": "🇹🇳", "name": "Tunisia", "code": "TN"},
    "NG": {"flag": "🇳🇬", "name": "Nigeria", "code": "NG"},
    "KE": {"flag": "🇰🇪", "name": "Kenya", "code": "KE"},
    "GH": {"flag": "🇬🇭", "name": "Ghana", "code": "GH"},
    "SN": {"flag": "🇸🇳", "name": "Senegal", "code": "SN"},
    "IL": {"flag": "🇮🇱", "name": "Israel", "code": "IL"},
    "JP": {"flag": "🇯🇵", "name": "Japan", "code": "JP"},
    "KR": {"flag": "🇰🇷", "name": "Korea", "code": "KR"},
    "CN": {"flag": "🇨🇳", "name": "China", "code": "CN"},
    "TW": {"flag": "🇹🇼", "name": "Taiwan", "code": "TW"},
    "HK": {"flag": "🇭🇰", "name": "Hong Kong", "code": "HK"},
    "PH": {"flag": "🇵🇭", "name": "Philippines", "code": "PH"},
    "VN": {"flag": "🇻🇳", "name": "Vietnam", "code": "VN"},
    "TH": {"flag": "🇹🇭", "name": "Thailand", "code": "TH"},
    "ID": {"flag": "🇮🇩", "name": "Indonesia", "code": "ID"},
    "MY": {"flag": "🇲🇾", "name": "Malaysia", "code": "MY"},
    "SG": {"flag": "🇸🇬", "name": "Singapore", "code": "SG"},
    "LATAM": {"flag": "🌎", "name": "Latin America", "code": "LAT"},
    "ARAB": {"flag": "🌍", "name": "Arab World", "code": "ARA"},
}

GENRE_FAMILIES = {
    "news": {"news", "informacion", "noticias", "zinios", "wiadomosci", "nachrichten", "info", "parliament", "weather", "business", "financial"},
    "sports": {"sports", "sport", "football", "soccer", "futbol", "cricket", "f1", "formula1", "golf", "tennis", "rugby", "basket", "basketball", "racing", "espn", "eurosport"},
    "movies": {"movies", "movie", "cinema", "films", "film", "kino", "action", "comedy", "drama", "thriller", "horror"},
    "kids": {"kids", "children", "cartoons", "cartoon", "animation", "anime", "disney", "nick", "nickelodeon", "junior"},
    "music": {"music", "hits", "dance", "rock", "pop", "classic", "mtv"},
    "doc": {"documentary", "doc", "history", "science", "nature", "wild", "discovery", "investigation", "crime", "planet"},
}

NUMBER_CANONICAL = {
    "1": "1", "one": "1", "i": "1", "uno": "1", "un": "1", "vienas": "1",
    "2": "2", "two": "2", "ii": "2", "dos": "2", "deux": "2", "du": "2",
    "3": "3", "three": "3", "iii": "3", "tres": "3", "trois": "3", "trys": "3",
    "4": "4", "four": "4", "iv": "4", "cuatro": "4", "quatre": "4", "keturi": "4",
    "5": "5", "five": "5", "v": "5", "cinco": "5", "cinq": "5", "penki": "5",
    "6": "6", "six": "6", "vi": "6", "seis": "6",
    "7": "7", "seven": "7", "vii": "7", "siete": "7",
    "8": "8", "eight": "8", "viii": "8", "ocho": "8",
    "9": "9", "nine": "9", "ix": "9", "nueve": "9",
    "10": "10", "ten": "10", "x": "10", "diez": "10",
    "24": "24",
}

NOISE_WORDS = {
    "tv", "television", "channel", "official", "live", "stream", "feed", "hd", "fhd", "uhd",
    "4k", "sd", "hevc", "h264", "h265", "1080p", "720p", "480p", "50fps", "60fps", "vip", "backup",
    "main", "raw", "the", "a", "an", "de", "la", "el", "le", "les", "und", "and", "et", "y",
}

def get_country_badge(code: str) -> str:
    if not code:
        return ""
    code_up = code.upper()
    info = COUNTRY_FLAGS.get(code_up)
    if info:
        return f"{info['flag']} {info['code']}"
    return code_up

def extract_country(name: str = "", tvg_id: str = "", category: str = "", tvg_country: str = "") -> str:
    if tvg_country and tvg_country.strip():
        tc = tvg_country.strip().lower()
        if tc in COUNTRY_MAP:
            return COUNTRY_MAP[tc]

    if tvg_id and "." in tvg_id:
        tld = tvg_id.rsplit(".", 1)[1].lower()
        if tld in COUNTRY_MAP:
            return COUNTRY_MAP[tld]

    combined = f" {name} {category} ".lower()

    # Bracketed or prefixed code: e.g. [UK], (UK), UK:, UK -, | UK |
    m = re.search(r"[\(\[\{\|\/]\s*([a-z]{2,3})\s*[\)\]\}\|\/]|^\s*([a-z]{2,3})\s*[:|\-•/]", combined)
    if m:
        c = (m.group(1) or m.group(2)).lower()
        if c in COUNTRY_MAP:
            return COUNTRY_MAP[c]

    # Standalone country word or known 2-letter country token
    for term, code in COUNTRY_MAP.items():
        if (len(term) >= 3 or term in KNOWN_2LETTER_COUNTRIES) and re.search(r"\b" + re.escape(term) + r"\b", combined):
            return code

    return ""

def clean_channel_tokens(s: str) -> tuple:
    if not s:
        return [], set()
    s = s.lower()
    s = re.sub(r"\[.*?\]|\(.*?\)|<.*?>|\{.*?\}", " ", s)
    s = re.sub(r"^[a-z]{2,3}\s*[:|\-•/]\s*", " ", s)
    s = re.sub(
        r"\b(uhd|4k|fhd|hd|sd|hevc|h265|h264|50fps|60fps|raw|vip|backup|main|feed|1080p|720p|480p|live|stream|channel|tv|plus)\b",
        lambda m: "plus" if m.group(1) == "plus" else " ",
        s,
    )
    s = s.replace("+", " plus ")
    s = re.sub(r"\b(nat\s*geo)\b", "national geographic", s)
    s = re.sub(r"\b(bbc\s*1)\b", "bbc one", s)
    s = re.sub(r"\b(bbc\s*2)\b", "bbc two", s)
    s = re.sub(r"\b(bbc\s*3)\b", "bbc three", s)
    s = re.sub(r"\b(bbc\s*4)\b", "bbc four", s)
    s = re.sub(r"\b(sky\s*sport)\b", "sky sports", s)
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = s.split()
    clean_toks = []
    nums = set()
    for t in tokens:
        if t in NUMBER_CANONICAL:
            canon = NUMBER_CANONICAL[t]
            nums.add(canon)
            clean_toks.append(canon)
        elif t not in NOISE_WORDS and len(t) >= 2:
            clean_toks.append(t)
    return clean_toks, nums

def detect_genre_families(tokens: set) -> set:
    detected = set()
    for fam, words in GENRE_FAMILIES.items():
        if any(w in tokens for w in words):
            detected.add(fam)
    return detected

def calculate_channel_match_score(query_raw: str, cand_raw: str, cand_tvg_id: str = "", cand_category: str = "", target_country: str = "", strict: bool = False) -> tuple:
    q_country = (target_country or "").strip().upper() or extract_country(query_raw)
    c_country = extract_country(cand_raw, cand_tvg_id, cand_category)

    q_toks, q_nums = clean_channel_tokens(query_raw)
    c_toks, c_nums = clean_channel_tokens(cand_raw)

    meta = {
        "country": c_country,
        "country_badge": get_country_badge(c_country),
        "target_country": q_country,
        "match_level": "none",
    }

    if not q_toks and not c_toks:
        return 0.0, meta

    q_str = " ".join(q_toks)
    c_str = " ".join(c_toks)

    # 1. Base string & token similarity
    if q_str == c_str:
        base_score = 1.0
    else:
        q_set = set(q_toks)
        c_set = set(c_toks)
        if not q_set:
            return 0.0, meta
        common_toks = q_set & c_set
        recall = len(common_toks) / len(q_set)
        precision = len(common_toks) / len(c_set) if c_set else 0.0
        f1 = (2 * recall * precision) / (recall + precision) if (recall + precision) > 0 else 0.0
        seq_ratio = difflib.SequenceMatcher(None, q_str, c_str).ratio()
        base_score = (0.65 * recall) + (0.20 * f1) + (0.15 * seq_ratio)

    score = base_score

    # 2. Number / Edition conflict detection
    if q_nums and c_nums:
        if q_nums.isdisjoint(c_nums):
            score -= 0.65
        else:
            score += 0.08
    elif q_nums and not c_nums:
        score -= 0.25
    elif not q_nums and c_nums:
        score -= 0.10

    # 3. Genre / Sub-brand family conflict detection
    q_genres = detect_genre_families(set(q_toks))
    c_genres = detect_genre_families(set(c_toks))
    if q_genres and c_genres:
        if q_genres.isdisjoint(c_genres):
            score -= 0.55

    # 4. Country affinity & mismatch penalties
    if q_country:
        if c_country:
            if q_country == c_country:
                score += 0.20
            else:
                score -= 0.55
                if strict:
                    return 0.0, meta
        else:
            score -= 0.06

    # 5. Exact tvg_id match bonus
    if score > 0.0 and cand_tvg_id and query_raw and cand_tvg_id.lower().replace(".", "").startswith(query_raw.lower().replace(" ", "")):
        score += 0.10

    score = max(0.0, min(1.0, score))

    if score >= 0.88:
        meta["match_level"] = "exact"
    elif score >= 0.72:
        meta["match_level"] = "strong"
    elif score >= 0.50:
        meta["match_level"] = "moderate"
    else:
        meta["match_level"] = "loose"

    return round(score, 3), meta

def clean_channel_name_for_match(name: str) -> str:
    clean_toks, _ = clean_channel_tokens(name)
    return " ".join(clean_toks)


def ensure_logo_catalog_loaded():
    with CATALOG_LOCK:
        if LOGO_CATALOG_CACHE["loaded"]:
            return LOGO_CATALOG_CACHE["items"]

    items = []
    by_id = {}
    by_clean = {}

    for entry in BUILTIN_LOGOS:
        clean = clean_channel_name_for_match(entry["name"])
        obj = {
            "name": entry["name"],
            "clean": clean,
            "url": entry["url"],
            "id": entry.get("id", ""),
            "source": "Curated HD",
        }
        items.append(obj)
        if obj["id"]:
            by_id[obj["id"].lower()] = obj["url"]
        if clean and clean not in by_clean:
            by_clean[clean] = obj["url"]

    try:
        req_logos = urllib.request.Request(
            "https://iptv-org.github.io/api/logos.json",
            headers={"User-Agent": "StreamForge-IPTV-Studio/3.0"},
        )
        with urllib.request.urlopen(req_logos, timeout=6.0) as resp:
            logos_data = json.loads(resp.read().decode("utf-8", errors="ignore"))

        seen_urls = {i["url"] for i in items}
        for item in logos_data:
            ch_id = item.get("channel") or ""
            url = item.get("url") or ""
            if not ch_id or not url or url in seen_urls:
                continue
            base_id = ch_id.split(".")[0]
            human_name = re.sub(r"([a-z])([A-Z0-9])", r"\1 \2", base_id)
            clean = clean_channel_name_for_match(human_name)
            if clean:
                obj = {
                    "name": human_name,
                    "clean": clean,
                    "url": url,
                    "id": ch_id,
                    "source": "IPTV-Org",
                }
                items.append(obj)
                seen_urls.add(url)
                if ch_id.lower() not in by_id:
                    by_id[ch_id.lower()] = url
                if clean not in by_clean:
                    by_clean[clean] = url
    except Exception:
        pass

    with CATALOG_LOCK:
        LOGO_CATALOG_CACHE["items"] = items
        LOGO_CATALOG_CACHE["by_id"] = by_id
        LOGO_CATALOG_CACHE["by_clean"] = by_clean
        LOGO_CATALOG_CACHE["loaded"] = True
    return items


def fast_lookup_logo(name: str, tvg_id: str = "") -> str:
    """Instant O(1) logo lookup for bulk operations."""
    ensure_logo_catalog_loaded()
    if tvg_id:
        hit = LOGO_CATALOG_CACHE["by_id"].get(tvg_id.lower())
        if hit:
            return hit
    clean = clean_channel_name_for_match(name)
    if clean:
        hit = LOGO_CATALOG_CACHE["by_clean"].get(clean)
        if hit:
            return hit
    return generate_svg_data_logo(name)


MULTI_STREAM_CACHE_FILE = DATA_DIR / "multi_source_catalog_cache.json"
STREAM_CACHE_FILE = DATA_DIR / "stream_catalog_cache.json"


def parse_provider_m3u_items(content: str, provider_name: str, provider_badge: str) -> list:
    """Fast streaming parser for remote M3U / M3U8 playlists."""
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    items = []
    current_info = None

    for line in lines:
        if line.startswith("#EXTINF:"):
            comma_idx = line.find(",")
            ch_name = line[comma_idx + 1:].strip() if comma_idx != -1 else ""
            inf_part = line[:comma_idx] if comma_idx != -1 else line
            attrs = dict(ATTR_REGEX.findall(inf_part))
            final_name = ch_name or attrs.get("tvg-name") or attrs.get("name") or "Live Stream"
            current_info = {
                "name": final_name,
                "tvg_id": attrs.get("tvg-id", ""),
                "logo": attrs.get("tvg-logo", ""),
                "category": attrs.get("group-title", "Live TV"),
            }
            continue

        if line.startswith(("http://", "https://", "rtmp://", "rtsp://")):
            if current_info is None:
                guessed = guess_name_from_url(line)
                current_info = {
                    "name": guessed,
                    "tvg_id": "",
                    "logo": "",
                    "category": "Live TV",
                }

            clean = clean_channel_name_for_match(current_info["name"])
            if clean:
                items.append({
                    "name": current_info["name"],
                    "clean": clean,
                    "tvg_id": current_info.get("tvg_id", ""),
                    "category": current_info.get("category", "Live TV"),
                    "url": line,
                    "logo": current_info.get("logo", ""),
                    "quality": "HD",
                    "source": provider_badge or provider_name,
                    "provider": provider_name,
                })
            current_info = None

    return items


def sync_all_provider_sources(force: bool = False) -> dict:
    """
    Fetches all enabled provider sources (Pluto TV, Samsung TV+, Plex, Roku, Tubi, Free-TV, IPTV-Org, etc.)
    concurrently, aggregates 25,000+ live streams, updates DB counts and writes multi_source_catalog_cache.json.
    """
    with get_db() as conn:
        providers = conn.execute(
            "SELECT * FROM provider_sources WHERE enabled = 1 ORDER BY position ASC"
        ).fetchall()
        providers = [dict(p) for p in providers]

    aggregated_streams = []
    seen_urls = set()

    # 1. Curated high-availability verified streams
    for entry in BUILTIN_STREAM_SOURCES:
        clean = clean_channel_name_for_match(entry["name"])
        item = {
            "name": entry["name"],
            "clean": clean,
            "tvg_id": entry.get("tvg_id", ""),
            "category": entry.get("category", "General"),
            "url": entry["url"],
            "logo": "",
            "quality": entry.get("quality", "HD"),
            "user_agent": entry.get("user_agent", ""),
            "referrer": entry.get("referrer", ""),
            "source": entry.get("source", "Verified CDN"),
            "provider": "Curated CDN",
        }
        aggregated_streams.append(item)
        seen_urls.add(entry["url"])

    def _fetch_one_provider(prov):
        p_url = prov["url"]
        p_name = prov["name"]
        p_badge = prov["badge"]
        p_id = prov["id"]
        try:
            req = urllib.request.Request(
                p_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamForge/4.0"},
            )
            with urllib.request.urlopen(req, timeout=14.0) as resp:
                text = resp.read().decode("utf-8", errors="ignore")
            parsed = parse_provider_m3u_items(text, p_name, p_badge)
            return p_id, parsed, None
        except Exception as e:
            return p_id, [], str(e)

    if providers:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_fetch_one_provider, providers))
    else:
        results = []

    now = int(time.time())
    with get_db() as conn:
        for p_id, p_items, err in results:
            conn.execute(
                "UPDATE provider_sources SET channel_count = ?, last_synced_at = ? WHERE id = ?",
                (len(p_items), now, p_id),
            )
            for itm in p_items:
                if itm["url"] not in seen_urls:
                    seen_urls.add(itm["url"])
                    aggregated_streams.append(itm)

    # Save to disk cache
    try:
        with open(MULTI_STREAM_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(aggregated_streams, f)
    except Exception:
        pass

    # Rebuild in-memory indexed structures
    by_clean = {}
    token_index = {}
    for idx, obj in enumerate(aggregated_streams):
        c = obj.get("clean") or clean_channel_name_for_match(obj.get("name", ""))
        obj["clean"] = c
        if c:
            by_clean.setdefault(c, []).append(obj)
            for tok in set(c.split()):
                if len(tok) >= 2:
                    token_index.setdefault(tok, []).append(idx)

    with CATALOG_LOCK:
        STREAM_SOURCES_CACHE["items"] = aggregated_streams
        STREAM_SOURCES_CACHE["by_clean"] = by_clean
        STREAM_SOURCES_CACHE["token_index"] = token_index
        STREAM_SOURCES_CACHE["loaded"] = True

    return {
        "total_streams": len(aggregated_streams),
        "providers_synced": len(providers),
        "synced_at": now,
    }


def ensure_stream_sources_loaded():
    """Loads & indexes Curated + Multi-Source catalog with disk caching for ultra-fast startup."""
    with CATALOG_LOCK:
        if STREAM_SOURCES_CACHE["loaded"]:
            return STREAM_SOURCES_CACHE

    items = []
    by_clean = {}
    token_index = {}

    def _add_item(obj):
        idx = len(items)
        items.append(obj)
        c = obj.get("clean") or clean_channel_name_for_match(obj.get("name", ""))
        obj["clean"] = c
        if c:
            by_clean.setdefault(c, []).append(obj)
            for tok in set(c.split()):
                if len(tok) >= 2:
                    token_index.setdefault(tok, []).append(idx)

    # 1. First add curated high-availability streams
    for entry in BUILTIN_STREAM_SOURCES:
        clean = clean_channel_name_for_match(entry["name"])
        _add_item({
            "name": entry["name"],
            "clean": clean,
            "tvg_id": entry.get("tvg_id", ""),
            "category": entry.get("category", "General"),
            "url": entry["url"],
            "quality": entry.get("quality", "HD"),
            "user_agent": entry.get("user_agent", ""),
            "referrer": entry.get("referrer", ""),
            "source": entry.get("source", "Verified CDN"),
            "provider": "Curated CDN",
        })

    # 2. Try loading cached streams from disk (fast boot)
    target_cache_file = MULTI_STREAM_CACHE_FILE if MULTI_STREAM_CACHE_FILE.exists() else STREAM_CACHE_FILE
    disk_items = None
    if target_cache_file.exists():
        try:
            with open(target_cache_file, "r", encoding="utf-8") as f:
                disk_items = json.load(f)
        except Exception:
            disk_items = None

    seen_urls = {i["url"] for i in items}
    if disk_items and isinstance(disk_items, list):
        for obj in disk_items:
            url = (obj.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            _add_item(obj)
            seen_urls.add(url)
    else:
        # Background worker to fetch and populate disk cache across all sources
        threading.Thread(target=sync_all_provider_sources, daemon=True).start()

    with CATALOG_LOCK:
        STREAM_SOURCES_CACHE["items"] = items
        STREAM_SOURCES_CACHE["by_clean"] = by_clean
        STREAM_SOURCES_CACHE["token_index"] = token_index
        STREAM_SOURCES_CACHE["loaded"] = True
    return STREAM_SOURCES_CACHE


def match_stream_candidates_fast(query: str, tvg_id: str = "", limit: int = 8, target_country: str = "", strict: bool = False) -> list:
    """Fast indexed lookup of stream candidates across multiple providers with multi-dimensional precision scoring."""
    cache = ensure_stream_sources_loaded()
    items = cache["items"]
    by_clean = cache["by_clean"]
    token_index = cache["token_index"]

    if not query and not tvg_id:
        return []

    # Target country: explicitly passed or extracted from query
    target_country = (target_country or "").strip().upper() or extract_country(query)
    q_toks, q_nums = clean_channel_tokens(query)
    clean_q = " ".join(q_toks)

    scored = []
    seen_urls = set()

    # 1. Exact clean name hits (direct index lookup)
    if clean_q and clean_q in by_clean:
        for item in by_clean[clean_q]:
            if item["url"] not in seen_urls:
                score, meta = calculate_channel_match_score(
                    query, item["name"], item.get("tvg_id", ""), item.get("category", ""), target_country, strict=strict
                )
                if item.get("source") == "Verified CDN":
                    score = min(1.0, score + 0.04)
                if score >= (0.70 if strict else 0.45):
                    scored.append((score, item, meta))
                    seen_urls.add(item["url"])

    # 2. Gather candidates from token index
    candidate_indices = set()
    for tok in q_toks:
        for idx in token_index.get(tok, []):
            candidate_indices.add(idx)

    # 3. Fallback scan if few token hits
    if len(candidate_indices) < 40 and clean_q:
        for idx, item in enumerate(items[:5000]):
            c = item["clean"]
            if clean_q in c or c in clean_q or any(tok in c for tok in q_toks):
                candidate_indices.add(idx)

    for idx in candidate_indices:
        item = items[idx]
        if item["url"] in seen_urls:
            continue

        score, meta = calculate_channel_match_score(
            query, item["name"], item.get("tvg_id", ""), item.get("category", ""), target_country, strict=strict
        )
        if tvg_id and item.get("tvg_id") and item["tvg_id"].lower() == tvg_id.lower():
            score = 1.0
            meta["match_level"] = "exact"
        elif item.get("source") == "Verified CDN":
            score = min(1.0, score + 0.03)

        min_thresh = 0.70 if strict else 0.45
        if score >= min_thresh:
            scored.append((score, item, meta))
            seen_urls.add(item["url"])

    # Sort strictly by score first!
    scored.sort(key=lambda x: (x[0], 1 if x[1].get("source") == "Verified CDN" else 0), reverse=True)

    # Provider diversity only applies within the TOP-TIER scores (e.g. within 0.06 of top score)
    top_score = scored[0][0] if scored else 0.0
    top_tier = [x for x in scored if x[0] >= max(0.75, top_score - 0.08)]
    lower_tier = [x for x in scored if x not in top_tier]

    grouped_by_source = {}
    for sc, itm, mt in top_tier:
        prov = itm.get("source") or itm.get("provider") or "Feed"
        grouped_by_source.setdefault(prov, []).append((sc, itm, mt))

    diversified = []
    while len(diversified) < limit and any(grouped_by_source.values()):
        for prov_key in list(grouped_by_source.keys()):
            if grouped_by_source[prov_key]:
                diversified.append(grouped_by_source[prov_key].pop(0))
                if len(diversified) >= limit:
                    break

    if len(diversified) < limit:
        for item in lower_tier:
            if len(diversified) >= limit:
                break
            diversified.append(item)

    out = []
    for score, item, meta in diversified[:limit]:
        final_logo = item.get("logo") or fast_lookup_logo(item["name"], item.get("tvg_id", ""))
        c_badge = meta.get("country_badge") or get_country_badge(extract_country(item["name"], item.get("tvg_id", ""), item.get("category", "")))
        out.append({
            "id": str(uuid.uuid4()),
            "query_name": query,
            "name": item["name"],
            "tvg_id": item.get("tvg_id", ""),
            "category": item.get("category", "Live TV"),
            "url": item["url"],
            "quality": item.get("quality", "HD"),
            "user_agent": item.get("user_agent", ""),
            "referrer": item.get("referrer", ""),
            "source": item.get("source", "Live Index"),
            "provider": item.get("provider", ""),
            "confidence": round(score * 100),
            "match_confidence": round(score * 100),
            "country": meta.get("country", ""),
            "country_code": meta.get("country", ""),
            "country_badge": c_badge,
            "match_level": meta.get("match_level", "strong"),
            "logo": final_logo,
            "status": "unknown",
            "latency_ms": 0,
        })
    return out



def search_live_web_for_channel_streams(query: str, limit: int = 6, check_live: bool = True, target_country: str = "") -> list:
    """
    Performs live web and open-source stream discovery using DuckDuckGo HTML search.
    Scrapes .m3u8 stream URLs and raw GitHub/web playlists, extracts streams matching query,
    probes every discovered stream live, and returns verified options.
    """
    clean_q = clean_channel_name_for_match(query)
    if not clean_q:
        return []

    target_country = (target_country or "").strip().upper() or extract_country(query)
    search_q = f"{query} {target_country} m3u8 live stream iptv" if target_country else f"{query} m3u8 live stream iptv"
    post_data = urllib.parse.urlencode({"q": search_q}).encode("utf-8")
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.7",
    }

    discovered_candidates = []
    seen_urls = set()

    try:
        req = urllib.request.Request(url, data=post_data, headers=headers)
        with urllib.request.urlopen(req, timeout=8.5) as resp:
            html_text = resp.read().decode("utf-8", errors="ignore")

        # 1. Direct .m3u8 links in HTML text or snippets
        direct_m3u8s = re.findall(r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?', html_text)
        for u in direct_m3u8s:
            if u not in seen_urls:
                seen_urls.add(u)
                discovered_candidates.append({
                    "name": query,
                    "url": u,
                    "source": "🌐 Web Live Search",
                    "provider": "Web Live Search",
                    "quality": "HD",
                })

        # 2. Extract result URLs from DuckDuckGo HTML
        found_urls = re.findall(r'class="result__url[^"]*"[^>]*href="([^"]+)"', html_text)
        if not found_urls:
            raw_hrefs = re.findall(r'href="([^"]+)"', html_text)
            for h in raw_hrefs:
                if "uddg=" in h:
                    try:
                        actual = urllib.parse.unquote(h.split("uddg=")[1].split("&")[0])
                        found_urls.append(actual)
                    except Exception:
                        pass
                elif h.startswith("http"):
                    found_urls.append(h)

        playlist_urls_to_fetch = []
        for actual in found_urls:
            if ".m3u8" in actual and actual not in seen_urls:
                m = re.findall(r'https?://[^\s"\'<>]+?\.m3u8(?:\?[^\s"\'<>]*)?', actual)
                for h in m:
                    if h not in seen_urls:
                        seen_urls.add(h)
                        discovered_candidates.append({
                            "name": query,
                            "url": h,
                            "source": "🌐 Web Live Search",
                            "provider": "Web Live Search",
                            "quality": "HD",
                        })
            elif "github.com" in actual and ("/blob/" in actual or ".m3u" in actual):
                raw_target = actual.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
                if raw_target not in playlist_urls_to_fetch:
                    playlist_urls_to_fetch.append(raw_target)
            elif "gist.github.com" in actual:
                raw_target = actual if "/raw" in actual else actual + "/raw"
                if raw_target not in playlist_urls_to_fetch:
                    playlist_urls_to_fetch.append(raw_target)
            elif "pastebin.com/raw" in actual and actual not in playlist_urls_to_fetch:
                playlist_urls_to_fetch.append(actual)

        # 3. Fetch up to 4 web playlists discovered in search results and extract matching channels
        for p_url in playlist_urls_to_fetch[:4]:
            try:
                p_req = urllib.request.Request(p_url, headers={"User-Agent": "Mozilla/5.0 StreamForge/4.0"})
                with urllib.request.urlopen(p_req, timeout=5.5) as p_resp:
                    p_body = p_resp.read(500000).decode("utf-8", errors="ignore")
                parsed_chans = parse_provider_m3u_items(p_body, "GitHub Web Search", "🐙 GitHub Direct")
                for ch in parsed_chans:
                    c_clean = ch.get("clean") or clean_channel_name_for_match(ch.get("name", ""))
                    if clean_q in c_clean or c_clean in clean_q or any(tok in c_clean for tok in clean_q.split() if len(tok) >= 3):
                        if ch["url"] not in seen_urls:
                            seen_urls.add(ch["url"])
                            discovered_candidates.append({
                                "name": ch.get("name") or query,
                                "url": ch["url"],
                                "source": "🐙 GitHub Direct",
                                "provider": "GitHub Web Discovery",
                                "quality": ch.get("quality", "HD"),
                                "logo": ch.get("logo", ""),
                                "tvg_id": ch.get("tvg_id", ""),
                            })
            except Exception:
                pass
    except Exception:
        pass

    results = []
    for cand in discovered_candidates:
        score, meta = calculate_channel_match_score(
            query, cand["name"], cand.get("tvg_id", ""), cand.get("category", ""), target_country=target_country
        )
        if score < 0.45:
            continue
        c_badge = meta.get("country_badge") or get_country_badge(extract_country(cand["name"], cand.get("tvg_id", ""), cand.get("category", "")))
        results.append({
            "id": str(uuid.uuid4()),
            "query_name": query,
            "name": cand["name"],
            "tvg_id": cand.get("tvg_id", ""),
            "category": "Web Stream",
            "url": cand["url"],
            "quality": cand.get("quality", "HD"),
            "user_agent": "",
            "referrer": "",
            "source": cand["source"],
            "provider": cand["provider"],
            "confidence": round(score * 100),
            "country_code": meta.get("country", ""),
            "country_badge": c_badge,
            "match_level": meta.get("match_level", "strong"),
            "logo": cand.get("logo") or fast_lookup_logo(cand["name"]),
            "status": "unknown",
            "latency_ms": 0,
        })
        if len(results) >= limit:
            break

    if check_live and results:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            probes = list(pool.map(lambda c: probe_stream(c, timeout=3.5), results))
        for cand, pr in zip(results, probes):
            cand["status"] = pr["status"]
            cand["latency_ms"] = pr["latency_ms"]
            cand["error"] = pr.get("error", "")

        def _web_rank_key(c):
            status_rank = 0 if c.get("status") == "online" else (1 if c.get("status") == "warning" else 2)
            conf = c.get("confidence", 0)
            tier = 0 if conf >= 75 else (1 if conf >= 55 else 2)
            lat = c.get("latency_ms", 0)
            return (status_rank, tier, -conf, lat if lat > 0 else 9999)

        results.sort(key=_web_rank_key)

    return results


def search_github_iptv_repositories(query: str, limit: int = 8) -> list:
    """
    Searches GitHub for public open-source IPTV playlists, repositories, and channel collections.
    Finds .m3u / .m3u8 playlist files in each discovered repository.
    """
    clean_q = re.sub(r'[^a-zA-Z0-9\s]', '', query or 'iptv').strip()
    encoded = urllib.parse.quote_plus(f"{clean_q} iptv m3u")
    url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&order=desc&per_page={limit}"
    headers = {
        "User-Agent": "StreamForge-IPTV-Finder/4.0 (Windows NT 10.0)",
        "Accept": "application/vnd.github.v3+json",
    }
    repos_out = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=9.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        items = data.get("items", [])

        def _inspect_repo(r):
            full_name = r.get("full_name", "")
            stars = r.get("stargazers_count", 0)
            desc = r.get("description") or "Open IPTV collection on GitHub"
            html_url = r.get("html_url", "")
            default_branch = r.get("default_branch", "main")
            m3u_files = []
            try:
                c_url = f"https://api.github.com/repos/{full_name}/contents"
                c_req = urllib.request.Request(c_url, headers=headers)
                with urllib.request.urlopen(c_req, timeout=4.0) as c_resp:
                    c_data = json.loads(c_resp.read().decode("utf-8", errors="ignore"))
                for entry in c_data:
                    name = entry.get("name", "")
                    if name.lower().endswith((".m3u", ".m3u8")):
                        d_url = entry.get("download_url") or f"https://raw.githubusercontent.com/{full_name}/{default_branch}/{name}"
                        m3u_files.append({
                            "name": name,
                            "download_url": d_url,
                            "size": entry.get("size", 0),
                        })
            except Exception:
                pass

            if not m3u_files:
                m3u_files.append({
                    "name": "playlist.m3u",
                    "download_url": f"https://raw.githubusercontent.com/{full_name}/{default_branch}/playlist.m3u",
                    "size": 0,
                })

            return {
                "full_name": full_name,
                "stars": stars,
                "description": desc,
                "html_url": html_url,
                "default_branch": default_branch,
                "files": m3u_files,
            }

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            repos_out = list(pool.map(_inspect_repo, items[:limit]))

    except Exception as e:
        print("GitHub repo search error:", e)

    return repos_out


def preview_github_playlist_channels(download_url: str, filter_query: str = "") -> dict:
    """
    Downloads and inspects a discovered GitHub M3U playlist file,
    extracts channel names, logos, categories, stream URLs, and allows testing.
    """
    if not download_url or not download_url.startswith(("http://", "https://")):
        return {"error": "Invalid playlist download URL"}

    req = urllib.request.Request(
        download_url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamForge/4.0"}
    )
    with urllib.request.urlopen(req, timeout=12.0) as resp:
        content = resp.read(5 * 1024 * 1024).decode("utf-8", errors="ignore")

    parsed = parse_provider_m3u_items(content, "GitHub Playlist", "🐙 GitHub")
    if filter_query:
        clean_f = clean_channel_name_for_match(filter_query)
        parsed = [ch for ch in parsed if clean_f in (ch.get("clean") or "") or (ch.get("clean") or "") in clean_f]

    return {
        "url": download_url,
        "total_channels": len(parsed),
        "channels": parsed[:100],
    }


class QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


YT_LIVE_MANIFEST_CACHE = {}
YT_CACHE_LOCK = threading.Lock()

def resolve_youtube_live_manifest(yt_url_or_id: str) -> str:
    """
    Extracts and caches the direct live HLS .m3u8 manifest for any YouTube video ID or channel URL.
    Cached for 2 hours to avoid repeat API hits while preventing token expiration.
    Silently rejects invalid IDs (e.g. channel IDs without live stream) with 0ms overhead.
    """
    if not yt_url_or_id:
        return ""
    clean_id = yt_url_or_id.strip()
    if "v=" in clean_id:
        clean_id = clean_id.split("v=")[1].split("&")[0]
    elif "youtu.be/" in clean_id:
        clean_id = clean_id.split("youtu.be/")[1].split("?")[0]
    elif "/channel/" in clean_id:
        clean_id = clean_id.split("/channel/")[1].split("/")[0].split("?")[0]
    elif "/live/" in clean_id:
        clean_id = clean_id.split("/live/")[1].split("/")[0].split("?")[0]

    now = time.time()
    with YT_CACHE_LOCK:
        if clean_id in YT_LIVE_MANIFEST_CACHE:
            cached_url, cached_at = YT_LIVE_MANIFEST_CACHE[clean_id]
            if now - cached_at < 7200:
                return cached_url

    # Check ID type
    if re.match(r"^[a-zA-Z0-9_-]{11}$", clean_id):
        watch_url = f"https://www.youtube.com/watch?v={clean_id}"
    elif clean_id.startswith("UC") and len(clean_id) >= 20:
        watch_url = f"https://www.youtube.com/channel/{clean_id}/live"
    elif clean_id.startswith(("http://", "https://")):
        watch_url = clean_id
    else:
        return ""

    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "logger": QuietLogger(),
            "skip_download": True,
            "socket_timeout": 3.5,
            "retries": 0,
            "nocheckcertificate": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            v_info = ydl.extract_info(watch_url, download=False)
            if not v_info:
                return ""
            formats = v_info.get("formats", []) or []
            hls_formats = [
                f for f in formats
                if f.get("protocol") in ("m3u8", "m3u8_native") or ".m3u8" in f.get("url", "")
            ]
            if hls_formats:
                best_hls = hls_formats[-1]["url"]
                with YT_CACHE_LOCK:
                    YT_LIVE_MANIFEST_CACHE[clean_id] = (best_hls, now)
                return best_hls
    except Exception:
        pass
    return ""


def search_youtube_live_streams_fast(channel_name: str, target_country: str = "", strict: bool = True, limit: int = 3, check_live: bool = True) -> list:
    """
    Searches YouTube for active 24/7 live television broadcasts matching channel_name.
    Applies multi-factor precision scoring (uploader & title) and strictly rejects non-videos, VODs, channels, and playlists.
    Resolves live HLS .m3u8 URLs and probes latency.
    """
    if not channel_name or len(channel_name.strip()) < 2:
        return []

    search_q = f"ytsearch5:{channel_name.strip()} live"
    candidates = []

    try:
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "logger": QuietLogger(),
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 3.5,
            "retries": 0,
            "nocheckcertificate": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(search_q, download=False)
            entries = res.get("entries", []) or []

            for item in entries:
                if not item:
                    continue

                # Reject non-video entries (channels, playlists, shelves, tabs)
                ie_key = (item.get("ie_key") or "").lower()
                if ie_key in ("youtubetab", "youtubechannel", "youtubeplaylist") or item.get("_type") in ("channel", "playlist"):
                    continue

                raw_url = item.get("url") or ""
                if "/channel/" in raw_url or "/user/" in raw_url or "/c/" in raw_url:
                    continue

                vid_id = (item.get("id") or "").strip()
                # A valid YouTube video ID is strictly 11 chars
                if not vid_id or len(vid_id) != 11 or not re.match(r"^[a-zA-Z0-9_-]{11}$", vid_id) or vid_id.startswith("UC"):
                    continue

                title = item.get("title") or ""
                uploader = item.get("channel") or item.get("uploader") or ""
                duration = item.get("duration")
                live_status = item.get("live_status")

                # In YouTube flat search:
                # Live streams have live_status == 'is_live' or (duration is None and live_status != 'was_live')
                if live_status == "was_live":
                    continue
                is_live = (live_status == "is_live") or (duration is None)
                if not is_live:
                    continue

                # Precision scoring against channel name
                score_u, meta_u = calculate_channel_match_score(channel_name, uploader, target_country=target_country, strict=strict)
                score_t, meta_t = calculate_channel_match_score(channel_name, title, target_country=target_country, strict=strict)

                best_score = max(score_u, score_t)
                best_meta = meta_u if score_u >= score_t else meta_t

                min_thresh = 0.65 if strict else 0.45
                if best_score < min_thresh:
                    continue

                candidates.append({
                    "video_id": vid_id,
                    "uploader": uploader,
                    "title": title,
                    "score": best_score,
                    "meta": best_meta,
                    "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg",
                })
    except Exception:
        return []

    candidates.sort(key=lambda c: c["score"], reverse=True)

    results = []
    for cand in candidates[:limit]:
        vid_id = cand["video_id"]
        hls_url = resolve_youtube_live_manifest(vid_id)
        if not hls_url:
            continue

        proxy_stream_url = f"/api/proxy-stream?yt={vid_id}"
        pr = probe_stream({"name": cand["title"], "url": hls_url}, timeout=2.5) if check_live else {"status": "online", "latency_ms": 200}

        results.append({
            "id": str(uuid.uuid4()),
            "name": f"{cand['uploader']} (YouTube Live)",
            "query_name": channel_name,
            "raw_title": cand["title"],
            "video_id": vid_id,
            "url": proxy_stream_url,
            "direct_hls_url": hls_url,
            "watch_url": f"https://www.youtube.com/watch?v={vid_id}",
            "quality": "1080p",
            "source": "▶️ YouTube Live",
            "provider": "YouTube Live Broadcast",
            "logo": cand["thumbnail"] or fast_lookup_logo(cand["uploader"]),
            "country": cand["meta"].get("country", ""),
            "country_badge": cand["meta"].get("country_badge", ""),
            "match_confidence": round(cand["score"] * 100),
            "confidence": round(cand["score"] * 100),
            "status": pr.get("status", "online"),
            "latency_ms": pr.get("latency_ms", 0),
            "category": "Live TV",
        })

    return results


RUTUBE_MANIFEST_CACHE = {}
RUTUBE_CACHE_LOCK = threading.Lock()


def resolve_rutube_live_manifest(rutube_url_or_id: str) -> str:
    """
    Extracts and caches the direct live HLS .m3u8 manifest for any RuTube video ID or live stream URL.
    RuTube live manifests have ~10-year expiration tokens (e.g. valid through 2036).
    Cached for 4 hours to avoid repeat API hits.
    """
    if not rutube_url_or_id:
        return ""
    clean_id = rutube_url_or_id.strip()
    match = re.search(r"([0-9a-fA-F]{32})", clean_id)
    if match:
        video_id = match.group(1).lower()
    else:
        return ""

    now = time.time()
    with RUTUBE_CACHE_LOCK:
        if video_id in RUTUBE_MANIFEST_CACHE:
            cached_url, cached_at = RUTUBE_MANIFEST_CACHE[video_id]
            if now - cached_at < 14400:
                return cached_url

    play_url = f"https://rutube.ru/api/play/options/{video_id}/?format=json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://rutube.ru/video/{video_id}/",
        "Accept": "application/json, text/plain, */*",
    }

    try:
        req = urllib.request.Request(play_url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            live_streams = (data.get("live_streams") or {}).get("hls", [])
            if live_streams and isinstance(live_streams, list):
                hls_url = live_streams[0].get("url")
                if hls_url:
                    with RUTUBE_CACHE_LOCK:
                        RUTUBE_MANIFEST_CACHE[video_id] = (hls_url, now)
                    return hls_url

            balancer = data.get("video_balancer") or {}
            m3u8_url = balancer.get("m3u8")
            if m3u8_url:
                with RUTUBE_CACHE_LOCK:
                    RUTUBE_MANIFEST_CACHE[video_id] = (m3u8_url, now)
                return m3u8_url
    except Exception:
        pass
    return ""


def search_rutube_live_streams_fast(channel_name: str, target_country: str = "", strict: bool = True, limit: int = 3, check_live: bool = True) -> list:
    """
    Searches RuTube for active 24/7 official live television broadcasts matching channel_name.
    Uses native REST API with zero-auth, precision scores author and title,
    and resolves the direct live HLS .m3u8 manifest (with 10-year expiration).
    """
    if not channel_name or len(channel_name.strip()) < 2:
        return []

    clean_name = channel_name.strip()
    query = f"{clean_name} прямой эфир"
    search_url = f"https://rutube.ru/api/search/video/?query={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }

    candidates = []
    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("results", []) or []

            for item in results:
                if not item:
                    continue
                is_live = item.get("is_livestream") is True
                if not is_live:
                    continue

                vid_id = (item.get("id") or "").strip()
                if not vid_id or len(vid_id) != 32:
                    continue

                title = (item.get("title") or "").strip()
                author_obj = item.get("author") or {}
                author_name = (author_obj.get("name") or "").strip()
                avatar_url = author_obj.get("avatar_url") or item.get("picture") or ""

                score_u, meta_u = calculate_channel_match_score(clean_name, author_name, target_country=target_country, strict=strict)
                score_t, meta_t = calculate_channel_match_score(clean_name, title, target_country=target_country, strict=strict)

                best_score = max(score_u, score_t)
                best_meta = meta_u if score_u >= score_t else meta_t

                min_thresh = 0.55 if strict else 0.40
                if best_score < min_thresh:
                    continue

                candidates.append({
                    "video_id": vid_id,
                    "author": author_name,
                    "title": title,
                    "score": best_score,
                    "meta": best_meta,
                    "avatar": avatar_url,
                })
    except Exception:
        return []

    candidates.sort(key=lambda c: c["score"], reverse=True)

    results = []
    for cand in candidates[:limit]:
        vid_id = cand["video_id"]
        hls_url = resolve_rutube_live_manifest(vid_id)
        if not hls_url:
            continue

        pr = probe_stream({"name": cand["title"], "url": hls_url}, timeout=2.5) if check_live else {"status": "online", "latency_ms": 220}

        results.append({
            "id": str(uuid.uuid4()),
            "name": f"{cand['author'] or cand['title']} (RuTube Live)",
            "query_name": clean_name,
            "raw_title": cand["title"],
            "video_id": vid_id,
            "url": hls_url,
            "direct_hls_url": hls_url,
            "watch_url": f"https://rutube.ru/video/{vid_id}/",
            "quality": "1080p",
            "source": "🟢 RuTube Live",
            "provider": "RuTube Live Broadcast",
            "logo": cand["avatar"] or fast_lookup_logo(cand["author"] or clean_name),
            "country": cand["meta"].get("country", "RU"),
            "country_badge": cand["meta"].get("country_badge", "🇷🇺 RU"),
            "match_confidence": round(cand["score"] * 100),
            "confidence": round(cand["score"] * 100),
            "status": pr.get("status", "online"),
            "latency_ms": pr.get("latency_ms", 0),
            "category": "Live TV",
        })

    return results


def search_dailymotion_live_streams_fast(channel_name: str, limit: int = 2, check_live: bool = True) -> list:
    """
    Searches Dailymotion for verified live broadcasts using official public REST API.
    Resolves live HLS stream using yt-dlp quietly.
    """
    if not channel_name or len(channel_name.strip()) < 2:
        return []

    clean_name = channel_name.strip()
    url = f"https://api.dailymotion.com/videos?search={urllib.parse.quote(clean_name)}&filters=live&limit=4&fields=id,title,owner.screenname,thumbnail_url"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("list", []) or []
    except Exception:
        return []

    results = []
    for it in items:
        vid_id = it.get("id")
        title = it.get("title", "")
        owner = it.get("owner.screenname", "")
        score_u, meta_u = calculate_channel_match_score(clean_name, owner, strict=False)
        score_t, meta_t = calculate_channel_match_score(clean_name, title, strict=False)
        best_score = max(score_u, score_t)
        if best_score < 0.50:
            continue

        dm_url = f"https://www.dailymotion.com/video/{vid_id}"
        hls_url = ""
        try:
            import yt_dlp
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "logger": QuietLogger(),
                "skip_download": True,
                "socket_timeout": 3.0,
                "retries": 0,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(dm_url, download=False)
                if info and info.get("formats"):
                    for f in info["formats"]:
                        if f.get("protocol") in ("m3u8", "m3u8_native") or ".m3u8" in f.get("url", ""):
                            hls_url = f["url"]
                            break
        except Exception:
            pass

        if not hls_url:
            continue

        pr = probe_stream({"name": title, "url": hls_url}, timeout=2.5) if check_live else {"status": "online", "latency_ms": 250}
        results.append({
            "id": str(uuid.uuid4()),
            "name": f"{owner or title} (Dailymotion Live)",
            "query_name": clean_name,
            "raw_title": title,
            "video_id": vid_id,
            "url": hls_url,
            "direct_hls_url": hls_url,
            "watch_url": dm_url,
            "quality": "HD",
            "source": "🌐 Dailymotion Live",
            "provider": "Dailymotion Live Broadcast",
            "logo": it.get("thumbnail_url") or fast_lookup_logo(clean_name),
            "country": "",
            "country_badge": "",
            "match_confidence": round(best_score * 100),
            "confidence": round(best_score * 100),
            "status": pr.get("status", "online"),
            "latency_ms": pr.get("latency_ms", 0),
            "category": "Live TV",
        })
        if len(results) >= limit:
            break

    return results


# ---------------------------------------------------------------------------
# Televizo.live Live Stream Extractor Engine
# ---------------------------------------------------------------------------

TELEVIZO_CATALOG_LOCK = threading.Lock()
TELEVIZO_CATALOG_CACHE = None

def get_televizo_catalog() -> list:
    global TELEVIZO_CATALOG_CACHE
    with TELEVIZO_CATALOG_LOCK:
        if TELEVIZO_CATALOG_CACHE is not None:
            return TELEVIZO_CATALOG_CACHE
        cat_file = DATA_DIR / "televizo_catalog.json"
        if not cat_file.exists():
            alt = Path(r"C:\Users\Jokubas\Pictures\output\channels.json")
            if alt.exists():
                try:
                    import shutil
                    shutil.copy2(alt, cat_file)
                except Exception:
                    pass
        if not cat_file.exists():
            return []
        try:
            with open(cat_file, "r", encoding="utf-8") as f:
                TELEVIZO_CATALOG_CACHE = json.load(f)
                return TELEVIZO_CATALOG_CACHE
        except Exception:
            return []

def _clean_channel_for_televizo(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r"^[A-Z]{2,3}\s*[:|\-•]\s*", "", s)
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", s)
    s = re.sub(r"\b(UHD|4K|FHD|HD|SD|HEVC|H265|H264|VIP|RAW|1080p|720p)\b", "", s, flags=re.IGNORECASE)
    return re.sub(r"[^a-zA-Z0-9\u0400-\u04FF]", "", s.lower())


def find_televizo_channel_info(query: str) -> dict:
    catalog = get_televizo_catalog()
    if not catalog or not query:
        return {}
    q_norm = _clean_channel_for_televizo(query)
    if not q_norm or len(q_norm) < 2:
        return {}

    # 1. Exact normalized channel name match
    for ch in catalog:
        c_norm = _clean_channel_for_televizo(ch.get("name", ""))
        if c_norm and c_norm == q_norm:
            return ch

    # 2. Exact page slug match
    for ch in catalog:
        page = ch.get("page", "").lower()
        slug = page.rsplit("/", 1)[-1].replace(".html", "").replace("-", "")
        if slug and slug == q_norm:
            return ch

    # 3. High-confidence fuzzy match (only for longer names >= 5 chars to prevent false positives like BTV -> EnglishClubTV)
    if len(q_norm) >= 5:
        best_ch = None
        best_ratio = 0.0
        for ch in catalog:
            c_norm = _clean_channel_for_televizo(ch.get("name", ""))
            if not c_norm:
                continue
            ratio = difflib.SequenceMatcher(None, q_norm, c_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_ch = ch
        if best_ch and best_ratio >= 0.86:
            return best_ch

    return {}


def extract_televizo_stream_sync(page_or_name: str, timeout_sec: int = 12) -> dict:
    """
    Extracts live .m3u8 stream from televizo.live using Playwright headless Chromium.
    Accepts direct televizo URL or channel name.
    """
    page_url = ""
    ch_info = {}
    if page_or_name.startswith("http://") or page_or_name.startswith("https://"):
        page_url = page_or_name
    else:
        ch_info = find_televizo_channel_info(page_or_name)
        page_url = ch_info.get("page", "")

    if not page_url:
        return {"found": False, "error": f"No televizo.live page found for '{page_or_name}'"}

    ad_domains = ["bl.rutube.ru/route", "rtbcdn.ru/user/"]
    streams = []

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--mute-audio",
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                ignore_https_errors=True,
            )
            page = context.new_page()

            def _filter_route(route):
                try:
                    u = route.request.url.lower()
                    if ".m3u8" in u:
                        route.continue_()
                        return
                    if route.request.resource_type in ("image", "font"):
                        route.abort()
                        return
                    route.continue_()
                except Exception:
                    pass

            page.route("**/*", _filter_route)

            def _capture_url(u: str, content_type: str = ""):
                u_low = u.lower()
                if any(ad in u_low for ad in ad_domains):
                    return
                parsed_u = urllib.parse.urlparse(u_low)
                path_only = parsed_u.path or ""
                # Never capture HTML player wrapper pages (e.g. playersjs.html?file=...m3u8) or static assets
                if path_only.endswith((".html", ".htm", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ico")):
                    return
                is_hls_ct = "mpegurl" in (content_type or "").lower()
                is_hls_url = ".m3u8" in path_only or ("livetv.php" in path_only and ".m3u8" in u_low) or ".m3u8" in u_low
                if is_hls_ct or is_hls_url:
                    if u not in streams:
                        streams.append(u)

            def _on_resp(resp):
                try:
                    ct = resp.headers.get("content-type", "")
                    if 200 <= resp.status < 400:
                        _capture_url(resp.url, ct)
                except Exception:
                    pass

            page.on("request", lambda req: _capture_url(req.url))
            page.on("response", _on_resp)

            t0 = time.time()
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)

            for _ in range(int(timeout_sec / 1.2)):
                if streams:
                    time.sleep(0.6)  # Allow redirect target (200 OK) to also be captured
                    break
                time.sleep(0.8)
                for frame in page.frames:
                    try:
                        for sel in [".vjs-big-play-button", "button.play", "video", ".play-btn", "div.play"]:
                            loc = frame.locator(sel).first
                            if loc.count() > 0:
                                loc.click(timeout=300)
                    except Exception:
                        pass

            try:
                page.unroute_all(behavior="ignoreErrors")
            except Exception:
                pass
            context.close()
            browser.close()
            elapsed = round(time.time() - t0, 2)
            if streams:
                # Prefer direct signed token URLs over 302 redirector URLs when available
                streams.sort(
                    key=lambda s: (
                        200 if ("tvcdnpotok" in s and "wmsAuthSign" not in s) else
                        150 if ("cdntvtech" in s or "cdntvmedia" in s) else
                        100 if "tvcdnpotok" in s else
                        (50 if ("index.m3u8" in s and "rtbcdn.ru" not in s) else (5 if "rtbcdn.ru" in s else 10))
                    ),
                    reverse=True,
                )
                best_u = streams[0]
                ref_hdr = ""
                if "cdntvtech" in best_u.lower():
                    pu = urllib.parse.urlparse(best_u)
                    ref_hdr = f"{pu.scheme}://{pu.netloc}/"
                return {
                    "found": True,
                    "url": best_u,
                    "referrer": ref_hdr,
                    "all_urls": streams,
                    "page": page_url,
                    "name": ch_info.get("name", page_or_name),
                    "logo": ch_info.get("logo", ""),
                    "category": ch_info.get("category", "Televizo Live"),
                    "elapsed_sec": elapsed,
                    "source": "📺 Televizo Live",
                }
            return {"found": False, "error": "No stream captured within timeout", "page": page_url, "elapsed_sec": elapsed}
    except Exception as e:
        return {"found": False, "error": str(e), "page": page_url}

def search_televizo_live_streams_fast(channel_name: str, check_live: bool = True) -> list:
    """Finds and extracts the live stream for a channel from televizo.live if available."""
    if not channel_name or len(channel_name.strip()) < 2:
        return []
    info = find_televizo_channel_info(channel_name)
    if not info or not info.get("page"):
        return []

    res = extract_televizo_stream_sync(info["page"], timeout_sec=10)
    if not res.get("found") or not res.get("url"):
        return []

    u = res["url"]
    pr = probe_stream({"name": info.get("name", channel_name), "url": u}, timeout=3.0) if check_live else {"status": "online", "latency_ms": 180}
    return [{
        "id": str(uuid.uuid4()),
        "name": f"{info.get('name', channel_name)} (Televizo Live)",
        "query_name": channel_name,
        "raw_title": info.get("name", channel_name),
        "url": u,
        "direct_hls_url": u,
        "watch_url": info.get("page", ""),
        "quality": "HD",
        "source": "📺 Televizo Live",
        "provider": "Televizo Live Extractor",
        "logo": info.get("logo") or fast_lookup_logo(channel_name),
        "country": "RU",
        "country_badge": "🇷🇺 RU",
        "match_confidence": 100,
        "confidence": 100,
        "status": pr.get("status", "online"),
        "latency_ms": pr.get("latency_ms", 180),
        "category": info.get("category", "Televizo Live"),
    }]

def sync_televizo_playlist_streams(playlist_id: str = None, background: bool = True) -> dict:
    """
    Scans channels in playlist (or all playlists if playlist_id is None) matching televizo channels
    or containing tvcdnpotok/cdntvmedia/cdntvtech, extracts fresh working .m3u8 tokens using Playwright,
    and updates channels and backup sources in DB.
    """
    with get_db() as conn:
        if playlist_id and playlist_id != "all":
            rows = conn.execute(
                "SELECT id, name, url, referrer, status, playlist_id FROM channels WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, url, referrer, status, playlist_id FROM channels"
            ).fetchall()
        channels = [dict(r) for r in rows]

    catalog = get_televizo_catalog()
    catalog_names = {re.sub(r"[^a-zA-Z0-9\u0400-\u04FF]", "", ch.get("name", "").lower()) for ch in catalog}

    to_refresh = []
    for ch in channels:
        u = ch.get("url", "").lower()
        norm_name = re.sub(r"[^a-zA-Z0-9\u0400-\u04FF]", "", ch.get("name", "").lower())
        if "tvcdnpotok" in u or "cdntvmedia" in u or "cdntvtech" in u or norm_name in catalog_names:
            to_refresh.append(ch)

    if not to_refresh:
        return {"ok": True, "refreshed_count": 0, "total_matched": 0, "updated": 0, "message": "No Televizo channels found"}

    # Prioritize offline / broken channels first
    to_refresh.sort(key=lambda c: 0 if c.get("status") in ("offline", "warning") else 1)

    def _worker(items):
        now = int(time.time())
        renewed_count = 0
        touched_playlists = set()
        for ch in items:
            try:
                res = extract_televizo_stream_sync(ch["name"], timeout_sec=10)
                if res.get("found") and res.get("url"):
                    fresh_url = res["url"]
                    fresh_ref = res.get("referrer") or ch.get("referrer") or ""
                    with get_db() as conn:
                        conn.execute(
                            """
                            UPDATE channels
                            SET url = ?, referrer = ?, status = 'online', latency_ms = 180, last_checked_at = ?, fail_count = 0, last_error = ''
                            WHERE id = ?
                            """,
                            (fresh_url, fresh_ref, now, ch["id"])
                        )
                        pid = ch.get("playlist_id")
                        if pid and pid not in touched_playlists:
                            touch_playlist(conn, pid)
                            touched_playlists.add(pid)
                    add_or_update_channel_source_db(
                        channel_id=ch["id"],
                        name=ch["name"],
                        url=fresh_url,
                        source_name="Televizo Live Extractor",
                        quality="HD",
                        priority=1,
                        confidence=100
                    )
                    renewed_count += 1
            except Exception as e:
                print(f"[Televizo Auto-Renew] Error refreshing {ch.get('name')}: {e}")
        return renewed_count

    if background:
        threading.Thread(target=_worker, args=(to_refresh,), daemon=True).start()
        return {
            "ok": True,
            "async": True,
            "matched": len(to_refresh),
            "updated": len(to_refresh),
            "message": f"Syncing {len(to_refresh)} Televizo channels in background. Working tokens are updating automatically!"
        }

    # Synchronous mode (used by the 5-hour scheduled loop)
    renewed_count = _worker(to_refresh)
    return {
        "ok": True,
        "refreshed_count": renewed_count,
        "total_matched": len(to_refresh),
        "playlist_id": playlist_id or "all"
    }


CHANNEL_SEARCH_ALIASES = {
    "pervyj kanal": ["первый канал", "1tv", "channel one"],
    "первый канал": ["pervyj kanal", "1tv", "channel one"],
    "1tv": ["первый канал", "pervyj kanal"],
    "rossiya 1": ["россия 1", "russia 1"],
    "россия 1": ["rossiya 1", "russia 1"],
    "rossiya 24": ["россия 24", "russia 24"],
    "россия 24": ["rossiya 24", "russia 24"],
    "ntv": ["нтв"],
    "нтв": ["ntv"],
    "tnt": ["тнт"],
    "тнт": ["tnt"],
    "sts": ["стс", "ctc"],
    "стс": ["sts", "ctc"],
    "ren tv": ["рен тв"],
    "рен тв": ["ren tv"],
    "match tv": ["матч тв"],
    "матч тв": ["match tv"],
    "pyatnica": ["пятница"],
    "пятница": ["pyatnica"],
    "domashnij": ["домашний"],
    "домашний": ["domashnij"],
    "zvezda": ["звезда"],
    "звезда": ["zvezda"],
    "tv3": ["тв3", "tv 3"],
    "tv 3": ["tv3", "тв3"],
    "lrt": ["lrt televizija", "lrt lituanica"],
    "bbc news": ["bbc world news"],
}


def _expand_channel_aliases(query: str) -> list:
    q_low = re.sub(r"\s+", " ", (query or "").strip().lower())
    q_clean = re.sub(r"^[a-z]{2,3}\s*[:|\-•]\s*", "", q_low)
    variants = [query]
    for k, alts in CHANNEL_SEARCH_ALIASES.items():
        if q_clean == k or q_low == k:
            for a in alts:
                if a not in variants:
                    variants.append(a)
    return variants[:3]


def find_stream_sources_by_name(query: str, tvg_id: str = "", limit: int = 8, check_live: bool = True, include_web: bool = True, include_yt: bool = True, include_rutube: bool = True, include_televizo: bool = True, target_country: str = "", strict: bool = True) -> list:
    """
    Parallel multi-source channel search across:
    - 35,000+ indexed FAST/IPTV-Org/Free-TV streams (with alias expansion)
    - Televizo Live Extractor
    - YouTube Live discovery
    - RuTube Live discovery
    - Open Web & GitHub IPTV M3U discovery
    """
    aliases = _expand_channel_aliases(query)
    top_candidates = []
    seen_urls = set()

    def _run_indexed():
        res = []
        for q_var in aliases:
            hits = match_stream_candidates_fast(q_var, tvg_id, limit=limit, target_country=target_country, strict=strict)
            res.extend(hits)
        return res

    def _run_televizo():
        if not include_televizo:
            return []
        for q_var in aliases:
            hits = search_televizo_live_streams_fast(q_var, check_live=check_live)
            if hits:
                return hits
        return []

    def _run_yt():
        if not include_yt:
            return []
        return search_youtube_live_streams_fast(query, target_country=target_country, strict=strict, limit=3, check_live=check_live)

    def _run_rutube():
        if not include_rutube:
            return []
        for q_var in aliases:
            hits = search_rutube_live_streams_fast(q_var, target_country=target_country, strict=strict, limit=3, check_live=check_live)
            if hits:
                return hits
        return []

    def _run_web():
        if not include_web:
            return []
        return search_live_web_for_channel_streams(query, limit=4, check_live=False, target_country=target_country)

    # Execute all discovery engines in parallel for 3-4x faster search
    futures = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as search_pool:
        futures["indexed"] = search_pool.submit(_run_indexed)
        if include_televizo and not IS_LIGHTWEIGHT_PANEL:
            futures["televizo"] = search_pool.submit(_run_televizo)
        elif include_televizo:
            # On lightweight Render panel, check televizo catalog match first before launching Chromium
            if find_televizo_channel_info(query):
                futures["televizo"] = search_pool.submit(_run_televizo)
        if include_yt:
            futures["yt"] = search_pool.submit(_run_yt)
        if include_rutube:
            futures["rutube"] = search_pool.submit(_run_rutube)
        if include_web:
            futures["web"] = search_pool.submit(_run_web)

    def _safe_res(key):
        fut = futures.get(key)
        if not fut:
            return []
        try:
            return fut.result(timeout=14) or []
        except Exception:
            return []

    televizo_candidates = _safe_res("televizo")
    rutube_candidates = _safe_res("rutube")
    yt_candidates = _safe_res("yt")
    indexed_candidates = _safe_res("indexed")
    web_candidates = _safe_res("web")

    all_candidates = []
    for c in televizo_candidates + indexed_candidates + rutube_candidates + yt_candidates + web_candidates:
        if c.get("url") and c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            all_candidates.append(c)
    top_candidates = all_candidates

    if check_live and top_candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PANEL_MAX_WORKERS) as pool:
            unprobed = [c for c in top_candidates if c.get("status") == "unknown"][:25]
            if unprobed:
                probes = list(pool.map(lambda c: probe_stream(c, timeout=3.5), unprobed))
                for cand, pr in zip(unprobed, probes):
                    cand["status"] = pr["status"]
                    cand["latency_ms"] = pr["latency_ms"]
                    cand["error"] = pr.get("error", "")

        def _rank_key(c):
            status_rank = 0 if c.get("status") == "online" else (1 if c.get("status") == "warning" else 2)
            conf = c.get("confidence", 0) or c.get("match_confidence", 0)
            tier = 0 if conf >= 75 else (1 if conf >= 55 else 2)
            lat = c.get("latency_ms", 0)
            return (status_rank, tier, -conf, lat if lat > 0 else 9999)

        top_candidates.sort(key=_rank_key)
    gc.collect()
    return top_candidates[:limit]


def bulk_find_sources_for_names(queries: list, check_live: bool = True, include_web: bool = True, include_yt: bool = True, include_rutube: bool = True, include_televizo: bool = True, target_country: str = "", strict: bool = True) -> list:
    """
    Takes up to 100 items: each item can be a string (channel name) or dict {"id": ..., "name": ..., "tvg_id": ...}.
    Finds multiple stream candidates per channel, probes EVERY option concurrently before displaying,
    and returns a verified options array per channel so the user can choose or auto-apply!
    If include_yt is True, also discovers verified 24/7 official YouTube live broadcasts.
    If include_rutube is True, discovers verified official RuTube 10-year live broadcasts.
    If include_web is True, queries live web & GitHub search for channels not found in the local catalog.
    Enforces country affinity and precision tiered ranking.
    """
    normalized_queries = []
    for q in queries[:100]:
        if isinstance(q, dict):
            name = (q.get("name") or "").strip()
            if name:
                normalized_queries.append({
                    "channel_id": q.get("id") or q.get("channel_id") or "",
                    "name": name,
                    "tvg_id": q.get("tvg_id") or "",
                    "category": q.get("category") or "",
                })
        elif isinstance(q, str) and q.strip():
            normalized_queries.append({
                "channel_id": "",
                "name": q.strip(),
                "tvg_id": "",
                "category": "",
            })

    # Gather candidates per query from indexed catalog using precision scoring
    per_query_cands = []
    all_to_probe = []
    for q_obj in normalized_queries:
        eff_country = target_country or extract_country(q_obj["name"], q_obj.get("tvg_id"), q_obj.get("category"))
        cands = match_stream_candidates_fast(q_obj["name"], q_obj["tvg_id"], limit=4, target_country=eff_country, strict=strict)
        per_query_cands.append((q_obj, cands, eff_country))
        if check_live:
            all_to_probe.extend(cands)

    # Concurrently probe EVERY single candidate stream
    if check_live and all_to_probe:
        with concurrent.futures.ThreadPoolExecutor(max_workers=PANEL_MAX_WORKERS) as pool:
            probes = list(pool.map(lambda c: probe_stream(c, timeout=3.5), all_to_probe))
        for cand, pr in zip(all_to_probe, probes):
            cand["status"] = pr["status"]
            cand["latency_ms"] = pr["latency_ms"]
            cand["error"] = pr.get("error", "")

    # Phase 1: Build initial tested options per query
    def _opt_rank_key(o):
        status_rank = 0 if o.get("status") == "online" else (1 if o.get("status") == "warning" else 2)
        conf = o.get("confidence", 0)
        tier = 0 if conf >= 75 else (1 if conf >= 55 else 2)
        lat = o.get("latency_ms", 0)
        return (status_rank, tier, -conf, lat if lat > 0 else 9999)

    per_query_options = []
    for q_obj, cands, eff_country in per_query_cands:
        tested_options = []
        for o_idx, cand in enumerate(cands):
            tested_options.append({
                "option_id": o_idx + 1,
                "name": cand["name"],
                "url": cand["url"],
                "quality": cand.get("quality", "HD"),
                "source": cand.get("source", "Live Feed"),
                "latency_ms": cand.get("latency_ms", 0),
                "status": cand.get("status", "unknown"),
                "error": cand.get("error", ""),
                "confidence": cand.get("confidence", 80),
                "country_code": cand.get("country_code", ""),
                "country_badge": cand.get("country_badge", ""),
                "match_level": cand.get("match_level", "strong"),
                "tvg_id": cand.get("tvg_id", ""),
                "logo": cand.get("logo") or fast_lookup_logo(cand["name"], cand.get("tvg_id", "")),
                "category": q_obj["category"] or cand.get("category") or "Live TV",
            })
        tested_options.sort(key=_opt_rank_key)
        online_options = [o for o in tested_options if o["status"] == "online"]
        final_opts = online_options if online_options else tested_options[:2]
        per_query_options.append({"q_obj": q_obj, "eff_country": eff_country, "final_options": final_opts})

    # Phase 2: Concurrent YouTube Live fallback for items missing online streams
    if include_yt:
        needs_yt = [
            (idx, item["q_obj"], item["eff_country"])
            for idx, item in enumerate(per_query_options)
            if not item["final_options"] or item["final_options"][0].get("status") != "online" or len(item["final_options"]) < 2
        ][:20]

        if needs_yt:
            def _fetch_yt(entry):
                idx, q_obj, eff_c = entry
                try:
                    hits = search_youtube_live_streams_fast(q_obj["name"], target_country=eff_c, strict=strict, limit=2, check_live=check_live)
                    return idx, hits
                except Exception:
                    return idx, []

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                for idx, yt_hits in pool.map(_fetch_yt, needs_yt):
                    if yt_hits:
                        target = per_query_options[idx]["final_options"]
                        q_obj = per_query_options[idx]["q_obj"]
                        for yh in yt_hits:
                            target.append({
                                "option_id": len(target) + 1,
                                "name": yh["name"],
                                "url": yh["url"],
                                "quality": yh.get("quality", "1080p"),
                                "source": yh.get("source", "▶️ YouTube Live"),
                                "latency_ms": yh.get("latency_ms", 0),
                                "status": yh.get("status", "online"),
                                "error": "",
                                "confidence": yh.get("confidence", 95),
                                "match_confidence": yh.get("match_confidence", 95),
                                "country_code": yh.get("country", ""),
                                "country_badge": yh.get("country_badge", ""),
                                "match_level": "exact",
                                "tvg_id": q_obj["tvg_id"],
                                "logo": yh.get("logo") or fast_lookup_logo(yh["name"]),
                                "category": q_obj["category"] or "Live TV",
                            })
                        target.sort(key=_opt_rank_key)

    # Phase 2b: Concurrent RuTube Live fallback for items missing online streams or CIS channels
    if include_rutube:
        needs_rutube = [
            (idx, item["q_obj"], item["eff_country"])
            for idx, item in enumerate(per_query_options)
            if not item["final_options"] or item["final_options"][0].get("status") != "online" or len(item["final_options"]) < 3 or item["eff_country"] in ("RU", "BY", "KZ", "UA", "CIS", "")
        ][:25]

        if needs_rutube:
            def _fetch_rutube(entry):
                idx, q_obj, eff_c = entry
                try:
                    hits = search_rutube_live_streams_fast(q_obj["name"], target_country=eff_c, strict=strict, limit=2, check_live=check_live)
                    return idx, hits
                except Exception:
                    return idx, []

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                for idx, rt_hits in pool.map(_fetch_rutube, needs_rutube):
                    if rt_hits:
                        target = per_query_options[idx]["final_options"]
                        q_obj = per_query_options[idx]["q_obj"]
                        for rh in rt_hits:
                            target.append({
                                "option_id": len(target) + 1,
                                "name": rh["name"],
                                "url": rh["url"],
                                "quality": rh.get("quality", "1080p"),
                                "source": rh.get("source", "🟢 RuTube Live"),
                                "latency_ms": rh.get("latency_ms", 0),
                                "status": rh.get("status", "online"),
                                "error": "",
                                "confidence": rh.get("confidence", 95),
                                "match_confidence": rh.get("match_confidence", 95),
                                "country_code": rh.get("country", "RU"),
                                "country_badge": rh.get("country_badge", "🇷🇺 RU"),
                                "match_level": "exact",
                                "tvg_id": q_obj["tvg_id"],
                                "logo": rh.get("logo") or fast_lookup_logo(rh["name"]),
                                "category": q_obj["category"] or "Live TV",
                            })
                        target.sort(key=_opt_rank_key)

    # Phase 2c: Concurrent Televizo Live Extractor fallback for items matching televizo catalog or CIS channels
    if include_televizo:
        needs_televizo = [
            (idx, item["q_obj"], item["eff_country"])
            for idx, item in enumerate(per_query_options)
            if not item["final_options"] or item["final_options"][0].get("status") != "online" or len(item["final_options"]) < 3 or find_televizo_channel_info(item["q_obj"]["name"])
        ][:25]

        if needs_televizo:
            def _fetch_televizo(entry):
                idx, q_obj, eff_c = entry
                try:
                    hits = search_televizo_live_streams_fast(q_obj["name"], check_live=check_live)
                    return idx, hits
                except Exception:
                    return idx, []

            with concurrent.futures.ThreadPoolExecutor(max_workers=(1 if IS_LIGHTWEIGHT_PANEL else 3)) as pool:
                for idx, tv_hits in pool.map(_fetch_televizo, needs_televizo):
                    if tv_hits:
                        target = per_query_options[idx]["final_options"]
                        q_obj = per_query_options[idx]["q_obj"]
                        for th in tv_hits:
                            target.append({
                                "option_id": len(target) + 1,
                                "name": th["name"],
                                "url": th["url"],
                                "quality": th.get("quality", "HD"),
                                "source": th.get("source", "📺 Televizo Live"),
                                "latency_ms": th.get("latency_ms", 180),
                                "status": th.get("status", "online"),
                                "error": "",
                                "confidence": th.get("confidence", 95),
                                "match_confidence": th.get("match_confidence", 95),
                                "country_code": th.get("country", "RU"),
                                "country_badge": th.get("country_badge", "🇷🇺 RU"),
                                "match_level": "exact",
                                "tvg_id": q_obj["tvg_id"],
                                "logo": th.get("logo") or fast_lookup_logo(th["name"]),
                                "category": q_obj["category"] or "Live TV",
                            })
                        target.sort(key=_opt_rank_key)

    # Phase 3: Concurrent Live Web search fallback for items still having no online stream
    if include_web:
        needs_web = [
            (idx, item["q_obj"], item["eff_country"])
            for idx, item in enumerate(per_query_options)
            if not item["final_options"] or item["final_options"][0].get("status") != "online"
        ][:15]

        if needs_web:
            def _fetch_web(entry):
                idx, q_obj, eff_c = entry
                try:
                    hits = search_live_web_for_channel_streams(q_obj["name"], limit=2, check_live=check_live, target_country=eff_c)
                    return idx, hits
                except Exception:
                    return idx, []

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                for idx, web_hits in pool.map(_fetch_web, needs_web):
                    if web_hits:
                        target = per_query_options[idx]["final_options"]
                        q_obj = per_query_options[idx]["q_obj"]
                        for wh in web_hits:
                            target.append({
                                "option_id": len(target) + 1,
                                "name": wh["name"],
                                "url": wh["url"],
                                "quality": wh.get("quality", "HD"),
                                "source": wh.get("source", "🌐 Web Live Search"),
                                "latency_ms": wh.get("latency_ms", 0),
                                "status": wh.get("status", "unknown"),
                                "error": wh.get("error", ""),
                                "confidence": wh.get("confidence", 85),
                                "country_code": wh.get("country_code", ""),
                                "country_badge": wh.get("country_badge", ""),
                                "match_level": wh.get("match_level", "strong"),
                                "tvg_id": wh.get("tvg_id", ""),
                                "logo": wh.get("logo") or fast_lookup_logo(wh["name"]),
                                "category": q_obj["category"] or "Live TV",
                            })
                        target.sort(key=_opt_rank_key)

    # Phase 4: Construct final results
    results = []
    for item in per_query_options:
        q_obj = item["q_obj"]
        final_options = item["final_options"]
        if not final_options:
            results.append({
                "channel_id": q_obj["channel_id"],
                "requested_name": q_obj["name"],
                "found": False,
                "name": q_obj["name"],
                "url": "",
                "logo": fast_lookup_logo(q_obj["name"]),
                "category": q_obj["category"] or "Uncategorized",
                "tvg_id": q_obj["tvg_id"],
                "quality": "SD",
                "status": "not_found",
                "latency_ms": 0,
                "source": "No Working Match",
                "confidence": 0,
                "country_badge": "",
                "options": [],
                "options_count": 0,
            })
            continue

        best = final_options[0]
        results.append({
            "channel_id": q_obj["channel_id"],
            "requested_name": q_obj["name"],
            "found": True,
            "name": best["name"],
            "url": best["url"],
            "logo": best["logo"],
            "category": q_obj["category"] or best["category"] or "Live TV",
            "tvg_id": best["tvg_id"],
            "quality": best.get("quality", "HD"),
            "status": best["status"],
            "latency_ms": best["latency_ms"],
            "source": best["source"],
            "confidence": best.get("confidence", 85),
            "country_badge": best.get("country_badge", ""),
            "options": final_options,
            "options_count": len(final_options),
        })

    return results


def generate_svg_data_logo(channel_name: str) -> str:
    clean = re.sub(r"\[.*?\]|\(.*?\)", "", channel_name or "TV").strip()
    words = [w for w in re.split(r"[\s\-_|]+", clean) if w]
    if len(words) >= 2:
        initials = (words[0][0] + words[1][0]).upper()
    elif words:
        initials = words[0][:3].upper()
    else:
        initials = "TV"
    initials = re.sub(r"[^A-Z0-9]", "", initials) or "TV"

    palettes = [
        ("#6366f1", "#a855f7"),
        ("#06b6d4", "#3b82f6"),
        ("#ec4899", "#8b5cf6"),
        ("#10b981", "#0ea5e9"),
        ("#f59e0b", "#ef4444"),
        ("#8b5cf6", "#06b6d4"),
    ]
    idx = sum(ord(c) for c in (channel_name or "TV")) % len(palettes)
    c1, c2 = palettes[idx]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" viewBox="0 0 240 240">'
        f'<defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">'
        f'<stop offset="0%" stop-color="{c1}"/><stop offset="100%" stop-color="{c2}"/>'
        f"</linearGradient></defs>"
        f'<rect width="240" height="240" rx="48" fill="#0f172a"/>'
        f'<rect x="6" y="6" width="228" height="228" rx="42" fill="url(#g)" fill-opacity="0.22" stroke="url(#g)" stroke-width="4"/>'
        f'<text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" '
        f'fill="#f8fafc" font-family="Inter,Segoe UI,sans-serif" font-weight="800" font-size="76" letter-spacing="2">{initials}</text>'
        f"</svg>"
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)


def find_logo_candidates(query: str, tvg_id: str = "", limit: int = 8) -> list:
    catalog = ensure_logo_catalog_loaded()
    clean_q = clean_channel_name_for_match(query)
    clean_id = clean_channel_name_for_match(tvg_id.split(".")[0] if tvg_id else "")

    scored = []
    seen_urls = set()

    for item in catalog:
        c_name = item["clean"]
        if not c_name or item["url"] in seen_urls:
            continue

        score = 0.0
        if tvg_id and item["id"].lower() == tvg_id.lower():
            score = 1.0
        elif clean_q and c_name == clean_q:
            score = 0.98 if item["source"] == "Curated HD" else 0.95
        elif clean_id and c_name == clean_id:
            score = 0.93
        elif clean_q and (clean_q in c_name or c_name in clean_q):
            ratio = min(len(clean_q), len(c_name)) / max(len(clean_q), len(c_name), 1)
            score = 0.72 + (0.20 * ratio)
            if item["source"] == "Curated HD":
                score += 0.04
        elif clean_q:
            q_tokens = set(clean_q.split())
            c_tokens = set(c_name.split())
            if q_tokens & c_tokens:
                sim = difflib.SequenceMatcher(None, clean_q, c_name).ratio()
                if sim >= 0.62:
                    score = sim * 0.85

        if score >= 0.55:
            scored.append((score, item))
            seen_urls.add(item["url"])

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [
        {
            "name": item["name"],
            "url": item["url"],
            "id": item["id"],
            "source": item["source"],
            "confidence": round(score * 100),
        }
        for score, item in scored[:limit]
    ]

    results.append({
        "name": f"{query or 'Channel'} (Generated Badge)",
        "url": generate_svg_data_logo(query or "TV"),
        "id": tvg_id or "",
        "source": "Studio Monogram",
        "confidence": 60,
    })
    return results


# ---------------------------------------------------------------------------
# M3U / M3U8 Parser & Exporter + Duplicate Analyzer
# ---------------------------------------------------------------------------

ATTR_REGEX = re.compile(r'([a-zA-Z0-9_-]+)\s*=\s*"([^"]*)"')


def parse_m3u_content(content: str, default_category: str = "Uncategorized") -> dict:
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    playlist_meta = {"epg_url": ""}
    channels = []

    current_info = None
    current_opts = {}

    for line in lines:
        if line.upper().startswith("#EXTM3U"):
            attrs = dict(ATTR_REGEX.findall(line))
            playlist_meta["epg_url"] = (
                attrs.get("url-tvg")
                or attrs.get("x-tvg-url")
                or attrs.get("tvg-url")
                or ""
            )
            continue

        if line.startswith("#EXTINF:"):
            attrs = dict(ATTR_REGEX.findall(line))
            comma_split = re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', line, maxsplit=1)
            display_name = comma_split[1].strip() if len(comma_split) > 1 else ""
            if not display_name:
                display_name = attrs.get("tvg-name") or "Untitled Channel"

            current_info = {
                "name": display_name,
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", display_name),
                "logo": attrs.get("tvg-logo", ""),
                "category": attrs.get("group-title", "") or default_category,
                "chno": attrs.get("tvg-chno", ""),
            }
            continue

        if line.startswith("#EXTGRP:"):
            grp = line.split(":", 1)[1].strip()
            if current_info is not None and grp:
                current_info["category"] = grp
            continue

        if line.startswith("#EXTVLCOPT:"):
            opt = line.split(":", 1)[1].strip()
            if "=" in opt:
                k, v = opt.split("=", 1)
                current_opts[k.strip().lower()] = v.strip()
            continue

        if line.startswith("#"):
            continue

        if "|" in line and ("http://" in line or "https://" in line or "rtmp://" in line):
            parts = [p.strip() for p in line.split("|") if p.strip()]
            url_part = next(
                (p for p in parts if p.startswith(("http://", "https://", "rtmp://", "rtsp://"))),
                None,
            )
            if url_part:
                other_parts = [p for p in parts if p != url_part]
                ch_name = other_parts[0] if other_parts else guess_name_from_url(url_part)
                ch_cat = other_parts[1] if len(other_parts) > 1 else default_category
                channels.append({
                    "name": ch_name,
                    "url": url_part,
                    "logo": "",
                    "category": ch_cat,
                    "tvg_id": "",
                    "tvg_name": ch_name,
                    "chno": "",
                    "user_agent": "",
                    "referrer": "",
                })
                current_info = None
                current_opts = {}
                continue

        if "," in line and not line.startswith(("http://", "https://", "rtmp://", "rtsp://")):
            parts = [p.strip() for p in line.split(",")]
            url_part = next(
                (p for p in parts if p.startswith(("http://", "https://", "rtmp://", "rtsp://"))),
                None,
            )
            if url_part:
                other_parts = [p for p in parts if p != url_part]
                ch_name = other_parts[0] if other_parts else guess_name_from_url(url_part)
                ch_cat = other_parts[1] if len(other_parts) > 1 else default_category
                channels.append({
                    "name": ch_name,
                    "url": url_part,
                    "logo": "",
                    "category": ch_cat,
                    "tvg_id": "",
                    "tvg_name": ch_name,
                    "chno": "",
                    "user_agent": "",
                    "referrer": "",
                })
                current_info = None
                current_opts = {}
                continue

        if line.startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://", "rtp://")):
            if current_info is None:
                guessed = guess_name_from_url(line)
                current_info = {
                    "name": guessed,
                    "tvg_id": "",
                    "tvg_name": guessed,
                    "logo": "",
                    "category": default_category,
                    "chno": "",
                }
            channels.append({
                "name": current_info["name"],
                "url": line,
                "logo": current_info.get("logo", ""),
                "category": current_info.get("category") or default_category,
                "tvg_id": current_info.get("tvg_id", ""),
                "tvg_name": current_info.get("tvg_name") or current_info["name"],
                "chno": str(current_info.get("chno") or ""),
                "user_agent": current_opts.get("http-user-agent", ""),
                "referrer": current_opts.get("http-referrer", ""),
            })
            current_info = None
            current_opts = {}

    return {"playlist_meta": playlist_meta, "channels": channels}


def guess_name_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        path_segments = [
            seg for seg in parsed.path.split("/")
            if seg and seg.lower() not in ("live", "hls", "stream", "streams", "playlist.m3u8", "index.m3u8", "master.m3u8", "mono.m3u8", "chunklist.m3u8")
        ]
        candidate = path_segments[-1] if path_segments else parsed.netloc.split(".")[0]
        candidate = re.sub(r"\.(m3u8|m3u|ts|mp4)$", "", candidate, flags=re.IGNORECASE)
        candidate = urllib.parse.unquote(candidate)
        candidate = re.sub(r"[_\-\.]+", " ", candidate).strip()
        if candidate:
            return candidate.title() if candidate.islower() else candidate
    except Exception:
        pass
    return "Live Stream"


def annotate_duplicates(candidate_channels: list, existing_channels: list) -> dict:
    existing_urls = {c["url"].strip().lower() for c in existing_channels if c.get("url")}
    existing_names = {
        clean_channel_name_for_match(c["name"])
        for c in existing_channels
        if c.get("name") and clean_channel_name_for_match(c["name"])
    }

    seen_new_urls = set()
    seen_new_names = set()

    dup_in_playlist = 0
    dup_in_new_list = 0
    unique_count = 0

    for idx, ch in enumerate(candidate_channels):
        url_key = (ch.get("url") or "").strip().lower()
        name_key = clean_channel_name_for_match(ch.get("name") or "")

        is_pl_dup = bool(
            (url_key and url_key in existing_urls)
            or (name_key and name_key in existing_names)
        )
        is_internal_dup = bool(
            (url_key and url_key in seen_new_urls)
            or (name_key and name_key in seen_new_names)
        )

        ch["_idx"] = idx
        ch["dup_playlist"] = is_pl_dup
        ch["dup_internal"] = is_internal_dup
        ch["status"] = ch.get("status", "unknown")
        ch["latency_ms"] = ch.get("latency_ms", 0)

        if is_pl_dup:
            dup_in_playlist += 1
        elif is_internal_dup:
            dup_in_new_list += 1
        else:
            unique_count += 1

        if url_key:
            seen_new_urls.add(url_key)
        if name_key:
            seen_new_names.add(name_key)

    return {
        "total": len(candidate_channels),
        "dup_in_playlist": dup_in_playlist,
        "dup_in_new_list": dup_in_new_list,
        "unique_count": unique_count,
    }


def build_m3u_export(playlist: dict, channels: list, smart_failover: bool = False, base_url: str = "") -> str:
    header = "#EXTM3U"
    if playlist.get("epg_url"):
        header += f' x-tvg-url="{playlist["epg_url"]}" url-tvg="{playlist["epg_url"]}"'
    out = [header]

    clean_base = (base_url or "").rstrip("/")

    for ch in channels:
        if not ch.get("enabled", 1):
            continue
        attrs = []
        if ch.get("tvg_id"):
            attrs.append(f'tvg-id="{ch["tvg_id"]}"')
        if ch.get("tvg_name") or ch.get("name"):
            attrs.append(f'tvg-name="{ch.get("tvg_name") or ch.get("name")}"')
        if ch.get("logo"):
            attrs.append(f'tvg-logo="{ch["logo"]}"')
        if ch.get("chno"):
            attrs.append(f'tvg-chno="{ch["chno"]}"')
        category = ch.get("category") or "Uncategorized"
        attrs.append(f'group-title="{category}"')

        attr_str = " " + " ".join(attrs) if attrs else ""
        out.append(f'#EXTINF:-1{attr_str},{ch.get("name", "Channel")}')
        if ch.get("user_agent"):
            out.append(f'#EXTVLCOPT:http-user-agent={ch["user_agent"]}')
        if ch.get("referrer"):
            out.append(f'#EXTVLCOPT:http-referrer={ch["referrer"]}')

        if smart_failover and clean_base:
            stream_url = f"{clean_base}/api/stream/{ch['id']}/live.m3u8"
        else:
            stream_url = ch.get("url", "")
        out.append(stream_url)

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Database Layer (WAL Mode + 30s Busy Timeout to Eliminate Locking)
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                epg_url TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                playlist_id TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                logo TEXT DEFAULT '',
                category TEXT DEFAULT 'Uncategorized',
                tvg_id TEXT DEFAULT '',
                tvg_name TEXT DEFAULT '',
                chno TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                referrer TEXT DEFAULT '',
                status TEXT DEFAULT 'unknown',
                latency_ms INTEGER DEFAULT 0,
                last_checked_at INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                position INTEGER DEFAULT 0,
                FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_channels_playlist ON channels(playlist_id, position);

            CREATE TABLE IF NOT EXISTS health_logs (
                id TEXT PRIMARY KEY,
                ran_at INTEGER NOT NULL,
                trigger_type TEXT NOT NULL,
                total_checked INTEGER DEFAULT 0,
                online_count INTEGER DEFAULT 0,
                offline_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS provider_sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                badge TEXT NOT NULL,
                provider_type TEXT NOT NULL DEFAULT 'fast_platform',
                channel_count INTEGER DEFAULT 0,
                last_synced_at INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                is_system INTEGER DEFAULT 0,
                position INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS channel_sources (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                source_name TEXT DEFAULT 'Backup Stream',
                quality TEXT DEFAULT 'HD',
                priority INTEGER DEFAULT 1,
                status TEXT DEFAULT 'unknown',
                latency_ms INTEGER DEFAULT 0,
                last_checked_at INTEGER DEFAULT 0,
                confidence INTEGER DEFAULT 90,
                last_error TEXT DEFAULT '',
                FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_channel_sources_channel ON channel_sources(channel_id, priority);
            """
        )

        try:
            conn.execute("ALTER TABLE health_logs ADD COLUMN failovers_count INTEGER DEFAULT 0")
        except Exception:
            pass

        default_providers = [
            (
                "prov_pluto",
                "Pluto TV Global FAST",
                "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/plutotv_all.m3u",
                "🪐 Pluto TV",
                "fast_platform",
                1,
                1,
                0,
            ),
            (
                "prov_samsung",
                "Samsung TV Plus Worldwide",
                "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/samsungtvplus_all.m3u",
                "📺 Samsung TV+",
                "fast_platform",
                1,
                1,
                1,
            ),
            (
                "prov_plex",
                "Plex Live TV Worldwide",
                "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/plex_all.m3u",
                "🎬 Plex Live",
                "fast_platform",
                1,
                1,
                2,
            ),
            (
                "prov_roku",
                "The Roku Channel",
                "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/roku_all.m3u",
                "🟣 Roku Live",
                "fast_platform",
                1,
                1,
                3,
            ),
            (
                "prov_tubi",
                "Tubi TV Live",
                "https://raw.githubusercontent.com/BuddyChewChew/app-m3u-generator/main/playlists/tubi_all.m3u",
                "🟡 Tubi Live",
                "fast_platform",
                1,
                1,
                4,
            ),
            (
                "prov_freetv",
                "Free-TV Worldwide Broadcasts",
                "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8",
                "📡 Free-TV Global",
                "public_broadcasts",
                1,
                1,
                5,
            ),
            (
                "prov_iptv_org",
                "IPTV-Org Global Live Index",
                "https://iptv-org.github.io/iptv/index.m3u",
                "🌐 IPTV-Org Index",
                "global_directory",
                1,
                1,
                6,
            ),
            (
                "prov_iptv_country",
                "IPTV-Org Country Specific Feeds",
                "https://iptv-org.github.io/iptv/index.country.m3u",
                "🗺️ IPTV Countries",
                "global_directory",
                1,
                1,
                7,
            ),
            (
                "prov_iptv_news",
                "IPTV-Org 24/7 Global News Feeds",
                "https://iptv-org.github.io/iptv/categories/news.m3u",
                "📰 Global News",
                "specialized_news",
                1,
                1,
                8,
            ),
            (
                "prov_iptv_sports",
                "IPTV-Org Sports Feeds",
                "https://iptv-org.github.io/iptv/categories/sports.m3u",
                "⚽ Sports Global",
                "specialized_sports",
                1,
                1,
                9,
            ),
            (
                "prov_iptv_movies",
                "IPTV-Org Movies & Cinema",
                "https://iptv-org.github.io/iptv/categories/movies.m3u",
                "🎬 Cinema & Movies",
                "specialized_cinema",
                1,
                1,
                10,
            ),
            (
                "prov_iptv_music",
                "IPTV-Org Music Channels",
                "https://iptv-org.github.io/iptv/categories/music.m3u",
                "🎵 Music Channels",
                "specialized_music",
                1,
                1,
                11,
            ),
            (
                "prov_freetv_uk",
                "Free-TV UK Broadcasts",
                "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_gb.m3u8",
                "🇬🇧 Free-TV UK",
                "regional_broadcast",
                1,
                1,
                12,
            ),
            (
                "prov_freetv_usa",
                "Free-TV USA Broadcasts",
                "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_us.m3u8",
                "🇺🇸 Free-TV USA",
                "regional_broadcast",
                1,
                1,
                13,
            ),
        ]
        conn.executemany(
            """
            INSERT OR IGNORE INTO provider_sources (id, name, url, badge, provider_type, enabled, is_system, position)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            default_providers,
        )

        existing_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(channels)").fetchall()
        }
        if "last_checked_at" not in existing_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN last_checked_at INTEGER DEFAULT 0")
        if "fail_count" not in existing_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN fail_count INTEGER DEFAULT 0")
        if "last_error" not in existing_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN last_error TEXT DEFAULT ''")

        try:
            total_f = conn.execute("SELECT COALESCE(SUM(failovers_count), 0) FROM health_logs").fetchone()[0]
            with SCHEDULER_LOCK:
                SCHEDULER_STATE["total_failovers"] = int(total_f)
        except Exception:
            pass


def touch_playlist(conn, playlist_id: str):
    conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (int(time.time()), playlist_id))


# ---------------------------------------------------------------------------
# Stream Health Probe, Channel Sources Manager & 2-Hour Fallback Guard
# ---------------------------------------------------------------------------

SCHEDULER_LOCK = threading.Lock()
SCHEDULER_STATE = {
    "enabled": True,
    "checks_per_day": 12,
    "interval_seconds": 2 * 3600,
    "last_run_at": int(time.time()),
    "next_run_at": int(time.time()) + 2 * 3600,
    "is_running": False,
    "last_summary": {"total": 0, "online": 0, "offline": 0, "failovers": 0},
    "total_failovers": 0,
}


def get_channel_sources_db(channel_id: str) -> list:
    """Retrieves all registered candidate/backup stream sources for a channel, ordered by priority."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_sources WHERE channel_id = ? ORDER BY priority ASC, rowid ASC",
            (channel_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def add_or_update_channel_source_db(channel_id: str, name: str, url: str, source_name: str = "Backup Stream", quality: str = "HD", priority: int = None, confidence: int = 90) -> dict:
    """Adds a new stream source candidate or updates an existing one for a channel."""
    clean_url = (url or "").strip()
    if not clean_url:
        return {}
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM channel_sources WHERE channel_id = ? AND url = ?",
            (channel_id, clean_url),
        ).fetchone()

        if existing:
            sid = existing["id"]
            if priority is not None:
                conn.execute("UPDATE channel_sources SET priority = ?, source_name = ? WHERE id = ?", (priority, source_name, sid))
            return dict(existing)

        if priority is None:
            max_p = conn.execute("SELECT MAX(priority) AS mx FROM channel_sources WHERE channel_id = ?", (channel_id,)).fetchone()
            priority = (max_p["mx"] or 1) + 1

        sid = str(uuid.uuid4())[:8]
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO channel_sources (id, channel_id, name, url, source_name, quality, priority, status, latency_ms, last_checked_at, confidence, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'unknown', 0, ?, ?, '')
            """,
            (sid, channel_id, name, clean_url, source_name, quality, priority, now, confidence),
        )
        return {
            "id": sid,
            "channel_id": channel_id,
            "name": name,
            "url": clean_url,
            "source_name": source_name,
            "quality": quality,
            "priority": priority,
            "confidence": confidence,
        }


def delete_channel_source_db(channel_id: str, source_id: str) -> bool:
    """Deletes a backup stream source for a channel."""
    with get_db() as conn:
        res = conn.execute("DELETE FROM channel_sources WHERE id = ? AND channel_id = ?", (source_id, channel_id))
        return res.rowcount > 0


def promote_channel_source_db(channel_id: str, source_id: str) -> bool:
    """Promotes a backup source to Priority 1 and sets it as the primary stream URL for the channel."""
    with get_db() as conn:
        src = conn.execute("SELECT * FROM channel_sources WHERE id = ? AND channel_id = ?", (source_id, channel_id)).fetchone()
        if not src:
            return False
        src_url = src["url"]

        # Shift existing priority 1 sources to priority 2
        conn.execute("UPDATE channel_sources SET priority = 2 WHERE channel_id = ? AND priority = 1", (channel_id,))
        conn.execute("UPDATE channel_sources SET priority = 1 WHERE id = ?", (source_id,))

        # Update channels table primary url
        conn.execute(
            """
            UPDATE channels
            SET url = ?, status = 'online', latency_ms = ?, fail_count = 0, last_error = ''
            WHERE id = ?
            """,
            (src_url, src["latency_ms"] or 120, channel_id),
        )
        return True


def auto_populate_channel_backups_db(channel_id: str, max_sources: int = 4) -> list:
    """
    Finds and stores at least 3-4 distinct verified backup sources for a channel
    using RuTube Live, YouTube Live, open IPTV CDNs, and web streams.
    """
    with get_db() as conn:
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not ch:
            return []
        ch_dict = dict(ch)

    # 1. Ensure current primary URL is saved as priority 1 in channel_sources
    add_or_update_channel_source_db(
        channel_id=channel_id,
        name=ch_dict["name"],
        url=ch_dict["url"],
        source_name="Primary Stream",
        quality="HD",
        priority=1,
        confidence=100,
    )

    # 2. Discover candidate streams via unified search engine
    candidates = find_stream_sources_by_name(
        query=ch_dict["name"],
        tvg_id=ch_dict.get("tvg_id", ""),
        limit=8,
        check_live=True,
        include_web=True,
        include_yt=True,
        include_rutube=True,
        strict=False,
    )

    stored = []
    seen_urls = {ch_dict["url"]}
    existing = get_channel_sources_db(channel_id)
    for e in existing:
        seen_urls.add(e["url"])

    p_counter = len(existing) + 1
    for cand in candidates:
        u = cand.get("url", "").strip()
        if not u or u in seen_urls:
            continue
        if cand.get("status") not in ("online", "warning"):
            continue
        seen_urls.add(u)
        s_obj = add_or_update_channel_source_db(
            channel_id=channel_id,
            name=cand.get("name", ch_dict["name"]),
            url=u,
            source_name=cand.get("source", "Auto Backup"),
            quality=cand.get("quality", "HD"),
            priority=p_counter,
            confidence=cand.get("confidence", 90),
        )
        stored.append(s_obj)
        p_counter += 1
        if len(stored) + len(existing) >= max_sources:
            break

    return get_channel_sources_db(channel_id)


def auto_populate_playlist_backups_db(playlist_id: str, max_sources_per_channel: int = 4) -> dict:
    """Batch-populates 3-4 backup sources for all channels in a playlist."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, name FROM channels WHERE playlist_id = ?", (playlist_id,)).fetchall()
        channels = [dict(r) for r in rows]

    populated = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(auto_populate_channel_backups_db, ch["id"], max_sources_per_channel): ch for ch in channels}
        for f in concurrent.futures.as_completed(futures):
            try:
                res = f.result()
                if len(res) > 1:
                    populated += 1
            except Exception:
                pass

    return {"total_channels": len(channels), "populated_channels": populated}


def _is_html_or_error_body(chunk: bytes, content_type: str = "") -> bool:
    ct = (content_type or "").lower()
    if "text/html" in ct or "application/xhtml" in ct:
        return True
    head = chunk.lstrip()[:200].lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<script", b'{"error', b'{"message')):
        return True
    return False


def probe_stream(channel_row: dict, timeout: float = 4.5) -> dict:
    """
    Deep stream liveness probe matching real IPTV player (VLC/TiviMate) behavior:
    1. Rejects HTML error pages, Cloudflare challenges, and empty responses.
    2. For HLS (.m3u8 / #EXTM3U), parses the manifest and verifies that at least one
       child variant playlist or media segment (.ts/.m4s) actually loads with video data.
    """
    url = (channel_row.get("url") or "").strip()
    cid = channel_row.get("id") or str(channel_row.get("_idx", ""))
    if not url.startswith(("http://", "https://")):
        return {"id": cid, "url": url, "status": "offline", "latency_ms": 0, "error": "Unsupported protocol"}

    start = time.time()
    headers = {
        "User-Agent": channel_row.get("user_agent")
        or "Mozilla/5.0 (VLC/3.0.20 LibVLC/3.0.20) StreamForge/3.0",
        "Accept": "*/*",
    }
    if channel_row.get("referrer"):
        headers["Referer"] = channel_row["referrer"]

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl() or url
            content_type = resp.headers.get("Content-Type", "")
            chunk = resp.read(16384)
            code = resp.getcode()
            elapsed = int((time.time() - start) * 1000)

            if not (200 <= code < 400) or not chunk:
                return {"id": cid, "url": url, "status": "offline", "latency_ms": elapsed, "error": f"HTTP {code} Empty"}

            if _is_html_or_error_body(chunk, content_type):
                return {"id": cid, "url": url, "status": "offline", "latency_ms": elapsed, "error": "HTML/Error page instead of stream"}

            is_hls = (
                chunk.lstrip().startswith(b"#EXTM3U")
                or "mpegurl" in content_type.lower()
                or ".m3u8" in final_url.lower()
            )

            if is_hls:
                text = chunk.decode("utf-8", errors="replace")
                child_urls = [
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                if not child_urls:
                    return {"id": cid, "url": url, "status": "offline", "latency_ms": elapsed, "error": "Empty HLS manifest (no segments)"}

                # Verify first child URL (variant .m3u8 or .ts/.m4s segment) is actually reachable
                first_child = urllib.parse.urljoin(final_url, child_urls[0])
                child_hdrs = dict(headers)
                if ".m3u8" not in first_child.lower():
                    child_hdrs["Range"] = "bytes=0-1023"

                creq = urllib.request.Request(first_child, headers=child_hdrs, method="GET")
                with urllib.request.urlopen(creq, timeout=min(timeout, 3.5)) as cresp:
                    ccode = cresp.getcode()
                    cct = cresp.headers.get("Content-Type", "")
                    cchunk = cresp.read(4096)
                    if not (200 <= ccode < 400) or not cchunk or _is_html_or_error_body(cchunk, cct):
                        return {"id": cid, "url": url, "status": "offline", "latency_ms": elapsed, "error": "Stream segment unreachable or blocked"}

                    # If child was a variant .m3u8 (master -> media playlist), verify it has segments too
                    if cchunk.lstrip().startswith(b"#EXTM3U"):
                        ctext = cchunk.decode("utf-8", errors="replace")
                        seg_urls = [
                            ln.strip()
                            for ln in ctext.splitlines()
                            if ln.strip() and not ln.strip().startswith("#")
                        ]
                        if not seg_urls:
                            return {"id": cid, "url": url, "status": "offline", "latency_ms": elapsed, "error": "Empty variant HLS playlist"}
                        first_seg = urllib.parse.urljoin(cresp.geturl() or first_child, seg_urls[0])
                        seg_hdrs = dict(headers)
                        seg_hdrs["Range"] = "bytes=0-511"
                        sreq = urllib.request.Request(first_seg, headers=seg_hdrs, method="GET")
                        with urllib.request.urlopen(sreq, timeout=min(timeout, 3.0)) as sresp:
                            schunk = sresp.read(512)
                            if not (200 <= sresp.getcode() < 400) or not schunk or _is_html_or_error_body(schunk, sresp.headers.get("Content-Type", "")):
                                return {"id": cid, "url": url, "status": "offline", "latency_ms": elapsed, "error": "Media segment failed"}

            elapsed = int((time.time() - start) * 1000)
            return {"id": cid, "url": url, "status": "online", "latency_ms": elapsed, "error": ""}
    except urllib.error.HTTPError as e:
        elapsed = int((time.time() - start) * 1000)
        status = "offline" if e.code in (401, 403, 404, 410, 500, 502, 503, 504) else "warning"
        return {"id": cid, "url": url, "status": status, "latency_ms": elapsed, "error": f"HTTP {e.code}"}
    except Exception as e:
        err_msg = str(e)
        if "timed out" in err_msg.lower():
            err_msg = "Connection timed out"
        elif "name or service not known" in err_msg.lower() or "getaddrinfo failed" in err_msg.lower():
            err_msg = "Host unreachable / DNS failed"
        return {"id": cid, "url": url, "status": "offline", "latency_ms": 0, "error": err_msg[:80]}


def run_global_health_check(trigger_type: str = "scheduled_2h_fallback_guard", playlist_id: str = None) -> dict:
    """
    Executes the 2-Hour Fallback Guard check.
    Probes every channel concurrently. If a channel's primary stream fails or expires:
      1. Probes registered backup sources from channel_sources.
      2. Automatically promotes the best working backup to primary and re-activates the channel!
      3. If all backups fail, runs auto-finder (RuTube, YouTube Live, open CDNs, web) to locate and assign fresh streams.
    Records failover stats and audit logs.
    """
    with SCHEDULER_LOCK:
        if SCHEDULER_STATE["is_running"]:
            return SCHEDULER_STATE["last_summary"]
        SCHEDULER_STATE["is_running"] = True

    now = int(time.time())
    try:
        with get_db() as conn:
            if playlist_id:
                rows = conn.execute(
                    "SELECT * FROM channels WHERE playlist_id = ?", (playlist_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM channels").fetchall()

        channel_dicts = [dict(r) for r in rows]
        results = []
        # Concurrently probe primary stream URLs
        with concurrent.futures.ThreadPoolExecutor(max_workers=PANEL_MAX_WORKERS) as pool:
            for res in pool.map(lambda c: probe_stream(c, timeout=3.8), channel_dicts):
                results.append(res)

        online_count = 0
        offline_count = 0
        failovers_count = 0

        # Process results and trigger automatic failover for dead/expired channels
        for ch, res in zip(channel_dicts, results):
            st = res["status"]
            cid = ch["id"]
            if st == "online":
                online_count += 1
                with get_db() as conn:
                    conn.execute(
                        """
                        UPDATE channels
                        SET status = ?, latency_ms = ?, last_checked_at = ?, fail_count = 0, last_error = ''
                        WHERE id = ?
                        """,
                        (st, res["latency_ms"], now, cid),
                    )
                continue

            # Primary stream is offline or expired! Initiate Fallback Guard failover
            backups = get_channel_sources_db(cid)
            backup_promoted = False

            # 1. Test existing backup streams in priority order
            for b_src in backups:
                if b_src["url"] == ch["url"]:
                    continue
                pr_b = probe_stream({"url": b_src["url"]}, timeout=3.0)
                if pr_b["status"] == "online":
                    promote_channel_source_db(cid, b_src["id"])
                    failovers_count += 1
                    online_count += 1
                    backup_promoted = True
                    break

            # 2. If it's a Televizo / tvcdnpotok stream, harvest fresh token immediately!
            if not backup_promoted:
                u_lower = ch.get("url", "").lower()
                if "tvcdnpotok" in u_lower or "cdntvmedia" in u_lower or "cdntvtech" in u_lower or find_televizo_channel_info(ch["name"]):
                    try:
                        tv_res = extract_televizo_stream_sync(ch["name"], timeout_sec=8)
                        if tv_res.get("found") and tv_res.get("url"):
                            fresh_url = tv_res["url"]
                            with get_db() as conn:
                                conn.execute(
                                    """
                                    UPDATE channels
                                    SET url = ?, status = 'online', latency_ms = 180, last_checked_at = ?, fail_count = 0, last_error = ''
                                    WHERE id = ?
                                    """,
                                    (fresh_url, now, cid),
                                )
                            add_or_update_channel_source_db(
                                channel_id=cid,
                                name=ch["name"],
                                url=fresh_url,
                                source_name="Televizo Live Extractor",
                                quality="HD",
                                priority=1,
                                confidence=100
                            )
                            failovers_count += 1
                            online_count += 1
                            backup_promoted = True
                    except Exception:
                        pass

            # 3. If no working backup exists, run live auto-finder (RuTube, YouTube, open CDNs, web)
            if not backup_promoted:
                try:
                    cands = find_stream_sources_by_name(ch["name"], tvg_id=ch.get("tvg_id", ""), limit=3, check_live=True)
                    online_cands = [c for c in cands if c.get("status") == "online" and c.get("url")]
                    if online_cands:
                        best = online_cands[0]
                        # Populate discovered sources into channel_sources
                        for c_idx, cand in enumerate(online_cands):
                            add_or_update_channel_source_db(
                                channel_id=cid,
                                name=cand.get("name", ch["name"]),
                                url=cand["url"],
                                source_name=cand.get("source", "Auto-Heal"),
                                quality=cand.get("quality", "HD"),
                                priority=c_idx + 1,
                                confidence=cand.get("confidence", 95),
                            )
                        with get_db() as conn:
                            conn.execute(
                                """
                                UPDATE channels
                                SET url = ?, status = 'online', latency_ms = ?, last_checked_at = ?, fail_count = 0, last_error = ''
                                WHERE id = ?
                                """,
                                (best["url"], best.get("latency_ms", 150), now, cid),
                            )
                        failovers_count += 1
                        online_count += 1
                        backup_promoted = True
                except Exception:
                    pass

            if not backup_promoted:
                offline_count += 1
                with get_db() as conn:
                    conn.execute(
                        """
                        UPDATE channels
                        SET status = ?, latency_ms = ?, last_checked_at = ?, fail_count = fail_count + 1, last_error = ?
                        WHERE id = ?
                        """,
                        (st, res["latency_ms"], now, res.get("error", "Stream unreachable"), cid),
                    )

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO health_logs (id, ran_at, trigger_type, total_checked, online_count, offline_count, failovers_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4())[:8], now, trigger_type, len(results), online_count, offline_count, failovers_count),
            )

        summary = {
            "ran_at": now,
            "trigger_type": trigger_type,
            "total": len(results),
            "online": online_count,
            "offline": offline_count,
            "failovers": failovers_count,
        }
        with SCHEDULER_LOCK:
            SCHEDULER_STATE["last_run_at"] = now
            SCHEDULER_STATE["next_run_at"] = now + SCHEDULER_STATE["interval_seconds"]
            SCHEDULER_STATE["last_summary"] = summary
            SCHEDULER_STATE["total_failovers"] += failovers_count
        return summary
    finally:
        with SCHEDULER_LOCK:
            SCHEDULER_STATE["is_running"] = False


def scheduled_health_monitor_loop():
    """Background daemon that runs channel fallback guard every 2 hours (12 checks/day)."""
    while True:
        time.sleep(30)
        with SCHEDULER_LOCK:
            enabled = SCHEDULER_STATE["enabled"]
            next_run = SCHEDULER_STATE["next_run_at"]
        if enabled and time.time() >= next_run:
            try:
                run_global_health_check(trigger_type="scheduled_2h_fallback_guard")
            except Exception:
                pass


TELEVIZO_RENEW_LOCK = threading.Lock()
TELEVIZO_RENEW_STATE = {
    "enabled": True,
    "interval_hours": 5,
    "interval_seconds": 5 * 3600,
    "last_run_at": 0,
    "next_run_at": int(time.time()) + 5 * 3600,
    "is_running": False,
    "last_renewed_count": 0,
    "last_total_matched": 0,
    "total_renewals": 0,
    "history": [],
}


def scheduled_televizo_renew_loop():
    """
    Background daemon that automatically re-extracts fresh live tokens for Televizo streams every 5 hours.
    Runs 24/7 in the cloud so playlist URLs are perpetually refreshed with active tokens.
    """
    time.sleep(45)  # Brief initial grace period after server start
    while True:
        with TELEVIZO_RENEW_LOCK:
            enabled = TELEVIZO_RENEW_STATE["enabled"]
            next_run = TELEVIZO_RENEW_STATE["next_run_at"]
            is_running = TELEVIZO_RENEW_STATE["is_running"]

        now = int(time.time())
        if enabled and not is_running and now >= next_run:
            print("[Televizo Auto-Renew] Starting 5-hour scheduled token renewal cycle...")
            try:
                with TELEVIZO_RENEW_LOCK:
                    TELEVIZO_RENEW_STATE["is_running"] = True

                res = sync_televizo_playlist_streams(playlist_id=None, background=False)
                renewed = res.get("refreshed_count", 0)
                matched = res.get("total_matched", 0)

                with TELEVIZO_RENEW_LOCK:
                    TELEVIZO_RENEW_STATE["last_run_at"] = now
                    TELEVIZO_RENEW_STATE["next_run_at"] = now + TELEVIZO_RENEW_STATE["interval_seconds"]
                    TELEVIZO_RENEW_STATE["last_renewed_count"] = renewed
                    TELEVIZO_RENEW_STATE["last_total_matched"] = matched
                    TELEVIZO_RENEW_STATE["total_renewals"] += renewed
                    TELEVIZO_RENEW_STATE["is_running"] = False
                    entry = {
                        "ran_at": now,
                        "renewed": renewed,
                        "matched": matched,
                        "status": "success",
                    }
                    TELEVIZO_RENEW_STATE["history"].insert(0, entry)
                    TELEVIZO_RENEW_STATE["history"] = TELEVIZO_RENEW_STATE["history"][:20]
                print(f"[Televizo Auto-Renew] Cycle complete: renewed {renewed}/{matched} channels with fresh tokens.")
            except Exception as e:
                print(f"[Televizo Auto-Renew] Error during renewal cycle: {e}")
                with TELEVIZO_RENEW_LOCK:
                    TELEVIZO_RENEW_STATE["is_running"] = False
                    TELEVIZO_RENEW_STATE["next_run_at"] = now + 600  # Retry in 10 minutes on error

        time.sleep(15)


def keep_alive_loop():
    """Background thread that pings external URL every 10 minutes to prevent cloud platform idle sleep (Render / Koyeb)."""
    while True:
        time.sleep(600)  # Ping every 10 minutes (Render sleeps after 15 min of inactivity)
        ext_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL")
        if ext_url:
            try:
                target = ext_url.rstrip("/") + "/api/health"
                req = urllib.request.Request(
                    target,
                    headers={"User-Agent": "StreamForgeKeepAlive/1.0"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    pass
            except Exception:
                pass


# ---------------------------------------------------------------------------
# IPTV Xtream Codes API Client
# ---------------------------------------------------------------------------

def fetch_xtream_codes_data(server_url: str, username: str, password: str) -> dict:
    clean_server = server_url.strip().rstrip("/")
    if clean_server.lower() in ("demo", "http://demo", "https://demo", "demo.xtream.local"):
        demo_channels = [
            {
                "name": "UK: BBC News HD",
                "url": "https://vs-hls-push-ww-live.akamaized.net/x=4/i=urn:bbc:pips:service:bbc_news_channel_hd/t=3840/v=pv14/b=5070016/main.m3u8",
                "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/62/BBC_News_2019.svg/512px-BBC_News_2019.svg.png",
                "category": "UK News",
                "tvg_id": "BBCNews.uk",
                "tvg_name": "BBC News",
                "chno": "101",
            },
            {
                "name": "UK: Sky News FHD",
                "url": "https://skynews2-plutolive-vo.akamaized.net/cdnhls/2112/master.m3u8",
                "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/de/Sky-news-logo.svg/512px-Sky-news-logo.svg.png",
                "category": "UK News",
                "tvg_id": "SkyNews.uk",
                "tvg_name": "Sky News",
                "chno": "102",
            },
            {
                "name": "US: CBS News 24/7",
                "url": "https://cbsn-us.cbsnstream.cbsnews.com/out/v1/55a8648e8f134e82a470f83d562deeca/master.m3u8",
                "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4e/CBS_logo.svg/512px-CBS_logo.svg.png",
                "category": "US News",
                "tvg_id": "CBSNews.us",
                "tvg_name": "CBS News",
                "chno": "103",
            },
            {
                "name": "US: Bloomberg TV 4K",
                "url": "https://liveproduseast.global.ssl.fastly.net/us/Channel-USTV-AWS-virginia-1/Source-USTV-1000-1_live.m3u8",
                "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5d/New_Bloomberg_Logo.svg/512px-New_Bloomberg_Logo.svg.png",
                "category": "Business",
                "tvg_id": "Bloomberg.us",
                "tvg_name": "Bloomberg TV",
                "chno": "104",
            },
            {
                "name": "SPORTS: Red Bull TV",
                "url": "https://rbmn-live.akamaized.net/hls/live/590964/BoRB-AT/master.m3u8",
                "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/91/Red_Bull_TV_logo.svg/512px-Red_Bull_TV_logo.svg.png",
                "category": "Sports",
                "tvg_id": "RedBullTV.at",
                "tvg_name": "Red Bull TV",
                "chno": "201",
            },
            {
                "name": "UK: BBC News Backup (Duplicate in Xtream List)",
                "url": "https://vs-hls-push-ww-live.akamaized.net/x=4/i=urn:bbc:pips:service:bbc_news_channel_hd/t=3840/v=pv14/b=5070016/main.m3u8",
                "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/62/BBC_News_2019.svg/512px-BBC_News_2019.svg.png",
                "category": "UK News",
                "tvg_id": "BBCNews.uk",
                "tvg_name": "BBC News",
                "chno": "105",
            },
            {
                "name": "VIP: Sky Sports Dead Feed (Offline Example)",
                "url": "https://offline-stream.example.invalid/live/demo/12345.m3u8",
                "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c5/Sky_Sports_logo_2020.svg/512px-Sky_Sports_logo_2020.svg.png",
                "category": "Sports",
                "tvg_id": "SkySports.uk",
                "tvg_name": "Sky Sports",
                "chno": "202",
            },
        ]
        return {
            "account": {
                "username": username or "demo_user",
                "status": "Active",
                "exp_date": "Unlimited (Demo Server)",
                "active_cons": "1",
                "max_connections": "5",
                "server_url": "demo.xtream.local",
            },
            "channels": demo_channels,
        }

    if not clean_server.startswith(("http://", "https://")):
        clean_server = "http://" + clean_server

    q_auth = f"username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}"
    base_api = f"{clean_server}/player_api.php?{q_auth}"
    headers = {"User-Agent": "Mozilla/5.0 (IPTVSmartersPro) StreamForge/3.0"}

    req_info = urllib.request.Request(base_api, headers=headers)
    with urllib.request.urlopen(req_info, timeout=10.0) as resp:
        info_data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    user_info = info_data.get("user_info") or {}
    if str(user_info.get("auth", 0)) == "0":
        raise ValueError("Xtream Codes authentication failed: Invalid username or password.")

    cat_map = {}
    try:
        req_cats = urllib.request.Request(f"{base_api}&action=get_live_categories", headers=headers)
        with urllib.request.urlopen(req_cats, timeout=10.0) as resp:
            cats_data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            if isinstance(cats_data, list):
                for c in cats_data:
                    cat_map[str(c.get("category_id"))] = c.get("category_name") or "Uncategorized"
    except Exception:
        pass

    req_streams = urllib.request.Request(f"{base_api}&action=get_live_streams", headers=headers)
    with urllib.request.urlopen(req_streams, timeout=15.0) as resp:
        streams_data = json.loads(resp.read().decode("utf-8", errors="ignore"))

    if not isinstance(streams_data, list):
        raise ValueError("Unexpected response from Xtream Codes get_live_streams.")

    channels = []
    for st in streams_data:
        stream_id = st.get("stream_id")
        if not stream_id:
            continue
        name = (st.get("name") or f"Stream {stream_id}").strip()
        cat_id = str(st.get("category_id") or "")
        category = cat_map.get(cat_id, "Xtream Live")
        stream_url = f"{clean_server}/live/{urllib.parse.quote(username)}/{urllib.parse.quote(password)}/{stream_id}.m3u8"
        channels.append({
            "name": name,
            "url": stream_url,
            "logo": (st.get("stream_icon") or "").strip(),
            "category": category,
            "tvg_id": (st.get("epg_channel_id") or "").strip(),
            "tvg_name": name,
            "chno": str(st.get("num") or ""),
        })

    return {
        "account": {
            "username": user_info.get("username", username),
            "status": user_info.get("status", "Active"),
            "exp_date": user_info.get("exp_date", ""),
            "active_cons": str(user_info.get("active_cons", "0")),
            "max_connections": str(user_info.get("max_connections", "1")),
            "server_url": clean_server,
        },
        "channels": channels,
    }


# ---------------------------------------------------------------------------
# Cloud Deployment Packager
# ---------------------------------------------------------------------------

def generate_cloud_bundle_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fname in [
            "app.py",
            "requirements.txt",
            "Dockerfile",
            "render.yaml",
            "Procfile",
            "fly.toml",
            "docker-compose.yml",
            "CLOUD_HOSTING_GUIDE.md",
            "README.md",
        ]:
            fpath = BASE_DIR / fname
            if fpath.exists():
                z.write(fpath, arcname=fname)
        for root, _, files in os.walk(STATIC_DIR):
            for file in files:
                full_p = Path(root) / file
                rel_p = full_p.relative_to(BASE_DIR)
                z.write(full_p, arcname=str(rel_p).replace("\\", "/"))
        for root, _, files in os.walk(DATA_DIR):
            for file in files:
                full_p = Path(root) / file
                rel_p = full_p.relative_to(BASE_DIR)
                z.write(full_p, arcname=str(rel_p).replace("\\", "/"))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTTP Request Handler & Universal CORS Stream Proxy
# ---------------------------------------------------------------------------

class IPTVStudioHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, data, status=200):
        try:
            raw = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/proxy-stream":
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.end_headers()
            return
        return super().do_HEAD()

    def _proxy_stream_request(self, target_url: str):
        """
        Universal CORS-bypass stream proxy.
        Fetches remote .m3u8 playlists and rewrites variant manifests & chunks to route
        through this proxy, emitting full CORS headers so in-browser video players never fail.
        Supports automatic resolution of YouTube Live stream URLs.
        """
        try:
            # Check if target_url is a YouTube video URL, resolve fresh HLS manifest
            if "youtube.com" in target_url or "youtu.be" in target_url:
                yt_vid_match = re.search(r"(?:v=|youtu\.be/|live/|embed/)([a-zA-Z0-9_-]{11})", target_url)
                if yt_vid_match:
                    vid_id = yt_vid_match.group(1)
                    resolved = resolve_youtube_live_manifest(vid_id)
                    if resolved:
                        target_url = resolved
                    else:
                        self.send_json({"error": f"YouTube Live stream not active for video {vid_id}", "status": 404}, status=404)
                        return

            req_headers = {
                "User-Agent": "Mozilla/5.0 (VLC/3.0.20 LibVLC/3.0.20) StreamForge/4.0",
                "Accept": "*/*",
            }
            if "cdntvtech" in target_url.lower():
                pu = urllib.parse.urlparse(target_url)
                req_headers["Referer"] = f"{pu.scheme}://{pu.netloc}/"
            range_hdr = self.headers.get("Range")
            if range_hdr:
                req_headers["Range"] = range_hdr

            req = urllib.request.Request(target_url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                status_code = resp.getcode()
                content_type = resp.headers.get("Content-Type", "")
                first_chunk = resp.read(65536)
                is_m3u8 = (
                    first_chunk.lstrip().startswith(b"#EXTM3U")
                    or "mpegurl" in content_type.lower()
                    or target_url.lower().endswith(".m3u8")
                    or "application/x-mpegurl" in content_type.lower()
                )

                if is_m3u8:
                    rest = resp.read(2 * 1024 * 1024)
                    body = first_chunk + rest
                    text = body.decode("utf-8", errors="replace")
                    lines = text.splitlines()
                    rewritten = []
                    for line in lines:
                        sline = line.strip()
                        if not sline:
                            rewritten.append(line)
                            continue
                        if sline.startswith("#"):
                            if 'URI="' in sline:
                                def _replace_uri(m):
                                    raw_u = m.group(1)
                                    abs_u = urllib.parse.urljoin(target_url, raw_u)
                                    return f'URI="/api/proxy-stream?url={urllib.parse.quote(abs_u)}"'
                                sline = re.sub(r'URI="([^"]+)"', _replace_uri, sline)
                            rewritten.append(sline)
                        else:
                            abs_u = urllib.parse.urljoin(target_url, sline)
                            rewritten.append(f"/api/proxy-stream?url={urllib.parse.quote(abs_u)}")

                    output_bytes = "\n".join(rewritten).encode("utf-8")
                    self.send_response(status_code)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl; charset=utf-8")
                    self.send_header("Content-Length", str(len(output_bytes)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(output_bytes)
                else:
                    # Stream binary video chunks directly without buffering entire stream in RAM
                    self.send_response(status_code)
                    if content_type:
                        self.send_header("Content-Type", content_type)
                    else:
                        self.send_header("Content-Type", "video/mp2t")
                    remote_cl = resp.headers.get("Content-Length")
                    if remote_cl:
                        self.send_header("Content-Length", remote_cl)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "*")
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    if first_chunk:
                        self.wfile.write(first_chunk)
                    while True:
                        chunk = resp.read(32768)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        except urllib.error.HTTPError as e:
            try:
                self.send_json(
                    {"error": f"Remote stream server returned HTTP {e.code}: {e.reason}", "status": e.code},
                    status=e.code,
                )
            except Exception:
                pass
        except Exception as e:
            try:
                self.send_json({"error": f"Stream proxy error: {e}", "status": 502}, status=502)
            except Exception:
                pass

    def do_GET(self):
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json({"error": f"Server error: {e}"}, 500)
            except Exception:
                pass

    def _handle_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/api/health", "/ping", "/health"):
            with SCHEDULER_LOCK:
                sched_ok = SCHEDULER_STATE.get("enabled", True)
            self.send_json({
                "status": "healthy",
                "service": "streamforge-iptv-studio",
                "scheduler_enabled": sched_ok,
                "timestamp": int(time.time()),
            })
            return

        m3u_match = re.match(r"^/m3u/([a-zA-Z0-9_-]+?)(?:\.(m3u8|m3u))?$", path)
        if m3u_match:
            pid = m3u_match.group(1)
            ext = m3u_match.group(2) or "m3u"
            with get_db() as conn:
                pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
                if not pl:
                    self.send_json({"error": "Playlist not found"}, 404)
                    return
                ch_rows = conn.execute(
                    "SELECT * FROM channels WHERE playlist_id = ? ORDER BY position ASC, rowid ASC",
                    (pid,),
                ).fetchall()
            smart = qs.get("smart", ["0"])[0] == "1"
            host_header = self.headers.get("Host", f"127.0.0.1:{SERVER_PORT}")
            base_url = f"http://{host_header}"
            net_info = get_network_info()
            if net_info.get("public_url"):
                base_url = net_info["public_url"]
            m3u_text = build_m3u_export(dict(pl), [dict(r) for r in ch_rows], smart_failover=smart, base_url=base_url)
            raw = m3u_text.encode("utf-8")
            safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", pl["name"]).strip("_") or "playlist"
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            if qs.get("download", ["0"])[0] == "1":
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{safe_name}.{ext}"'
                )
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        stream_match = re.match(r"^/api/stream/([a-zA-Z0-9_-]+)/live\.m3u8$", path)
        if stream_match:
            cid = stream_match.group(1)
            with get_db() as conn:
                ch = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
                if not ch:
                    self.send_response(404)
                    self.end_headers()
                    return
                ch_dict = dict(ch)
                sources = conn.execute(
                    "SELECT * FROM channel_sources WHERE channel_id = ? ORDER BY priority ASC",
                    (cid,)
                ).fetchall()
                src_dicts = [dict(s) for s in sources]

            # Test primary stream first (fast 1.5s probe)
            pr = probe_stream(ch_dict, timeout=1.5)
            if pr["status"] == "online":
                self.send_response(302)
                self.send_header("Location", ch_dict["url"])
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return

            # Primary stream failed! Test backup sources in priority order
            for s in src_dicts:
                if s["url"] == ch_dict["url"]:
                    continue
                pr_b = probe_stream({"url": s["url"]}, timeout=1.5)
                if pr_b["status"] == "online":
                    def _promote():
                        try:
                            with get_db() as c:
                                c.execute("UPDATE channels SET url = ?, status = 'online', latency_ms = ? WHERE id = ?", (s["url"], pr_b["latency_ms"], cid))
                                c.execute("UPDATE channel_sources SET priority = 1 WHERE id = ?", (s["id"],))
                        except Exception:
                            pass
                    threading.Thread(target=_promote, daemon=True).start()

                    self.send_response(302)
                    self.send_header("Location", s["url"])
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    return

            # Fallback redirect to current primary url anyway
            self.send_response(302)
            self.send_header("Location", ch_dict["url"])
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        ch_sources_match = re.match(r"^/api/channels/([a-zA-Z0-9_-]+)/sources$", path)
        if ch_sources_match:
            cid = ch_sources_match.group(1)
            sources = get_channel_sources_db(cid)
            self.send_json({"channel_id": cid, "sources": sources})
            return

        if path == "/api/network-info":
            self.send_json(get_network_info())
            return

        if path == "/api/televizo/auto-renew/status":
            with TELEVIZO_RENEW_LOCK:
                st = dict(TELEVIZO_RENEW_STATE)
            self.send_json(st)
            return

        if path == "/api/scheduler/status":
            logs = []
            broken_rows = []
            try:
                with get_db() as conn:
                    logs = conn.execute(
                        "SELECT * FROM health_logs ORDER BY ran_at DESC LIMIT 10"
                    ).fetchall()
                    broken_rows = conn.execute(
                        """
                        SELECT c.*, p.name AS playlist_name
                        FROM channels c
                        JOIN playlists p ON p.id = c.playlist_id
                        WHERE c.status IN ('offline', 'warning')
                        ORDER BY c.last_checked_at DESC
                        LIMIT 100
                        """
                    ).fetchall()
            except Exception:
                pass

            with SCHEDULER_LOCK:
                sched_copy = dict(SCHEDULER_STATE)
            with TELEVIZO_RENEW_LOCK:
                televizo_copy = dict(TELEVIZO_RENEW_STATE)
            self.send_json({
                "scheduler": sched_copy,
                "televizo_renew": televizo_copy,
                "logs": [dict(r) for r in logs],
                "broken_channels": [dict(r) for r in broken_rows],
            })
            return

        if path == "/api/playlists":
            with get_db() as conn:
                rows = conn.execute(
                    """
                    SELECT p.*,
                           COUNT(c.id) AS channel_count,
                           COUNT(DISTINCT c.category) AS category_count,
                           SUM(CASE WHEN c.logo != '' THEN 1 ELSE 0 END) AS with_logo_count,
                           SUM(CASE WHEN c.status = 'offline' THEN 1 ELSE 0 END) AS offline_count,
                           SUM(CASE WHEN c.status = 'online' THEN 1 ELSE 0 END) AS online_count
                    FROM playlists p
                    LEFT JOIN channels c ON c.playlist_id = p.id
                    GROUP BY p.id
                    ORDER BY p.updated_at DESC
                    """
                ).fetchall()
            self.send_json({"playlists": [dict(r) for r in rows]})
            return

        pl_detail_match = re.match(r"^/api/playlists/([a-zA-Z0-9_-]+)$", path)
        if pl_detail_match:
            pid = pl_detail_match.group(1)
            with get_db() as conn:
                pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
                if not pl:
                    self.send_json({"error": "Playlist not found"}, 404)
                    return
                channels = conn.execute(
                    "SELECT * FROM channels WHERE playlist_id = ? ORDER BY position ASC, rowid ASC",
                    (pid,),
                ).fetchall()
                backup_rows = conn.execute(
                    """
                    SELECT channel_id, COUNT(*) as cnt
                    FROM channel_sources
                    GROUP BY channel_id
                    """
                ).fetchall()
                b_map = {r["channel_id"]: r["cnt"] for r in backup_rows}

            ch_list = []
            for c in channels:
                cd = dict(c)
                cd["backup_count"] = b_map.get(cd["id"], 0)
                ch_list.append(cd)

            self.send_json({
                "playlist": dict(pl),
                "channels": ch_list,
            })
            return

        if path == "/api/logos/search":
            q = qs.get("q", [""])[0]
            tvg_id = qs.get("tvg_id", [""])[0]
            candidates = find_logo_candidates(q, tvg_id, limit=10)
            self.send_json({"candidates": candidates})
            return

        if path == "/api/sources/search":
            q = qs.get("q", [""])[0]
            tvg_id = qs.get("tvg_id", [""])[0]
            country = qs.get("country", [""])[0]
            strict = qs.get("strict", ["1"])[0] == "1"
            check_live = qs.get("probe", ["1"])[0] == "1"
            include_web = qs.get("include_web", ["1"])[0] == "1"
            include_yt = qs.get("include_yt", ["1"])[0] == "1"
            include_rutube = qs.get("include_rutube", ["1"])[0] == "1"
            include_televizo = qs.get("include_televizo", ["1"])[0] == "1"
            sources = find_stream_sources_by_name(q, tvg_id, limit=10, check_live=check_live, include_web=include_web, include_yt=include_yt, include_rutube=include_rutube, include_televizo=include_televizo, target_country=country, strict=strict)
            self.send_json({"sources": sources})
            return

        if path == "/api/sources/rutube-live":
            q = qs.get("q", [""])[0]
            country = qs.get("country", [""])[0]
            strict = qs.get("strict", ["1"])[0] == "1"
            check_live = qs.get("probe", ["1"])[0] == "1"
            limit = int(qs.get("limit", ["5"])[0])
            sources = search_rutube_live_streams_fast(q, target_country=country, strict=strict, limit=limit, check_live=check_live)
            self.send_json({"sources": sources, "query": q})
            return

        if path == "/api/sources/youtube-live":
            q = qs.get("q", [""])[0]
            country = qs.get("country", [""])[0]
            strict = qs.get("strict", ["1"])[0] == "1"
            check_live = qs.get("probe", ["1"])[0] == "1"
            limit = int(qs.get("limit", ["5"])[0])
            sources = search_youtube_live_streams_fast(q, target_country=country, strict=strict, limit=limit, check_live=check_live)
            self.send_json({"sources": sources, "query": q})
            return

        if path == "/api/sources/web-search":
            q = qs.get("q", [""])[0]
            country = qs.get("country", [""])[0]
            check_live = qs.get("probe", ["1"])[0] == "1"
            sources = search_live_web_for_channel_streams(q, limit=8, check_live=check_live, target_country=country)
            self.send_json({"sources": sources, "query": q})
            return

        if path == "/api/github/search-repos":
            q = qs.get("q", [""])[0]
            repos = search_github_iptv_repositories(q, limit=10)
            self.send_json({"repositories": repos, "query": q})
            return

        if path == "/api/sources/providers":
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT * FROM provider_sources ORDER BY position ASC, rowid ASC"
                ).fetchall()
            cache = ensure_stream_sources_loaded()
            self.send_json({
                "providers": [dict(r) for r in rows],
                "total_streams": len(cache["items"]),
                "providers_count": len(rows),
            })
            return

        if path == "/api/proxy-stream":
            yt_id = qs.get("yt", [""])[0]
            url = qs.get("url", [""])[0]
            if yt_id:
                manifest_url = resolve_youtube_live_manifest(yt_id)
                if not manifest_url:
                    self.send_json({"error": f"Could not resolve live YouTube manifest for video ID {yt_id}. The broadcast may be offline.", "status": 404}, 404)
                    return
                self._proxy_stream_request(manifest_url)
                return
            if not url or not url.startswith(("http://", "https://")):
                self.send_json({"error": "Missing or invalid url parameter"}, 400)
                return
            self._proxy_stream_request(url)
            return

        if path == "/api/download-cloud-bundle":
            zip_bytes = generate_cloud_bundle_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="streamforge-cloud-deploy.zip"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(zip_bytes)
            return

        return super().do_GET()

    def do_POST(self):
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self.send_json({"error": f"Server error: {e}"}, 500)
            except Exception:
                pass

    def _handle_post(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self.read_json()

        # Bulk Search Stream Sources by Channel Names List (up to 100 channels at once!)
        if path == "/api/sources/bulk-search":
            names_in = body.get("names") or []
            raw_text = body.get("raw_text") or ""
            channel_ids = body.get("channel_ids") or []
            country = (body.get("country") or "").strip()
            strict = bool(body.get("strict", True))
            check_live = bool(body.get("probe_live", True))
            include_web = bool(body.get("include_web", True))
            include_yt = bool(body.get("include_yt", True))
            include_rutube = bool(body.get("include_rutube", True))
            include_televizo = bool(body.get("include_televizo", True))

            queries = []
            if channel_ids:
                placeholders = ",".join("?" for _ in channel_ids[:100])
                with get_db() as conn:
                    rows = conn.execute(
                        f"SELECT id, name, tvg_id, category FROM channels WHERE id IN ({placeholders})",
                        channel_ids[:100],
                    ).fetchall()
                queries.extend([dict(r) for r in rows])

            if raw_text and not names_in:
                names_in = [line.strip() for line in raw_text.splitlines() if line.strip()]

            for item in names_in:
                if len(queries) >= 100:
                    break
                queries.append(item)

            results = bulk_find_sources_for_names(queries[:100], check_live=check_live, include_web=include_web, include_yt=include_yt, include_rutube=include_rutube, include_televizo=include_televizo, target_country=country, strict=strict)
            found_count = sum(1 for r in results if r.get("found"))
            online_count = sum(1 for r in results if r.get("status") == "online")
            self.send_json({
                "total_requested": len(queries[:100]),
                "found_count": found_count,
                "online_count": online_count,
                "results": results,
            })
            return

        # Apply Bulk Found Sources to Existing Broken Channels (in Non-Working Channels Center)
        if path == "/api/sources/bulk-apply":
            replacements = body.get("replacements") or []
            now = int(time.time())
            updated = 0
            with get_db() as conn:
                for rep in replacements:
                    cid = rep.get("channel_id")
                    url = (rep.get("url") or "").strip()
                    if not cid or not url:
                        continue
                    status = rep.get("status") if rep.get("status") in ("online", "warning") else "online"
                    latency = int(rep.get("latency_ms") or 0)
                    logo = (rep.get("logo") or "").strip()
                    conn.execute(
                        """
                        UPDATE channels
                        SET url = ?,
                            logo = CASE WHEN logo = '' AND ? != '' THEN ? ELSE logo END,
                            status = ?,
                            latency_ms = ?,
                            last_checked_at = ?,
                            fail_count = 0,
                            last_error = ''
                        WHERE id = ?
                        """,
                        (url, logo, logo, status, latency, now, cid),
                    )
                    updated += 1
            self.send_json({"updated": updated})
            return

        # GitHub Playlist Preview & Inspection
        if path == "/api/github/preview-playlist":
            playlist_url = (body.get("url") or "").strip()
            filter_q = (body.get("filter_query") or "").strip()
            data = preview_github_playlist_channels(playlist_url, filter_query=filter_q)
            self.send_json(data)
            return

        # Provider Sources Management (Add, Toggle, Delete, Sync)
        if path == "/api/sources/providers/add":
            name = (body.get("name") or "").strip()
            url = (body.get("url") or "").strip()
            badge = (body.get("badge") or "").strip()
            prov_type = (body.get("provider_type") or "custom_m3u").strip()

            if not name or not url or not url.startswith(("http://", "https://")):
                self.send_json({"error": "Valid provider name and HTTP/HTTPS M3U URL are required"}, 400)
                return

            if not badge:
                badge = f"⚡ {name[:12]}"

            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamForge/4.0"},
                )
                with urllib.request.urlopen(req, timeout=14.0) as resp:
                    text = resp.read().decode("utf-8", errors="ignore")
                parsed_items = parse_provider_m3u_items(text, name, badge)
            except Exception as e:
                self.send_json({"error": f"Failed to fetch M3U from provider URL: {e}"}, 400)
                return

            pid = "prov_" + str(uuid.uuid4())[:8]
            now = int(time.time())
            with get_db() as conn:
                max_pos = conn.execute("SELECT MAX(position) AS m FROM provider_sources").fetchone()["m"] or 0
                conn.execute(
                    """
                    INSERT INTO provider_sources (id, name, url, badge, provider_type, channel_count, last_synced_at, enabled, is_system, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
                    """,
                    (pid, name, url, badge, prov_type, len(parsed_items), now, max_pos + 1),
                )
                row = conn.execute("SELECT * FROM provider_sources WHERE id = ?", (pid,)).fetchone()

            threading.Thread(target=sync_all_provider_sources, daemon=True).start()
            self.send_json({
                "provider": dict(row),
                "channels_indexed": len(parsed_items),
                "message": f"Successfully added provider with {len(parsed_items)} streams!",
            }, 201)
            return

        if path == "/api/sources/providers/toggle":
            pid = body.get("id")
            enabled = int(bool(body.get("enabled", True)))
            with get_db() as conn:
                conn.execute("UPDATE provider_sources SET enabled = ? WHERE id = ?", (enabled, pid))
            threading.Thread(target=sync_all_provider_sources, daemon=True).start()
            self.send_json({"id": pid, "enabled": enabled})
            return

        if path == "/api/sources/providers/delete":
            pid = body.get("id")
            with get_db() as conn:
                row = conn.execute("SELECT * FROM provider_sources WHERE id = ?", (pid,)).fetchone()
                if not row:
                    self.send_json({"error": "Provider not found"}, 404)
                    return
                if row["is_system"]:
                    self.send_json({"error": "Cannot delete system default provider. You can disable it instead."}, 400)
                    return
                conn.execute("DELETE FROM provider_sources WHERE id = ?", (pid,))
            threading.Thread(target=sync_all_provider_sources, daemon=True).start()
            self.send_json({"deleted": pid})
            return

        if path == "/api/sources/providers/sync":
            threading.Thread(target=sync_all_provider_sources, daemon=True).start()
            self.send_json({"status": "syncing", "message": "Syncing all active stream providers in background"})
            return

        # Public Tunnel & Custom Domain Configuration
        if path == "/api/tunnel/action":
            action = body.get("action", "start")
            provider = body.get("provider", "localhost.run")
            if "custom_base_url" in body:
                with TUNNEL_LOCK:
                    TUNNEL_STATE["custom_base_url"] = (body.get("custom_base_url") or "").strip().rstrip("/")
            if action == "start":
                info = start_public_tunnel(provider=provider)
                self.send_json(info)
                return
            elif action == "stop":
                stop_public_tunnel()
                self.send_json(get_network_info())
                return
            self.send_json(get_network_info())
            return

        # 2-Hour Fallback Guard & Fast Parallel Auto-Heal Broken Channels
        if path == "/api/scheduler/action":
            action = body.get("action")
            if action in ("run_now", "run_fallback_guard"):
                pid = body.get("playlist_id")
                summary = run_global_health_check(trigger_type="manual_fallback_guard", playlist_id=pid)
                self.send_json({"summary": summary})
                return
            elif action == "configure":
                with SCHEDULER_LOCK:
                    if "enabled" in body:
                        SCHEDULER_STATE["enabled"] = bool(body["enabled"])
                    if "checks_per_day" in body:
                        cpd = max(1, min(24, int(body["checks_per_day"])))
                        SCHEDULER_STATE["checks_per_day"] = cpd
                        SCHEDULER_STATE["interval_seconds"] = int((24 * 3600) / cpd)
                        SCHEDULER_STATE["next_run_at"] = int(time.time()) + SCHEDULER_STATE["interval_seconds"]
                self.send_json({"scheduler": dict(SCHEDULER_STATE)})
                return
            elif action == "populate_playlist_backups":
                pid = body.get("playlist_id")
                if not pid:
                    self.send_json({"error": "playlist_id required"}, 400)
                    return
                res = auto_populate_playlist_backups_db(pid, max_sources_per_channel=4)
                self.send_json(res)
                return
            elif action == "auto_heal_broken":
                pid = body.get("playlist_id")
                # 1. Read up to 100 broken channels outside network loop
                with get_db() as conn:
                    if pid:
                        broken_rows = conn.execute(
                            "SELECT id, name, tvg_id, category FROM channels WHERE playlist_id = ? AND status IN ('offline', 'warning') LIMIT 100",
                            (pid,),
                        ).fetchall()
                    else:
                        broken_rows = conn.execute(
                            "SELECT id, name, tvg_id, category FROM channels WHERE status IN ('offline', 'warning') LIMIT 100"
                        ).fetchall()

                broken_list = [dict(r) for r in broken_rows]
                # 2. Match & probe in parallel outside DB lock (includes RuTube, YouTube, open CDNs, web)
                matched_results = bulk_find_sources_for_names(broken_list, check_live=True, include_rutube=True, include_yt=True)

                # 3. Fast DB update & populate channel_sources with options
                healed = 0
                now = int(time.time())
                with get_db() as conn:
                    for res in matched_results:
                        if res.get("found") and res.get("url") and res.get("status") in ("online", "warning"):
                            cid = res["channel_id"]
                            conn.execute(
                                """
                                UPDATE channels
                                SET url = ?, status = 'online', latency_ms = ?, last_checked_at = ?, fail_count = 0, last_error = ''
                                WHERE id = ?
                                """,
                                (res["url"], res["latency_ms"], now, cid),
                            )
                            # Populate backup sources
                            opts = res.get("options") or []
                            for o_idx, opt in enumerate(opts):
                                add_or_update_channel_source_db(
                                    channel_id=cid,
                                    name=opt.get("name", res.get("name", "Stream")),
                                    url=opt["url"],
                                    source_name=opt.get("source", "Auto-Heal"),
                                    quality=opt.get("quality", "HD"),
                                    priority=o_idx + 1,
                                    confidence=opt.get("confidence", 95),
                                )
                            healed += 1
                self.send_json({"healed": healed, "total_broken": len(broken_list), "results": matched_results})
                return

        # Channel Backup Sources Management
        ch_add_source_match = re.match(r"^/api/channels/([a-zA-Z0-9_-]+)/sources$", path)
        if ch_add_source_match:
            cid = ch_add_source_match.group(1)
            name = (body.get("name") or "Backup Stream").strip()
            url = (body.get("url") or "").strip()
            source_name = (body.get("source_name") or "Custom Backup").strip()
            quality = (body.get("quality") or "HD").strip()
            priority = body.get("priority")
            res = add_or_update_channel_source_db(cid, name, url, source_name=source_name, quality=quality, priority=priority)
            self.send_json({"source": res})
            return

        ch_promote_match = re.match(r"^/api/channels/([a-zA-Z0-9_-]+)/promote-source$", path)
        if ch_promote_match:
            cid = ch_promote_match.group(1)
            source_id = body.get("source_id")
            success = promote_channel_source_db(cid, source_id)
            self.send_json({"success": success})
            return

        ch_del_source_match = re.match(r"^/api/channels/([a-zA-Z0-9_-]+)/delete-source$", path)
        if ch_del_source_match:
            cid = ch_del_source_match.group(1)
            source_id = body.get("source_id")
            success = delete_channel_source_db(cid, source_id)
            self.send_json({"success": success})
            return

        ch_auto_backups_match = re.match(r"^/api/channels/([a-zA-Z0-9_-]+)/auto-backups$", path)
        if ch_auto_backups_match:
            cid = ch_auto_backups_match.group(1)
            sources = auto_populate_channel_backups_db(cid, max_sources=4)
            self.send_json({"channel_id": cid, "sources": sources, "total_backups": len(sources)})
            return

        pl_auto_backups_match = re.match(r"^/api/playlists/([a-zA-Z0-9_-]+)/auto-populate-backups$", path)
        if pl_auto_backups_match:
            pid = pl_auto_backups_match.group(1)
            res = auto_populate_playlist_backups_db(pid, max_sources_per_channel=4)
            self.send_json(res)
            return

        # Extract single fresh stream from Televizo.live
        if path == "/api/televizo/extract":
            channel_name = (body.get("channel_name") or body.get("name") or "").strip()
            page_url = (body.get("page_url") or body.get("url") or "").strip()
            res = extract_televizo_stream_sync(page_url or channel_name, timeout_sec=12)
            self.send_json(res)
            return

        # Auto-sync/refresh all Televizo channels in a playlist (or across all playlists) with fresh tokens
        if path == "/api/televizo/sync-playlist":
            pid = body.get("playlist_id")
            res = sync_televizo_playlist_streams(pid)
            self.send_json(res)
            return

        # Televizo 5-Hour Auto-Renew Trigger
        if path == "/api/televizo/auto-renew/trigger":
            def _async_renew():
                with TELEVIZO_RENEW_LOCK:
                    TELEVIZO_RENEW_STATE["is_running"] = True
                try:
                    res = sync_televizo_playlist_streams(playlist_id=None, background=False)
                    now = int(time.time())
                    with TELEVIZO_RENEW_LOCK:
                        TELEVIZO_RENEW_STATE["last_run_at"] = now
                        TELEVIZO_RENEW_STATE["next_run_at"] = now + TELEVIZO_RENEW_STATE["interval_seconds"]
                        TELEVIZO_RENEW_STATE["last_renewed_count"] = res.get("refreshed_count", 0)
                        TELEVIZO_RENEW_STATE["last_total_matched"] = res.get("total_matched", 0)
                        TELEVIZO_RENEW_STATE["total_renewals"] += res.get("refreshed_count", 0)
                        TELEVIZO_RENEW_STATE["is_running"] = False
                        entry = {
                            "ran_at": now,
                            "renewed": res.get("refreshed_count", 0),
                            "matched": res.get("total_matched", 0),
                            "status": "manual_success",
                        }
                        TELEVIZO_RENEW_STATE["history"].insert(0, entry)
                        TELEVIZO_RENEW_STATE["history"] = TELEVIZO_RENEW_STATE["history"][:20]
                except Exception:
                    with TELEVIZO_RENEW_LOCK:
                        TELEVIZO_RENEW_STATE["is_running"] = False

            threading.Thread(target=_async_renew, daemon=True).start()
            self.send_json({"ok": True, "message": "Televizo token renewal started in background"})
            return

        # Televizo 5-Hour Auto-Renew Configure
        if path == "/api/televizo/auto-renew/config":
            with TELEVIZO_RENEW_LOCK:
                if "enabled" in body:
                    TELEVIZO_RENEW_STATE["enabled"] = bool(body["enabled"])
                if "interval_hours" in body:
                    hrs = max(1, min(24, int(body["interval_hours"])))
                    TELEVIZO_RENEW_STATE["interval_hours"] = hrs
                    TELEVIZO_RENEW_STATE["interval_seconds"] = hrs * 3600
                    TELEVIZO_RENEW_STATE["next_run_at"] = int(time.time()) + TELEVIZO_RENEW_STATE["interval_seconds"]
                ret = dict(TELEVIZO_RENEW_STATE)
            self.send_json({"ok": True, "config": ret})
            return

        # Probe arbitrary batch of candidate channels
        if path == "/api/streams/probe-batch":
            items = body.get("channels") or []
            for idx, item in enumerate(items):
                item["_idx"] = idx
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=PANEL_MAX_WORKERS) as pool:
                for res in pool.map(lambda c: probe_stream(c, timeout=3.8), items[:200]):
                    results.append(res)
            gc.collect()
            self.send_json({"results": results})
            return

        # Offload & Sync with GitHub Actions
        if path == "/api/github/sync-db":
            sync_db_from_github_raw()
            init_db()
            self.send_json({"ok": True, "message": "Synced latest database from GitHub Actions"})
            return

        if path == "/api/github/trigger-renew":
            gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
            if not gh_token:
                self.send_json({
                    "ok": False,
                    "error": "GITHUB_TOKEN env var not set on Render. Trigger manually in GitHub Actions tab or add GITHUB_TOKEN."
                }, 400)
                return
            try:
                api_url = "https://api.github.com/repos/zeroping4500-maker/streamforge-iptv-studio/actions/workflows/renew_streams.yml/dispatches"
                req = urllib.request.Request(
                    api_url,
                    data=json.dumps({"ref": "main"}).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {gh_token}",
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "StreamForgePanel/1.0",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    self.send_json({"ok": True, "status": resp.getcode(), "message": "GitHub Actions 7GB renewal workflow triggered!"})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
            return

        # IPTV Xtream Codes Login & Inspect
        if path == "/api/xtream/inspect":
            server_url = (body.get("server_url") or "").strip()
            username = (body.get("username") or "").strip()
            password = (body.get("password") or "").strip()
            playlist_id = body.get("playlist_id")

            if not server_url:
                self.send_json({"error": "Please provide an Xtream Server URL (or type 'demo')"}, 400)
                return

            try:
                xtream_data = fetch_xtream_codes_data(server_url, username, password)
            except Exception as e:
                self.send_json({"error": f"Xtream Codes Connection Error: {e}"}, 400)
                return

            existing_channels = []
            if playlist_id:
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT name, url FROM channels WHERE playlist_id = ?", (playlist_id,)
                    ).fetchall()
                    existing_channels = [dict(r) for r in rows]

            dup_summary = annotate_duplicates(xtream_data["channels"], existing_channels)
            cats = {}
            for ch in xtream_data["channels"]:
                c = ch.get("category") or "Uncategorized"
                cats[c] = cats.get(c, 0) + 1

            self.send_json({
                "account": xtream_data["account"],
                "duplicate_summary": dup_summary,
                "categories": [{"name": k, "count": v} for k, v in sorted(cats.items())],
                "channels": xtream_data["channels"],
            })
            return

        # Create playlist
        if path == "/api/playlists":
            name = (body.get("name") or "New IPTV Playlist").strip()
            description = (body.get("description") or "").strip()
            epg_url = (body.get("epg_url") or "").strip()
            pid = str(uuid.uuid4())[:8]
            now = int(time.time())
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO playlists (id, name, description, epg_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, name, description, epg_url, now, now),
                )
                pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
            self.send_json({"playlist": dict(pl)}, 201)
            return

        # Update / Rename / Duplicate / Delete Playlist
        pl_match = re.match(r"^/api/playlists/([a-zA-Z0-9_-]+)/(update|delete|duplicate)$", path)
        if pl_match:
            pid, action = pl_match.group(1), pl_match.group(2)
            with get_db() as conn:
                pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
                if not pl:
                    self.send_json({"error": "Playlist not found"}, 404)
                    return
                now = int(time.time())
                if action == "update":
                    name = (body.get("name") or pl["name"]).strip()
                    desc = body.get("description", pl["description"])
                    epg = body.get("epg_url", pl["epg_url"])
                    conn.execute(
                        "UPDATE playlists SET name = ?, description = ?, epg_url = ?, updated_at = ? WHERE id = ?",
                        (name, desc, epg, now, pid),
                    )
                    updated = conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
                    self.send_json({"playlist": dict(updated)})
                    return
                elif action == "delete":
                    conn.execute("DELETE FROM playlists WHERE id = ?", (pid,))
                    self.send_json({"deleted": pid})
                    return
                elif action == "duplicate":
                    new_id = str(uuid.uuid4())[:8]
                    new_name = f'{pl["name"]} (Copy)'
                    conn.execute(
                        "INSERT INTO playlists (id, name, description, epg_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (new_id, new_name, pl["description"], pl["epg_url"], now, now),
                    )
                    ch_rows = conn.execute(
                        "SELECT * FROM channels WHERE playlist_id = ? ORDER BY position ASC", (pid,)
                    ).fetchall()
                    for ch in ch_rows:
                        conn.execute(
                            """
                            INSERT INTO channels (id, playlist_id, name, url, logo, category, tvg_id, tvg_name, chno, user_agent, referrer, status, latency_ms, last_checked_at, enabled, position)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                new_id,
                                ch["name"],
                                ch["url"],
                                ch["logo"],
                                ch["category"],
                                ch["tvg_id"],
                                ch["tvg_name"],
                                ch["chno"],
                                ch["user_agent"],
                                ch["referrer"],
                                ch["status"],
                                ch["latency_ms"],
                                ch["last_checked_at"],
                                ch["enabled"],
                                ch["position"],
                            ),
                        )
                    new_pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (new_id,)).fetchone()
                    self.send_json({"playlist": dict(new_pl)}, 201)
                    return

        # Import M3U / Bulk .m3u8 Preview
        if path == "/api/import/preview":
            source_url = (body.get("url") or "").strip()
            raw_content = body.get("content") or ""
            default_cat = (body.get("default_category") or "Imported").strip()
            playlist_id = body.get("playlist_id")

            if source_url and not raw_content:
                try:
                    req = urllib.request.Request(
                        source_url,
                        headers={"User-Agent": "Mozilla/5.0 StreamForge-IPTV/3.0"},
                    )
                    with urllib.request.urlopen(req, timeout=12.0) as resp:
                        raw_content = resp.read().decode("utf-8", errors="replace")
                except Exception as e:
                    self.send_json({"error": f"Failed to fetch remote M3U URL: {e}"}, 400)
                    return

            parsed_m3u = parse_m3u_content(raw_content, default_category=default_cat)
            existing_channels = []
            if playlist_id:
                with get_db() as conn:
                    rows = conn.execute(
                        "SELECT name, url FROM channels WHERE playlist_id = ?", (playlist_id,)
                    ).fetchall()
                    existing_channels = [dict(r) for r in rows]

            dup_summary = annotate_duplicates(parsed_m3u["channels"], existing_channels)
            cats = {}
            for ch in parsed_m3u["channels"]:
                c = ch.get("category") or "Uncategorized"
                cats[c] = cats.get(c, 0) + 1

            self.send_json({
                "playlist_meta": parsed_m3u["playlist_meta"],
                "total_channels": len(parsed_m3u["channels"]),
                "duplicate_summary": dup_summary,
                "categories": [{"name": k, "count": v} for k, v in sorted(cats.items())],
                "channels": parsed_m3u["channels"],
            })
            return

        # Commit imported channels (Perform all network probes OUTSIDE DB transaction!)
        if path == "/api/import/commit":
            target_pid = body.get("playlist_id")
            new_playlist_name = (body.get("new_playlist_name") or "").strip()
            epg_url = (body.get("epg_url") or "").strip()
            channels_in = body.get("channels") or []
            raw_content = body.get("raw_content") or ""
            default_cat = (body.get("default_category") or "Uncategorized").strip()
            auto_find_logos = bool(body.get("auto_find_logos", False))
            dedup_existing = bool(body.get("deduplicate_urls", True))
            dedup_internal = bool(body.get("deduplicate_internal", True))
            dedup_by_name = bool(body.get("deduplicate_by_name", False))
            check_on_upload = bool(body.get("check_streams_on_upload", False))
            only_import_working = bool(body.get("only_import_working", False))

            if raw_content and not channels_in:
                parsed_m3u = parse_m3u_content(raw_content, default_category=default_cat)
                channels_in = parsed_m3u["channels"]
                if not epg_url and parsed_m3u["playlist_meta"].get("epg_url"):
                    epg_url = parsed_m3u["playlist_meta"]["epg_url"]

            now = int(time.time())
            # Step 1: Read existing channels & create playlist if needed
            with get_db() as conn:
                if not target_pid or new_playlist_name:
                    target_pid = str(uuid.uuid4())[:8]
                    conn.execute(
                        "INSERT INTO playlists (id, name, description, epg_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            target_pid,
                            new_playlist_name or "Imported M3U Playlist",
                            f"Imported {len(channels_in)} channels",
                            epg_url,
                            now,
                            now,
                        ),
                    )
                elif epg_url:
                    conn.execute(
                        "UPDATE playlists SET epg_url = CASE WHEN epg_url = '' THEN ? ELSE epg_url END WHERE id = ?",
                        (epg_url, target_pid),
                    )

                existing_rows = conn.execute(
                    "SELECT name, url FROM channels WHERE playlist_id = ?", (target_pid,)
                ).fetchall()

            existing_urls = {r["url"].strip().lower() for r in existing_rows if r["url"]}
            existing_names = {
                clean_channel_name_for_match(r["name"])
                for r in existing_rows
                if r["name"] and clean_channel_name_for_match(r["name"])
            }

            # Step 2: Filter duplicates in memory
            seen_batch_urls = set()
            seen_batch_names = set()
            filtered_batch = []
            skipped_existing = 0
            skipped_internal = 0

            for ch in channels_in:
                url = (ch.get("url") or "").strip()
                if not url:
                    continue
                url_key = url.lower()
                name = (ch.get("name") or guess_name_from_url(url)).strip()
                name_key = clean_channel_name_for_match(name)

                if dedup_existing and (
                    url_key in existing_urls
                    or (dedup_by_name and name_key and name_key in existing_names)
                ):
                    skipped_existing += 1
                    continue

                if dedup_internal and (
                    url_key in seen_batch_urls
                    or (dedup_by_name and name_key and name_key in seen_batch_names)
                ):
                    skipped_internal += 1
                    continue

                seen_batch_urls.add(url_key)
                if name_key:
                    seen_batch_names.add(name_key)
                ch["name"] = name
                filtered_batch.append(ch)

            # Step 3: Probe streams OUTSIDE DB lock!
            if check_on_upload and filtered_batch:
                need_probe = [c for c in filtered_batch if c.get("status") not in ("online", "offline", "warning")]
                for idx, item in enumerate(need_probe):
                    item["_idx"] = idx
                if need_probe:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=PANEL_MAX_WORKERS) as pool:
                        probes = list(pool.map(lambda c: probe_stream(c, timeout=3.5), need_probe[:150]))
                    for item, pr in zip(need_probe[:150], probes):
                        item["status"] = pr["status"]
                        item["latency_ms"] = pr["latency_ms"]
                        item["last_error"] = pr.get("error", "")

            if auto_find_logos:
                ensure_logo_catalog_loaded()

            # Step 4: Fast DB insert
            inserted = 0
            skipped_offline = 0
            online_count = 0
            offline_count = 0

            with get_db() as conn:
                max_pos = (
                    conn.execute(
                        "SELECT COALESCE(MAX(position), -1) FROM channels WHERE playlist_id = ?",
                        (target_pid,),
                    ).fetchone()[0]
                    + 1
                )

                for ch in filtered_batch:
                    status = ch.get("status", "unknown")
                    latency_ms = int(ch.get("latency_ms") or 0)
                    last_error = ch.get("last_error") or ""

                    if status == "online":
                        online_count += 1
                    elif status in ("offline", "warning"):
                        offline_count += 1
                        if only_import_working:
                            skipped_offline += 1
                            continue

                    name = ch["name"]
                    url = ch["url"].strip()
                    logo = (ch.get("logo") or "").strip()
                    tvg_id = (ch.get("tvg_id") or "").strip()

                    if auto_find_logos and not logo:
                        logo = fast_lookup_logo(name, tvg_id)

                    conn.execute(
                        """
                        INSERT INTO channels (id, playlist_id, name, url, logo, category, tvg_id, tvg_name, chno, user_agent, referrer, status, latency_ms, last_checked_at, last_error, position)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            target_pid,
                            name,
                            url,
                            logo,
                            (ch.get("category") or default_cat or "Uncategorized").strip(),
                            tvg_id,
                            (ch.get("tvg_name") or name).strip(),
                            str(ch.get("chno") or ""),
                            (ch.get("user_agent") or "").strip(),
                            (ch.get("referrer") or "").strip(),
                            status,
                            latency_ms,
                            now if status != "unknown" else 0,
                            last_error,
                            max_pos + inserted,
                        ),
                    )
                    inserted += 1

                touch_playlist(conn, target_pid)

            self.send_json({
                "playlist_id": target_pid,
                "inserted": inserted,
                "skipped": skipped_existing + skipped_internal,
                "skipped_existing": skipped_existing,
                "skipped_internal": skipped_internal,
                "skipped_offline": skipped_offline,
                "online_count": online_count,
                "offline_count": offline_count,
            })
            return

        # Add / Update / Delete / Bulk Modify Channels in a Playlist
        ch_action_match = re.match(
            r"^/api/playlists/([a-zA-Z0-9_-]+)/channels/(add|update|bulk-delete|bulk-category|auto-logos|clean-names|deduplicate|reorder|auto-number|check-streams|category-action)$",
            path,
        )
        if ch_action_match:
            pid, action = ch_action_match.group(1), ch_action_match.group(2)

            # Handle check-streams OUTSIDE long DB lock
            if action == "check-streams":
                ids = body.get("ids") or []
                with get_db() as conn:
                    if ids:
                        placeholders = ",".join("?" for _ in ids[:100])
                        rows = conn.execute(
                            f"SELECT * FROM channels WHERE playlist_id = ? AND id IN ({placeholders})",
                            [pid, *ids[:100]],
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT * FROM channels WHERE playlist_id = ? LIMIT 80", (pid,)
                        ).fetchall()

                channel_dicts = [dict(r) for r in rows]
                results = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=PANEL_MAX_WORKERS) as pool:
                    for res in pool.map(lambda c: probe_stream(c, timeout=4.0), channel_dicts):
                        results.append(res)

                now = int(time.time())
                with get_db() as conn:
                    for res in results:
                        if res["status"] == "online":
                            conn.execute(
                                "UPDATE channels SET status = ?, latency_ms = ?, last_checked_at = ?, fail_count = 0, last_error = '' WHERE id = ?",
                                (res["status"], res["latency_ms"], now, res["id"]),
                            )
                        else:
                            conn.execute(
                                "UPDATE channels SET status = ?, latency_ms = ?, last_checked_at = ?, fail_count = fail_count + 1, last_error = ? WHERE id = ?",
                                (res["status"], res["latency_ms"], now, res.get("error", ""), res["id"]),
                            )
                self.send_json({"results": results})
                return

            if action == "auto-logos":
                ensure_logo_catalog_loaded()

            with get_db() as conn:
                pl = conn.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
                if not pl:
                    self.send_json({"error": "Playlist not found"}, 404)
                    return

                if action == "add":
                    max_pos = (
                        conn.execute(
                            "SELECT COALESCE(MAX(position), -1) FROM channels WHERE playlist_id = ?",
                            (pid,),
                        ).fetchone()[0]
                        + 1
                    )
                    cid = str(uuid.uuid4())
                    name = (body.get("name") or "New Channel").strip()
                    url = (body.get("url") or "").strip()
                    logo = (body.get("logo") or "").strip()
                    tvg_id = (body.get("tvg_id") or "").strip()
                    if body.get("auto_logo") and not logo:
                        logo = fast_lookup_logo(name, tvg_id)

                    conn.execute(
                        """
                        INSERT INTO channels (id, playlist_id, name, url, logo, category, tvg_id, tvg_name, chno, user_agent, referrer, position)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cid,
                            pid,
                            name,
                            url,
                            logo,
                            (body.get("category") or "Uncategorized").strip(),
                            tvg_id,
                            (body.get("tvg_name") or name).strip(),
                            str(body.get("chno") or ""),
                            (body.get("user_agent") or "").strip(),
                            (body.get("referrer") or "").strip(),
                            max_pos,
                        ),
                    )
                    touch_playlist(conn, pid)
                    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
                    self.send_json({"channel": dict(ch)}, 201)
                    return

                if action == "update":
                    cid = body.get("id")
                    cur = conn.execute(
                        "SELECT * FROM channels WHERE id = ? AND playlist_id = ?", (cid, pid)
                    ).fetchone()
                    if not cur:
                        self.send_json({"error": "Channel not found"}, 404)
                        return
                    new_url = (body.get("url", cur["url"]) or "").strip()
                    new_status = body.get("status", cur["status"])
                    if new_url != cur["url"] and "status" not in body:
                        new_status = "unknown"
                    conn.execute(
                        """
                        UPDATE channels
                        SET name = ?, url = ?, logo = ?, category = ?, tvg_id = ?, tvg_name = ?, chno = ?, user_agent = ?, referrer = ?, status = ?, last_error = ?, enabled = ?
                        WHERE id = ? AND playlist_id = ?
                        """,
                        (
                            (body.get("name", cur["name"]) or "Channel").strip(),
                            new_url,
                            (body.get("logo", cur["logo"]) or "").strip(),
                            (body.get("category", cur["category"]) or "Uncategorized").strip(),
                            (body.get("tvg_id", cur["tvg_id"]) or "").strip(),
                            (body.get("tvg_name", cur["tvg_name"]) or "").strip(),
                            str(body.get("chno", cur["chno"]) or ""),
                            (body.get("user_agent", cur["user_agent"]) or "").strip(),
                            (body.get("referrer", cur["referrer"]) or "").strip(),
                            new_status,
                            "" if new_status in ("online", "unknown") else cur["last_error"],
                            int(body.get("enabled", cur["enabled"])),
                            cid,
                            pid,
                        ),
                    )
                    touch_playlist(conn, pid)
                    updated = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
                    self.send_json({"channel": dict(updated)})
                    return

                if action == "bulk-delete":
                    ids = body.get("ids") or []
                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        conn.execute(
                            f"DELETE FROM channels WHERE playlist_id = ? AND id IN ({placeholders})",
                            [pid, *ids],
                        )
                    touch_playlist(conn, pid)
                    self.send_json({"deleted": len(ids)})
                    return

                if action == "bulk-category":
                    ids = body.get("ids") or []
                    new_category = (body.get("category") or "Uncategorized").strip()
                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        conn.execute(
                            f"UPDATE channels SET category = ? WHERE playlist_id = ? AND id IN ({placeholders})",
                            [new_category, pid, *ids],
                        )
                    touch_playlist(conn, pid)
                    self.send_json({"updated": len(ids), "category": new_category})
                    return

                if action == "category-action":
                    op = body.get("operation")
                    old_cat = body.get("category") or ""
                    new_cat = (body.get("new_category") or "Uncategorized").strip()
                    if op == "rename":
                        cur = conn.execute(
                            "UPDATE channels SET category = ? WHERE playlist_id = ? AND category = ?",
                            (new_cat, pid, old_cat),
                        )
                        touch_playlist(conn, pid)
                        self.send_json({"affected": cur.rowcount})
                        return
                    elif op == "unassign_category":
                        cur = conn.execute(
                            "UPDATE channels SET category = 'Uncategorized' WHERE playlist_id = ? AND category = ?",
                            (pid, old_cat),
                        )
                        touch_playlist(conn, pid)
                        self.send_json({"affected": cur.rowcount})
                        return
                    elif op == "remove_channels":
                        cur = conn.execute(
                            "DELETE FROM channels WHERE playlist_id = ? AND category = ?",
                            (pid, old_cat),
                        )
                        touch_playlist(conn, pid)
                        self.send_json({"affected": cur.rowcount})
                        return

                if action == "auto-logos":
                    ids = body.get("ids") or []
                    overwrite = bool(body.get("overwrite", False))

                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        rows = conn.execute(
                            f"SELECT * FROM channels WHERE playlist_id = ? AND id IN ({placeholders})",
                            [pid, *ids],
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT * FROM channels WHERE playlist_id = ?", (pid,)
                        ).fetchall()

                    matched_real = 0
                    matched_generated = 0
                    for r in rows:
                        if r["logo"] and not overwrite and not r["logo"].startswith("data:image/svg+xml"):
                            continue
                        logo_url = fast_lookup_logo(r["name"], r["tvg_id"])
                        if logo_url.startswith("data:image/svg+xml"):
                            matched_generated += 1
                        else:
                            matched_real += 1
                        conn.execute(
                            "UPDATE channels SET logo = ? WHERE id = ?",
                            (logo_url, r["id"]),
                        )

                    touch_playlist(conn, pid)
                    self.send_json({
                        "matched_real": matched_real,
                        "matched_generated": matched_generated,
                        "total_updated": matched_real + matched_generated,
                    })
                    return

                if action == "clean-names":
                    find_text = body.get("find_text") or ""
                    replace_text = body.get("replace_text") or ""
                    strip_quality_tags = bool(body.get("strip_quality_tags", True))
                    strip_country_prefix = bool(body.get("strip_country_prefix", True))
                    strip_brackets = bool(body.get("strip_brackets", True))

                    rows = conn.execute(
                        "SELECT id, name FROM channels WHERE playlist_id = ?", (pid,)
                    ).fetchall()
                    updated_count = 0
                    for r in rows:
                        new_name = r["name"]
                        if find_text:
                            new_name = new_name.replace(find_text, replace_text)
                        if strip_brackets:
                            new_name = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", " ", new_name)
                        if strip_country_prefix:
                            new_name = re.sub(r"^[A-Z]{2,3}\s*[:|\-•]\s*", "", new_name)
                        if strip_quality_tags:
                            new_name = re.sub(
                                r"\b(UHD|4K|FHD|HD|SD|HEVC|H265|H264|50FPS|60FPS|VIP|RAW|1080p|720p)\b",
                                "",
                                new_name,
                                flags=re.IGNORECASE,
                            )
                        new_name = re.sub(r"\s+", " ", new_name).strip(" -|:")
                        if new_name and new_name != r["name"]:
                            conn.execute(
                                "UPDATE channels SET name = ?, tvg_name = ? WHERE id = ?",
                                (new_name, new_name, r["id"]),
                            )
                            updated_count += 1
                    touch_playlist(conn, pid)
                    self.send_json({"updated": updated_count})
                    return

                if action == "deduplicate":
                    by = body.get("by", "url")
                    rows = conn.execute(
                        "SELECT id, name, url FROM channels WHERE playlist_id = ? ORDER BY position ASC",
                        (pid,),
                    ).fetchall()
                    seen = set()
                    to_delete = []
                    for r in rows:
                        key = (
                            r["url"].strip().lower()
                            if by == "url"
                            else clean_channel_name_for_match(r["name"])
                        )
                        if key in seen:
                            to_delete.append(r["id"])
                        else:
                            seen.add(key)
                    if to_delete:
                        placeholders = ",".join("?" for _ in to_delete)
                        conn.execute(
                            f"DELETE FROM channels WHERE id IN ({placeholders})", to_delete
                        )
                    touch_playlist(conn, pid)
                    self.send_json({"removed": len(to_delete)})
                    return

                if action == "auto-number":
                    start_num = int(body.get("start", 1))
                    sort_by = body.get("sort_by", "current")
                    order_sql = "position ASC"
                    if sort_by == "name":
                        order_sql = "LOWER(name) ASC"
                    elif sort_by == "category":
                        order_sql = "LOWER(category) ASC, LOWER(name) ASC"

                    rows = conn.execute(
                        f"SELECT id FROM channels WHERE playlist_id = ? ORDER BY {order_sql}",
                        (pid,),
                    ).fetchall()
                    for idx, r in enumerate(rows):
                        conn.execute(
                            "UPDATE channels SET chno = ?, position = ? WHERE id = ?",
                            (str(start_num + idx), idx, r["id"]),
                        )
                    touch_playlist(conn, pid)
                    self.send_json({"updated": len(rows)})
                    return

                if action == "reorder":
                    ordered_ids = body.get("ordered_ids") or []
                    for idx, cid in enumerate(ordered_ids):
                        conn.execute(
                            "UPDATE channels SET position = ? WHERE id = ? AND playlist_id = ?",
                            (idx, cid, pid),
                        )
                    touch_playlist(conn, pid)
                    self.send_json({"reordered": len(ordered_ids)})
                    return

        self.send_json({"error": "Unknown API endpoint"}, 404)


def sync_db_from_github_raw():
    """On lightweight cloud panels (Render), sync latest DB updated by GitHub Actions."""
    repo_db_url = os.environ.get(
        "GITHUB_DB_RAW_URL",
        "https://raw.githubusercontent.com/zeroping4500-maker/streamforge-iptv-studio/main/data/iptv_studio.db",
    )
    try:
        req = urllib.request.Request(repo_db_url, headers={"User-Agent": "StreamForgeSync/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            if data and data.startswith(b"SQLite format 3"):
                tmp_path = DATA_DIR / "iptv_studio.db.tmp"
                tmp_path.write_bytes(data)
                os.replace(tmp_path, DB_PATH)
                print(f"[Cloud Panel] Synced latest database ({len(data)} bytes) from GitHub Actions.")
    except Exception as e:
        print(f"[Cloud Panel] GitHub DB sync skipped: {e}")


def run_server(port: int = 8765):
    global SERVER_PORT
    SERVER_PORT = port
    is_lightweight_panel = bool(
        os.environ.get("RENDER")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or os.environ.get("OFFLOAD_HEAVY_TASKS") == "1"
    )
    if is_lightweight_panel:
        sync_db_from_github_raw()

    init_db()
    threading.Thread(target=ensure_logo_catalog_loaded, daemon=True).start()
    threading.Thread(target=ensure_stream_sources_loaded, daemon=True).start()

    if is_lightweight_panel:
        print("[Cloud Panel Mode] Heavy background Playwright/probe loops offloaded to GitHub Actions.")
        with SCHEDULER_LOCK:
            SCHEDULER_STATE["enabled"] = False
        with TELEVIZO_RENEW_LOCK:
            TELEVIZO_RENEW_STATE["enabled"] = False
    else:
        threading.Thread(target=scheduled_health_monitor_loop, daemon=True).start()
        threading.Thread(target=scheduled_televizo_renew_loop, daemon=True).start()

    threading.Thread(target=keep_alive_loop, daemon=True).start()
    if not (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL") or is_lightweight_panel):
        threading.Thread(target=start_public_tunnel, args=("localhost.run",), daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", port), IPTVStudioHandler)
    print(f"StreamForge IPTV Studio running at http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    import sys
    if "--renew-televizo" in sys.argv:
        init_db()
        print("Running manual Televizo token renewal pass across all playlists...")
        res = sync_televizo_playlist_streams(playlist_id=None, background=False)
        print(f"Done: renewed {res.get('refreshed_count', 0)} of {res.get('total_matched', 0)} channels.")
        sys.exit(0)
    run_server(SERVER_PORT)
