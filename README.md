# Flâneur Backend

A tiny Vercel serverless proxy that generates a neighborhood's musical identity with Claude. The Anthropic API key lives only here as an environment variable — never in the iOS app.

## What it does

`POST /api/generate` with a neighborhood name + city → returns JSON:

```json
{
  "neighborhood": "Tribeca",
  "city": "New York",
  "anchorTracks": ["Brian Eno — An Ending (Ascent)", "..."],
  "atmosphereTags": ["atmospheric", "calm", "nocturnal"],
  "peopleProfile": ["sophisticated", "atmospheric"],
  "transitionColor": "#3E4A4C",
  "transitionTextColor": "#CFD6D2",
  "blurb": "fog settles where the galleries sleep",
  "generatedAt": "..."
}
```

The iOS app calls this for any neighborhood that doesn't have hand-curated (golden-9) data, then feeds anchorTracks → Last.fm → Spotify as usual.

## Deploy

1. Create a new GitHub repo and push these files.
2. On [vercel.com](https://vercel.com), New Project → import the repo → Deploy.
3. In the Vercel project → Settings → Environment Variables, add:
   - `ANTHROPIC_API_KEY` — your new Anthropic key (the one you regenerated)
   - `FLANEUR_APP_KEY` — any long random string you invent; the app must send the same value
4. Redeploy so the env vars take effect.

## Test it

```bash
curl -X POST https://YOUR-PROJECT.vercel.app/api/generate \
  -H "Content-Type: application/json" \
  -H "x-flaneur-key: YOUR_FLANEUR_APP_KEY" \
  -d '{"neighborhood":"Nolita","city":"New York","lat":40.722,"lon":-73.996}'
```

You should get back JSON with 8–10 anchor tracks, affective tags, and colors. Run it a few times on different neighborhoods (Financial District, Two Bridges, Williamsburg) to eyeball quality before wiring the app.

## Notes

- Model: `claude-sonnet-4-6`. Adjust in `api/generate.js` if needed.
- Validation rejects malformed output (non-JSON, off-vocabulary tags, bad hex) with a 502 so the app can fall back gracefully.
- `FLANEUR_APP_KEY` is light protection against random abuse of your endpoint — it'll still be embedded in the app, but it stops casual quota-burning. For stronger protection later, add rate limiting or per-device tokens.
