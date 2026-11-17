#!/usr/bin/env python3
"""4c — Build each neighborhood's scored candidate pool (spec §评分模型), offline.

One slider-independent pool per neighborhood. Every track carries its per-layer scores
in [0, 1] plus one reason line per layer; the app only weights, ranks and picks.

  History     nominates: anchors (1.0, "Hand-picked for this place"), MusicBrainz
              recordings made at places inside the hood (1.0), and Last.fm
              track.getSimilar expansions of the anchors (score = match).
  Atmosphere  nominates: the 50 MuSe tracks nearest the hood's target VAD — but only
              among artists in the hood's *artist gate* (anchor artists + their Last.fm
              similar artists). Approved spec change 2026-11-14: VAD alone has no genre
              grounding. Scores every pool track by VAD-distance percentile; no VAD → 0.5.
  People      needs FSQ OS Places (connection details not yet in .env) → null for now.
  Time        the app computes s_time from `arousal` and the place-local hour.

The MusicBrainz recordings go through the same artist gate (studios and arenas record
every genre), with per-place and per-artist caps.

VAD scale: MuSe values and Warriner mood-word values are both Warriner-lexicon means, so
both are mapped through the same per-dimension min/max of MuSe → identical 0–1 space.

Usage:
    .venv/bin/python -s -E pools.py                  # all Manhattan hoods
    .venv/bin/python -s -E pools.py "East Harlem"    # one hood (name or id)
Writes out/pools/<id>.json and out/pools_manhattan_review.md.
"""
import csv
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import vad_backfill  # cached Last.fm GET
from identities import GOLDEN_MOODS

HERE = Path(__file__).resolve().parent
APP_CURATED = Path.home() / "Documents/flaneur/flaneur/flaneur/Resources/curated.json"
IDENTITIES = HERE / "out" / "identities"
MB = HERE / "out" / "mb_manhattan.json"
MUSE = HERE / "data" / "muse" / "muse_dataset.csv"
WARRINER = HERE / "data" / "warriner" / "Ratings_Warriner_et_al.csv"
OUT = HERE / "out" / "pools"
REVIEW = HERE / "out" / "pools_manhattan_review.md"

POOL_VERSION = 1
SIMILAR_PER_ANCHOR = 15      # track.getSimilar limit per anchor
SIMILAR_TOTAL = 60           # keep the highest-match expansions
GATE_SIMILAR_ARTISTS = 30    # artist.getSimilar per anchor artist
GATE_MIN_MATCH = 0.15        # drop weakly-similar artists from the gate
ATMOSPHERE_N = 50
MB_PER_PLACE = 8
MB_PER_ARTIST = 3
MB_TOTAL = 40
REASON_MAX = 40

# MusicBrainz live-album filler: "[banter]", "Intro", "Tuning/Breather", "Introductions by Elvis"…
NOT_A_SONG = re.compile(
    r"^(stage |band |spoken |the )?(intro(duction)?s?|announcements?|applause|banter|"
    r"tuning(/breather)?|talk(ing)?|speech|dialogue|crowd( noise)?|outro|encore( break)?( \d+)?|"
    r"filler|unknown|untitled|monologue|interview|silence|hidden track)( by .*| \d+)?$", re.I)


def is_song(title):
    t = title.strip()
    if re.fullmatch(r"\[.*\]", t):
        return False
    return not NOT_A_SONG.match(t.strip("[]() ").strip())


# ---------- normalization ----------

