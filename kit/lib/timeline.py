"""Line up the stopwatch's blocks with a recording.

The frames carry the board's clock, the stopwatch runs on the computer. Since
kit 1.1 every recording holds clock marks (`T <computer time> <board time>`)
and every block file holds the computer time at which each block started, so
the two are matched directly and the stopwatch can be started at any moment
during the recording.

Older recordings or block files have no clock: the blocks are then counted
from the first record in the recording, and `--offset` shifts them.

All times returned here are board seconds, the same scale as the frames.
"""
import csv

from openany import opentext


def scan(path):
    """clock marks, and the first and last board time in a recording"""
    syncs, first, last = [], None, None
    for line in opentext(path):
        p = line.split()
        if len(p) < 3:
            continue
        if p[0] == "T":
            try:
                syncs.append((float(p[1]), int(p[2]) / 1e6))
            except ValueError:
                pass
        elif p[0] in ("F", "A", "Y"):
            try:
                t = int(p[1]) / 1e6
            except ValueError:
                continue
            first = t if first is None else min(first, t)
            last = t if last is None else max(last, t)
    return syncs, first, last


def to_board(epoch, syncs):
    host, board = min(syncs, key=lambda s: abs(s[0] - epoch))
    return board + (epoch - host)


def windows(blocks_path, capture, offset=0.0):
    """(blocks, how, first, last): each block as dict(block, start, end, note)
    in board seconds; how is "clock" or "stopwatch"."""
    syncs, first, last = scan(capture)
    with open(blocks_path, newline="") as f:
        rows = list(csv.DictReader(f))
    clock = bool(syncs) and all(r.get("start_epoch") for r in rows)
    out = []
    for r in rows:
        if clock:
            s = to_board(float(r["start_epoch"]), syncs)
            e = to_board(float(r["end_epoch"]), syncs)
        else:
            s = (first or 0.0) + float(r["start_s"])
            e = (first or 0.0) + float(r["end_s"])
        out.append(dict(block=r["block"], start=s + offset, end=e + offset,
                        note=r.get("note", "")))
    return out, ("clock" if clock else "stopwatch"), first, last


def explain(blocks, how, first, last):
    """lines to print: how the blocks were placed, and any that do not fit"""
    msg = []
    if how == "stopwatch":
        msg.append("note: no clock marks, so the blocks are counted from the "
                   "start of the recording. If the stopwatch started later, "
                   "pass --offset with the delay in seconds.")
    if first is not None:
        for b in blocks:
            if b["start"] < first - 1 or b["end"] > last + 1:
                msg.append(f"warning: block {b['block']} lies partly outside "
                           f"the recording, so its numbers cover less time.")
    return msg
