#!/usr/bin/env python3
"""consolidate.py: merge the hourly logs of many homes into three tables.

    python3 consolidate.py fleet/*/*_C5.csv.gz --out fleet/

Reads any number of bfi-log.py files, from any number of homes, and writes:

    fleet_per-device-per-hour.csv   one row per (home, device, hour)  <- the analysis table
    fleet_per-device.csv            one row per (home, device)        <- the census table
    fleet_capture-health.csv        one row per (home, hour)          <- what is missing

Nothing here is clever. It is deliberately one pass, one schema, no database,
so that a student can open the result in a spreadsheet and a supervisor can
re-run the whole fleet in a minute.
"""
import argparse, collections, csv, gzip, os, statistics, sys, datetime as dt

HOURLY = ["home", "station", "hour", "requests", "reports", "rate_hz",
          "med_gap_ms", "cv_gap", "yield_pct", "legacy_pct", "rssi_med",
          "fmt_req", "std", "nr", "nc", "bw", "ng", "angle_samples"]
STATION = ["home", "station", "vendor_prefix", "randomized", "hours_seen",
           "requests", "reports", "rate_hz", "yield_pct", "legacy_pct",
           "std", "nr", "nc", "bw", "ng", "first_seen", "last_seen"]
HEALTH = ["home", "hour", "rows", "requests", "reports", "heartbeats",
          "gap_max_s", "status"]


def read(path):
    home = os.path.basename(path).split("_")[0]
    with gzip.open(path, "rt", newline="") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            rdr = csv.DictReader([line] + [l for l in fh])
            for r in rdr:
                r["home"] = home
                yield r
            break


def hour_of(ts):
    return dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()

    req = collections.defaultdict(list)     # (home, sta, hour) -> [t, rssi]
    rep = collections.defaultdict(list)
    cfg = {}
    fmtreq = {}
    beats = collections.Counter()
    rows_per = collections.Counter()
    seen = collections.defaultdict(list)

    for path in a.files:
        for r in read(path):
            home, rec, ts = r["home"], r["rec"], float(r["t"])
            h = hour_of(ts)
            rows_per[(home, h)] += 1
            if rec == "H":
                beats[(home, h)] += 1
                continue
            sta = r["sta"]
            if not sta:
                continue
            key = (home, sta, h)
            if rec == "N":
                req[key].append((ts, int(r["rssi"] or 0)))
                fmtreq[(home, sta)] = r["fmt"]
            elif rec == "R":
                rep[key].append((ts, int(r["rssi"] or 0), int(r["legacy"] or 0),
                                 bool(r["extra"])))
                if r["nr"]:
                    cfg[(home, sta)] = (r["fmt"], r["nr"], r["nc"], r["bw"], r["ng"])
            seen[(home, sta)].append(ts)

    os.makedirs(a.out, exist_ok=True)

    # ---- per (home, station, hour) ---------------------------------------
    with open(os.path.join(a.out, "fleet_per-device-per-hour.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(HOURLY)
        for key in sorted(set(req) | set(rep)):
            home, sta, h = key
            rq = sorted(t for t, _ in req.get(key, []))
            rp = rep.get(key, [])
            gaps = [(b - a_) * 1000 for a_, b in zip(rq, rq[1:])]
            span = (rq[-1] - rq[0]) if len(rq) > 1 else 0
            std, nr, nc, bw, ng = cfg.get((home, sta), ("", "", "", "", ""))
            w.writerow([
                home, sta, h, len(rq), len(rp),
                f"{len(rq)/span:.3f}" if span else "",
                f"{statistics.median(gaps):.0f}" if gaps else "",
                f"{statistics.pstdev(gaps)/statistics.mean(gaps):.2f}" if len(gaps) > 4 else "",
                f"{len(rp)/len(rq)*100:.1f}" if rq else "",
                f"{sum(x[2] for x in rp)/len(rp)*100:.0f}" if rp else "",
                statistics.median([r for _, r in req.get(key, [])]) if req.get(key) else "",
                fmtreq.get((home, sta), ""), std, nr, nc, bw, ng,
                sum(1 for x in rp if x[3]),
            ])

    # ---- per (home, station) ---------------------------------------------
    with open(os.path.join(a.out, "fleet_per-device.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(STATION)
        for (home, sta), times in sorted(seen.items()):
            rq = [k for k in req if k[0] == home and k[1] == sta]
            rp = [k for k in rep if k[0] == home and k[1] == sta]
            nreq = sum(len(req[k]) for k in rq)
            nrep = sum(len(rep[k]) for k in rp)
            allrep = [x for k in rp for x in rep[k]]
            span = max(times) - min(times)
            std, nr, nc, bw, ng = cfg.get((home, sta), ("", "", "", "", ""))
            w.writerow([
                home, sta, sta.split("-")[0], int(sta.startswith("rand")),
                len({hour_of(t) for t in times}), nreq, nrep,
                f"{nreq/span:.3f}" if span else "",
                f"{nrep/nreq*100:.1f}" if nreq else "",
                f"{sum(x[2] for x in allrep)/len(allrep)*100:.0f}" if allrep else "",
                std, nr, nc, bw, ng,
                dt.datetime.fromtimestamp(min(times)).isoformat(timespec="seconds"),
                dt.datetime.fromtimestamp(max(times)).isoformat(timespec="seconds"),
            ])

    # ---- what is missing --------------------------------------------------
    with open(os.path.join(a.out, "fleet_capture-health.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(HEALTH)
        hours = collections.defaultdict(list)
        for (home, sta, h) in set(req) | set(rep):
            hours[(home, h)].append((sta))
        for (home, h) in sorted(set(rows_per)):
            nreq = sum(len(v) for k, v in req.items() if k[0] == home and k[2] == h)
            nrep = sum(len(v) for k, v in rep.items() if k[0] == home and k[2] == h)
            ts = sorted(t for k, v in req.items() if k[0] == home and k[2] == h
                        for t, _ in v)
            gap = max((b - a_ for a_, b in zip(ts, ts[1:])), default=0)
            status = ("no requests" if not nreq else
                      "gap > 5 min" if gap > 300 else
                      "few heartbeats" if beats[(home, h)] < 50 else "ok")
            w.writerow([home, h, rows_per[(home, h)], nreq, nrep,
                        beats[(home, h)], f"{gap:.0f}", status])

    print("wrote fleet_per-device-per-hour.csv, fleet_per-device.csv, "
          "fleet_capture-health.csv to", a.out)


if __name__ == "__main__":
    sys.exit(main())
