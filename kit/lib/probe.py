#!/usr/bin/env python3
"""Drive the bfi-probe firmware over USB-Serial/JTAG.

Usage:
  probe.py CH DWELL [--dump] [--fail] [--data] [--bssid MAC]
                    [--port /dev/ttyACM0] [--out FILE]

Parks the C5 on a channel, waits DWELL seconds, prints the counters.
With --dump it also records every NDPA / BF-report / Trigger frame as hex
for offline decoding. --fail counts frames the radio detects but cannot
decode, --data counts data frames per device (one A line per second), and
--bssid limits the recording to one access point.

With --bssid, frames from other networks on the same channel are left out of
the recording: a sounding request is kept only if the access point sent it,
a report only if it is addressed to the access point. Activity lines of
devices seen only on other networks are left out too.

The recording also gets a clock mark every few seconds,

    T <computer time, Unix seconds> <board time, microseconds>

so the stopwatch (which runs on the computer) can be lined up with the frames
(which carry the board's time) without starting both at the same moment.
"""
import argparse
import re
import sys
import time

import serial

SYNC_EVERY_S = 5.0
FRAME = re.compile(r"F -?\d+ \d+ \d+ -?\d+ \d+ \d+ ")
GLUED = re.compile(r"(?<=[0-9a-f])(?=F -?\d+ \d+ \d+ -?\d+ \d+ \d+ [0-9a-f]{32} )")
PROMPT = re.compile(r"^(?:probe> ?)+")
ADDR_HEX = 44                      # hex digits up to the end of the third address


def parse_mac(text):
    return bytes.fromhex(text.replace(":", "").replace("-", "").lower())


class Filter:
    """Keeps the frames of one network and adds clock marks."""

    def __init__(self, bssid=None):
        self.bssid = parse_mac(bssid) if bssid else None
        self.own, self.foreign = set(), set()
        self.dropped = 0
        self.last_sync = 0.0

    def own_frame(self, typ, sub, head):
        """head: the first 22 bytes of the frame"""
        if typ == 1 and sub in (2, 5):           # Trigger, NDPA: sent by the router
            ta = bytes([head[10] & 0xFE]) + head[11:16]   # minus the bandwidth bit
            return ta == self.bssid, head[4:10]
        if typ == 0 and sub == 14:               # report: addressed to the router
            return self.bssid in (head[4:10], head[16:22]), head[10:16]
        if typ == 0 and sub == 8:                # beacon: sent by the router
            return head[16:22] == self.bssid, None
        return True, None

    def line(self, line, now):
        """returns the lines to keep for one line read from the board"""
        out = []
        for part in GLUED.split(PROMPT.sub("", line)):
            kept = self._one(part)
            if kept is not None:
                out.append(kept)
        if out and now - self.last_sync >= SYNC_EVERY_S:
            p = out[-1].split()
            if p and p[0] in ("F", "A", "Y") and len(p) > 1 and p[1].lstrip("-").isdigit():
                out.append(f"T {now:.3f} {p[1]}")
                self.last_sync = now
        return out

    def _one(self, part):
        p = part.split()
        if not p:
            return part
        if p[0] == "F" and self.bssid and len(p) >= 9 and FRAME.match(part):
            try:
                head = bytes.fromhex(p[8][:ADDR_HEX])
            except ValueError:
                return part                      # clipped line; the tools skip it
            if len(head) < 22:
                return part
            mine, dev = self.own_frame(int(p[2]), int(p[3]), head)
            if dev and dev[0] & 0x01 == 0:
                (self.own if mine else self.foreign).add(dev)
            if not mine:
                self.dropped += 1
                return None
        elif p[0] == "A" and self.bssid and len(p) > 2:
            try:
                dev = parse_mac(p[2])
            except ValueError:
                return part
            if dev in self.foreign and dev not in self.own:
                self.dropped += 1
                return None
        return part


def drain(ser, seconds, sink=None, filt=None):
    filt = filt or Filter()
    end = time.time() + seconds
    pending, kept = "", []
    while time.time() < end:
        n = ser.in_waiting
        if not n:
            time.sleep(0.02)
            continue
        pending += ser.read(n).decode("utf-8", "replace")
        *lines, pending = pending.split("\n")
        now = time.time()
        for raw in lines:
            for keep in filt.line(raw.rstrip("\r"), now):
                kept.append(keep)
                if sink:
                    sink.write(keep + "\n")
        if sink:
            sink.flush()
    if pending and sink:
        sink.write(pending + "\n")
    return "\n".join(kept)


def cmd(ser, line, wait=0.6):
    """send a console command and return the board's reply, without any frame
    or activity records that streamed in meanwhile: those are not filtered
    yet and must not end up in the log"""
    ser.write((line + "\r\n").encode())
    ser.flush()
    time.sleep(wait)
    text = ser.read(ser.in_waiting or 1).decode("utf-8", "replace")
    keep = [l for l in text.splitlines(True)
            if not re.search(r"(^|> ?)[FAYT] -?\d+ ", l)]
    return "".join(keep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("channel", type=int)
    ap.add_argument("dwell", type=float)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--fail", action="store_true")
    ap.add_argument("--data", action="store_true")
    ap.add_argument("--bssid")
    ap.add_argument("--beacon", type=int, default=0)
    ap.add_argument("--out")
    a = ap.parse_args()

    ser = serial.Serial(a.port, 115200, timeout=0.2)
    time.sleep(0.4)
    ser.reset_input_buffer()

    # the network filter first and the dump last, so nothing unfiltered streams
    print(cmd(ser, ""), end="")
    print(cmd(ser, "dump off"), end="")
    print(cmd(ser, "bssid " + (a.bssid if a.bssid else "any")), end="")
    print(cmd(ser, "fail " + ("on" if a.fail else "off")), end="")
    print(cmd(ser, "data " + ("on" if a.data else "off")), end="")
    print(cmd(ser, f"ch {a.channel}", wait=1.2), end="")
    print(cmd(ser, "clear"), end="")
    if not a.beacon:
        # a beacon request outlives the command that made it, e.g. the router
        # lookup before a recording; cancel it so no announcement is recorded
        print(cmd(ser, "beacon 0"), end="")
    ser.reset_input_buffer()
    if a.dump:
        print(cmd(ser, "dump on"), end="")

    # last, so the beacons land in the capture file and not in the command echo
    if a.beacon:
        print(cmd(ser, f"beacon {a.beacon}"), end="")

    sink = open(a.out, "a", encoding="utf-8") if a.out else None
    filt = Filter(a.bssid)
    print(f"--- dwelling {a.dwell:.0f}s on channel {a.channel} ---", flush=True)
    text = drain(ser, a.dwell, sink, filt)
    if sink:
        sink.close()

    # frame lines are noisy; summarise them rather than echoing thousands
    acts = [l for l in text.splitlines() if l.startswith("A ") or l.startswith("Y ")]
    if acts:
        print(f"[{len(acts)} activity lines]")
        for l in acts[:6]:
            print("  " + l[:160])
    frames = [l for l in text.splitlines() if l.startswith("F ")]
    if frames:
        print(f"[{len(frames)} frame records captured"
              + (f" -> {a.out}]" if a.out else " (not saved; use --out)]"))
        for l in frames[:3]:
            print("  " + l[:160] + ("..." if len(l) > 160 else ""))
    if filt.dropped:
        print(f"[{filt.dropped} records from other networks left out]")

    print(cmd(ser, "stats", wait=1.0), end="")
    ser.close()


if __name__ == "__main__":
    sys.exit(main())
