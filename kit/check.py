#!/usr/bin/env python3
"""Check your recordings before you submit them.

    python kit/check.py --id 12-345-678

Looks at everything in data/ belonging to that ID and reports what is missing
or wrong. Run it before you hand in, and fix whatever it lists.
"""
import argparse
import csv
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from openany import opentext  # noqa: E402
from addresses import TEXT, STANDIN_RE, fields, base, beacons  # noqa: E402
import timeline  # noqa: E402

NEEDED = {
    "check": "the five minute test from Task 1",
    "baseline": "the half hour from Task 2",
    "experiment": "the five blocks from Task 3",
    "outside1": "the corridor recording from Task 5",
    "outside2": "the furthest position from Task 5",
}
OPTIONAL = {"experiment2": "the repeat of Task 3"}
BLOCK_FILES = {"experiment": "{id}_blocks.csv", "experiment2": "{id}_blocks2.csv"}
BLOCKS = ["idle_1", "still", "walking", "idle_2", "busy"]
MIN_BLOCK_S = 180
MAX_BLOCK_SPREAD_S = 30


def counts(path):
    """requests, reports, activity lines, clock marks and crash messages"""
    req = rep = act = marks = crashed = 0
    for line in opentext(path):
        if line.startswith("F "):
            p = line.split(None, 4)
            if len(p) > 3:
                if p[2] == "1" and p[3] == "5":
                    req += 1
                elif p[2] == "0" and p[3] == "14":
                    rep += 1
        elif line.startswith("A "):
            act += 1
        elif line.startswith("T "):
            marks += 1
        elif "Guru Meditation" in line or "Rebooting..." in line:
            crashed += 1
    return req, rep, act, marks, crashed


def leftovers(path):
    """real addresses and router announcements still in a file"""
    real, n_beacons = set(), 0
    for line in opentext(path):
        n_beacons += len(beacons(line))
        for mac in TEXT.findall(line.lower()):
            if not STANDIN_RE.match(mac) and mac != "ff:ff:ff:ff:ff:ff":
                real.add(mac)
        for addr, ta in fields(line):
            if STANDIN_RE.match(addr):
                continue
            dev = base(addr, ta)
            if dev:
                real.add(dev)
    return real, n_beacons


def check_blocks(a, task, problems):
    path = os.path.join(a.dir, BLOCK_FILES[task].format(id=a.id))
    rec = os.path.join(a.dir, f"{a.id}_{task}.txt")
    if not os.path.exists(path):
        problems.append(f"missing {os.path.basename(path)} from kit/blocks.py")
        return
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    got = [r["block"] for r in rows]
    if got != BLOCKS:
        problems.append(f"{os.path.basename(path)} has blocks {got}, expected {BLOCKS}")
        return
    lens = [float(r["end_s"]) - float(r["start_s"]) for r in rows]
    print(f"   {os.path.basename(path):16s} {', '.join(f'{l/60:.1f}' for l in lens)} minutes")
    if max(lens) - min(lens) > MAX_BLOCK_SPREAD_S:
        problems.append("the blocks have very different lengths. Did "
                        "the stopwatch run the whole time?")
    if min(lens) < MIN_BLOCK_S:
        problems.append("at least one block is under three minutes, "
                        "which is too short to analyse.")
    if os.path.exists(rec):
        blocks, how, first, last = timeline.windows(path, rec)
        if first is not None and how == "clock":
            late = [b["block"] for b in blocks if b["end"] > last + 1 or b["start"] < first - 1]
            if late:
                problems.append(f"blocks {', '.join(late)} lie outside the "
                                f"{task} recording. Record the experiment again, "
                                f"with --block-minutes matching the stopwatch.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--dir", default="data")
    a = ap.parse_args()

    problems, notes = [], []
    print(f"checking {a.dir}/ for {a.id}\n")

    tasks = dict(NEEDED)
    tasks.update({t: w for t, w in OPTIONAL.items()
                  if os.path.exists(os.path.join(a.dir, f"{a.id}_{t}.txt"))})
    for task, what in tasks.items():
        path = os.path.join(a.dir, f"{a.id}_{task}.txt")
        if not os.path.exists(path):
            problems.append(f"missing {os.path.basename(path)}: {what}")
            continue
        size = os.path.getsize(path)
        req, rep, act, marks, crashed = counts(path)
        print(f"   {task:11s} {size/1e6:6.1f} MB  "
              f"{req:6d} requests  {rep:6d} reports  {act:5d} activity lines")
        if size < 1000:
            problems.append(f"{task} is almost empty, so the board heard "
                            f"nothing. Record it again.")
        elif crashed:
            problems.append(f"{task}: the board crashed and restarted during "
                            f"this recording ({crashed} restart messages in "
                            f"the file). Its contents are not a measurement. "
                            f"Record it again.")
        elif req == 0 and rep == 0 and act == 0 and marks == 0:
            problems.append(f"{task} holds no records at all, only console "
                            f"output. The board was not capturing. Record it "
                            f"again.")
        elif req == 0 and rep == 0:
            notes.append(f"{task} has no soundings at all. That can be a real "
                         f"result, but say so in your report.")
        if not marks:
            notes.append(f"{task} has no clock marks, so it was recorded with an "
                         f"older kit. The blocks will be counted from its start.")
        if not os.path.exists(path + ".meta.json"):
            notes.append(f"{task} has no .meta.json beside it. Did you record "
                         f"it with --task?")
        if not os.path.exists(path + ".log"):
            notes.append(f"{task} has no .log beside it, so the health check "
                         f"in summary.py will not work.")

    print()
    for task in BLOCK_FILES:
        if task in tasks:
            check_blocks(a, task, problems)

    real, n_beacons = set(), 0
    for path in glob.glob(os.path.join(a.dir, f"{a.id}_*")):
        if path.endswith(".original"):
            problems.append(f"delete {os.path.basename(path)} before "
                            f"submitting")
            continue
        if path.endswith(".png"):
            continue
        r, b = leftovers(path)
        real |= r
        n_beacons += b
    if real:
        problems.append(f"{len(real)} real device addresses are still in your "
                        f"files, for example {sorted(real)[0]}. Run "
                        f"kit/anonymise.py.")
    if n_beacons:
        problems.append(f"{n_beacons} router announcements, which hold your "
                        f"network's name, are still in your files. Run "
                        f"kit/anonymise.py.")
    labels = os.path.join(a.dir, "device-labels.csv")
    if not os.path.exists(labels):
        problems.append("missing device-labels.csv from kit/anonymise.py")

    print()
    for n in notes:
        print(f"   note:    {n}")
    for p in problems:
        print(f"   PROBLEM: {p}")
    if not problems:
        print("   everything expected is here. You can submit.")
    print()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
