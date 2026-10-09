#!/usr/bin/env python3
"""Turn the answers one device sent into one number per block.

    python kit/motion.py data/me_experiment.txt --blocks data/me_blocks.csv

The number is how much the description of the radio path changed between one
moment and a moment ten seconds later, measured in steps of the scale the
device itself uses. A room where nothing moves gives a small number. A room
with somebody walking in it gives a larger one.

Only the answers are used, so this works even when your router's questions are
in a format the board cannot read.

Writes two pictures next to the recording: one bar per block, and the whole
session as a line with the blocks marked.
"""
import argparse
import math
import os
import statistics
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))
from openany import opentext  # noqa: E402
from angles import angle_plan, CB_VHT, NG_VHT, NS_VHT, NS_HE   # noqa: E402
import timeline                                                 # noqa: E402

LAG = 10.0


def load(path, device=None):
    """every answer as its raw angle values, plus the scale they are on.
    Times are board seconds, the scale the block windows use."""
    rows, ts, macs, geom = [], [], [], None
    cut = 0
    for line in opentext(path):
        if not line.startswith("F "):
            continue
        p = line.split()
        if len(p) < 9 or p[2] != "0" or p[3] != "14":
            continue
        try:
            pay = bytes.fromhex(p[8])
        except ValueError:
            continue
        mac = ":".join(f"{b:02x}" for b in pay[10:16])
        if device and mac != device:
            continue
        cat = pay[24]
        if cat == 21:
            v = int.from_bytes(pay[26:29], "little")
            head, ng = 29, NG_VHT[(v >> 8) & 3]
            cb, ft = (v >> 10) & 1, (v >> 11) & 1
        elif cat == 30:
            v = int.from_bytes(pay[26:31], "little")
            head, ng = 31, 4 if ((v >> 8) & 1) == 0 else 16
            cb, ft = (v >> 9) & 1, (v >> 10) & 3
        else:
            continue
        nc, nr, bw = (v & 7) + 1, ((v >> 3) & 7) + 1, (v >> 6) & 3
        key = (cat, nr, nc, bw, ng, cb, ft)
        if geom is None:
            bphi, bpsi = CB_VHT[1 if ft else 0][cb]
            ns = NS_VHT[bw][ng] if cat == 21 else NS_HE[bw][ng]
            plan = angle_plan(nr, nc)
            widths = [bphi if k == "phi" else bpsi for k, _, _ in plan]
            per_sc = sum(widths)
            off = np.cumsum([0] + widths[:-1])
            # bit positions of every angle in the report, grouped by how many
            # bits that angle uses, so each group can be gathered in one go
            groups = {}
            for k, w in enumerate(widths):
                groups.setdefault(w, []).append(k)
            take = {}
            for w, ks in groups.items():
                base = (np.arange(ns)[:, None] * per_sc +
                        np.array([off[k] for k in ks])[None, :])
                take[w] = (base.reshape(-1, 1) + np.arange(w)[None, :],
                           np.array([i * len(widths) + k
                                     for i in range(ns) for k in ks]))
            per = np.array([2 ** w for w in widths] * ns)
            geom = (key, ns, per_sc, take, per, len(widths), 360.0 / 2 ** bphi)
        elif key != geom[0]:
            continue
        _, ns, per_sc, take, per, nang, step = geom
        body = pay[head + nc:-4]
        if len(body) * 8 < ns * per_sc:
            cut += len(pay) < int(p[5])          # stored shorter than sent
            continue
        bits = np.unpackbits(np.frombuffer(body, dtype=np.uint8),
                             bitorder="little")
        vals = np.zeros(ns * nang, dtype=np.int32)
        for w, (idx, where) in take.items():
            vals[where] = (bits[idx] * (1 << np.arange(w))).sum(axis=1)
        rows.append(vals)
        ts.append(int(p[1]) / 1e6)
        macs.append(mac)
    if not rows:
        return None, cut
    return (np.array(rows), np.array(ts), geom[4], geom[6], macs), cut


def busiest(path):
    seen = {}
    for line in opentext(path):
        if line.startswith("F "):
            p = line.split()
            if len(p) > 8 and p[2] == "0" and p[3] == "14":
                try:
                    pay = bytes.fromhex(p[8])
                except ValueError:
                    continue
                m = ":".join(f"{b:02x}" for b in pay[10:16])
                seen[m] = seen.get(m, 0) + 1
    if not seen:
        sys.exit("no answers in this recording, so there is nothing to measure.")
    return max(seen, key=seen.get), seen