def norm(s):
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s*[\(\[].*?[\)\]]", "", s)                 # (remastered), [live]
    s = re.sub(r"\s+-\s+(.*remaster.*|live.*|mono|stereo|single version.*)$", "", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", "", s)
    s = s.replace("&", "and")
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def key(artist, title):
    return f"{norm(artist)}|{norm(title)}"


def split_seed(seed):
    a, t = (x.strip() for x in seed.split("—", 1))
    return a, t


# ---------- VAD ----------

def load_muse():
    rows = list(csv.DictReader(open(MUSE, newline="", encoding="utf-8")))
    dims = ("valence_tags", "arousal_tags", "dominance_tags")
    lo = [min(float(r[d]) for r in rows) for d in dims]
    hi = [max(float(r[d]) for r in rows) for d in dims]
    scale = lambda v: tuple(round((v[i] - lo[i]) / (hi[i] - lo[i]), 4) for i in range(3))
    by_key, by_artist = {}, defaultdict(list)
    for r in rows:
        t = {"artist": r["artist"], "title": r["track"],
             "vad": scale([float(r[d]) for d in dims]),
             "spotifyId": r["spotify_id"] or None, "mbid": r["mbid"] or None}
        by_key.setdefault(key(r["artist"], r["track"]), t)
        by_artist[norm(r["artist"])].append(t)
    return by_key, by_artist, scale


def mood_vad(scale):
    lex = {}
    for r in csv.DictReader(open(WARRINER, newline="", encoding="utf-8")):
        lex[r["Word"].strip().lower()] = (float(r["V.Mean.Sum"]), float(r["A.Mean.Sum"]), float(r["D.Mean.Sum"]))
    return {w: scale(v) for w, v in lex.items()}


def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


# ---------- Last.fm ----------

def similar_tracks(artist, title):
    try:
        d = vad_backfill.lastfm("track.getsimilar", artist=artist, track=title, limit=SIMILAR_PER_ANCHOR)
    except Exception:
        return []
    out = []
    for t in (d.get("similartracks") or {}).get("track") or []:
        try:
            out.append((t["artist"]["name"], t["name"], float(t["match"])))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def similar_artists(artist):
    try:
        d = vad_backfill.lastfm("artist.getsimilar", artist=artist, limit=GATE_SIMILAR_ARTISTS)
    except Exception:
        return []
    out = []
    for a in (d.get("similarartists") or {}).get("artist") or []:
        try:
            if float(a["match"]) >= GATE_MIN_MATCH:
                out.append(a["name"])
        except (KeyError, TypeError, ValueError):
            pass
    return out


# ---------- reasons (≤ 40 chars, fallbacks, never truncate) ----------

def fit(*options):
    for o in options:
        if o and len(o) <= REASON_MAX:
            return o
    return None


def mb_reason(place, year):
    y = f", {year}" if year else ""
    return fit(f"Recorded at {place}{y}", f"Recorded near here{y}", "Recorded near here")


def similar_reason(anchor_title):
    return fit(f"Close to {anchor_title}", "Close to an anchor of this place")


def atmosphere_reason(vad, moods, mood_vads):
    if vad is None:
        return None
    near = sorted((m for m in moods if m in mood_vads), key=lambda m: dist(vad, mood_vads[m]))[:2]
    if not near:
        return None
    return fit(f"Feels like here: {near[0]}, {near[1]}" if len(near) == 2 else None,
               f"Feels like here: {near[0]}")


# ---------- hoods ----------

def load_hoods():
    curated = json.load(open(APP_CURATED))
    mb = json.load(open(MB))
    hoods = []
    for name, moods in GOLDEN_MOODS.items():
        e = curated[name]
        hoods.append({"id": f"New York/{name}", "name": name, "golden": True,
                      "anchors": e["seedTracks"], "moodWords": moods})
    for p in sorted(IDENTITIES.glob("*.json")):
        d = json.load(open(p))
        hoods.append({"id": d["id"], "name": d["name"], "golden": False,
                      "anchors": d["anchorTracks"], "moodWords": d["moodWords"]})
    return hoods, mb


# ---------- build ----------

def build(hood, mb, muse_key, muse_artist, mood_vads):
    pool = {}   # key → candidate

    def add(artist, title):
        k = key(artist, title)
        c = pool.get(k)
        if c is None:
            m = muse_key.get(k)
            c = pool[k] = {"artist": artist, "title": title, "history": 0.0,
                           "atmosphere": None, "people": None, "memory": None,
                           "vad": list(m["vad"]) if m else None,
                           "arousal": m["vad"][1] if m else None,
                           "spotifyId": m["spotifyId"] if m else None,
                           "mbid": m["mbid"] if m else None,
                           "sources": [], "reasons": {}}
        return c

    def history(c, score, reason, source):
        if source not in c["sources"]:
            c["sources"].append(source)
        if score > c["history"] or (score == c["history"] and "history" not in c["reasons"]):
            c["history"] = round(score, 4)
            if reason:
                c["reasons"]["history"] = reason

    # Anchors
    anchor_artists = []
    for seed in hood["anchors"]:
        a, t = split_seed(seed)
        history(add(a, t), 1.0, "Hand-picked for this place", "anchor")
        for part in re.split(r"\s*(?:/|,|&| and )\s*", a):
            if part and part not in anchor_artists:
                anchor_artists.append(part)

    # Artist gate — one group per anchor artist (itself + its similar artists)
    groups = [{norm(a)} | {norm(x) for x in similar_artists(a)} for a in anchor_artists]
    gate = set().union(*groups)

    # MusicBrainz, gated + capped
    per_place, per_artist, taken = Counter(), Counter(), 0
    recs = sorted(mb.get(hood["id"], []), key=lambda r: (r["year"] is None, r["year"] or 0))
    for r in recs:
        if taken >= MB_TOTAL:
            break
        na = norm(r["artist"])
        if not is_song(r["title"]) or na not in gate or per_place[r["place"]] >= MB_PER_PLACE or per_artist[na] >= MB_PER_ARTIST:
            continue
        k = key(r["artist"], r["title"])
        if k in pool and "musicbrainz" in pool[k]["sources"]:
            continue
        per_place[r["place"]] += 1
        per_artist[na] += 1
        taken += 1
        c = add(r["artist"], r["title"])
        if not c["mbid"]:
            c["mbid"] = r["recording_id"]
        history(c, 1.0, mb_reason(r["place"], r["year"]), "musicbrainz")

    # getSimilar expansions — keep the best SIMILAR_TOTAL by match
    exp = []
    for seed in hood["anchors"]:
        a, t = split_seed(seed)
        exp += [(m, sa, st, t) for sa, st, m in similar_tracks(a, t)]
    exp.sort(reverse=True)
    seen = set()
    for m, sa, st, anchor_title in exp:
        k = key(sa, st)
        if k in seen:
            continue
        seen.add(k)
        if len(seen) > SIMILAR_TOTAL:
            break
        history(add(sa, st), m, similar_reason(anchor_title), "similar")

    # Atmosphere nomination: gated MuSe, nearest to target VAD
    moods = hood["moodWords"]
    target = [round(sum(mood_vads[w][i] for w in moods) / len(moods), 4) for i in range(3)]
    # Round-robin over anchor groups so MuSe's coverage skew (lots of Anglo soul, little
    # salsa/Cantopop) can't let one anchor's neighbors take every slot.
    queues = []
    for g in groups:
        q = sorted((t for a in g for t in muse_artist.get(a, [])), key=lambda t: dist(t["vad"], target))
        if q:
            queues.append(q)
    nominated, i = 0, 0
    while nominated < ATMOSPHERE_N and any(queues):
        q = queues[i % len(queues)]
        i += 1
        if not q:
            continue
        t = q.pop(0)
        c = add(t["artist"], t["title"])
        if "atmosphere" not in c["sources"]:
            c["sources"].append("atmosphere")
            nominated += 1

    # Atmosphere score: percentile 1 − rank/N over the whole pool; no VAD → 0.5
    cands = list(pool.values())
    n = len(cands)
    ranked = sorted((c for c in cands if c["vad"]), key=lambda c: dist(c["vad"], target))
    for rank, c in enumerate(ranked):
        c["atmosphere"] = round(1 - rank / n, 4)
        r = atmosphere_reason(c["vad"], moods, mood_vads)
        if r:
            c["reasons"]["atmosphere"] = r
    for c in cands:
        if c["atmosphere"] is None:
            c["atmosphere"] = 0.5

    return {"version": POOL_VERSION, "id": hood["id"], "name": hood["name"], "golden": hood["golden"],
            "moodWords": moods, "targetVAD": target, "peopleAvailable": False,
            "gateArtists": len(gate), "candidates": cands}


# ---------- review ----------

def default_rank(c):
    """Spec default mix with People/Memory inert: H30 A30 (P15 neutral, M off)."""
    h, a, p = c["history"], c["atmosphere"], 0.5
    w = {"h": 30 / 75, "a": 30 / 75, "p": 15 / 75}
    t = 0.5 if c["arousal"] is None else 1 - abs(c["arousal"] - 0.55)
    return 0.9 * (w["h"] * h + w["a"] * a + w["p"] * p) + 0.1 * t


def write_review(pools):
    L = ["# Manhattan — candidate pools (4c) for review", "",
         "Default mix H30 A30 P15 (People neutral until FSQ), Time at 0.55. Top 15 per hood; "
         "the app samples from the top 25.", "",
         "| hood | pool | anchors | MB | similar | atmos | with VAD |", "|---|---|---|---|---|---|---|"]
    for p in pools:
        cs = p["candidates"]
        cnt = Counter(s for c in cs for s in c["sources"])
        L.append(f"| {p['name']} | {len(cs)} | {cnt['anchor']} | {cnt['musicbrainz']} | {cnt['similar']} "
                 f"| {cnt['atmosphere']} | {sum(1 for c in cs if c['vad'])} |")
    for p in pools:
        L += ["", f"## {p['name']}", f"_{', '.join(p['moodWords'])}_ · target VAD {p['targetVAD']}", ""]
        for c in sorted(p["candidates"], key=default_rank, reverse=True)[:15]:
            why = c["reasons"].get("history") if c["history"] * 0.4 >= c["atmosphere"] * 0.4 else None
            why = why or c["reasons"].get("atmosphere") or c["reasons"].get("history") or "—"
            L.append(f"- {c['artist']} — {c['title']}  ·  H {c['history']:.2f} A {c['atmosphere']:.2f}  ·  _{why}_")
        mbs = [c for c in p["candidates"] if "musicbrainz" in c["sources"]]
        if mbs:
            L.append("")
            L.append("MusicBrainz kept: " + "; ".join(f"{c['artist']} — {c['title']}" for c in mbs[:12])
                     + (" …" if len(mbs) > 12 else ""))
    REVIEW.write_text("\n".join(L) + "\n")


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else None
    hoods, mb = load_hoods()
    if want:
        hoods = [h for h in hoods if want in (h["name"], h["id"])]
        if not hoods:
            sys.exit(f"no hood {want!r}")
    muse_key, muse_artist, scale = load_muse()
    mood_vads = mood_vad(scale)
    missing = {w for h in hoods for w in h["moodWords"] if w not in mood_vads}
    if missing:
        sys.exit(f"mood words not in Warriner: {missing}")
    OUT.mkdir(parents=True, exist_ok=True)
    pools = []
    for i, h in enumerate(hoods, 1):
        p = build(h, mb, muse_key, muse_artist, mood_vads)
        (OUT / (h["id"].replace("/", "__") + ".json")).write_text(json.dumps(p, ensure_ascii=False, indent=1))
        print(f"[{i}/{len(hoods)}] {h['name']}: {len(p['candidates'])} candidates", flush=True)
        pools.append(p)
    if not want:
        write_review(pools)
        print(f"review → {REVIEW}")


if __name__ == "__main__":
    main()
