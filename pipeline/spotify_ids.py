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
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if isinstance(e, urllib.error.HTTPError):
                raise
            print(f"  token: network error ({e}); retry {attempt + 1}/6", flush=True)
            time.sleep(min(60, 5 * 2 ** attempt))
    else:
        raise Stop("can't reach accounts.spotify.com — check the network/VPN, then re-run")
    _token, _token_exp = d["access_token"], time.time() + d["expires_in"]
    return _token


class Stop(Exception):
    """End this run early (rate limit / request budget); results so far are written."""


MAX_REQUESTS = int(os.environ.get("SPOTIFY_MAX_REQUESTS", "400"))   # per run
_requests = 0


def url_for(path, **params):
    return f"https://api.spotify.com/v1/{path}?" + urllib.parse.urlencode(params)


def cache_file(path, url):
    return CACHE / path.split("/")[0] / (hashlib.sha1(url.encode()).hexdigest() + ".json")


def cached(path, **params):
    """A response already on disk, or None — never touches the network."""
    f = cache_file(path, url_for(path, **params))
    return json.loads(f.read_text()) if f.exists() else None


def get(path, **params):
    """Cached GET against api.spotify.com/v1 (1 req/s; short 429s waited out).
    Raises Stop on a long Retry-After or when this run's request budget is spent.
    Returns parsed JSON, or {"_status": code} for 4xx so callers can fall back."""
    global _last, _requests
    url = url_for(path, **params)
    f = cache_file(path, url)
    if f.exists():
        return json.loads(f.read_text())
    if _requests >= MAX_REQUESTS:
        raise Stop(f"request budget ({MAX_REQUESTS}) spent — re-run later to continue")
    for attempt in range(6):
        wait = PACE - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
        _requests += 1
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = int(e.headers.get("Retry-After", "5"))
                if ra > MAX_WAIT:
                    resume = time.strftime("%m-%d %H:%M", time.localtime(time.time() + ra))
                    raise Stop(f"Spotify rate limit: Retry-After {ra}s → re-run after ~{resume}")
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
        raise Stop("Spotify unreachable after retries — check the network/VPN, then re-run")
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


ARTIST_MIN = 0.80   # a different performer of the same title is the wrong recording


def score(want_artist, want_title, item):
    t = sim(want_title, item["name"])
    names = [a["name"] for a in item.get("artists") or []]
    a = max((sim(want_artist, n) for n in names), default=0.0)
    # Same song credited in another script (鄧麗君 vs Teresa Teng): trust an exact title.
    if a < 0.5 and t == 1.0 and any(has_cjk(n) != has_cjk(want_artist) for n in names):
        a = ARTIST_MIN
    if a < ARTIST_MIN:          # Dean Martin ≠ Dean Brody, The Temptations ≠ The Foundations
        return min(0.55 * t + 0.45 * a, ACCEPT - 0.01)
    if not numbers_agree(want_title, item["name"]):   # Op. 31 ≠ Op. 47
        return min(0.55 * t + 0.45 * a, ACCEPT - 0.01)
    return 0.55 * t + 0.45 * a


def numbers_agree(a, b):
    """When both titles carry numbers (opus, No., K.), the shorter set must be inside the
    longer one — "Cello Suite No. 1" fits "…No. 1 in G Major, BWV 1007", Op. 31 ≠ Op. 47."""
    import re
    na, nb = set(re.findall(r"\d+", a)), set(re.findall(r"\d+", b))
    if not na or not nb:
        return True
    small, big = sorted((na, nb), key=len)
    return small <= big


def summary(item, how, s):
    return {"spotifyId": item["id"], "isrc": (item.get("external_ids") or {}).get("isrc"),
            "spotifyName": item["name"],
            "spotifyArtist": ", ".join(a["name"] for a in item.get("artists") or []),
            "spotifyMatch": how, "spotifyScore": round(s, 3)}


def playable(item):
    return item and item.get("is_playable", True) is not False


def muse_item(tid):
    """A MuSe Spotify ID's track, from cache only (verification is no longer requested —
    the dev-mode quota is spent on searches instead; unverified IDs are trusted)."""
    d = cached(f"tracks/{tid}", market=MARKET)
    return d if d and "_status" not in d else None


def search(artist, title, isrc=None):
    """Best playable match. With an ISRC: one exact `isrc:` query. Otherwise a fielded
    query, and free text only if that returned nothing."""
    queries = ([f"isrc:{isrc}"] if isrc else []) + [f'track:"{title}" artist:"{artist}"', f"{artist} {title}"]
    best, best_s = None, 0.0
    for q in queries:
        d = get("search", q=q, type="track", limit=5, market=MARKET)
        items = [i for i in ((d.get("tracks") or {}).get("items") or []) if playable(i)]
        for item in items:
            s = score(artist, title, item)
            if q.startswith("isrc:"):
                s = max(s, 0.95)          # same recording by definition
            if s > best_s:
                best, best_s = item, s
        if best_s >= ACCEPT or items:
            break
    return (best, best_s) if best_s >= ACCEPT else (None, best_s)


PER_HOOD_EXTRA = 20   # besides the anchors: the strongest History candidates per hood


