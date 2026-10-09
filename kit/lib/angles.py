#!/usr/bin/env python3
"""Unpack the Givens angles from captured compressed beamforming reports.

Handles VHT (action category 21) and HE (category 30). They differ in the
MIMO Control field and in how many subcarriers carry feedback; the angles
themselves are packed and quantised the same way.

The question this answers is not "did a report arrive" but "are the angles in
it readable". Counting reports only needs the frame header; using them for
sensing needs every quantised angle, in order, for every subcarrier.

Three checks, none of which can pass by accident:

  length    the number of angle bits implied by Nr, Nc, bandwidth, grouping
            and codebook has to match the bytes that are actually there.
  frequency a real channel is smooth across subcarriers, so neighbouring
            subcarriers must give similar angles. Mis-parsed bits look white,
            so the step between neighbours is compared against the same
            angles with the subcarrier order shuffled.
  time      soundings milliseconds apart must look alike, and soundings
            seconds apart must look less alike.

Bit order follows 802.11: the first bit of the report is the least significant
bit of the first octet, and per subcarrier the angles run
phi(i,i)..phi(Nr-1,i), psi(i+1,i)..psi(Nr,i) for i = 1..min(Nc, Nr-1).

Usage: angles.py CAPTURE [--device MAC] [--limit N] [--he]
                         [--pcap REFERENCE.pcap]
"""
import argparse
import collections
import math
import random
import statistics
import struct
import subprocess
import sys
import tempfile

CB_VHT = {0: {0: (4, 2), 1: (6, 4)}, 1: {0: (7, 5), 1: (9, 7)}}
NG_VHT = {0: 1, 1: 2, 2: 4}
NS_VHT = {0: {1: 52,  2: 30,  4: 16},
          1: {1: 108, 2: 58,  4: 30},
          2: {1: 234, 2: 122, 4: 62},
          3: {1: 468, 2: 244, 4: 124}}
# HE feeds back on more subcarriers than VHT and groups them differently.
# Both entries that appear in our captures are confirmed against the frame
# lengths: 20 MHz with Ng=4 lands on exactly 64, 80 MHz on 250.
NS_HE = {0: {4: 64,  16: 20},
         1: {4: 122, 16: 32},
         2: {4: 250, 16: 64},
         3: {4: 500, 16: 160}}
BW = {0: "20MHz", 1: "40MHz", 2: "80MHz", 3: "160MHz"}


class Bits:
    """LSB-first bit reader, the order the standard packs these fields in."""

    def __init__(self, data):
        self.d, self.pos = data, 0

    def read(self, n):
        v = 0
        for i in range(n):
            byte, bit = divmod(self.pos + i, 8)
            v |= ((self.d[byte] >> bit) & 1) << i
        self.pos += n
        return v

    def left(self):
        return len(self.d) * 8 - self.pos


def angle_plan(nr, nc):
    """(kind, i, l) for each angle of one subcarrier, in transmission order."""
    plan = []
    for i in range(1, min(nc, nr - 1) + 1):
        plan += [("phi", i, l) for l in range(i, nr)]
        plan += [("psi", i, l) for l in range(i + 1, nr + 1)]
    return plan


def parse(payload):
    """One compressed beamforming report -> header, SNRs, angles.

    VHT (category 21) and HE (category 30) differ in the MIMO Control field,
    which is 3 octets against 5, and in how many subcarriers carry feedback.
    Everything after that, including the order and quantisation of the angles,
    is the same, so both go down the same path.
    """
    if len(payload) < 32 or payload[25] != 0:
        return None
    cat = payload[24]
    if cat == 21:
        v = int.from_bytes(payload[26:29], "little")
        nc, nr = (v & 0x7) + 1, ((v >> 3) & 0x7) + 1
        bw = (v >> 6) & 0x3
        ng, cb, ft = NG_VHT[(v >> 8) & 0x3], (v >> 10) & 0x1, (v >> 11) & 0x1
        token, ru = (v >> 18) & 0x3F, None
        head = 29
        gen = "VHT"
    elif cat == 30:
        # HE MIMO Control: Nc(3) Nr(3) BW(2) Grouping(1) Codebook(1)
        # FeedbackType(2) RemainingSegments(3) FirstSegment(1)
        # RuStart(7) RuEnd(7) DialogToken(6)
        v = int.from_bytes(payload[26:31], "little")
        nc, nr = (v & 0x7) + 1, ((v >> 3) & 0x7) + 1
        bw = (v >> 6) & 0x3
        ng = 4 if ((v >> 8) & 1) == 0 else 16
        cb, ft = (v >> 9) & 0x1, (v >> 10) & 0x3
        token = (v >> 30) & 0x3F
        ru = ((v >> 16) & 0x7F, (v >> 23) & 0x7F)
        head = 31
        gen = "HE"
    else:
        return None

    bphi, bpsi = CB_VHT[1 if ft else 0][cb]
    ns = NS_VHT[bw][ng] if cat == 21 else NS_HE[bw][ng]
    plan = angle_plan(nr, nc)
    need = ns * sum(bphi if k == "phi" else bpsi for k, _, _ in plan)

    snr = [22 + struct.unpack("b", payload[head + i:head + 1 + i])[0] / 4
           for i in range(nc)]
    body = payload[head + nc:-4]                    # angles, minus the FCS
    if len(body) * 8 < need:
        return None

    b = Bits(body)
    sub = []
    for _ in range(ns):
        one = []
        for kind, _, _ in plan:
            if kind == "phi":
                k = b.read(bphi)
                one.append(k * math.pi / 2 ** (bphi - 1) + math.pi / 2 ** bphi)
            else:
                k = b.read(bpsi)
                one.append(k * math.pi / 2 ** (bpsi + 1) + math.pi / 2 ** (bpsi + 2))
        sub.append(one)
    return dict(gen=gen, nr=nr, nc=nc, bw=bw, ng=ng, cb=cb, ft=ft, token=token,
                ru=ru, bphi=bphi, bpsi=bpsi, ns=ns, snr=snr, plan=plan, sub=sub,
                need_bits=need, have_bits=len(body) * 8, seq=None)


