#!/usr/bin/env python3
"""4a — Backfill valence/arousal/dominance for tracks MuSe doesn't have.

MuSe computed VAD from each song's Last.fm tags, matched against the Warriner et al.
(2013) affective lexicon. We repeat that for tracks outside MuSe (e.g. Cantopop,
bachata), then **calibrate** onto MuSe's own scale: compute our estimate for a sample of
tracks MuSe *does* have, and fit a per-dimension linear map ours → MuSe. The correlation
from that fit is the honest measure of how far to trust a backfilled value.

Data licenses: Warriner lexicon CC BY-NC-ND 3.0 (used to compute scores, never shipped
or modified); MuSe CC BY 4.0; Last.fm API terms (non-commercial).

Usage (from anywhere):
    python3 -s -E vad_backfill.py calibrate 150     # fit + report on 150 MuSe tracks
    python3 -s -E vad_backfill.py demo <curated.json>  # backfill golden-9 anchors
"""
import csv
import hashlib
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEXICON = HERE / "data" / "warriner" / "Ratings_Warriner_et_al.csv"
MUSE = HERE / "data" / "muse" / "muse_dataset.csv"
CACHE = HERE / "data" / "lastfm" / "toptags"
CALIBRATION = HERE / "out" / "vad_calibration.json"

# The app's public read-only key unless overridden.
LASTFM_KEY = os.environ.get("LASTFM_API_KEY") or "ddaf58167608c952ae1159df778913b6"
MIN_MATCHED_TAGS = 2

_last = 0.0


def lexicon():
    lex = {}
    with open(LEXICON, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lex[r["Word"].strip().lower()] = (float(r["V.Mean.Sum"]), float(r["A.Mean.Sum"]), float(r["D.Mean.Sum"]))
    return lex


def lastfm(method, **params):
    """Cached Last.fm GET (≤ ~4 req/s)."""
    global _last
    key = "|".join(f"{k}={v}" for k, v in sorted(params.items())).lower()
    path = CACHE / method / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if path.exists():
        return json.loads(path.read_text())
    wait = 0.25 - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    q = urllib.parse.urlencode({"method": method, "api_key": LASTFM_KEY, "format": "json",
                                "autocorrect": 1, **params})
    with urllib.request.urlopen(f"https://ws.audioscrobbler.com/2.0/?{q}", timeout=30) as r:
        data = json.loads(r.read())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def tags_for(artist, title):
    """[(tag, weight)] for the track; falls back to the artist's tags if the track has none."""
    def parse(d, root):
        out = []
        for t in ((d.get(root) or {}).get("tag") or []):
            try:
                out.append((t["name"].strip().lower(), float(t.get("count", 0))))
            except (KeyError, ValueError):
                pass
        return [(n, c) for n, c in out if c > 0]
    tags = parse(lastfm("track.gettoptags", artist=artist, track=title), "toptags")
    if len(tags) < MIN_MATCHED_TAGS:
        tags = parse(lastfm("artist.gettoptags", artist=artist), "toptags")
    return tags


def raw_vad(tags, lex):
    """Tag-count-weighted mean VAD over tags found in the lexicon (multi-word tags match
    word by word). None if fewer than MIN_MATCHED_TAGS tags matched."""
    acc, wsum, matched = [0.0, 0.0, 0.0], 0.0, 0
    for name, weight in tags:
        words = [w for w in name.replace("-", " ").split() if w in lex]
        if not words:
            continue
        matched += 1
        for w in words:
            for i in range(3):
                acc[i] += lex[w][i] * weight / len(words)
        wsum += weight
    if matched < MIN_MATCHED_TAGS or wsum == 0:
        return None
    return tuple(a / wsum for a in acc)


def fit(xs, ys):
    """Least-squares y = a·x + b, plus Pearson r."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    a = sxy / sxx if sxx else 0
    r = sxy / math.sqrt(sxx * syy) if sxx and syy else 0
    return a, my - a * mx, r


def calibrate(n):
    lex = lexicon()
    with open(MUSE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    random.Random(42).shuffle(rows)
    pairs = []
    for r in rows:
        if len(pairs) >= n:
            break
        try:
            ours = raw_vad(tags_for(r["artist"], r["track"]), lex)
        except Exception as e:
            print("  skip:", e, flush=True)
            continue
        if ours:
            theirs = (float(r["valence_tags"]), float(r["arousal_tags"]), float(r["dominance_tags"]))
            pairs.append((ours, theirs))
            if len(pairs) % 25 == 0:
                print(f"  {len(pairs)}/{n}", flush=True)
    result = {}
    print(f"\nCalibration on {len(pairs)} MuSe tracks (ours → MuSe raw scale):")
    for i, dim in enumerate(("valence", "arousal", "dominance")):
        a, b, r = fit([p[0][i] for p in pairs], [p[1][i] for p in pairs])
        result[dim] = {"a": a, "b": b, "r": r}
        print(f"- {dim:9s} MuSe ≈ {a:.3f}·ours + {b:.3f}   Pearson r = {r:.2f}")
    CALIBRATION.parent.mkdir(exist_ok=True)
    CALIBRATION.write_text(json.dumps({"n": len(pairs), **result}, indent=2))


# MuSe's observed raw range (muse_profile.py) — the same 0–1 normalization the pool uses.
MUSE_MIN, MUSE_SPAN = 0.108113, 8.36689


def backfill(artist, title, lex, cal):
    raw = raw_vad(tags_for(artist, title), lex)
    if raw is None:
        return None
    out = []
    for i, dim in enumerate(("valence", "arousal", "dominance")):
        muse_raw = cal[dim]["a"] * raw[i] + cal[dim]["b"]
        out.append(min(1.0, max(0.0, (muse_raw - MUSE_MIN) / MUSE_SPAN)))
    return tuple(out)


def demo(curated_path):
    lex, cal = lexicon(), json.loads(CALIBRATION.read_text())
    curated = json.loads(Path(curated_path).read_text())
    for hood in ("Chinatown", "Washington Heights"):
        print(f"\n**{hood}**")
        for seed in curated[hood]["seedTracks"]:
            sep = "—" if "—" in seed else "–"
            artist, title = (s.strip() for s in seed.split(sep, 1))
            vad = backfill(artist, title, lex, cal)
            tags = [t for t, _ in tags_for(artist, title)[:6]]
            print(f"- {artist} — {title}: " + (f"V{vad[0]:.2f} A{vad[1]:.2f} D{vad[2]:.2f}" if vad else "no VAD")
                  + f"   tags: {', '.join(tags)}")


if __name__ == "__main__":
    if sys.argv[1] == "calibrate":
        calibrate(int(sys.argv[2]) if len(sys.argv) > 2 else 150)
    else:
        demo(sys.argv[2])
