#!/usr/bin/env python3
"""One day of Romanian rail, from the national GTFS conversion.

Usage:
    python3 build/build_ro.py <gtfs.zip|dir> <YYYYMMDD> [-o data/ro-trains.json]

The feed covers the national mainline operators. Classification is purely
by GTFS route_type, because the Romanian line names do not fit the German
name-based rules in build_gtfs.py:

  intercity  102 IC + 103 IR
  night      105 IR-N
  regional   106 R, R-E, R-M

Rail-replacement buses (route_type 3) and every other route type are left
out. The feed has no trip headsigns, so each hover destination is the name
of the final timed stop.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_gtfs import (Feed, hhmmss, load_shapes, simplify, shape_track,
                        stop_fracs, enc_shape)

CLASSES = ["intercity", "regional", "night"]
BY_TYPE = {102: "intercity", 103: "intercity", 105: "night",
           106: "regional"}
SOURCE = ("data.gov.ro / S.C. Informatică Feroviară, GTFS conversion by "
          "Jonah Brüchert (MDB mdb-3236)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gtfs")
    ap.add_argument("date")
    ap.add_argument("-o", "--out", default="data/ro-trains.json")
    ap.add_argument("--note", default="")
    ap.add_argument("--bbox", default="20.2,43.5,29.0,48.1",
                    help="a trip is kept if it calls at least once inside")
    ap.add_argument("--shape-tol", type=float, default=200.0)
    ap.add_argument("--tmp", default="/tmp/ro-shapes")
    args = ap.parse_args()
    minlon, minlat, maxlon, maxlat = (float(x) for x in args.bbox.split(","))

    feed = Feed(args.gtfs)

    stops = {}
    for r in feed.rows("stops.txt"):
        try:
            stops[r["stop_id"]] = (float(r["stop_lon"]),
                                   float(r["stop_lat"]),
                                   (r.get("stop_name") or "").strip())
        except (ValueError, KeyError):
            continue
    print(f"stops: {len(stops)}")

    routes = {}
    for r in feed.rows("routes.txt"):
        try:
            rt = int(r.get("route_type") or -1)
        except ValueError:
            continue
        cls = BY_TYPE.get(rt)
        if cls:
            routes[r["route_id"]] = (
                cls, (r.get("route_short_name")
                      or r.get("route_long_name") or "").strip())
    print(f"rail routes: {len(routes)}")

    svc = feed.active_services(args.date)
    print(f"services active on {args.date}: {len(svc)}")

    trips = {}
    for r in feed.rows("trips.txt"):
        if r.get("service_id") in svc and r.get("route_id") in routes:
            cls, name = routes[r["route_id"]]
            trips[r["trip_id"]] = {
                "cls": cls, "name": name, "st": [],
                "shape": r.get("shape_id") or None,
            }
    print(f"candidate trips: {len(trips)}")

    for r in feed.rows("stop_times.txt"):
        t = trips.get(r.get("trip_id", ""))
        if t is None or r.get("stop_id") not in stops:
            continue
        a, dp = hhmmss(r.get("arrival_time") or ""), hhmmss(
            r.get("departure_time") or "")
        if a is None and dp is None:
            continue
        a = a if a is not None else dp
        dp = dp if dp is not None else a
        t["st"].append((int(r["stop_sequence"]), r["stop_id"], a, dp))

    kept = []
    for t in trips.values():
        st = sorted(t["st"])
        if len(st) < 2:
            continue
        if not any(minlon <= stops[s][0] <= maxlon
                   and minlat <= stops[s][1] <= maxlat for _, s, _, _ in st):
            continue
        kept.append((t, st))
    print(f"trips inside the frame: {len(kept)}")

    tracks = {}
    if args.shape_tol > 0:
        sdir = feed.shapes_dir(args.tmp)
        wanted = {t["shape"] for t, _ in kept if t["shape"]}
        if sdir and wanted:
            for sid, pts in load_shapes(sdir, wanted).items():
                simp = simplify(pts, args.shape_tol)
                tracks[sid] = (simp, shape_track(simp))
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
    for t, st in kept:
        seq = [[idx(s), a // 60, dp // 60] for _, s, a, dp in st]
        for i in range(1, len(seq)):
            if seq[i][1] < seq[i - 1][2]:
                seq[i][1] = seq[i - 1][2]
            if seq[i][2] < seq[i][1]:
                seq[i][2] = seq[i][1]
        rec = {"c": CLASSES.index(t["cls"]), "n": t["name"],
               "h": stops[st[-1][1]][2], "s": seq}
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

    live = [c for c in CLASSES if counts[c]]
    if live != CLASSES:
        remap = {CLASSES.index(c): i for i, c in enumerate(live)}
        for rec in out_trips:
            rec["c"] = remap[rec["c"]]
        counts = {c: counts[c] for c in live}

    stations = [[round(stops[s][0], 4), round(stops[s][1], 4), stops[s][2]]
                for s in order]
    d = datetime.date(int(args.date[:4]), int(args.date[4:6]), int(args.date[6:]))
    doc = {"tunit": "min", "date": d.isoformat(),
           "weekday": d.strftime("%A"), "classes": live,
           "counts": counts, "source": SOURCE, "note": args.note,
           "stations": stations, "trips": out_trips}
    if out_shapes:
        doc["shapes"] = out_shapes
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"), ensure_ascii=False)
    print(f"{args.out}: {len(out_trips)} trips, {len(stations)} stations, "
          f"{len(out_shapes)} shapes ({matched} on tracks), "
          f"{os.path.getsize(args.out)/1e6:.2f} MB")
    for c in live:
        print(f"  {c:<9} {counts[c]}")


if __name__ == "__main__":
    main()