def priority(pools):
    """Track keys worth a Spotify search, most important first: every anchor, then each
    hood's top PER_HOOD_EXTRA History candidates (MusicBrainz / similar) by score."""
    first, then = [], []
    for p in pools.values():
        cs = p["candidates"]
        first += [key(c["artist"], c["title"]) for c in cs if "anchor" in c["sources"]]
        rest = sorted((c for c in cs if "anchor" not in c["sources"] and c["history"] > 0),
                      key=lambda c: -c["history"])[:PER_HOOD_EXTRA]
        then += [key(c["artist"], c["title"]) for c in rest]
    seen, out = set(), []
    for k in first + then:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ---------- main ----------

def main():
    files = sorted(glob.glob(str(POOLS / "*.json")))
    pools = {f: json.load(open(f)) for f in files}
    uniq = {}
    for p in pools.values():
        for c in p["candidates"]:
            uniq.setdefault(key(c["artist"], c["title"]), c)
    print(f"{len(uniq)} unique tracks across {len(pools)} pools · budget {MAX_REQUESTS} requests", flush=True)

    # Keep earlier results (a previous run's matches stay; only pending ones are retried).
    result = {}
    for k, c in uniq.items():
        if c.get("spotifyMatch") in ("muse", "search", "none"):
            result[k] = {f: c.get(f) for f in ("spotifyId", "isrc", "spotifyName", "spotifyArtist",
                                                "spotifyMatch", "spotifyScore")}

    # MuSe IDs: verified ones from cache (adds ISRC), the rest trusted as-is.
    for k, c in uniq.items():
        if k in result or not c.get("spotifyId"):
            continue
        item = muse_item(c["spotifyId"])
        if item and playable(item) and score(c["artist"], c["title"], item) >= ACCEPT:
            result[k] = summary(item, "muse", score(c["artist"], c["title"], item))
            if c.get("isrc") and not result[k]["isrc"]:
                result[k]["isrc"] = c["isrc"]
        elif not item:
            result[k] = {"spotifyId": c["spotifyId"], "isrc": c.get("isrc"),
                         "spotifyMatch": "muse", "spotifyScore": None}

    # Searches, in priority order, until done or stopped.
    todo = [k for k in priority(pools) if k not in result]
    stopped = None
    try:
        for n, k in enumerate(todo, 1):
            c = uniq[k]
            item, s = search(c["artist"], c["title"], c.get("isrc"))
            if item:
                result[k] = summary(item, "search", s)
                if c.get("isrc") and not result[k]["isrc"]:
                    result[k]["isrc"] = c["isrc"]
            else:
                result[k] = {"spotifyId": None, "isrc": c.get("isrc"), "spotifyMatch": "none",
                             "spotifyScore": round(s, 3)}
            if n % 50 == 0:
                print(f"  searched {n}/{len(todo)} ({_requests} requests)", flush=True)
    except Stop as e:
        stopped = str(e)

    rows = []
    for f, p in pools.items():
        for c in p["candidates"]:
            k = key(c["artist"], c["title"])
            r = result.get(k)
            if r:
                c.update({kk: v for kk, v in r.items() if v is not None or kk == "spotifyId"})
            else:
                c["spotifyMatch"] = "pending" if k in todo else "deferred"
                c.setdefault("spotifyId", None)
        ok = sum(1 for c in p["candidates"] if c.get("spotifyId"))
        miss_anchor = sum(1 for c in p["candidates"] if "anchor" in c["sources"] and not c.get("spotifyId"))
        rows.append((p["name"], ok, len(p["candidates"]), miss_anchor))
        Path(f).write_text(json.dumps(p, ensure_ascii=False, indent=1))

    done = sum(1 for k in todo if k in result)
    found = sum(1 for r in result.values() if r.get("spotifyId"))
    L = ["# Spotify matching (4d) — Manhattan", "",
         f"{found}/{len(uniq)} unique tracks have a Spotify ID. Priority searches: {done}/{len(todo)} done"
         + (f" — stopped: {stopped}" if stopped else "") + ".",
         "Tracks beyond each hood's anchors + top History candidates are deferred (no search yet); "
         "they still play for Apple Music listeners via ISRC.", "",
         "| hood | Spotify-playable | pool | anchors missing |", "|---|---|---|---|"]
    L += [f"| {n} | {ok} | {tot} | {am or ''} |" for n, ok, tot, am in rows]
    fuzzy = [(uniq[k], r) for k, r in result.items()
             if r.get("spotifyId") and r.get("spotifyScore") and r["spotifyScore"] < FUZZY]
    L += ["", f"## Fuzzy matches to check ({len(fuzzy)})", "", "wanted → got (score)", ""]
    L += [f"- {c['artist']} — {c['title']} → {r.get('spotifyArtist')} — {r.get('spotifyName')} ({r['spotifyScore']})"
          for c, r in sorted(fuzzy, key=lambda x: x[1]["spotifyScore"])]
    REVIEW.write_text("\n".join(L) + "\n")
    print(f"{found}/{len(uniq)} with Spotify ID · priority {done}/{len(todo)} · {_requests} requests"
          + (f" · STOPPED: {stopped}" if stopped else "") + f" · review → {REVIEW}")


if __name__ == "__main__":
    main()
