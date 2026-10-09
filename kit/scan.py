#!/usr/bin/env python3
"""List the Wi-Fi networks around you, and profile one of them.

    python kit/scan.py
    python kit/scan.py --profile "My Network"

The board only listens. It never transmits and never joins a network.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
ROW = re.compile(r"^(.*?)\s{2,}(2\.4 GHz|5 GHz)\s+(\d+)\s+(\d+) MHz\s+(-?\d+) dBm\s+(\S+)\s*$")


def networks(port):
    """run the scan and return the parsed rows, printing what the user sees"""
    out = subprocess.run([PY, os.path.join(ROOT, "logger", "bfi-log.py"),
                          "--list-networks", "--port", port],
                         capture_output=True, text=True).stdout
    print(out)
    rows = []
    for line in out.splitlines():
        m = ROW.match(line.rstrip())
        if m:
            rows.append(dict(ssid=m.group(1).strip(), band=m.group(2),
                             channel=int(m.group(3)), width=int(m.group(4)),
                             signal=int(m.group(5)), wifi=m.group(6)))
    return rows


def profile(port, ssid, rows):
    hits = [r for r in rows if r["ssid"] == ssid]
    if not hits:
        sys.exit(f"'{ssid}' was not in the scan. Check the spelling, and use "
                 f"the name exactly as it is printed above.")
    row = sorted(hits, key=lambda r: (r["band"] != "5 GHz", -r["signal"]))[0]
    print(f"profiling '{ssid}' on channel {row['channel']} ({row['band']})\n")
    # The firmware dumps the next N beacons from *any* network on the channel,
    # because the router's address is not known yet. On a busy channel the
    # neighbours announce several times per second, so the budget has to be
    # large enough that the wanted network's beacon lands in it; one retry
    # covers an unlucky window.
    for attempt in (1, 2):
        with tempfile.TemporaryDirectory() as tmp:
            dump = os.path.join(tmp, "beacon.txt")
            subprocess.run([PY, os.path.join(HERE, "lib", "probe.py"),
                            str(row["channel"]), "25", "--dump", "--beacon", "60",
                            "--port", port, "--out", dump],
                           capture_output=True, text=True)
            if not os.path.exists(dump) or os.path.getsize(dump) == 0:
                sys.exit("nothing was recorded. Is the board plugged in?")
            r = subprocess.run([PY, os.path.join(HERE, "lib", "router_info.py"), dump],
                               capture_output=True, text=True)
        section = only_network(r.stdout or "", ssid)
        if section:
            print(section)
            return
        if attempt == 1:
            print("the network's announcement was not among the captured "
                  "beacons (the neighbours' were). Trying once more ...\n")
    print(r.stdout or r.stderr)
    sys.exit(f"'{ssid}' announced nothing in two windows. Move the board "
             f"closer to the router and try again.")


def only_network(text, ssid):
    """the sections of router_info.py output that belong to one network"""
    out, keep = [], False
    for line in text.splitlines():
        if line.startswith("=== "):
            keep = line[4:].split("   (")[0].strip() == ssid
        if keep:
            out.append(line)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", metavar="NETWORK",
                    help="read one announcement from this network and print "
                         "what the router says about itself")
    ap.add_argument("--port", default="/dev/ttyACM0")
    a = ap.parse_args()
    rows = networks(a.port)
    if a.profile:
        profile(a.port, a.profile, rows)


if __name__ == "__main__":
    main()
