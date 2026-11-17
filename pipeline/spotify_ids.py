#!/usr/bin/env python3
"""4d — Attach Spotify track IDs (+ ISRC) to every pool candidate, offline.

Spotify lookups move out of the app (spec): the app gets ready-to-play IDs from
/v1/pool and never searches. Uses the Client Credentials flow (no user), so it needs
SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET in pipeline/.env.

Per unique track (artist|title key, shared across hoods):
  1. MuSe gave a spotify_id → verify it with GET /tracks (IDs from the dataset can be
     stale/relinked); keep it if it still exists and is playable in the US market.
  2. Otherwise search `track:"…" artist:"…"` (then free text), take the best of 5 by
     normalized title + artist similarity. Below the bar → no ID (track is dropped from
     the served pool rather than playing the wrong song).
Every API response is cached under data/spotify/ (gitignored), so re-runs are free.

Usage:
    .venv/bin/python -s -E spotify_ids.py          # all out/pools/*.json, in place
Writes out/spotify_manhattan_review.md (match rates + every fuzzy match to eyeball).
"""
import base64
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

from dotenv import load_dotenv

from pools import key, norm

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")
CACHE = HERE / "data" / "spotify"
POOLS = HERE / "out" / "pools"
REVIEW = HERE / "out" / "spotify_manhattan_review.md"
MARKET = "US"
ACCEPT = 0.80          # combined score needed for a search match
FUZZY = 0.92           # below this (but ≥ ACCEPT) → listed for human review

PACE = 1.0            # s between requests — dev-mode quota is tight (5 req/s earned a 7 h ban)
MAX_WAIT = 300        # longer Retry-After → stop and say when to re-run
_token, _token_exp, _last = None, 0.0, 0.0


def token():
    global _token, _token_exp
    if _token and time.time() < _token_exp - 60:
        return _token
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in pipeline/.env")
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    _token, _token_exp = d["access_token"], time.time() + d["expires_in"]
    return _token


def get(path, **params):
    """Cached GET against api.spotify.com/v1 (1 req/s; short 429s waited out, long ones stop the run).
    Returns parsed JSON, or {"_status": code} for 4xx so callers can fall back."""
    global _last
    url = f"https://api.spotify.com/v1/{path}?" + urllib.parse.urlencode(params)
    f = CACHE / path.split("/")[0] / (hashlib.sha1(url.encode()).hexdigest() + ".json")
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
            if e.code == 429:
                ra = int(e.headers.get("Retry-After", "5"))
                if ra > MAX_WAIT:
                    resume = time.strftime("%H:%M", time.localtime(time.time() + ra))
                    sys.exit(f"Spotify rate limit: Retry-After {ra}s → re-run after ~{resume}. "
                             "Everything fetched so far is cached.")
                print(f"  429, waiting {ra}s", flush=True)
                time.sleep(ra + 1)
                continue
            if e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            d = {"_status": e.code, "_body": e.read().decode(errors="replace")[:300]}
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"  network error ({e}); retry {attempt + 1}/6", flush=True)
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
    else:
        raise RuntimeError(f"Spotify kept failing: {url}")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d))
    return d


# ---------- matching ----------

def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:            # "Under the Boardwalk" vs "Under the Boardwalk - 2007 Remaster"
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def has_cjk(s):
    return any("぀" <= c <= "鿿" or "가" <= c <= "힯" for c in s)


def score(want_artist, want_title, item):
    t = sim(want_title, item["name"])
    names = [a["name"] for a in item.get("artists") or []]
    a = max((sim(want_artist, n) for n in names), default=0.0)
    # Same song credited in another script (鄧麗君 vs Teresa Teng): trust an exact title.
    if a < 0.5 and t == 1.0 and any(has_cjk(n) != has_cjk(want_artist) for n in names):
        a = 0.75
    return 0.55 * t + 0.45 * a


def summary(item, how, s):
    return {"spotifyId": item["id"], "isrc": (item.get("external_ids") or {}).get("isrc"),
            "spotifyName": item["name"],
            "spotifyArtist": ", ".join(a["name"] for a in item.get("artists") or []),
            "spotifyMatch": how, "spotifyScore": round(s, 3)}


def playable(item):
    return item and item.get("is_playable", True) is not False


