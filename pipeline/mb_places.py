#!/usr/bin/env python3
"""4a — MusicBrainz: recordings made at places inside a borough's neighborhoods.

For History's "place-linked recordings" (score 1.0, reason "Recorded at {place}, {year}").

1. Search MusicBrainz places inside a lat/long box (paged, 100 per page).
2. Keep places that fall inside a neighborhood of the requested borough (the app's own
   detector, via geo.py).
3. For each, fetch its "recorded at" recording relationships with artist credits.

Respects MusicBrainz's rate limit (1 request/second, identifying User-Agent). Every
response is cached under data/musicbrainz/, so the run can be stopped and resumed.

Output: out/mb_<borough>.json  {hood_id: [{recording_id, title, artist, year, place}]}
        out/mb_<borough>.md    per-neighborhood summary

Usage: python3 -I mb_places.py Manhattan
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import geo

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data" / "musicbrainz"
OUT = HERE / "out"
UA = "FlaneurPipeline/0.1 ( https://github.com/miazhang2025/flaneur-backend )"
API = "https://musicbrainz.org/ws/2"

# Bounding boxes per borough (generous; precise filtering is by polygon).
BOXES = {
    "Manhattan": (40.698, 40.882, -74.022, -73.905),
}

_last_call = 0.0


def get(url, cache_file):
    global _last_call
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    for attempt in range(5):
        wait = 1.5 - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data))
            return data
        except urllib.error.HTTPError as e:
            if e.code < 500:              # client error (e.g. 400) — retrying won't help
                raise
            print(f"  retry {attempt + 1} after {e}", flush=True)
            time.sleep(2 ** attempt)
        except Exception as e:  # network hiccup; back off and retry
            print(f"  retry {attempt + 1} after {e}", flush=True)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up on {url}")


# MusicBrainz search rejects deep paging (HTTP 400 at offset 500), so the box is split
# into latitude bands small enough that each band's results fit in the first pages.
BANDS = 8
MAX_OFFSET = 400


def search_places(borough):
    lat0, lat1, lon0, lon1 = BOXES[borough]
    step = (lat1 - lat0) / BANDS
    seen = {}
    for b in range(BANDS):
        a, z = lat0 + b * step, lat0 + (b + 1) * step
        q = f"lat:[{a:.4f} TO {z:.4f}] AND long:[{lon0} TO {lon1}]"
        offset = 0
        while True:
            url = f"{API}/place/?query={urllib.parse.quote(q)}&fmt=json&limit=100&offset={offset}"
            page = get(url, CACHE / "search" / f"{borough}-band{b}-{offset}.json")
            for p in page.get("places", []):
                seen[p["id"]] = p
            offset += 100
            if offset >= page.get("count", 0):
                break
            if offset > MAX_OFFSET:
                print(f"  ⚠️ band {b} has {page['count']} places — more than one band can page through", flush=True)
                break
    return list(seen.values())


def main():
    borough = sys.argv[1] if len(sys.argv) > 1 else "Manhattan"
    hoods = [h for h in geo.load() if h.borough == borough]
    print(f"{borough}: {len(hoods)} neighborhoods", flush=True)

    places = search_places(borough)
    located = []
    for p in places:
        c = p.get("coordinates")
        if not c:
            continue
        hood = geo.resolve(hoods, float(c["longitude"]), float(c["latitude"]))
        if hood:
            located.append((p, hood))
    print(f"{len(places)} MusicBrainz places in the box · {len(located)} inside {borough} neighborhoods", flush=True)

    by_hood = defaultdict(list)
    failed = []
    for i, (p, hood) in enumerate(located, 1):
        url = f"{API}/place/{p['id']}?inc=recording-rels+artist-credits&fmt=json"
        try:
            data = get(url, CACHE / "places" / f"{p['id']}.json")
        except Exception as e:
            # Keep going; a rerun retries just the missing ones (everything else is cached).
            failed.append(p["name"])
            print(f"  ✗ skipped {p['name']}: {e}", flush=True)
            continue
        recs = [r for r in data.get("relations", []) if r.get("type") == "recorded at" and r.get("recording")]
        for r in recs:
            rec = r["recording"]
            artist = "".join(a["name"] + a.get("joinphrase", "") for a in rec.get("artist-credit", []))
            date = r.get("begin") or rec.get("first-release-date") or ""
            by_hood[hood.id].append({
                "recording_id": rec["id"], "title": rec["title"], "artist": artist,
                "year": int(date[:4]) if date[:4].isdigit() else None,
                "place": p["name"], "place_type": p.get("type"),
            })
        if i % 25 == 0 or i == len(located):
            print(f"  {i}/{len(located)} places fetched", flush=True)

    OUT.mkdir(exist_ok=True)
    (OUT / f"mb_{borough.lower()}.json").write_text(json.dumps(by_hood, ensure_ascii=False, indent=1))

    lines = [f"# MusicBrainz — recordings made in {borough}\n",
             f"{len(located)} places in neighborhoods · {sum(len(v) for v in by_hood.values()):,} 'recorded at' links"
             + (f" · ⚠️ {len(failed)} places not fetched (rerun to retry)" if failed else "") + "\n",
             "| Neighborhood | Recordings | Distinct artists | Top places |", "|---|---|---|---|"]
    for h in sorted(hoods, key=lambda h: -len(by_hood.get(h.id, []))):
        recs = by_hood.get(h.id, [])
        top = defaultdict(int)
        for r in recs:
            top[r["place"]] += 1
        top_s = ", ".join(f"{k} ({v})" for k, v in sorted(top.items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"| {h.name} | {len(recs)} | {len({r['artist'] for r in recs})} | {top_s} |")
    (OUT / f"mb_{borough.lower()}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
