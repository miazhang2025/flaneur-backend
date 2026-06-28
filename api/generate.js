// api/generate.js
// Flâneur backend — generates a neighborhood's musical identity via Claude.
// The Anthropic API key lives ONLY here (Vercel env var), never in the iOS app.

const AFFECTIVE_TAGS = [
  "calm", "melancholic", "dreamy", "romantic", "energetic", "uplifting",
  "soulful", "passionate", "nocturnal", "atmospheric", "mellow", "dark",
  "ethereal", "nostalgic", "sophisticated", "raw", "warm", "cinematic"
];

// ---- Prompt construction -------------------------------------------------

function buildPrompt({ neighborhood, city, lat, lon }) {
  return `You are the music curator for Flâneur, an app that scores a city's neighborhoods with music as someone walks through them. Your taste is cinematic, literary, and culturally grounded — think Wong Kar-wai, not a generic playlist. You choose music that makes a place feel like a film scene.

Generate the musical identity for this neighborhood:
- Neighborhood: ${neighborhood}
- City: ${city}
${lat && lon ? `- Approx coordinates: ${lat}, ${lon}` : ""}

Return ONLY a JSON object (no markdown, no prose) with exactly these fields:

{
  "anchorTracks": [ "Artist — Title", ... ],   // 8-10 real, findable songs that define this neighborhood's cultural/sonic DNA. Use real artists and real tracks. Mix eras where it fits the place. These seed a similarity search, so pick songs that define a vibe, not obscure deep cuts that have no neighbors.
  "atmosphereTags": [ ... ],   // 3 affective tags describing how the PLACE FEELS, chosen ONLY from this controlled list: ${AFFECTIVE_TAGS.join(", ")}
  "peopleProfile": [ ... ],    // 2 affective tags inferred from the neighborhood's POI character (nightlife→energetic, churches/parks→calm/serene, galleries→sophisticated, etc.), ONLY from the same controlled list
  "transitionColor": "#RRGGBB",     // a cinematic field color for the neighborhood-name transition card — emotional, not literal. Evoke the feeling/film of the place, not its literal building colors.
  "transitionTextColor": "#RRGGBB", // high-contrast text color readable on the field
  "blurb": "..."   // one short, restrained, poetic line (max ~12 words) in Flâneur's film-noir voice — the city speaking
}

Rules:
- anchorTracks MUST be real songs by real artists, formatted exactly "Artist — Title" with an em dash.
- atmosphereTags and peopleProfile MUST only use tags from the controlled list — no other words.
- Colors must be valid 6-digit hex. Cinematic and emotional. Ensure text is legible on the field.
- Output strictly valid JSON, nothing else.`;
}

// ---- Validation ----------------------------------------------------------

function validate(data) {
  const errs = [];
  if (!Array.isArray(data.anchorTracks) || data.anchorTracks.length < 6)
    errs.push("anchorTracks must be an array of >= 6 items");
  const tagsOK = (arr) => Array.isArray(arr) && arr.every(t => AFFECTIVE_TAGS.includes(t));
  if (!tagsOK(data.atmosphereTags)) errs.push("atmosphereTags must be from the controlled vocabulary");
  if (!tagsOK(data.peopleProfile)) errs.push("peopleProfile must be from the controlled vocabulary");
  const hex = (s) => typeof s === "string" && /^#[0-9A-Fa-f]{6}$/.test(s);
  if (!hex(data.transitionColor)) errs.push("transitionColor must be #RRGGBB");
  if (!hex(data.transitionTextColor)) errs.push("transitionTextColor must be #RRGGBB");
  return errs;
}

// ---- Handler -------------------------------------------------------------

export default async function handler(req, res) {
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  // Simple shared-secret gate so randoms can't burn your Claude quota.
  const appKey = req.headers["x-flaneur-key"];
  if (!process.env.FLANEUR_APP_KEY || appKey !== process.env.FLANEUR_APP_KEY) {
    return res.status(401).json({ error: "Unauthorized" });
  }

  const { neighborhood, city, lat, lon } = req.body || {};
  if (!neighborhood || !city) {
    return res.status(400).json({ error: "neighborhood and city are required" });
  }

  try {
    const anthropicRes = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": process.env.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01"
      },
      body: JSON.stringify({
        model: "claude-sonnet-4-6",
        max_tokens: 1024,
        messages: [{ role: "user", content: buildPrompt({ neighborhood, city, lat, lon }) }]
      })
    });

    if (!anthropicRes.ok) {
      const body = await anthropicRes.text();
      return res.status(502).json({ error: "Anthropic API error", detail: body });
    }

    const payload = await anthropicRes.json();
    let text = (payload.content || [])
      .filter(b => b.type === "text")
      .map(b => b.text)
      .join("")
      .trim();

    // Strip accidental code fences
    text = text.replace(/^```(?:json)?/i, "").replace(/```$/, "").trim();

    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      return res.status(502).json({ error: "Model returned non-JSON", raw: text });
    }

    const errs = validate(data);
    if (errs.length) {
      return res.status(502).json({ error: "Validation failed", details: errs, data });
    }

    // Attach identity + a cache hint
    return res.status(200).json({
      neighborhood,
      city,
      ...data,
      generatedAt: new Date().toISOString()
    });
  } catch (err) {
    return res.status(500).json({ error: "Server error", detail: String(err) });
  }
}