def separations(A, ts, per, lag, tol=0.35):
    """for each answer, how different the answer `lag` seconds later was"""
    j = np.searchsorted(ts, ts + lag)
    i = np.arange(len(ts))
    ok = j < len(ts)
    i, j = i[ok], j[ok]
    good = np.abs((ts[j] - ts[i]) - lag) <= lag * tol
    i, j = i[good], j[good]
    if len(i) == 0:
        return np.array([]), np.array([])
    d = np.abs(A[j].astype(int) - A[i].astype(int)) % per
    return ts[i], np.minimum(d, per - d).mean(axis=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--blocks", help="the stopwatch file from kit/blocks.py")
    ap.add_argument("--device", help="which device to look at (default: the "
                                     "one that answered most)")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="seconds to shift the block times by, if the "
                         "stopwatch was started after the recording")
    ap.add_argument("--lag", type=float, default=LAG,
                    help="seconds between the two answers compared")
    a = ap.parse_args()

    dev, seen = busiest(a.capture)
    if a.device:
        dev = a.device.lower()
    print(f"device: {dev}  ({seen.get(dev, 0)} answers)")
    if not a.device:
        print("note: no --device given, so this is the device that answered "
              "most. Pass --device with the device you chose in Task 3.")
    if len(seen) > 1:
        others = ", ".join(f"{m} ({n})" for m, n in
                           sorted(seen.items(), key=lambda x: -x[1])[1:4])
        print(f"others in this recording: {others}")

    got, cut = load(a.capture, dev)
    if got is None:
        if cut:
            sys.exit(f"that device's answers are longer than the board can store "
                     f"({cut} were cut short), so they cannot be read. Say so in "
                     f"your report, and choose another device if there is one.")
        sys.exit("that device's answers are in a format this tool cannot read.")
    A, ts, per, step, _ = got
    print(f"{len(A)} answers over {(ts[-1] - ts[0])/60:.1f} minutes, "
          f"{A.shape[1]} values each, one step is {step:.1f} degrees")

    t, d = separations(A, ts, per, a.lag)
    if len(d) < 10:
        sys.exit(f"only {len(d)} pairs {a.lag:.0f} s apart. Your home is slow: "
                 f"try --lag 30, or record for longer.")
    print(f"{len(d)} pairs {a.lag:.0f} s apart, "
          f"overall change {d.mean():.2f} steps\n")

    rows = []
    origin = ts[0]
    if a.blocks:
        blocks, how, first, last = timeline.windows(a.blocks, a.capture, a.offset)
        origin = first if first is not None else origin
        for m in timeline.explain(blocks, how, first, last):
            print(m)
        for b in blocks:
            s, e = b["start"], b["end"]
            sel = (t >= s) & (t < e)
            rows.append((b["block"], s - origin, e - origin, int(sel.sum()),
                         float(d[sel].mean()) if sel.sum() else float("nan"),
                         b["note"]))
        print(f"   {'block':10s} {'pairs':>6s} {'change':>8s}   note")
        for name, s, e, n, m, note in rows:
            print(f"   {name:10s} {n:6d} {m:8.2f}   {note}")
        done = [r for r in rows if r[3] > 3]
        if len(done) > 1:
            lo = min(done, key=lambda r: r[4])
            hi = max(done, key=lambda r: r[4])
            print(f"\n   quietest block: {lo[0]} at {lo[4]:.2f} steps")
            print(f"   busiest block:  {hi[0]} at {hi[4]:.2f} steps")
            print(f"   ratio: {hi[4]/lo[4]:.1f} times")

    plot(a.capture, t - origin, d, rows, step, a.lag)


def plot(path, t, d, rows, step, lag):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stem = os.path.splitext(path)[0]
    if rows:
        fig, ax = plt.subplots(figsize=(6, 3.2))
        names = [r[0] for r in rows]
        vals = [r[4] for r in rows]
        ax.bar(names, vals, color="#4477aa")
        ax.set_ylabel(f"change over {lag:.0f} s\n(steps of {step:.1f} degrees)")
        ax.set_title("how much the radio path moved, per block")
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
        fig.tight_layout()
        fig.savefig(stem + "_blocks.png", dpi=150)
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    ax.plot(t / 60, d, lw=0.9, color="#4477aa")
    for k, (name, s, e, n, m, note) in enumerate(rows):
        ax.axvspan(s / 60, e / 60, color="#dddddd" if k % 2 else "#f2f2f2",
                   zorder=0)
        ax.text((s + e) / 120, ax.get_ylim()[1] * 0.95, name, ha="center",
                va="top", fontsize=8, rotation=90)
    ax.set_xlabel("minutes into the recording")
    ax.set_ylabel(f"change over {lag:.0f} s")
    ax.grid(alpha=0.3)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(stem + "_timeline.png", dpi=150)
    plt.close(fig)
    print(f"\nwrote {os.path.basename(stem)}_blocks.png and "
          f"{os.path.basename(stem)}_timeline.png")


if __name__ == "__main__":
    main()