def records(path, device=None, limit=None):
    out = []
    for line in open(path, errors="replace"):
        if not line.startswith("F "):
            continue
        p = line.split()
        if len(p) < 9 or p[2] != "0" or p[3] != "14":
            continue
        try:
            pay = bytes.fromhex(p[8])
        except ValueError:
            continue           # a line clipped by the USB console, skip it
        mac = ":".join(f"{x:02x}" for x in pay[10:16])
        if device and mac != device:
            continue
        r = parse(pay)
        if not r:
            continue
        r["us"], r["mac"] = int(p[1]), mac
        r["seq"] = (pay[22] | (pay[23] << 8)) >> 4
        r["raw"] = pay
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def circ_step(a, b, period):
    d = abs(a - b) % period
    return min(d, period - d)


def frequency_test(r):
    """mean step between neighbouring subcarriers, against a shuffled control"""
    real, ctrl = [], []
    order = list(range(r["ns"]))
    shuffled = order[:]
    random.shuffle(shuffled)
    for j, (kind, _, _) in enumerate(r["plan"]):
        period = 2 * math.pi if kind == "phi" else math.pi / 2
        col = [r["sub"][i][j] for i in order]
        real += [circ_step(col[i], col[i + 1], period) for i in range(len(col) - 1)]
        mix = [r["sub"][i][j] for i in shuffled]
        ctrl += [circ_step(mix[i], mix[i + 1], period) for i in range(len(mix) - 1)]
    return statistics.mean(real), statistics.mean(ctrl)


def distance(a, b):
    """mean angle distance between two reports of the same shape"""
    if a["ns"] != b["ns"] or a["plan"] != b["plan"]:
        return None
    d = []
    for i in range(a["ns"]):
        for j, (kind, _, _) in enumerate(a["plan"]):
            period = 2 * math.pi if kind == "phi" else math.pi / 2
            d.append(circ_step(a["sub"][i][j], b["sub"][i][j], period))
    return statistics.mean(d)


