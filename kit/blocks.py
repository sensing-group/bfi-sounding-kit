#!/usr/bin/env python3
"""Stopwatch for the five blocks. Press enter at the start of each one.

    python kit/blocks.py --id 12-345-678
    python kit/blocks.py --id 12-345-678 --block-minutes 8
    python kit/blocks.py --id 12-345-678 --task experiment2 --block-minutes 8

Start it once the recording is running. It writes the computer time at which
each block starts and ends, and the recording holds the matching clock marks,
so kit/motion.py and kit/summary.py place the blocks exactly, however long
after the recording the stopwatch was started.
"""
import argparse
import csv
import os
import sys
import time

BLOCKS = [
    ("idle_1", "nobody in the room"),
    ("still", "one person sitting as still as is comfortable"),
    ("walking", "one person walking slowly around the room"),
    ("idle_2", "nobody in the room, repeat of idle_1"),
    ("busy", "nobody in the room, chosen device downloading"),
]
FILES = {"experiment": "{id}_blocks.csv", "experiment2": "{id}_blocks2.csv"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="your student ID. Names the file for you.")
    ap.add_argument("--task", choices=list(FILES), default="experiment",
                    help="which experiment recording these blocks belong to")
    ap.add_argument("--out")
    ap.add_argument("--block-minutes", "--minutes", dest="minutes", type=float,
                    default=4.0, help="length of each block (8 if your home is slow)")
    a = ap.parse_args()
    if not a.out:
        if not a.id:
            sys.exit("give --id <your student ID>, or --out <file>")
        a.out = os.path.join("data", FILES[a.task].format(id=a.id))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    if os.path.exists(a.out):
        sys.exit(f"{a.out} already exists. A repeat of the experiment goes in "
                 f"--task experiment2; otherwise rename the old file first.")

    print("Start this once the recording is running.\n")
    input("press enter to begin ...")
    t0 = time.time()
    rows = []
    for i, (name, what) in enumerate(BLOCKS, 1):
        start = time.time()
        print(f"\nblock {i} of {len(BLOCKS)}: {name.upper()}  -  {what}")
        print(f"   {a.minutes:g} minutes, starting now")
        end = start + a.minutes * 60
        while time.time() < end:
            left = end - time.time()
            print(f"\r   {int(left)//60}:{int(left)%60:02d} left   ",
                  end="", flush=True)
            time.sleep(min(1.0, max(left, 0.01)))
        stop = time.time()
        rows.append(dict(block=name, start_s=round(start - t0),
                         end_s=round(stop - t0), note=what,
                         start_epoch=f"{start:.3f}", end_epoch=f"{stop:.3f}"))
        print("\r   done                 ")

    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, ["block", "start_s", "end_s", "note",
                               "start_epoch", "end_epoch"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {a.out}. You can stop the recording now.")


if __name__ == "__main__":
    main()
