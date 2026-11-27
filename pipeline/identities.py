#!/usr/bin/env python3
"""4b — Build-time neighborhood identities (replaces runtime /api/generate).

For every neighborhood in a borough without hand-curated data, ask Claude once for:
  • 8–10 anchor tracks ("Artist — Title", real and findable)
  • 3 mood words from the spec's fixed feeling vocabulary (→ target VAD for Atmosphere)
  • a transition-card field color + legible text color
  • a one-line blurb (≤12 words) in Flâneur's voice

Grounded in what's actually there: MusicBrainz venues/studios recorded in that
neighborhood (out/mb_<borough>.json) and two golden-9 examples for style.

Every anchor is then checked against Last.fm; misses are flagged for review. Results
are written per neighborhood (resumable) and summarized in a review sheet. Nothing is
published — a human reviews first.

Usage (run with the pipeline venv):
    .venv/bin/python identities.py Manhattan --only "Hudson Square"   # one, to check
    .venv/bin/python identities.py Manhattan                          # the rest
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import geo
import vad_backfill  # Last.fm helper (cached)

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "identities"
load_dotenv(HERE / ".env")

MODEL = "claude-opus-5-5"
PRICE_IN, PRICE_OUT = 4.00, 20.00          # $/MTok, Claude Opus 5.5

# Spec: feeling vocabulary (shared by user feeling tags and neighborhood mood words).
MOOD_WORDS = [
    "calm", "slow", "spacious", "warm", "tender", "dreamy", "nostalgic", "bittersweet",
    "melancholy", "lonely", "solemn", "sacred", "elegant", "romantic", "mysterious",
    "cinematic", "proud", "playful", "joyful", "gritty", "raw", "crowded", "restless",
    "tense", "defiant", "electric",
]

# Spec: golden-9 mood words — two used as style examples in the prompt.
GOLDEN_MOODS = {
    "Chinatown": ["nostalgic", "cinematic", "crowded"],
    "Greenwich Village": ["warm", "tender", "nostalgic"],
    "Harlem": ["proud", "joyful", "sacred"],
    "Lower East Side": ["gritty", "restless", "raw"],
    "East Village": ["defiant", "playful", "raw"],
    "Washington Heights": ["joyful", "warm", "electric"],
    "Upper West Side": ["elegant", "calm", "spacious"],
    "Tribeca": ["slow", "spacious", "mysterious"],
    "SoHo": ["elegant", "playful", "electric"],
}
EXAMPLES = ["Chinatown", "Lower East Side"]

SCHEMA = {
    "type": "object",
    "properties": {
        "anchorTracks": {"type": "array", "items": {"type": "string"}},
        "moodWords": {"type": "array", "items": {"type": "string", "enum": MOOD_WORDS}},
        "transitionColor": {"type": "string"},
        "transitionTextColor": {"type": "string"},
        "blurb": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["anchorTracks", "moodWords", "transitionColor", "transitionTextColor", "blurb", "rationale"],
    "additionalProperties": False,
}

SYSTEM = """You are the music curator for Flâneur, an iOS app that scores a city's neighborhoods with music as someone walks through them. Each neighborhood has a "sound identity": a handful of anchor songs that define its cultural DNA, three mood words for how the place feels, a cinematic color for the card that appears when you cross into it, and one line in the city's voice.

Your taste is cinematic, literary, and culturally grounded — music that makes a place feel like a scene in a film, rooted in who actually built and lives in the neighborhood and what has actually been played and recorded there. Avoid generic "New York" playlists: two adjacent neighborhoods should sound different.

