#!/usr/bin/env python3
"""Offline checks for the smotret.tv importer in source_importers.py.

No network: fixtures mirror the real pages (category card markup, player page
with its inline `var streams` array). Run: python3 scripts/check_smotret_import.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import source_importers as ext

LISTING = """
<div class="grid">
  <ul>
    <li class="card" data-channel-id="1hd" data-channel-name="1HD смотреть онлайн">
      <a class="vest" href="/music/1hd" title="1HD"></a>
      <img data-src="/images/channels/1hd.png" alt="1HD">
    </li>
    <li class="card" data-channel-id="7-tv" data-channel-name="7 ТВ">
      <a class="vest" href="/razvlekatelnye/7-tv"></a>
    </li>
    <li class="card" data-channel-id="rbk" data-channel-name="РБК — смотреть онлайн прямой эфир бесплатно">
      <a class="vest" href="/news/rbk"></a>
    </li>
  </ul>
</div>
"""

PLAYER = """
<script>
  var config = {"muted": true};
  var streams = ["https://web.onair-radio.eu/video/video/chunklist.m3u8", "/error.m3u8"];
</script>
"""

PLAYER_FALLBACK = """
<video id="v" src="https://edge.example.net/live/stream_720.m3u8"></video>
"""

PLAYER_EMBED = "<html><body>ok.ru embed</body></html>"

CHANNEL_PAGE = '<html><body><iframe src="/music/iframes/1hd.html" allowfullscreen></iframe></body></html>'


def check(label, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'} {label}{'' if condition else f' -- {detail}'}")
    return bool(condition)


def main():
    ok = True
    cards = ext.parse_smotret_listing(LISTING, "Music")
    ok &= check("listing finds every channel card", len(cards) == 3, str(cards))
    first = cards[0]
    ok &= check("listing keeps channel id", first["id"] == "1hd", first)
    ok &= check("listing strips 'смотреть онлайн'", first["name"] == "1HD", first["name"])
    ok &= check("listing strips the long '— смотреть онлайн прямой эфир' tail",
                cards[2]["name"] == "РБК", cards[2]["name"])
    ok &= check("listing resolves absolute channel page",
                first["page"] == "https://smotret.tv/music/1hd", first["page"])
    ok &= check("listing resolves absolute logo",
                first["logo"] == "https://smotret.tv/images/channels/1hd.png", first["logo"])
    ok &= check("listing carries category", cards[1]["category"] == "Music", cards[1]["category"])

    streams = ext.parse_smotret_streams(PLAYER)
    ok &= check("player streams: real HLS kept, /error.m3u8 dropped",
                streams == ["https://web.onair-radio.eu/video/video/chunklist.m3u8"], streams)
    ok &= check("player streams fallback to a bare .m3u8 in markup",
                ext.parse_smotret_streams(PLAYER_FALLBACK) == ["https://edge.example.net/live/stream_720.m3u8"])
    ok &= check("embed-only player page yields no streams", ext.parse_smotret_streams(PLAYER_EMBED) == [])
    ok &= check("non-http stream entries are refused",
                ext.parse_smotret_streams('var streams = ["/error.m3u8", "javascript:x"]') == [])

    res = ext.resolve_smotret_channel("not-a-url")
    ok &= check("invalid channel page reported, not raised", res["ok"] is False and bool(res["error"]))

    with tempfile.TemporaryDirectory() as tmp:
        cache_file = os.path.join(tmp, "cache.json")
        ext._smotret_save_cache(cache_file, {"1hd": {"url": "https://x/y.m3u8", "ts": 1}})
        loaded = ext._smotret_load_cache(cache_file)
        ok &= check("cache round-trips", loaded.get("1hd", {}).get("url") == "https://x/y.m3u8", loaded)
        ok &= check("missing cache file is empty, not fatal", ext._smotret_load_cache(os.path.join(tmp, "nope.json")) == {})

    ok &= check("category list covers the ten requested pages",
                [slug for slug, _ in ext.SMOTRET_CATEGORIES] == [
                    "", "music", "news", "sport", "detskie", "christian",
                    "razvlekatelnye", "poznavatelnye", "region", "relax"],
                [slug for slug, _ in ext.SMOTRET_CATEGORIES])

    print("ALL-PASS" if ok else "FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
