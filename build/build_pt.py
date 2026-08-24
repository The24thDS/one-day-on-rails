#!/usr/bin/env python3
"""One day of Portuguese rail, from the CP and Fertagus GTFS feeds.

Usage:
    python3 build/build_pt.py <cp.zip|dir> <fertagus.zip|dir> <YYYYMMDD> \
        [-o data/pt-trains.json]

CP's Alfa Pendular, Intercidades, InterRegional and Regional routes all use
GTFS route_type 2, so the displayed route short name is the classifier. CP's
route_type 109 Urbanos are regional too; Fertagus's three route_type 2 routes
are also regional. Everything else is left out.

The two feeds are merged with feed-local IDs namespaced, and stations with the
same name and rounded coordinates are emitted once. CP has no shapes.txt and
therefore uses straight lines; Fertagus's published shapes go through the
shared simplification and stop-projection pipeline.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gtfs import (Feed, active_services, enc_shape, hhmmss, load_shapes,
                        shape_track, simplify, stop_fracs)

CLASSES = ["ice", "intercity", "regional"]
SOURCE = ("CP – Comboios de Portugal (publico.cp.pt) and Fertagus "
          "(fertagus.pt), official GTFS feeds")


def route_class(route, operator):
    """Return (class, short name, append trip number) for a kept route."""
    short = (route.get("route_short_name") or "").strip()
    if not short:
        return None
    try:
        route_type = int(route.get("route_type") or -1)
    except (TypeError, ValueError):
        return None
    if route_type not in (2, 109):
        return None

    token = short.split(None, 1)[0].upper()
    if route_type == 109:
        return "regional", short, False
    if token == "AP":
        return "ice", short, True
    if token in ("IC", "IR"):
        return "intercity", short, True
    if token == "R":
        return "regional", short, True
    if operator == "fertagus" and short == "Fertagus":
        return "regional", short, False
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cp", help="CP GTFS zip or directory")
    ap.add_argument("fertagus", help="Fertagus GTFS zip or directory")
    ap.add_argument("date", help="service date, YYYYMMDD")
    ap.add_argument("-o", "--out", default="data/pt-trains.json")
    ap.add_argument("--note", default="")
    ap.add_argument("--bbox", default="-9.6,36.8,-6.7,42.3",
                    help="a trip is kept if it calls at least once inside")
    ap.add_argument("--shape-tol", type=float, default=200.0)
    ap.add_argument("--tmp", default="/tmp/pt-shapes")
    args = ap.parse_args()
    minlon, minlat, maxlon, maxlat = (float(x) for x in args.bbox.split(","))

    sources = [("cp", args.cp), ("fertagus", args.fertagus)]
    feeds = [Feed(src) for _, src in sources]
    stops, trips = {}, {}

    for fi, (operator, src) in enumerate(sources):
        ns = f"{fi}:"
        feed = feeds[fi]
        feed_stops = 0
        for r in feed.rows("stops.txt"):
            try:
                stops[ns + r["stop_id"]] = (
                    float(r["stop_lon"]), float(r["stop_lat"]),
                    (r.get("stop_name") or "").strip())
                feed_stops += 1
            except (KeyError, TypeError, ValueError):
                continue

        routes = {}
        for r in feed.rows("routes.txt"):
            kept = route_class(r, operator)
            if kept:
                routes[r["route_id"]] = kept

        services = active_services(src, args.date)
        feed_trips = 0
        for r in feed.rows("trips.txt"):
            route_id = r.get("route_id")
            if r.get("service_id") not in services or route_id not in routes:
                continue
            cls, short, with_number = routes[route_id]
            if with_number:
                number = (r.get("trip_short_name") or "").strip()
                name = f"{short} {number}".strip() if number else short
            else:
                name = short
            trip_id = r.get("trip_id")
            if not trip_id:
                continue
            trips[ns + trip_id] = {
                "cls": cls,
                "name": name,
                "head": (r.get("trip_headsign") or "").strip(),
                "st": [],
                "shape": (ns + r["shape_id"]
                          if r.get("shape_id") else None),
            }
            feed_trips += 1

        print(f"  {src}: {feed_stops} stops, {len(routes)} rail routes, "
              f"{len(services)} active services, {feed_trips} active trips")

        for r in feed.rows("stop_times.txt"):
            t = trips.get(ns + (r.get("trip_id") or ""))
            sid = ns + (r.get("stop_id") or "")
            if t is None or sid not in stops:
                continue
            arr = hhmmss(r.get("arrival_time") or "")
            dep = hhmmss(r.get("departure_time") or "")
            if arr is None and dep is None:
                continue
            arr = arr if arr is not None else dep
            dep = dep if dep is not None else arr
            try:
                sequence = int(r["stop_sequence"])
            except (KeyError, TypeError, ValueError):
                continue
            t["st"].append((sequence, sid, arr, dep))

    print(f"stops: {len(stops)}")
    print(f"candidate trips: {len(trips)}")

    tracks = {}
    if args.shape_tol > 0:
        for fi, (_, _) in enumerate(sources):
            ns = f"{fi}:"
            wanted = {t["shape"][len(ns):] for t in trips.values()
                      if t["shape"] and t["shape"].startswith(ns)}
            if not wanted:
                continue
            sdir = feeds[fi].shapes_dir(os.path.join(args.tmp, str(fi)))
            if not sdir:
                continue
            for sid, pts in load_shapes(sdir, wanted).items():
                simp = simplify(pts, args.shape_tol)
                tracks[ns + sid] = (simp, shape_track(simp))
    print(f"shapes: {len(tracks)}")

    used, order, coord_key = {}, [], {}

    def idx(sid):
        if sid in used:
            return used[sid]
        lon, lat, name = stops[sid]
        key = (name, round(lon, 3), round(lat, 3))
        if key in coord_key:
            used[sid] = coord_key[key]
        else:
            used[sid] = coord_key[key] = len(order)
            order.append(sid)
        return used[sid]

    out_shapes, shape_out_idx, frac_cache = [], {}, {}
    out_trips, counts, matched = [], {c: 0 for c in CLASSES}, 0
    for t in trips.values():
        st = sorted(t["st"])
        if len(st) < 2:
            continue
        if not any(minlon <= stops[s][0] <= maxlon
                   and minlat <= stops[s][1] <= maxlat
                   for _, s, _, _ in st):
            continue

        seq = [[idx(s), arr // 60, dep // 60]
               for _, s, arr, dep in st]
        for i in range(1, len(seq)):
            if seq[i][1] < seq[i - 1][2]:
                seq[i][1] = seq[i - 1][2]
            if seq[i][2] < seq[i][1]:
                seq[i][2] = seq[i][1]

        rec = {"c": CLASSES.index(t["cls"]), "n": t["name"],
               "h": t["head"], "s": seq}
        if t["shape"] in tracks:
            ck = (t["shape"], tuple(s for _, s, _, _ in st))
            if ck not in frac_cache:
                frac_cache[ck] = stop_fracs(
                    tracks[t["shape"]][1],
                    [stops[s][:2] for _, s, _, _ in st])
            fr = frac_cache[ck]
            if fr is not None:
                if t["shape"] not in shape_out_idx:
                    shape_out_idx[t["shape"]] = len(out_shapes)
                    out_shapes.append(enc_shape(tracks[t["shape"]][0]))
                rec["p"] = [shape_out_idx[t["shape"]], fr]
                matched += 1
        out_trips.append(rec)
        counts[t["cls"]] += 1

    stations = [[round(stops[s][0], 4), round(stops[s][1], 4), stops[s][2]]
                for s in order]
    d = datetime.date(int(args.date[:4]), int(args.date[4:6]),
                      int(args.date[6:]))
    doc = {
        "tunit": "min",
        "date": d.isoformat(),
        "weekday": d.strftime("%A"),
        "classes": CLASSES,
        "counts": counts,
        "source": SOURCE,
        "note": args.note,
        "stations": stations,
        "trips": out_trips,
    }
    if out_shapes:
        doc["shapes"] = out_shapes

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)

    print(f"{args.out}: {len(out_trips)} trips, {len(stations)} stations, "
          f"{len(out_shapes)} shapes ({matched} on tracks), "
          f"{os.path.getsize(args.out)/1e6:.2f} MB")
    for c in CLASSES:
        print(f"  {c:<10} {counts[c]}")


if __name__ == "__main__":
    main()
