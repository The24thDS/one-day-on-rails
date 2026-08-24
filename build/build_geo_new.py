#!/usr/bin/env python3
"""Basemaps for the New York, Switzerland, London, Romania and Portugal pages.

Usage:
    python3 build/build_geo_new.py ny     <us-atlas-dir>          -o data/ny-geo.json
    python3 build/build_geo_new.py ch     <natural-earth-geojson> -o data/ch-geo.json
    python3 build/build_geo_new.py ro     <natural-earth-geojson> -o data/ro-geo.json
    python3 build/build_geo_new.py pt     <natural-earth-geojson> -o data/pt-geo.json
    python3 build/build_geo_new.py london <uk-geojson> <ne-geojson> -o data/london-geo.json

All five emit the same {"outline": rings, "states": rings} the page draws:
"outline" is the land/figure layer, "states" the fainter division lines.

  ny      US Census counties (us-atlas 1:10M, public domain) around the
          harbour -- at metro scale the county lines double as the shape of
          Manhattan, Long Island and the Jersey shore.
  ch      Natural Earth 1:10M: Switzerland's cantons as divisions, the
          country ring as outline, and the big lakes, because Swiss rail
          runs along the water and the map is unreadable without it.
  ro      Natural Earth 1:10M: Romania's country ring as outline and its 42
          admin-1 divisions as faint state lines; no water features.
  pt      Natural Earth 1:10M: mainland Portugal's country ring as outline and
          its 18 district divisions as faint state lines.
  london  ONS local authority districts for the Greater London boroughs,
          plus the Thames from Natural Earth.
"""
import argparse, json, math, os, sys


def rings_of(geom):
    polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
             else geom["coordinates"] if geom["type"] == "MultiPolygon" else [])
    for poly in polys:
        for ring in poly:
            yield ring


def lines_of(geom):
    if geom["type"] == "LineString":
        yield geom["coordinates"]
    elif geom["type"] == "MultiLineString":
        yield from geom["coordinates"]


def thin(ring, tol_deg, keep_min=4):
    """Drop points closer together than the tolerance; rings shorter than
    keep_min points survive untouched so small islands do not vanish."""
    if len(ring) <= keep_min:
        return [[round(x, 4), round(y, 4)] for x, y in ring]
    out = [ring[0]]
    for p in ring[1:-1]:
        if abs(p[0] - out[-1][0]) + abs(p[1] - out[-1][1]) >= tol_deg:
            out.append(p)
    out.append(ring[-1])
    return [[round(x, 4), round(y, 4)] for x, y in out]


def inside(ring, box):
    lon0, lat0, lon1, lat1 = box
    return any(lon0 <= x <= lon1 and lat0 <= y <= lat1 for x, y in ring)


def topo_feature(topo, layer):
    """Decode a TopoJSON object into GeoJSON-ish features, no library."""
    tf = topo.get("transform")
    sx, sy = (tf["scale"] if tf else (1, 1))
    ox, oy = (tf["translate"] if tf else (0, 0))

    def arc(i):
        rev = i < 0
        if rev:
            i = ~i
        pts, x, y = [], 0, 0
        for dx, dy in topo["arcs"][i]:
            if tf:
                x += dx
                y += dy
                pts.append([x * sx + ox, y * sy + oy])
            else:
                pts.append([dx, dy])
        return pts[::-1] if rev else pts

    def ring(idxs):
        out = []
        for i in idxs:
            seg = arc(i)
            out.extend(seg if not out else seg[1:])
        return out

    for g in topo["objects"][layer]["geometries"]:
        t = g.get("type")
        if t == "Polygon":
            yield g.get("properties", {}), [ring(r) for r in g["arcs"]]
        elif t == "MultiPolygon":
            yield g.get("properties", {}), [ring(r) for poly in g["arcs"]
                                            for r in poly]


def build_ny(src):
    """us-atlas counties-10m TopoJSON -> the counties around the harbour."""
    topo = json.load(open(os.path.join(src, "counties-10m.json")))
    box = (-74.90, 40.10, -72.60, 41.80)
    outline, states = [], []
    for props, rings in topo_feature(topo, "counties"):
        for r in rings:
            if not inside(r, box):
                continue
            t = thin(r, 0.0012)
            if len(t) >= 4:
                outline.append(t)
    # One layer here: the county lines are the coastline as well.
    return {"outline": outline, "states": outline}


