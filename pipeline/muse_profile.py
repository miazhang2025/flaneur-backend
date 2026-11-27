#!/usr/bin/env python3
"""4a — profile the MuSe dataset before building pools on it.

Answers three questions for the coverage report:
  1. What scale are MuSe's valence/arousal/dominance on? (spec open question)
  2. How well does MuSe cover each golden-9 neighborhood's music? (anchor-artist hits)
  3. What would Atmosphere pull? The nearest MuSe tracks to each golden-9 target VAD
     (spec draft mood words → mean VAD).

Stdlib only. Usage (from the pipeline/ dir):
    python3 -I muse_profile.py ../../flaneur/flaneur/flaneur/Resources/curated.json
"""
import csv
import json
import math
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
MUSE = HERE / "data" / "muse" / "muse_dataset.csv"

# Spec draft: golden-9 mood words → target VAD (already on 0–1).
GOLDEN_TARGETS = {
    "Chinatown":          (0.57, 0.53, 0.50),
    "Greenwich Village":  (0.73, 0.33, 0.52),
    "Harlem":             (0.80, 0.53, 0.72),
    "Lower East Side":    (0.40, 0.70, 0.53),
    "East Village":       (0.57, 0.70, 0.65),
    "Washington Heights": (0.82, 0.63, 0.65),
    "Upper West Side":    (0.74, 0.28, 0.62),
    "Tribeca":            (0.55, 0.30, 0.48),
    "SoHo":               (0.78, 0.63, 0.67),
}


def norm_name(s):
    s = unicodedata.normalize("NFKC", s or "").casefold().strip()
    return " ".join(s.split())


def load_muse():
    rows = []
    with open(MUSE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                v, a, d = (float(r["valence_tags"]), float(r["arousal_tags"]), float(r["dominance_tags"]))
            except (TypeError, ValueError):
                continue
            rows.append({"track": r["track"], "artist": r["artist"], "v": v, "a": a, "d": d,
                         "mbid": r["mbid"] or None, "spotify": r["spotify_id"] or None})
    return rows


def describe(values):
    s = sorted(values)
    n = len(s)
    q = lambda p: s[min(n - 1, int(p * n))]
    return f"min {s[0]:.2f}  p1 {q(.01):.2f}  median {q(.5):.2f}  p99 {q(.99):.2f}  max {s[-1]:.2f}"


def main():
    curated_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    rows = load_muse()
    print(f"# MuSe profile\n\n{len(rows):,} tracks with all three VAD values\n")

    print("## 1. VAD scale")
    for k, label in (("v", "valence"), ("a", "arousal"), ("d", "dominance")):
        print(f"- {label:9s} {describe([r[k] for r in rows])}")
    with_sp = sum(1 for r in rows if r["spotify"])
    with_mb = sum(1 for r in rows if r["mbid"])
    print(f"- Spotify ID: {with_sp:,} ({with_sp / len(rows):.0%}) · MusicBrainz ID: {with_mb:,} ({with_mb / len(rows):.0%})\n")

    lo = min(min(r["v"], r["a"], r["d"]) for r in rows)
    hi = max(max(r["v"], r["a"], r["d"]) for r in rows)
    scale = (1.0, 9.0) if lo >= 0.9 and hi <= 9.1 else (lo, hi)
    print(f"→ treating the raw scale as {scale[0]:g}–{scale[1]:g}, normalized x' = (x − {scale[0]:g}) / {scale[1] - scale[0]:g}\n")
    to01 = lambda x: (x - scale[0]) / (scale[1] - scale[0])
    for r in rows:
        r["v01"], r["a01"], r["d01"] = to01(r["v"]), to01(r["a"]), to01(r["d"])
    print(f"- arousal after normalizing: {describe([r['a01'] for r in rows])}")
    print("  (Time's target curve spans 0.30–0.65; compare against this spread.)\n")

    by_artist = defaultdict(list)
    for r in rows:
        by_artist[norm_name(r["artist"])].append(r)

    if curated_path:
        curated = json.loads(curated_path.read_text())
        print("## 2. Golden-9 coverage — anchor artists found in MuSe")
        for hood, entry in curated.items():
            artists = []
            for seed in entry.get("seedTracks", []):
                for sep in ("—", "–"):
                    if sep in seed:
                        artists.append(seed.split(sep)[0].strip())
                        break
            hits = [a for a in artists if norm_name(a) in by_artist]
            tracks = sum(len(by_artist[norm_name(a)]) for a in hits)
            print(f"- **{hood}**: {len(hits)}/{len(artists)} anchor artists, {tracks} MuSe tracks by them"
                  + (f" — missing: {', '.join(a for a in artists if a not in hits)}" if len(hits) < len(artists) else ""))
        print()

    print("## 3. What Atmosphere would pull — nearest 12 of the 50 to each target VAD")
    for hood, target in GOLDEN_TARGETS.items():
        dist = lambda r: math.dist((r["v01"], r["a01"], r["d01"]), target)
        near = sorted(rows, key=dist)[:50]
        radius = dist(near[-1])
        print(f"\n**{hood}** target V{target[0]:.2f} A{target[1]:.2f} D{target[2]:.2f} · 50th nearest at distance {radius:.3f}")
        for r in near[:12]:
            print(f"  - {r['artist']} — {r['track']}")


if __name__ == "__main__":
    main()
