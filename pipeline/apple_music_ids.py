#!/usr/bin/env python3
"""4d (Apple side) — Attach ISRC + Apple Music IDs to every pool candidate, offline.

Apple Music API (developer token, no user) has far looser limits than Spotify's dev
mode, so it does the bulk of the matching:
  1. Candidates with an ISRC already → GET /catalog/us/songs?filter[isrc]=… (25 per call).
  2. The rest → /catalog/us/search (artist + title), best of 5 by the same title/artist
     scorer as spotify_ids.py (≥ 0.80 accepted).
The ISRC it finds lets spotify_ids.py match with one exact `isrc:` query instead of
fuzzy searches. Apple Music IDs here are US-storefront; the app re-resolves by ISRC in
the listener's own storefront.

Needs in pipeline/.env (from developer.apple.com → Keys, a key with Media Services):
  APPLE_TEAM_ID, APPLE_KEY_ID, APPLE_PRIVATE_KEY_PATH (the downloaded AuthKey_XXXX.p8 —
  keep it out of the repo, e.g. in pipeline/data/, which is gitignored).
Responses are cached under data/applemusic/ (gitignored).

Usage:
    .venv/bin/python -s -E apple_music_ids.py
Writes the fields into out/pools/*.json in place and out/apple_music_review.md.
"""
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
from dotenv import load_dotenv

from pools import key
from spotify_ids import score, ACCEPT, FUZZY

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
CACHE = HERE / "data" / "applemusic"
POOLS = HERE / "out" / "pools"
REVIEW = HERE / "out" / "apple_music_review.md"
STOREFRONT = "us"
PACE = 0.12        # s between requests (Apple's limit is generous; stay polite)

_token, _token_exp, _last = None, 0.0, 0.0


def token():
    """ES256 developer token, valid 12 h."""
    global _token, _token_exp
    if _token and time.time() < _token_exp - 300:
        return _token
    team, kid, path = (os.environ.get(k) for k in ("APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY_PATH"))
    if not (team and kid and path):
        sys.exit("Set APPLE_TEAM_ID, APPLE_KEY_ID and APPLE_PRIVATE_KEY_PATH in pipeline/.env")
    key_path = Path(path).expanduser()
    if not key_path.is_absolute():
        key_path = HERE / key_path
    now = int(time.time())
    _token_exp = now + 12 * 3600
    _token = jwt.encode({"iss": team, "iat": now, "exp": _token_exp},
                        key_path.read_text(), algorithm="ES256", headers={"kid": kid})
    return _token


def get(path, **params):
    """Cached GET against api.music.apple.com/v1; 4xx → {"_status": code}."""
    global _last
    url = f"https://api.music.apple.com/v1/{path}?" + urllib.parse.urlencode(params)
    f = CACHE / path.split("/")[-1] / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if f.exists():
        return json.loads(f.read_text())
    for attempt in range(6):
        wait = PACE - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("Apple Music API rejected the developer token (401) — check team/key IDs and the .p8")
            if e.code == 429 or e.code >= 500:
                ra = int(e.headers.get("Retry-After", "0") or 0)
                if ra > 300:
                    sys.exit(f"Apple Music rate limit: Retry-After {ra}s. Everything so far is cached.")
                time.sleep(max(ra, 2 ** attempt))
                continue
            d = {"_status": e.code}
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"  network error ({e}); retry {attempt + 1}/6", flush=True)
            time.sleep(min(60, 5 * 2 ** attempt))
    else:
        raise RuntimeError(f"Apple Music kept failing: {url}")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d))
    return d


def as_item(song):
    """Apple song resource → the {name, artists[]} shape the shared scorer expects."""
    a = song.get("attributes") or {}
    return {"id": song["id"], "name": a.get("name", ""), "artists": [{"name": a.get("artistName", "")}],
            "isrc": a.get("isrc"), "playable": bool(a.get("playParams"))}