def pcap_reports(path):
    """BF report frames from a reference capture, keyed by (source, sequence)."""
    with tempfile.NamedTemporaryFile(suffix=".pcap") as tmp:
        subprocess.run(["tshark", "-r", path, "-Y",
                        "wlan.fc.type_subtype==0x000e", "-F", "pcap", "-w", tmp.name],
                       check=True, capture_output=True)
        blob = open(tmp.name, "rb").read()
    magic = blob[:4]
    if magic not in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        sys.exit("unexpected pcap byte order")
    nano = magic == b"\x4d\x3c\xb2\xa1"
    link = struct.unpack("<I", blob[20:24])[0]
    out, off = [], 24
    while off + 16 <= len(blob):
        ts, tus, caplen, _ = struct.unpack("<IIII", blob[off:off + 16])
        frame = blob[off + 16:off + 16 + caplen]
        off += 16 + caplen
        if link == 127:                      # radiotap, skip its header
            if len(frame) < 4:
                continue
            frame = frame[struct.unpack("<H", frame[2:4])[0]:]
        if len(frame) < 30:
            continue
        sa = ":".join(f"{b:02x}" for b in frame[10:16])
        seq = (frame[22] | (frame[23] << 8)) >> 4
        out.append((frame, ts + (tus / 1e9 if nano else tus / 1e6)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--device")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--pcap")
    ap.add_argument("--he", action="store_true",
                    help="look at the HE reports rather than the VHT ones")
    a = ap.parse_args()

    rs = records(a.capture, a.device, a.limit)
    if not rs:
        sys.exit("no readable beamforming reports in this capture. Reports "
                 "longer than the board can store are cut short and cannot be "
                 "read; kit/summary.py shows which devices that affects.")
    kinds = collections.Counter(x["gen"] for x in rs)
    print(f"{len(rs)} reports parsed from {a.capture.split('/')[-1]}"
          f"  ({', '.join(f'{v} {k}' for k, v in kinds.most_common())})")

    if a.he:
        rs = [x for x in rs if x["gen"] == "HE"]
        if not rs:
            sys.exit("no readable Wi-Fi 6 reports in this capture (see "
                     "kit/summary.py: they may be too long for the board)")
    r = rs[0]
    print(f"\n-- what one report contains --")
    print(f"   {r['gen']} Nr={r['nr']} Nc={r['nc']} {BW[r['bw']]} Ng={r['ng']} "
          f"codebook={r['cb']} ({r['bphi']} bits per phi, {r['bpsi']} per psi)"
          + (f" RU {r['ru'][0]}-{r['ru'][1]}" if r["ru"] else ""))
    print(f"   {r['ns']} subcarriers x {len(r['plan'])} angles "
          f"= {r['ns']*len(r['plan'])} angles, {r['need_bits']} bits, "
          f"{r['have_bits']} present")
    print(f"   average SNR per stream: "
          + ", ".join(f"{s:.2f} dB" for s in r["snr"]))
    names = ", ".join(f"{k}{l}{i}" for k, i, l in r["plan"])
    print(f"   angle order: {names}")
    for i in (0, 1, 2, r["ns"] // 2, r["ns"] - 1):
        vals = "  ".join(f"{math.degrees(v):7.1f}" for v in r["sub"][i])
        print(f"   subcarrier {i:3d}: {vals}  (degrees)")

    ok = sum(1 for x in rs if x["need_bits"] <= x["have_bits"])
    print(f"\n-- length check --\n   {ok}/{len(rs)} reports carry at least the "
          f"bits their header promises")

    real, ctrl = [], []
    for x in rs[:80]:
        a1, a2 = frequency_test(x)
        real.append(a1)
        ctrl.append(a2)
    rm, cm = statistics.mean(real), statistics.mean(ctrl)
    print(f"\n-- frequency smoothness --")
    print(f"   neighbouring subcarriers differ by {math.degrees(rm):5.1f} deg on average")
    print(f"   shuffled subcarriers differ by     {math.degrees(cm):5.1f} deg")
    print(f"   ratio {rm/cm:.3f}" + ("  (smooth: the angles track a channel)"
                                     if rm / cm < 0.7 else
                                     "  (no structure: parsing is wrong)"))

    same = [x for x in rs if x["plan"] == rs[0]["plan"] and x["ns"] == rs[0]["ns"]]
    near, far = [], []
    for i in range(len(same) - 1):
        d = distance(same[i], same[i + 1])
        gap = (same[i + 1]["us"] - same[i]["us"]) / 1e6
        (near if gap < 0.5 else far).append(d)
    print(f"\n-- stability in time --")
    if near:
        print(f"   {len(near):4d} pairs less than 0.5 s apart: "
              f"{math.degrees(statistics.mean(near)):5.1f} deg apart")
    if far:
        print(f"   {len(far):4d} pairs more than 0.5 s apart: "
              f"{math.degrees(statistics.mean(far)):5.1f} deg apart")
    pairs = [distance(random.choice(same), random.choice(same)) for _ in range(200)]
    print(f"    200 random pairs from the whole capture: "
          f"{math.degrees(statistics.mean(pairs)):5.1f} deg apart")

    if a.pcap:
        # Sequence numbers wrap every 4096 frames, so they cannot identify a
        # frame across half an hour. The bodies can: the first 36 bytes of a
        # report body are unique in practice, so they locate the same frame in
        # the reference capture, and the rest is then compared byte for byte.
        # The reference was recorded with a snaplen, so the comparison runs
        # over as much of the body as that capture actually holds.
        ref = pcap_reports(a.pcap)
        idx = {}
        for frame, _ in ref:
            if len(frame) > 60:
                idx.setdefault(frame[24:60], []).append(frame)
        exact = differ = absent = 0
        overlaps = []
        for x in rs:
            cands = idx.get(x["raw"][24:60])
            if not cands:
                absent += 1
                continue
            mine = x["raw"][24:-4]
            best = max(cands, key=len)
            n = min(len(best) - 24, len(mine))
            overlaps.append(n)
            if best[24:24 + n] == mine[:n]:
                exact += 1
            else:
                differ += 1
        print(f"\n-- against the reference receiver --")
        print(f"   {len(ref)} report frames in the reference capture")
        cov = f"{min(overlaps)} to {max(overlaps)}" if overlaps else "none"
        print(f"   {exact}/{len(rs)} of the C5's reports found there and "
              f"identical byte for byte")
        print(f"   over {cov} bytes of report body, which is what the "
              f"reference's snaplen kept")
        if differ:
            print(f"   {differ} located but with differing bytes")
        if absent:
            print(f"   {absent} not present in the reference capture")


if __name__ == "__main__":
    main()