def build_ch(ne):
    """Natural Earth: Swiss cantons, the country ring, and the lakes."""
    prov = json.load(open(os.path.join(
        ne, "ne_10m_admin_1_states_provinces_lakes.geojson"), encoding="utf-8"))
    cantons = []
    for f in prov["features"]:
        p = f["properties"]
        if (p.get("iso_a2") or p.get("adm0_a3")) not in ("CH", "CHE"):
            continue
        for r in rings_of(f["geometry"]):
            t = thin(r, 0.004)
            if len(t) >= 4:
                cantons.append(t)

    countries = json.load(open(os.path.join(
        ne, "ne_10m_admin_0_countries_lakes.geojson"), encoding="utf-8"))
    box = (5.0, 45.2, 11.5, 48.4)
    outline = []
    for f in countries["features"]:
        p = f["properties"]
        if p.get("ADM0_A3") not in ("CHE", "LIE", "AUT", "FRA", "DEU", "ITA"):
            continue
        for r in rings_of(f["geometry"]):
            if not inside(r, box):
                continue
            t = thin(r, 0.004)
            if len(t) >= 4:
                outline.append(t)

    lakes = json.load(open(os.path.join(ne, "ne_10m_lakes_europe.geojson"),
                           encoding="utf-8"))
    lakebox = (5.5, 45.6, 10.9, 48.0)
    water = []
    for f in lakes["features"]:
        for r in rings_of(f["geometry"]):
            if not inside(r, lakebox):
                continue
            t = thin(r, 0.002)
            if len(t) >= 4:
                water.append(t)
    print(f"  cantons {len(cantons)} rings, borders {len(outline)}, "
          f"lakes {len(water)}")
    return {"outline": outline, "states": cantons + water}


def build_ro(ne):
    """Natural Earth: Romania's country ring and 42 admin-1 divisions."""
    prov = json.load(open(os.path.join(
        ne, "ne_10m_admin_1_states_provinces_lakes.geojson"),
        encoding="utf-8"))
    states = []
    seen_boundaries = set()
    for f in prov["features"]:
        p = f["properties"]
        # The admin-1 source also contains lake features. Keep only the
        # country divisions, never water boundaries.
        if p.get("featurecla") == "Lake":
            continue
        if p.get("featurecla") != "Admin-1 scale rank":
            continue
        if p.get("admin") != "Romania" and p.get("iso_a2") != "RO":
            continue
        for r in rings_of(f["geometry"]):
            t = thin(r, 0.004)
            if len(t) < 4:
                continue
            # Ilfov contains Bucharest as a hole, while Bucharest is also a
            # feature of its own. Do not emit that shared boundary twice.
            key = tuple(tuple(p) for p in t)
            key = min(key, tuple(reversed(key)))
            if key in seen_boundaries:
                continue
            seen_boundaries.add(key)
            states.append(t)

    countries = json.load(open(os.path.join(
        ne, "ne_10m_admin_0_countries_lakes.geojson"), encoding="utf-8"))
    outline = []
    for f in countries["features"]:
        p = f["properties"]
        if (p.get("ISO_A2") or p.get("iso_a2")) not in ("RO", "ROU") \
                and p.get("ADM0_A3") != "ROU":
            continue
        for r in rings_of(f["geometry"]):
            t = thin(r, 0.004)
            if len(t) >= 4:
                outline.append(t)
    print(f"  Romania {len(outline)} outline rings, "
          f"{len(states)} division rings")
    return {"outline": outline, "states": states}


