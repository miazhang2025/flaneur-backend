#!/usr/bin/env python3
"""4e — Export pools + identities as the static, versioned API the app reads.

Served by Vercel straight from `public/` (CDN-cached, no function):

  GET /v1/index.json         {"apiVersion":1, "builtAt", "hoods": {id: {"slug","hash","name","city"}}}
  GET /v1/pool/<slug>.json   one neighborhood: identity + scored candidates
  GET /v1/flags.json         remote switches ({"memoryEnabled": false})

The app fetches index.json (small; tells it which pools changed via `hash`), then a pool
by slug. Pools are public data, so no app key. Candidates without any platform ID are
left out. Field names are the contract with the app's PoolService (4f) — bump
API_VERSION (and the /v1 path, if breaking) rather than changing them in place.

Usage:
    .venv/bin/python -s -E export.py            # → ../public/v1/
    .venv/bin/python -s -E export.py --check    # validate only, write nothing
    .venv/bin/python -s -E export.py --bundle   # also copy golden-9 pools into the app
                                                # (flaneur/Resources/pools/, offline fallback)
"""
import hashlib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

from pools import APP_CURATED, GOLDEN_MOODS, IDENTITIES, OUT as POOLS

HERE = Path(__file__).resolve().parent
PUBLIC = HERE.parent / "public" / "v1"
API_VERSION = 1
FLAGS = {"memoryEnabled": False}
APP_BUNDLE_POOLS = APP_CURATED.parent / "pools"


def slug(hood_id):
    s = unicodedata.normalize("NFKD", hood_id).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")       # "New York/Hell's Kitchen" → new-york-hell-s-kitchen


def identities():
    out = {}
    curated = json.load(open(APP_CURATED))
    for name, moods in GOLDEN_MOODS.items():
        e = curated[name]
        out[f"New York/{name}"] = {"curated": True, "moodWords": moods, "anchorTracks": e["seedTracks"],
                                   "transitionColor": e["transitionColor"],
                                   "transitionTextColor": e["transitionTextColor"], "blurb": None}
    for p in IDENTITIES.glob("*.json"):
        d = json.load(open(p))
        out[d["id"]] = {"curated": False, "moodWords": d["moodWords"], "anchorTracks": d["anchorTracks"],
                        "transitionColor": d["transitionColor"],
                        "transitionTextColor": d["transitionTextColor"], "blurb": d["blurb"]}
    return out


def candidate(c):
    return {
        "artist": c["artist"], "title": c["title"],
        "isrc": c.get("isrc"), "spotifyId": c.get("spotifyId"),
        "scores": {"history": c["history"], "atmosphere": c["atmosphere"],
                   "people": c["people"], "memory": c["memory"]},
        "arousal": c["arousal"],
        "reasons": c["reasons"],
    }


def main():
    check = "--check" in sys.argv
    ids = identities()
    pools = sorted(POOLS.glob("*.json"))
    if not pools:
        sys.exit("no pools — run pools.py first")
    index, files, problems = {}, {}, []
    for f in pools:
        p = json.load(open(f))
        if not any("spotifyMatch" in c for c in p["candidates"]):
            problems.append(f"{p['name']}: no Spotify matching yet (run spotify_ids.py)")
            continue
        cands = [candidate(c) for c in p["candidates"] if c.get("spotifyId") or c.get("isrc")]
        if len(cands) < 25:
            problems.append(f"{p['name']}: only {len(cands)} playable candidates (< 25)")
        ident = ids.get(p["id"])
        if not ident:
            problems.append(f"{p['name']}: no identity")
            continue
        city, name = p["id"].split("/", 1)
        body = {"apiVersion": API_VERSION, "id": p["id"], "name": name, "city": city,
                "identity": ident, "peopleAvailable": p["peopleAvailable"], "candidates": cands}
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        s = slug(p["id"])
        files[f"pool/{s}.json"] = text
        index[p["id"]] = {"slug": s, "hash": hashlib.sha1(text.encode()).hexdigest()[:12],
                          "name": name, "city": city}
    slugs = [v["slug"] for v in index.values()]
    if len(slugs) != len(set(slugs)):
        problems.append("slug collision")
    for msg in problems:
        print("!", msg)
    print(f"{len(index)} pools ready, "
          f"{sum(len(t) for t in files.values()) / 1e6:.2f} MB uncompressed")
    if check or problems:
        sys.exit(1 if problems else 0)
    files["index.json"] = json.dumps({"apiVersion": API_VERSION, "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                      "hoods": index}, ensure_ascii=False, separators=(",", ":"))
    files["flags.json"] = json.dumps(FLAGS)
    for rel, text in files.items():
        out = PUBLIC / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    print(f"wrote {len(files)} files → {PUBLIC}")
    if "--bundle" in sys.argv:
        APP_BUNDLE_POOLS.mkdir(exist_ok=True)
        golden = [hid for hid, ident in ids.items() if ident["curated"] and hid in index]
        for hid in golden:
            s = index[hid]["slug"]
            (APP_BUNDLE_POOLS / f"{s}.json").write_text(files[f"pool/{s}.json"])
        print(f"bundled {len(golden)} golden pools → {APP_BUNDLE_POOLS}")


if __name__ == "__main__":
    main()
