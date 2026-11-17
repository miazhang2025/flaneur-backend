"""Neighborhood geometry — a faithful port of the app's detector.

Mirrors Neighborhood.contains / NeighborhoodService.neighborhood(at:) in the iOS app
(and the app repo's scripts/compute_anchors.py): largest exterior ring only, ray
casting, first containing feature in source order. Keeping this identical means a point
the pipeline assigns to a neighborhood is the neighborhood the app would detect there.
"""
import json
import os
from pathlib import Path

# The app repo's bundled GeoJSONs. Override with FLANEUR_APP_RESOURCES.
DEFAULT_RESOURCES = Path(os.environ.get(
    "FLANEUR_APP_RESOURCES",
    Path(__file__).resolve().parents[2] / "flaneur" / "flaneur" / "flaneur" / "Resources",
))

# Same order as Neighborhood.defaultSources (order matters for overlaps).
SOURCES = [
    ("nyc-pediacities", "New York", "America/New_York"),
    ("jersey-city", "Jersey City", "America/New_York"),
    ("san-francisco", "San Francisco", "America/Los_Angeles"),
    ("pittsburgh", "Pittsburgh", "America/New_York"),
    ("london", "London", "Europe/London"),
]


def _polygons(geometry):
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return []


def _area(ring):
    s, j = 0.0, len(ring) - 1
    for i in range(len(ring)):
        s += (ring[j][0] + ring[i][0]) * (ring[j][1] - ring[i][1])
        j = i
    return abs(s) / 2


def ring_contains(ring, lon, lat):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        a, b = ring[i], ring[j]
        if (a[1] > lat) != (b[1] > lat):
            if lon < (b[0] - a[0]) * (lat - a[1]) / (b[1] - a[1]) + a[0]:
                inside = not inside
        j = i
    return inside


class Neighborhood:
    __slots__ = ("id", "name", "city", "borough", "tz", "ring", "anchor", "props")

    def __init__(self, name, city, borough, tz, ring, anchor, props):
        self.id, self.name, self.city, self.borough = f"{city}/{name}", name, city, borough
        self.tz, self.ring, self.anchor, self.props = tz, ring, anchor, props

    def contains(self, lon, lat):
        return ring_contains(self.ring, lon, lat)


def load(resources=DEFAULT_RESOURCES):
    hoods = []
    for resource, city, tz in SOURCES:
        data = json.loads((Path(resources) / f"{resource}.geojson").read_text())
        for f in data["features"]:
            p = f["properties"]
            name = p.get("name") or p.get("neighborhood")
            polys = [x for x in _polygons(f["geometry"]) if x and len(x[0]) >= 3]
            if not name or not polys:
                continue
            ring = max(polys, key=lambda x: _area(x[0]))[0]
            hoods.append(Neighborhood(name, city, p.get("borough"), tz, ring, p.get("anchor"), p))
    return hoods


def resolve(hoods, lon, lat):
    """First containing neighborhood, or None (strict — no nearest-neighbor snapping)."""
    for h in hoods:
        if h.contains(lon, lat):
            return h
    return None
