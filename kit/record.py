#!/usr/bin/env python3
"""Record one window on one channel.

    python kit/record.py --network "MyWiFi" --task baseline --id 12-345-678
    python kit/record.py --channel 36 --minutes 30 --out data/me_baseline.txt

Everything the board prints is also saved next to the recording, as
<out>.log, because the counters at the end of it are what `summary.py` reads
to tell you whether you were on the right channel.

When the router's address is known, only your own network is recorded: frames
from neighbouring networks on the same channel are left out.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
VERSION = "1.1"
FIRMWARE = "2026-10-08_bfi-probe_esp32c5.bin"

# every recording the exercise asks for, with its length in minutes. Using
# --task keeps the file names and the durations the same for everyone. The
# experiments run five blocks plus a margin for starting the stopwatch.
TASKS = {"check": 5, "baseline": 30, "experiment": None, "experiment2": None,
         "outside1": 5, "outside2": 5}
N_BLOCKS = 5
BLOCK_MARGIN_MIN = 5
STANDIN = re.compile(r"^0[23]:00:00:00:00:[0-9a-f]{2}$")
ROW = re.compile(r"^(.*?)\s{2,}(2\.4 GHz|5 GHz)\s+(\d+)\s+(\d+) MHz\s+(-?\d+) dBm\s+(\S+)\s*$")


def find_network(ssid, port):
    """turn a network name into the channel and the router's address

    The per-device traffic counts need the router's address, because a
    receipt only carries the address of the device it is meant for, not the
    network it belongs to. Nobody knows their router's address by heart, so
    it is read out of the network's own announcements.
    """
    out = subprocess.run([PY, os.path.join(ROOT, "logger", "bfi-log.py"),
                          "--list-networks", "--port", port],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        m = ROW.match(line.rstrip())
        if m and m.group(1).strip() == ssid:
            rows.append((m.group(2), int(m.group(3)), int(m.group(5))))
    if not rows:
        return None, None
    band, channel, _ = sorted(rows, key=lambda r: (r[0] != "5 GHz", -r[2]))[0]
    print(f"'{ssid}' is on channel {channel} ({band})")

    with tempfile.TemporaryDirectory() as tmp:
        dump = os.path.join(tmp, "b.txt")
        # the beacon budget is shared with every other network on the channel
        # (see kit/scan.py), so it has to outnumber the neighbours' beacons
        subprocess.run([PY, os.path.join(HERE, "lib", "probe.py"),
                        str(channel), "10", "--dump", "--beacon", "60",
                        "--port", port, "--out", dump], capture_output=True)
        router = None
        if os.path.exists(dump):
            for line in open(dump, errors="replace"):
                p = line.split()
                if len(p) < 9 or p[2] != "0" or p[3] != "8":
                    continue
                try:
                    fr = bytes.fromhex(p[8])
                except ValueError:
                    continue
                i, name = 36, None
                while i + 2 <= len(fr):
                    eid, ln = fr[i], fr[i + 1]
                    if eid == 0:
                        name = fr[i + 2:i + 2 + ln].decode("utf8", "replace")
                        break
                    i += 2 + ln
                if name == ssid:
                    router = ":".join(f"{b:02x}" for b in fr[16:22])
                    break
    if router:
        print(f"router address: {router}")
    else:
        print("could not read the router's address from its announcements, so "
              "the per-device traffic counts will be left out. Carry on.")
    return channel, router


def earlier(student, out):
    """channel and router address of this student's latest earlier recording"""
    folder = os.path.dirname(os.path.abspath(out))
    best = None
    for name in os.listdir(folder):
        if not (name.startswith(f"{student}_") and name.endswith(".meta.json")):
            continue
        try:
            with open(os.path.join(folder, name)) as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if m.get("channel") and (best is None or m.get("started", "") > best.get("started", "")):
            best = m
    if not best:
        return None, None
    router = best.get("router")
    if router and STANDIN.match(router):
        router = None                    # replaced by kit/anonymise.py already
    return best["channel"], router


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", help="your network's name, as kit/scan.py "
                                      "prints it. Finds the channel and the "
                                      "router's address for you.")
    ap.add_argument("--channel", type=int)
    ap.add_argument("--task", choices=list(TASKS),
                    help="which recording this is. Sets the file name and the "
                         "length for you.")
    ap.add_argument("--id", help="your student ID, used in the file name")
    ap.add_argument("--minutes", type=float)
    ap.add_argument("--block-minutes", type=float, default=4.0,
                    help="experiments only: length of each block (4, or 8 if "
                         "your home is slow)")
    ap.add_argument("--out")
    ap.add_argument("--router", metavar="ADDRESS",
                    help="your router's address, if you know it. Without it "
                         "the per-device traffic counts are left out.")
    ap.add_argument("--port", default="/dev/ttyACM0")
    a = ap.parse_args()
    if not a.network and not a.channel:
        sys.exit("give either --network \"My Network\" or --channel <number>")
    if a.task:
        if not a.id:
            sys.exit("--task also needs --id, for example --id 12-345-678")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", a.id):
            sys.exit("--id may contain only letters, digits, dashes and "
                     "underscores")
        if TASKS[a.task] is None:
            TASKS[a.task] = N_BLOCKS * a.block_minutes + BLOCK_MARGIN_MIN
        a.minutes = a.minutes or TASKS[a.task]
        a.out = a.out or os.path.join("data", f"{a.id}_{a.task}.txt")
    if not a.out or not a.minutes:
        sys.exit("give --task and --id, or else --out and --minutes")

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    if os.path.exists(a.out):
        sys.exit(f"{a.out} already exists. Pick another name so you do not "
                 f"lose a measurement you have already taken.")

    if a.network:
        channel, found = find_network(a.network, a.port)
        if channel is None and a.id:
            channel, found = earlier(a.id, a.out)
            if channel:
                print(f"'{a.network}' is not heard from here. Recording on "
                      f"channel {channel}, the channel of your earlier "
                      f"recordings. If nothing arrives, that is your result.")
        if channel is None:
            sys.exit(f"'{a.network}' was not heard. Run kit/scan.py to see the "
                     f"exact name, and check you are near the router.")
        a.channel = channel
        a.router = a.router or found
    if not a.router:
        print("note: without the router's address, frames from neighbouring "
              "networks on this channel cannot be left out.")

    cmd = [PY, os.path.join(HERE, "lib", "probe.py"), str(a.channel),
           str(int(a.minutes * 60)), "--dump", "--fail", "--data",
           "--port", a.port, "--out", a.out]
    if a.router:
        cmd += ["--bssid", a.router]

    meta = dict(task=a.task, student=a.id, channel=a.channel,
                router=a.router, minutes=a.minutes,
                block_minutes=a.block_minutes if a.task and a.task.startswith("experiment") else None,
                started=datetime.datetime.now().astimezone().isoformat(
                    timespec="seconds"),
                firmware=FIRMWARE, kit=VERSION)
    with open(a.out + ".meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(f"recording {a.minutes:g} minutes on channel {a.channel}")
    print("you can leave this running; press ctrl-c only if you want to stop early\n")
    with open(a.out + ".log", "w") as log:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
        for line in p.stdout:
            log.write(line)
            if line.startswith(("S ", "R ", "---", "[")):
                print(line, end="")
        p.wait()

    # A board that crashes during the recording reboots in a loop and fills
    # the file with its boot messages instead of records, while this script
    # would otherwise still report success. Catch that here, because to
    # summary.py and check.py such a file merely looks like a quiet channel.
    records = crashed = 0
    for line in open(a.out, errors="replace"):
        if line.startswith(("F ", "A ", "Y ", "T ")):
            records += 1
        elif "Guru Meditation" in line or "Rebooting..." in line:
            crashed += 1
    if crashed or not records:
        print(f"\nPROBLEM: the recording holds {records} records", end="")
        if crashed:
            print(f" and the board crashed and restarted during it "
                  f"({crashed} restart messages found)", end="")
        print(".\nUnplug the board, plug it back in, and record again. If this "
              "repeats, disable USB power saving for the board (Windows: 'USB "
              "selective suspend'; macOS: prevent sleep while recording).")
        sys.exit(1)

    print(f"\nsaved {a.out}")
    print(f"next:  python kit/summary.py {a.out}")


if __name__ == "__main__":
    main()