def by_isrc(isrcs):
    """isrc → Apple song item (25 per request)."""
    out = {}
    for i in range(0, len(isrcs), 25):
        chunk = isrcs[i:i + 25]
        d = get(f"catalog/{STOREFRONT}/songs", **{"filter[isrc]": ",".join(chunk)})
        for song in d.get("data") or []:
            item = as_item(song)
            if item["isrc"] and item["playable"]:
                out.setdefault(item["isrc"], item)
    return out


def search(artist, title):
    d = get(f"catalog/{STOREFRONT}/search", term=f"{artist} {title}", types="songs", limit=5)
    best, best_s = None, 0.0
    for song in ((d.get("results") or {}).get("songs") or {}).get("data") or []:
        item = as_item(song)
        if not item["playable"]:
            continue
        s = score(artist, title, item)
        if s > best_s:
            best, best_s = item, s
    return (best, best_s) if best_s >= ACCEPT else (None, best_s)


def main():
    files = sorted(glob.glob(str(POOLS / "*.json")))
    pools = {f: json.load(open(f)) for f in files}
    uniq = {}
    for p in pools.values():
        for c in p["candidates"]:
            uniq.setdefault(key(c["artist"], c["title"]), c)
    print(f"{len(uniq)} unique tracks across {len(pools)} pools", flush=True)

    # Only ISRCs from elsewhere (Spotify) are lookup keys; ones this script wrote on an
    # earlier run are recomputed, so a stricter matcher can correct them.
    def given_isrc(c):
        return c.get("isrc") if c.get("isrcSource") != "apple" else None

    known = by_isrc(sorted({given_isrc(c) for c in uniq.values() if given_isrc(c)}))
    result, fuzzy, n = {}, [], 0
    for k, c in uniq.items():
        item = known.get(given_isrc(c) or "")
        if item:
            result[k] = {"appleMusicId": item["id"], "isrc": item["isrc"], "appleMatch": "isrc"}
        else:
            item, s = search(c["artist"], c["title"])
            if item:
                result[k] = {"appleMusicId": item["id"], "isrc": given_isrc(c) or item["isrc"],
                             "appleMatch": "search", "appleScore": round(s, 3)}
                if s < FUZZY:
                    fuzzy.append((c, item, s))
            else:
                result[k] = {"appleMusicId": None, "appleMatch": "none"}
        n += 1
        if n % 250 == 0:
            print(f"  {n}/{len(uniq)}", flush=True)

    rows = []
    for f, p in pools.items():
        for c in p["candidates"]:
            r = result[key(c["artist"], c["title"])]
            if c.get("isrcSource") == "apple":       # recomputed below
                c.pop("isrc", None)
                c.pop("isrcSource", None)
            c["appleMusicId"] = r["appleMusicId"]
            c["appleMatch"] = r["appleMatch"]
            if r.get("isrc") and not c.get("isrc"):
                c["isrc"] = r["isrc"]
                c["isrcSource"] = "apple"
        ok = sum(1 for c in p["candidates"] if c["appleMusicId"])
        rows.append((p["name"], ok, len(p["candidates"])))
        Path(f).write_text(json.dumps(p, ensure_ascii=False, indent=1))

    found = sum(1 for r in result.values() if r["appleMusicId"])
    with_isrc = sum(1 for r in result.values() if r.get("isrc"))
    L = ["# Apple Music matching (4d) — Manhattan", "",
         f"{found}/{len(uniq)} unique tracks on Apple Music (US); {with_isrc} now carry an ISRC.", "",
         "| hood | on Apple Music | pool |", "|---|---|---|"]
    L += [f"| {n} | {ok} | {tot} |" for n, ok, tot in rows]
    L += ["", f"## Fuzzy matches to check ({len(fuzzy)})", ""]
    L += [f"- {c['artist']} — {c['title']} → {i['artists'][0]['name']} — {i['name']} ({round(s, 3)})"
          for c, i, s in sorted(fuzzy, key=lambda x: x[2])]
    REVIEW.write_text("\n".join(L) + "\n")
    print(f"{found}/{len(uniq)} on Apple Music · {with_isrc} with ISRC · review → {REVIEW}")


if __name__ == "__main__":
    main()