Output rules:
- anchorTracks: 8–10 real, findable recordings, each exactly "Artist — Title" with an em dash (—). They seed a similarity search, so prefer songs with clear stylistic neighbors over obscure deep cuts. Mix eras where it fits the place. Never invent songs.
- moodWords: exactly 3 distinct words from the allowed vocabulary, describing how the place feels to walk through.
- transitionColor / transitionTextColor: 6-digit hex (#RRGGBB). The field color is emotional, not literal; the text color must be clearly legible on it.
- blurb: one restrained, poetic line, at most 12 words, film-noir voice — the city speaking.
- rationale: one or two plain sentences on why these choices fit this place (for the human reviewer; not shown in the app)."""


def golden_example(name, curated):
    e = curated[name]
    return (f"Example — {name}, Manhattan:\n"
            f"  anchors: {'; '.join(e['seedTracks'][:6])}; …\n"
            f"  moodWords: {', '.join(GOLDEN_MOODS[name])}\n"
            f"  colors: {e['transitionColor']} / {e['transitionTextColor']}\n"
            f"  sound: {e['soundIdentity']}")


def grounding(hood_id, mb):
    recs = mb.get(hood_id, [])
    if not recs:
        return "MusicBrainz lists no recordings made at venues or studios in this neighborhood."
    places = Counter(r["place"] for r in recs).most_common(6)
    artists = Counter(r["artist"] for r in recs).most_common(12)
    return ("Recorded here, per MusicBrainz (venues/studios inside the neighborhood, with recording counts): "
            + ", ".join(f"{p} ({n})" for p, n in places)
            + ". Most-recorded artists there: " + ", ".join(a for a, _ in artists)
            + ". Studios and arenas record every genre — use this as evidence of the scene, not as a playlist.")


def km(a, b):
    """Great-circle distance between two [lon, lat] points."""
    import math
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def nearby_golden(hood, curated, radius_km=2.0):
    """Anchor artists of hand-curated neighborhoods within `radius_km` — so a new
    neighborhood doesn't just re-voice its famous neighbor."""
    out = []
    for name, e in curated.items():
        if hood.anchor and e.get("anchor") and km(hood.anchor, e["anchor"]) <= radius_km:
            artists = sorted({t.split("—")[0].strip() for t in e["seedTracks"] if "—" in t})
            out.append(f"{name}: {', '.join(artists)}")
    return out


def prompt(hood, curated, mb):
    lat, lon = (hood.anchor[1], hood.anchor[0]) if hood.anchor else (None, None)
    neighbors = nearby_golden(hood, curated)
    return "\n\n".join([
        "Style reference (hand-curated):\n" + "\n\n".join(golden_example(n, curated) for n in EXAMPLES),
        f"Allowed mood words: {', '.join(MOOD_WORDS)}.",
        f"Generate the sound identity for: {hood.name} — {hood.borough}, {hood.city}"
        + (f" (around {lat:.4f}, {lon:.4f})" if lat else "") + ".",
        grounding(hood.id, mb),
    ] + ([
        "Nearby hand-curated neighborhoods already anchored by these artists — don't reuse them "
        "unless one is genuinely essential to this neighborhood too; find what makes this place "
        "sound different from its neighbors:\n" + "\n".join(f"- {n}" for n in neighbors)
    ] if neighbors else []))


def validate(d):
    errs = []
    tracks = d.get("anchorTracks", [])
    if not 8 <= len(tracks) <= 10:
        errs.append(f"{len(tracks)} anchors (want 8–10)")
    if any("—" not in t for t in tracks):
        errs.append("anchor without em dash")
    moods = d.get("moodWords", [])
    if len(moods) != 3 or len(set(moods)) != 3:
        errs.append(f"mood words {moods} (want 3 distinct)")
    for k in ("transitionColor", "transitionTextColor"):
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", d.get(k, "")):
            errs.append(f"{k} not #RRGGBB")
    if len(d.get("blurb", "").split()) > 12:
        errs.append("blurb over 12 words")
    return errs


def contrast(a, b):
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    l1, l2 = sorted((lum(a), lum(b)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def on_lastfm(seed):
    artist, title = (s.strip() for s in seed.split("—", 1))
    try:
        d = vad_backfill.lastfm("track.getinfo", artist=artist, track=title)
    except Exception:
        return None
    return "track" in d


def generate(client, hood, curated, mb):
    for attempt in range(3):
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt(hood, curated, mb)}],
        )
        usage = (resp.usage.input_tokens, resp.usage.output_tokens)
        if resp.stop_reason == "refusal":
            cat = resp.stop_details.category if resp.stop_details else None
            raise RuntimeError(f"refused ({cat})")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
        errs = validate(data)
        if not errs:
            return data, usage, resp.model
        print(f"  ✗ attempt {attempt + 1}: {'; '.join(errs)} — retrying", flush=True)
    raise RuntimeError(f"invalid after 3 attempts: {errs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borough")
    ap.add_argument("--only", help="generate just this neighborhood (by name)")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY missing — add it to pipeline/.env")
    client = anthropic.Anthropic()

    curated = json.loads((geo.DEFAULT_RESOURCES / "curated.json").read_text())
    mb_path = HERE / "out" / f"mb_{args.borough.lower()}.json"
    mb = json.loads(mb_path.read_text()) if mb_path.exists() else {}

    # Unique neighborhoods (some names span several island polygons), minus hand-curated.
    seen, hoods = set(), []
    for h in geo.load():
        if h.borough == args.borough and h.name not in curated and h.id not in seen:
            seen.add(h.id)
            hoods.append(h)
    if args.only:
        hoods = [h for h in hoods if h.name == args.only]
        if not hoods:
            sys.exit(f"no uncurated {args.borough} neighborhood named {args.only!r}")

    OUT.mkdir(parents=True, exist_ok=True)
    spent_in = spent_out = 0
    for h in hoods:
        path = OUT / f"{h.id.replace('/', '__')}.json"
        if path.exists():
            continue
        t0 = time.time()
        try:
            data, (tin, tout), served_by = generate(client, h, curated, mb)
        except Exception as e:
            print(f"✗ {h.name}: {e}", flush=True)
            continue
        spent_in += tin
        spent_out += tout
        data["lastfmFound"] = {t: on_lastfm(t) for t in data["anchorTracks"]}
        data.update({"id": h.id, "name": h.name, "borough": h.borough, "city": h.city,
                     "model": served_by, "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "usage": {"input_tokens": tin, "output_tokens": tout}})
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        found = sum(1 for v in data["lastfmFound"].values() if v)
        print(f"✓ {h.name}: {', '.join(data['moodWords'])} · {data['transitionColor']} · "
              f"{found}/{len(data['anchorTracks'])} anchors on Last.fm · {time.time() - t0:.0f}s", flush=True)

    cost = spent_in / 1e6 * PRICE_IN + spent_out / 1e6 * PRICE_OUT
    print(f"\nThis run: {spent_in:,} input + {spent_out:,} output tokens ≈ ${cost:.2f}")
    write_review(args.borough)


def write_review(borough):
    files = sorted(OUT.glob(f"*.json"))
    rows = [json.loads(p.read_text()) for p in files]
    rows = [r for r in rows if r.get("borough") == borough]
    total = sum(r["usage"]["input_tokens"] / 1e6 * PRICE_IN + r["usage"]["output_tokens"] / 1e6 * PRICE_OUT for r in rows)
    lines = [f"# {borough} — generated identities for review\n",
             f"{len(rows)} neighborhoods · total generation cost ≈ ${total:.2f} · model {MODEL}\n",
             "✓ = anchor found on Last.fm · ✗ = not found (likely misspelled or invented — fix or replace) · "
             "contrast = text/field ratio (≥4.5 is comfortably legible)\n"]
    for r in sorted(rows, key=lambda r: r["name"]):
        c = contrast(r["transitionColor"], r["transitionTextColor"])
        lines += [f"## {r['name']}",
                  f"**{' · '.join(r['moodWords'])}** — _{r['blurb']}_  ",
                  f"colors {r['transitionColor']} / {r['transitionTextColor']} (contrast {c:.1f}{' ⚠️' if c < 4.5 else ''})  ",
                  f"why: {r['rationale']}\n"]
        lines += [f"- {'✓' if r['lastfmFound'].get(t) else '✗'} {t}" for t in r["anchorTracks"]]
        lines.append("")
    (HERE / "out" / f"identities_{borough.lower()}_review.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
