#!/usr/bin/env python3
"""Per-device metrics for one or more C5 captures, side by side.

Reads capture files written by probe.py (F lines for sounding frames, A lines
for the per-second activity counters) and prints, per capture and per device:
how often it is sounded, how regular that is, whether the reply is readable,
and how busy the device was. This is the metrics table of the class brief,
computed from a real capture.

Usage:  compare_runs.py CAPTURE.txt [CAPTURE.txt ...]
"""
import statistics as st
import sys
import os

from openany import opentext


def mac(h):
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def load(path):
    """requests per target, reports per sender, activity per device, and per
    sender the number of reports cut short: the board stores at most a fixed
    number of bytes per frame, and a report longer than that cannot be read"""
    req, rep, act, fmt, cut = {}, {}, {}, {}, {}
    trig = 0
    for line in opentext(path):
        p = line.split()
        if not p:
            continue
        if p[0] == "F" and len(p) > 8:
            t = int(p[1]) / 1e6
            try:
                fr = bytes.fromhex(p[8])
            except ValueError:
                continue
            if p[2] == "1" and p[3] == "5" and len(fr) >= 10:       # NDPA
                req.setdefault(mac(fr[4:10].hex()), []).append(t)
            elif p[2] == "0" and p[3] == "14" and len(fr) >= 25:    # report
                src = mac(fr[10:16].hex())
                rep.setdefault(src, []).append(t)
                fmt[src] = {21: "VHT", 30: "HE", 36: "EHT"}.get(fr[24], fr[24])
                if len(fr) < int(p[5]):          # p[5]: length on air
                    cut[src] = cut.get(src, 0) + 1
            elif p[2] == "1" and p[3] == "2":
                trig += 1
        elif p[0] == "A" and len(p) > 10:
            d = act.setdefault(p[2], [0, 0, 0, 0])
            d[0] += 1                       # seconds with any activity
            d[1] += int(p[7])               # acks addressed to it
            d[2] += int(p[8])               # block acks it sent
            d[3] += int(p[9])               # block acks it received
    return req, rep, act, fmt, trig, cut


def cadence(ts):
    if len(ts) < 3:
        return None
    g = sorted(b - a for a, b in zip(ts, ts[1:]))
    mean = st.mean(g)
    return {"median": st.median(g), "p90": g[int(0.9 * len(g))], "max": g[-1],
            "cv": st.pstdev(g) / mean if mean else 0}


def main(paths):
    for path in paths:
        req, rep, act, fmt, trig, _cut = load(path)
        if not req and not rep:
            print(f"\n=== {os.path.basename(path)}: no sounding frames")
            continue
        allt = [t for v in req.values() for t in v] or [t for v in rep.values() for t in v]
        span = max(allt) - min(allt)
        nreq = sum(len(v) for v in req.values())
        nrep = sum(len(v) for v in rep.values())
        print(f"\n=== {os.path.basename(path)}")
        print(f"    {span/60:.1f} min | requests {nreq} ({nreq/span:.2f}/s) | "
              f"reports {nrep} ({nrep/span:.2f}/s) | triggers {trig} | "
              f"devices sounded {len([k for k in req if not k.startswith('ff')])}")
        hdr = (f"    {'device':18} {'req':>6} {'req/s':>6} {'rep':>6} {'rep/s':>6} "
               f"{'from':>4} {'gap med':>8} {'gap p90':>8} {'CV':>6} {'fmt':>4} "
               f"{'acks/s':>7} {'ba/s':>7}")
        print(hdr)
        keys = sorted(set(req) | set(rep), key=lambda k: -(len(req.get(k, [])) + len(rep.get(k, []))))
        for k in keys:
            r, p = req.get(k, []), rep.get(k, [])
            # cadence comes from the requests when they are audible, otherwise
            # from the replies, which put a floor under the true sounding rate
            src, ts = ("req", r) if len(r) >= 3 else ("rep", p)
            c = cadence(ts)
            a = act.get(k)
            g = (f"{c['median']*1000:7.0f}m {c['p90']*1000:7.0f}m {c['cv']:6.2f}"
                 if c else f"{'-':>8} {'-':>8} {'-':>6}")
            print(f"    {k:18} {len(r):6d} {len(r)/span:6.2f} {len(p):6d} {len(p)/span:6.2f} "
                  f"{src:>4} {g} {str(fmt.get(k,'-')):>4} "
                  f"{(a[1]/span if a else 0):7.1f} {((a[2]+a[3])/span if a else 0):7.1f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