def build_pt(ne):
    """Natural Earth: mainland Portugal's country ring and 18 districts."""
    prov = json.load(open(os.path.join(
        ne, "ne_10m_admin_1_states_provinces_lakes.geojson"),
        encoding="utf-8"))
    mainland_box = (-10.0, 36.5, -6.5, 42.5)
    states = []
    seen_boundaries = set()
    for f in prov["features"]:
        p = f["properties"]
        # The admin-1 source also contains lake features. Keep only the
        # country divisions, never water boundaries.
        if p.get("featurecla") == "Lake":
            continue
        if p.get("featurecla") != "Admin-1 scale rank":
            continue
        if p.get("admin") != "Portugal" and p.get("iso_a2") != "PT":
            continue
        # Faro and Lisboa contain small offshore components in addition to
        # their mainland polygons. Keep one mainland ring per district;
        # Azores and Madeira have no component in this box at all.
        candidates = [r for r in rings_of(f["geometry"])
                      if inside(r, mainland_box)]
        if not candidates:
            continue
        r = max(candidates, key=len)
        t = thin(r, 0.004)
        if len(t) < 4:
            continue
        # Adjacent districts share boundaries in Natural Earth's source;
        # do not emit a shared boundary twice.
        key = tuple(tuple(p) for p in t)
        key = min(key, tuple(reversed(key)))
        if key in seen_boundaries:
            continue
        seen_boundaries.add(key)
        states.append(t)

    countries = json.load(open(os.path.join(
        ne, "ne_10m_admin_0_countries_lakes.geojson"), encoding="utf-8"))
    # Portugal is a MultiPolygon: its largest ring is the mainland, while
    # the other rings are the Azores, Madeira and small offshore islands.
    outline_box = (-10.0, 36.0, -6.0, 43.0)
    candidates = []
    for f in countries["features"]:
        p = f["properties"]
        if p.get("ISO_A2") != "PT" and p.get("ADM0_A3") != "PRT":
            continue
        candidates.extend(r for r in rings_of(f["geometry"])
                          if inside(r, outline_box))

    outline = []
    if candidates:
        ring = max(candidates, key=len)
        t = thin(ring, 0.004)
        if len(t) >= 4:
            outline.append(t)
    print(f"  Portugal {len(outline)} mainland outline rings, "
          f"{len(states)} district rings")
    return {"outline": outline, "states": states}


def build_london(uk, ne):
    """Greater London's boroughs, with the Thames drawn through them."""
    lad = json.load(open(os.path.join(uk, "json", "administrative", "gb",
                                      "lad.json"), encoding="utf-8"))
    box = (-0.60, 51.24, 0.35, 51.73)
    boroughs = []
    for f in lad["features"]:
        name = (f["properties"].get("LAD13NM")
                or f["properties"].get("lad13nm") or "")
        for r in rings_of(f["geometry"]):
            if not inside(r, box):
                continue
            t = thin(r, 0.0015)
            if len(t) >= 4:
                boroughs.append(t)
    rivers = json.load(open(os.path.join(
        ne, "ne_10m_rivers_lake_centerlines.geojson"), encoding="utf-8"))
    thames = []
    for f in rivers["features"]:
        nm = (f["properties"].get("name") or "")
        if "Thames" not in nm:
            continue
        for line in lines_of(f["geometry"]):
            seg = [p for p in line if box[0] <= p[0] <= box[2]
                   and box[1] <= p[1] <= box[3]]
            t = thin(seg, 0.0008)
            if len(t) >= 2:
                thames.append(t)
    print(f"  boroughs {len(boroughs)} rings, Thames {len(thames)} lines")
    return {"outline": boroughs, "states": boroughs + thames}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", choices=["ny", "ch", "ro", "pt", "london"])
    ap.add_argument("src", nargs="+")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()
    doc = ({"ny": lambda: build_ny(args.src[0]),
            "ch": lambda: build_ch(args.src[0]),
            "ro": lambda: build_ro(args.src[0]),
            "pt": lambda: build_pt(args.src[0]),
            "london": lambda: build_london(args.src[0], args.src[1])}
           [args.which])()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    pts = sum(len(r) for r in doc["outline"]) + sum(len(r) for r in doc["states"])
    print(f"{args.out}: {len(doc['outline'])}+{len(doc['states'])} rings, "
          f"{pts} points, {os.path.getsize(args.out)/1e3:.0f} kB")


if __name__ == "__main__":
    main()