def verify(ids):
    """id → track item for IDs that still resolve (batch, falling back to one-by-one)."""
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        print(f"  verifying {i}/{len(ids)}", flush=True)
        d = get("tracks", ids=",".join(chunk), market=MARKET)
        if "_status" in d:                    # batch endpoint unavailable → single GETs
            for tid in chunk:
                one = get(f"tracks/{tid}", market=MARKET)
                if "_status" not in one:
                    out[tid] = one
            continue
        for item in d.get("tracks") or []:
            if item:
                out[item["id"]] = item
    return out


def search(artist, title):
    best, best_s = None, 0.0
    queries = [f'track:"{title}" artist:"{artist}"', f"{artist} {title}"]
    for q in queries:
        d = get("search", q=q, type="track", limit=5, market=MARKET)
        for item in ((d.get("tracks") or {}).get("items") or []):
            if not playable(item):
                continue
            s = score(artist, title, item)
            if s > best_s:
                best, best_s = item, s
        if best_s >= FUZZY:
            break
    return (best, best_s) if best_s >= ACCEPT else (None, best_s)


# ---------- main ----------

def main():
    files = sorted(glob.glob(str(POOLS / "*.json")))
    pools = {f: json.load(open(f)) for f in files}
    uniq = {}
    for p in pools.values():
        for c in p["candidates"]:
            uniq.setdefault(key(c["artist"], c["title"]), c)
    print(f"{len(uniq)} unique tracks across {len(pools)} pools", flush=True)

    muse_ids = sorted({c["spotifyId"] for c in uniq.values() if c.get("spotifyId")})
    verified = verify(muse_ids)
    print(f"MuSe IDs: {len(verified)}/{len(muse_ids)} still resolve", flush=True)

    result, misses = {}, []
    for n, (k, c) in enumerate(uniq.items(), 1):
        item = verified.get(c.get("spotifyId") or "")
        if playable(item) and score(c["artist"], c["title"], item) >= ACCEPT:
            result[k] = summary(item, "muse", score(c["artist"], c["title"], item))
        else:
            item, s = search(c["artist"], c["title"])
            if item:
                result[k] = summary(item, "search", s)
            else:
                result[k] = {"spotifyId": None, "isrc": None, "spotifyMatch": "none", "spotifyScore": round(s, 3)}
                misses.append(c)
        if n % 250 == 0:
            print(f"  {n}/{len(uniq)}", flush=True)

    rows = []
    for f, p in pools.items():
        for c in p["candidates"]:
            for f_ in ("spotifyName", "spotifyArtist"):
                c.pop(f_, None)
            c.update(result[key(c["artist"], c["title"])])
        ok = sum(1 for c in p["candidates"] if c["spotifyId"])
        rows.append((p["name"], ok, len(p["candidates"]),
                     sum(1 for c in p["candidates"] if "anchor" in c["sources"] and not c["spotifyId"])))
        Path(f).write_text(json.dumps(p, ensure_ascii=False, indent=1))

    fuzzy = [(c, result[k]) for k, c in uniq.items()
             if result[k]["spotifyId"] and result[k]["spotifyScore"] < FUZZY]
    found = sum(1 for r in result.values() if r["spotifyId"])
    L = ["# Spotify matching (4d) — Manhattan", "",
         f"{found}/{len(uniq)} unique tracks matched "
         f"({sum(1 for r in result.values() if r['spotifyMatch'] == 'muse')} via MuSe ID, "
         f"{sum(1 for r in result.values() if r['spotifyMatch'] == 'search')} via search). "
         "Unmatched tracks are left out of the served pool.", "",
         "| hood | playable | pool | anchors missing |", "|---|---|---|---|"]
    L += [f"| {n} | {ok} | {tot} | {am or ''} |" for n, ok, tot, am in rows]
    L += ["", f"## Fuzzy matches to check ({len(fuzzy)})", "",
          "wanted → got (score)", ""]
    L += [f"- {c['artist']} — {c['title']} → {r['spotifyArtist']} — {r['spotifyName']} ({r['spotifyScore']})"
          for c, r in sorted(fuzzy, key=lambda x: x[1]["spotifyScore"])]
    anchors_missing = [c for c in misses if "anchor" in c["sources"]]
    L += ["", f"## Anchors with no match ({len(anchors_missing)})", ""]
    L += [f"- {c['artist']} — {c['title']}" for c in anchors_missing]
    REVIEW.write_text("\n".join(L) + "\n")
    print(f"{found}/{len(uniq)} matched · review → {REVIEW}")


if __name__ == "__main__":
    main()
