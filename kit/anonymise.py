#!/usr/bin/env python3
"""Replace device addresses before you submit your recordings.

    python kit/anonymise.py data/*                        # list the addresses
    python kit/anonymise.py data/* --labels labels.csv    # replace them

Every address is replaced by a stand-in of the same form, starting at
02:00:00:00:00:01, both where it is written as text and where it sits inside
the raw frames. The recordings stay readable by the tools, and the stand-ins
are consistent across all the files you pass in one call, so a device keeps
the same identity in every recording. Router announcements (beacons), which
carry your network's name and the router's model, are removed.

A mapping file is written next to your data listing which stand-in is which
device, using the labels you provide. Submit that file with your recordings.
Your `labels.csv` holds the real addresses, so keep it and do not submit it.

labels.csv has one line per device you recognise:

    a1:b2:c3:d4:e5:f6,desktop
    a1:b2:c3:d4:e5:aa,tablet
"""
import argparse
import csv
import glob
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from addresses import (TEXT, STANDIN, STANDIN_RE, fields, base, is_group,  # noqa: E402
                       signalled, beacons)


def is_text(path):
    with open(path, "rb") as f:
        return b"\0" not in f.read(4096)


def expand(patterns):
    """data/* also on Windows, where the shell does not expand it"""
    out = []
    for p in patterns:
        out += sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--labels", help="csv of address,label")
    ap.add_argument("--out", default=None,
                    help="where to write the mapping (default: beside the data)")
    a = ap.parse_args()

    files = []
    for f in expand(a.files):
        if (not os.path.isfile(f) or f.endswith((".original", ".png", ".gz"))
                or os.path.basename(f) == "device-labels.csv"):
            continue
        if not is_text(f):
            print(f"skipping {f}: not a text recording, so it cannot be cleaned "
                  f"here. Do not submit it.")
            continue
        files.append(f)
    if not files:
        sys.exit("no files to work on. Compressed recordings must be "
                 "uncompressed first.")

    seen = {}
    for path in files:
        with open(path, errors="replace") as f:
            for line in f:
                found = TEXT.findall(line.lower())
                found += [b for b in (base(addr, ta) for addr, ta in fields(line)) if b]
                for mac in found:
                    if STANDIN_RE.match(mac) or is_group(mac):
                        continue
                    seen[mac] = seen.get(mac, 0) + 1

    if not a.labels:
        print("addresses in these files, most frequent first:\n")
        for mac, n in sorted(seen.items(), key=lambda x: -x[1]):
            print(f"   {mac}   {n} times")
        print("\nWrite the ones you recognise into labels.csv as "
              "'address,label', then run again with --labels labels.csv")
        return 0

    labels = {}
    with open(a.labels) as f:
        for row in csv.reader(f):
            if len(row) >= 2 and TEXT.fullmatch(row[0].strip().lower()):
                labels[row[0].strip().lower()] = row[1].strip()

    order = sorted(seen, key=lambda m: -seen[m])
    mapping = {mac: STANDIN.format(i + 1) for i, mac in enumerate(order)}
    pairs = []
    for mac, standin in mapping.items():
        for real, fake in ((mac, standin), (signalled(mac), signalled(standin))):
            pairs += [(real, fake), (real.upper(), fake),
                      (real.replace(":", ""), fake.replace(":", ""))]

    for path in files:
        shutil.copy(path, path + ".original")
        kept, dropped = [], 0
        with open(path, errors="replace") as f:
            for line in f:
                spans = beacons(line)
                for s, e in reversed(spans):
                    line = line[:s] + line[e:]
                dropped += len(spans)
                if spans and not line.replace("probe>", "").strip():
                    continue
                for real, fake in pairs:
                    if real in line:
                        line = line.replace(real, fake)
                kept.append(line)
        with open(path, "w") as f:
            f.writelines(kept)
        print(f"cleaned {path}" + (f" ({dropped} router announcements removed)"
                                   if dropped else ""))

    out = a.out or os.path.join(os.path.dirname(files[0]) or ".",
                                "device-labels.csv")
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["standin", "label", "times_seen"])
        for mac in order:
            w.writerow([mapping[mac], labels.get(mac, ""), seen[mac]])
    print(f"\nwrote {out}: {len(mapping)} devices, "
          f"{sum(1 for m in order if labels.get(m))} of them labelled.")
    print("Submit that file. Keep labels.csv yourself and do not submit it.")
    print("Delete the .original files once you have checked the result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
