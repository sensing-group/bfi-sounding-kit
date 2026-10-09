#!/usr/bin/env python3
"""Read a recording and say what is in it.

    python kit/summary.py data/me_baseline.txt
    python kit/summary.py data/me_experiment.txt --blocks data/me_blocks.csv

Prints, in this order:
  the health check    were you on the right channel, did you hear anything,
  the device table    who the router talks to, how often, how regularly,
  the traffic table   how busy each device was,
  per block           with --blocks, the same per labelled block.
"""
import argparse
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
from openany import opentext  # noqa: E402
from compare_runs import load, cadence          # noqa: E402
import timeline                                   # noqa: E402

GEN = {"VHT": "Wi-Fi 5", "HE": "Wi-Fi 6", "EHT": "Wi-Fi 7"}


def fmt_label(fmt, n_answers, n_cut):
    """what the board could do with a device's answers"""
    if not fmt:
        return "-"
    gen = GEN.get(fmt, str(fmt))
    if fmt == "EHT":
        return f"{gen} answer"
    if n_cut >= n_answers:
        return f"{gen} answer, cut short (too long for the board)"
    if n_cut:
        return f"{gen} answer, readable ({n_cut} of {n_answers} cut short)"
    return f"{gen} answer, readable"


def health(path):
    """the three things Part A asks for, read from the board's own counters"""
    log = path + ".log"
    if not os.path.exists(log) and path.endswith(".gz"):
        log = path[:-3] + ".log"          # the log is never compressed
    stats = []
    if os.path.exists(log):
        stats = [l for l in opentext(log) if l.startswith("S ")]
    beacons = questions = answers = None
    for l in stats:
        if "BEACON=" in l:
            for field in l.split("|")[-1].split():
                if field.startswith("NDPA="):
                    questions = int(field[5:])
                if field.startswith("BFRPT="):
                    answers = int(field[6:])
                if field.startswith("BEACON="):
                    beacons = int(field[7:])
    print("== health check")
    if beacons is None:
        print("   no counters found (no .log beside the recording), skipping")
        return
    print(f"   beacons heard     {beacons}")
    print(f"   questions heard   {questions}")
    print(f"   answers heard     {answers}")
    if beacons == 0 and (questions or answers):
        print("   !! zero beacons but other traffic: you are on the wrong slice")
        print("      of the channel. Run kit/scan.py again and use the channel")
        print("      it prints for your network, then record again.")
    elif beacons == 0:
        print("   !! nothing is arriving. Move the board closer to the router,")
        print("      and check you used the 5 GHz entry.")
    elif questions and answers:
        print("   -> you hear both questions and answers. Best case.")
    elif answers:
        print("   -> answers only. Your router asks in a format this board")
        print("      cannot read. Normal, carry on, and note it in your report.")
    elif questions:
        print("   -> questions only. Your devices answer in a format this")
        print("      board cannot read. Normal, carry on, and note it.")
    else:
        print("   -> beacons but no soundings. Note it; it is a real result.")
    print()


def per_device(req, rep, act, fmt, cut, span, title="== devices"):
    print(title)
    rows = sorted(set(req) | set(rep) | set(act),
                  key=lambda m: -(len(req.get(m, [])) + len(rep.get(m, []))))
    print(f"   {'device':20s} {'questions':>9s} {'answers':>8s} {'per s':>6s} "
          f"{'typical gap':>12s} {'slow gap':>9s} {'receipts':>9s}  format")
    for m in rows:
        if m.startswith(("ff:", "01:", "33:")):
            continue
        q, a = req.get(m, []), rep.get(m, [])
        ts = q if len(q) >= 3 else a
        c = cadence(sorted(ts))
        traffic = sum(act.get(m, [0, 0, 0, 0])[1:])
        rate = len(q) / span if q else (len(a) / span if a else 0)
        gap = f"{c['median']*1000:.0f} ms" if c else "-"
        p90 = f"{c['p90']*1000:.0f} ms" if c else "-"
        print(f"   {m:20s} {len(q):9d} {len(a):8d} {rate:6.2f} {gap:>12s} "
              f"{p90:>9s} {traffic:9d}  {fmt_label(fmt.get(m), len(a), cut.get(m, 0))}")
    print()


def blocks_table(capture, blocks_path, req, act, offset=0.0, device=None):
    if device:
        req = {k: v for k, v in req.items() if k == device}
        act = {k: v for k, v in act.items() if k == device}
    blocks, how, first, last = timeline.windows(blocks_path, capture, offset)
    print(f"== per block ({device if device else 'all devices together'})")
    for m in timeline.explain(blocks, how, first, last):
        print(f"   {m}")
    print(f"   {'block':10s} {'from':>7s} {'to':>7s} {'questions/s':>12s} "
          f"{'receipts/s':>11s}  note")
    origin = first or 0.0
    for b in blocks:
        s, e = b["start"], b["end"]
        dur = max(e - s, 1)
        q = sum(1 for v in req.values() for t in v if s <= t < e)
        a = 0
        for m, rows in act.items():
            a += sum(n for t, n in rows if s <= t < e)
        print(f"   {b['block']:10s} {s - origin:7.0f} {e - origin:7.0f} {q/dur:12.2f} "
              f"{a/dur:11.1f}  {b['note']}")
    print()


def activity_series(path):
    """(time, receipts) per device, from the per-second activity lines"""
    act = {}
    for line in opentext(path):
        p = line.split()
        if p and p[0] == "A" and len(p) > 10:
            t = int(p[1]) / 1e6
            act.setdefault(p[2], []).append(
                (t, int(p[7]) + int(p[8]) + int(p[9])))
    return act


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--blocks", help="the stopwatch file from kit/blocks.py")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="seconds to shift the block times by")
    ap.add_argument("--device", help="limit the per-block table to one device, "
                                     "the one you watched in Task 3")
    a = ap.parse_args()

    health(a.capture)
    req, rep, act, fmt, trig, cut = load(a.capture)
    if not req and not rep:
        print("no soundings in this recording at all.")
        return
    allt = [t for v in req.values() for t in v] + [t for v in rep.values() for t in v]
    span = max(max(allt) - min(allt), 1)
    print(f"== {os.path.basename(a.capture)}: {span/60:.1f} minutes\n")
    per_device(req, rep, act, fmt, cut, span)

    if a.blocks:
        blocks_table(a.capture, a.blocks, req, activity_series(a.capture),
                     a.offset, a.device)


if __name__ == "__main__":
    main()
