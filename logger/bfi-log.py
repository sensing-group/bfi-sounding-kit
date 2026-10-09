#!/usr/bin/env python3
"""bfi-log.py: the logger every student runs. One cable, two commands.

    python3 bfi-log.py --list-networks                  # 1. which networks can the C5 hear?
    python3 bfi-log.py --home H07 --network "MyWiFi"    # 2. log your own network (Ctrl-C stops)

What it does
  * asks the ESP32-C5 to scan (listen only; it never transmits), finds the network
    you named, and parks the C5 on that network's channel;
  * keeps only frames from YOUR access point; neighbours' networks are dropped;
  * writes one compact CSV row per sounding request and per report;
  * keeps the large angle payload only in sampling windows (first minute of each
    hour, or always with --angles-all), so a day costs megabytes, not gigabytes;
  * starts a new gzip file every hour, so a crash costs one hour, not a day;
  * re-checks the network's channel every hour and follows it if the router moved;
  * replaces every MAC address with a keyed hash; the key never leaves this computer,
    and stored payloads have their address header removed;
  * prints a heartbeat every minute, so a dead capture is obvious at once.

Output, in --outdir (default: ./bfi-data):
    H07_2026-10-12_14h_ch100_C5.csv.gz     one per hour: home, date, hour, channel
    H07_labels.csv                         activity labels, written by --mark
    H07_PRIVATE-key_do-not-share.bin       hashing key; stays on this computer
"""
import argparse, csv, datetime as dt, gzip, hashlib, hmac, os, signal, sys, time

try:
    import serial
except ImportError:
    sys.exit("pyserial missing:  pip install pyserial")

SCHEMA = 3          # 3: stored payload starts at the category byte, no addresses
BWL = {0: "20", 1: "40", 2: "80", 3: "160"}
NGV = {0: 1, 1: 2, 2: 4}
NGH = {0: 4, 1: 16}
SCAN_BW = {1: "20", 2: "40", 3: "80", 4: "160", 5: "80+80"}
COLS = ["t", "rec", "sta", "bss", "aid", "rssi", "len", "fmt",
        "nr", "nc", "bw", "ng", "cb", "legacy", "tsf", "extra"]


# ---------------------------------------------------------------- talking to the C5
def send(ser, line, wait=0.8):
    ser.write((line + "\r\n").encode()); ser.flush(); time.sleep(wait)


def scan(ser, timeout=30):
    """Ask the C5 for a listen-only scan. Returns one dict per network heard."""
    send(ser, "dump off", 0.5)
    ser.reset_input_buffer()
    send(ser, "scan", 0)
    nets, end = [], time.time() + timeout
    while time.time() < end:
        line = ser.readline().decode("utf-8", "replace").strip()
        if not line.startswith("W "):
            continue
        p = line.split(" ", 8)
        if p[1] == "end":
            break
        if len(p) < 8:
            continue
        nets.append(dict(bssid=p[1], ch=int(p[2]), bw=SCAN_BW.get(int(p[3]), "20"),
                         rssi=int(p[5]), std=p[7], ssid=p[8] if len(p) > 8 else ""))
    return nets


def band(ch):
    return "5 GHz" if ch >= 32 else "2.4 GHz"


def list_networks(nets):
    print(f"\n{'network name':32} {'band':8} {'channel':>7} {'width':>6} {'signal':>7}  wifi")
    print("-" * 72)
    for n in sorted(nets, key=lambda n: (n["ssid"].lower(), -n["rssi"])):
        name = n["ssid"] or "(hidden)"
        print(f"{name[:32]:32} {band(n['ch']):8} {n['ch']:7d} {n['bw']+' MHz':>6} "
              f"{n['rssi']:5d} dBm  {n['std']}")
    print(f"\n{len(nets)} networks heard. Beamforming happens mostly on 5 GHz with "
          f"wifi 'ac' or 'ax'.\n")


def choose(nets, name, want_band=None):
    """Pick the channel of the named network: 5 GHz if it has one (that is where
    sounding happens), strongest signal otherwise. Returns (channel, bssids, width)."""
    mine = [n for n in nets if n["ssid"] == name]
    if want_band:
        mine = [n for n in mine if band(n["ch"]).startswith(want_band)]
    if not mine:
        return None, set(), None
    five = [n for n in mine if n["ch"] >= 32]
    best = max(five or mine, key=lambda n: n["rssi"])
    bssids = {n["bssid"] for n in mine if n["ch"] == best["ch"]}
    return best["ch"], bssids, best["bw"]


def tune(ser, ch):
    send(ser, f"ch {ch}", 1.0)
    send(ser, "dump on", 0.5)
    send(ser, "clear", 0.3)


# ---------------------------------------------------------------- privacy
def load_key(outdir, home):
    p = os.path.join(outdir, f"{home}_PRIVATE-key_do-not-share.bin")
    if not os.path.exists(p):
        with open(p, "wb") as f:
            f.write(os.urandom(32))
        os.chmod(p, 0o600)
    return open(p, "rb").read()


def pseudo(macbytes, key):
    """Keep the vendor prefix, hash the rest: 'a46b40-9f2c1a' is a stable name for
    the device across the whole capture, but it is not the device's address."""
    tag = hmac.new(key, macbytes, hashlib.sha256).hexdigest()[:6]
    return ("rand" if macbytes[0] & 0x02 else macbytes[:3].hex()) + "-" + tag


def mac(b):
    return ":".join(f"{x:02x}" for x in b)


# ---------------------------------------------------------------- frames
def parse_ndpa(pay):
    """Sounding request: which stations are asked, and (for a single station) its MAC."""
    ta, ra = pay[10:16], pay[4:10]
    he = bool(pay[16] & 2)
    body = pay[17:-4]
    step = 4 if he else 2
    infos = [body[i:i + step] for i in range(0, len(body) - step + 1, step)]
    out = []
    for inf in infos:
        v = int.from_bytes(inf, "little")
        aid, nc = (v & 0x7FF, ((v >> 29) & 7) + 1) if he else (v & 0xFFF, ((v >> 13) & 7) + 1)
        out.append((aid, nc, "HE" if he else "VHT",
                    ra if (len(infos) == 1 and ra[0] != 0xFF) else None, ta))
    return out


def parse_report(pay):
    """Beamforming report: MIMO Control says how finely the channel was quantised."""
    src, cat = pay[10:16], pay[24]
    if cat == 21:
        v = int.from_bytes(pay[26:29], "little")
        return src, ("VHT", ((v >> 3) & 7) + 1, (v & 7) + 1,
                     BWL[(v >> 6) & 3], NGV[(v >> 8) & 3], (v >> 10) & 1)
    if cat == 30:
        v = int.from_bytes(pay[26:31], "little")
        return src, ("HE", ((v >> 3) & 7) + 1, (v & 7) + 1,
                     BWL[(v >> 6) & 3], NGH[(v >> 8) & 1], (v >> 10) & 1)
    return src, (f"cat{cat}", "", "", "", "", "")


class Rotator:
    """One gzip file per hour and channel."""
    def __init__(self, outdir, home, meta):
        self.outdir, self.home, self.meta = outdir, home, meta
        self.key, self.fh, self.w = None, None, None

    def row(self, r, ch):
        key = dt.datetime.now().strftime("%Y-%m-%d_%Hh") + f"_ch{ch}"
        if key != self.key:
            self.close()
            path = os.path.join(self.outdir, f"{self.home}_{key}_C5.csv.gz")
            new = not os.path.exists(path)
            self.fh = gzip.open(path, "at", newline="", compresslevel=6)
            self.w = csv.writer(self.fh)
            if new:
                self.fh.write("# " + self.meta + "\n")
                self.w.writerow(COLS)
            self.key = key
            print(f"-> writing {os.path.basename(path)}", flush=True)
        self.w.writerow(r)

    def close(self):
        if self.fh:
            self.fh.flush(); self.fh.close(); self.fh = None


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Log Wi-Fi sounding with an ESP32-C5.")
    ap.add_argument("--list-networks", action="store_true", help="scan, list networks, exit")
    ap.add_argument("--home", help="your anonymous home ID, e.g. H07")
    ap.add_argument("--network", help="the name (SSID) of YOUR Wi-Fi network")
    ap.add_argument("--band", choices=["2.4", "5"], help="force a band if your network has both")
    ap.add_argument("--channel", type=int, help="expert: fixed channel, no network filter")
    ap.add_argument("--minutes", type=float, help="stop after this many minutes")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--outdir", default="bfi-data")
    ap.add_argument("--angle-seconds", type=int, default=60,
                    help="seconds of full angle payload kept per hour (0 = never)")
    ap.add_argument("--angles-all", action="store_true",
                    help="keep every angle payload (use for the 20-minute scripted block)")
    ap.add_argument("--mark", metavar="LABEL", help="write an activity label and exit")
    a = ap.parse_args()

    if a.mark:
        if not a.home: sys.exit("--mark needs --home")
        os.makedirs(a.outdir, exist_ok=True)
        p = os.path.join(a.outdir, f"{a.home}_labels.csv")
        new = not os.path.exists(p)
        with open(p, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["t", "time", "label"])
            w.writerow([f"{time.time():.3f}", f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", a.mark])
        print(f"label '{a.mark}' written at {dt.datetime.now():%H:%M:%S}")
        return

    try:
        ser = serial.Serial(a.port, 115200, timeout=0.5)
    except serial.SerialException as e:
        sys.exit(f"cannot open {a.port}: {e}\nIs the C5 plugged in? Is your user in the "
                 f"'dialout' group?")
    time.sleep(0.4); ser.reset_input_buffer()

    if a.list_networks:
        print("scanning (listen only, about 10 seconds) ...", flush=True)
        list_networks(scan(ser))
        return

    if not a.home or not (a.network or a.channel):
        sys.exit("to log, give --home and --network (see --list-networks for names)")

    # ---- which channel, which access point
    bssids = set()
    if a.network:
        print(f"looking for '{a.network}' (listen only) ...", flush=True)
        ch, bssids, width = choose(scan(ser), a.network, a.band)
        if not ch:
            sys.exit(f"'{a.network}' not heard. Run --list-networks and copy the name exactly.")
        print(f"found '{a.network}' on channel {ch} ({band(ch)}, {width} MHz), "
              f"access point {', '.join(sorted(bssids))}")
        if ch < 32:
            print("note: 2.4 GHz only. Sounding is rare there; a 5 GHz network is much better.")
    else:
        ch = a.channel
    tune(ser, ch)

    os.makedirs(a.outdir, exist_ok=True)
    key = load_key(a.outdir, a.home)
    meta = (f"schema={SCHEMA} home={a.home} channel={ch} "
            f"start={dt.datetime.now().astimezone().isoformat(timespec='seconds')} "
            f"angle_seconds={a.angle_seconds} angles_all={int(a.angles_all)}")
    rot = Rotator(a.outdir, a.home, meta)
    running = {"go": True}
    signal.signal(signal.SIGINT, lambda *_: running.update(go=False))
    stop_at = time.time() + a.minutes * 60 if a.minutes else None

    n_req = n_rep = n_ang = n_other = 0
    last_beat = last_scan = time.time()
    last_total, quiet_warned = None, False
    print(f"logging as {a.home}. Ctrl-C to stop.\n", flush=True)

    def mine(b):             # b: 6 bytes; the TA may carry the bandwidth-signalling bit
        return not bssids or mac(bytes([b[0] & 0xFE]) + b[1:]) in bssids

    while running["go"] and not (stop_at and time.time() > stop_at):
        line = ser.readline().decode("utf-8", "replace").strip()
        now = time.time()

        if line.startswith("S ch="):                      # radio's own frame counter
            total = int(line.split("total=")[1].split()[0])
            if last_total is not None and total == last_total:
                print("!! the radio heard nothing for a minute; re-tuning", flush=True)
                tune(ser, ch)
            last_total = total

        elif line.startswith("F "):
            p = line.split()
            if len(p) >= 9:
                tsf, typ, sub = int(p[1]), int(p[2]), int(p[3])
                rssi, slen = int(p[4]), int(p[5])
                rx, pay = bytes.fromhex(p[7]), bytes.fromhex(p[8])
                ts = f"{now:.3f}"
                wnd = a.angles_all or (a.angle_seconds > 0 and
                                       dt.datetime.now().minute == 0 and
                                       dt.datetime.now().second < a.angle_seconds)
                if typ == 1 and sub == 5 and len(pay) >= 21:
                    if not mine(pay[10:16]):
                        n_other += 1
                    else:
                        for aid, nc, fmt, ra, ta in parse_ndpa(pay):
                            rot.row([ts, "N", pseudo(ra, key) if ra else "", pseudo(ta, key),
                                     aid, rssi, slen, fmt, "", nc, "", "", "", "", tsf, ""], ch)
                            n_req += 1
                elif typ == 0 and sub == 14 and len(pay) >= 32:
                    if not mine(pay[4:10]):
                        n_other += 1
                    else:
                        src, (std, nr, nc, bw, ng, cb) = parse_report(pay)
                        legacy = int(int.from_bytes(rx[4:8], "little") == 0)
                        # keep the report body only: category byte onwards. The first
                        # 24 bytes are the frame header with the raw MAC addresses.
                        keep = pay[24:].hex() if wnd else ""
                        n_ang += bool(keep)
                        rot.row([ts, "R", pseudo(src, key), "", "", rssi, slen,
                                 std, nr, nc, bw, ng, cb, legacy, tsf, keep], ch)
                        n_rep += 1

        if now - last_beat >= 60:                         # heartbeat
            rot.row([f"{now:.3f}", "H", "", "", "", "", "", "", "", "", "", "", "", "",
                     "", f"req={n_req} rep={n_rep} ang={n_ang} other={n_other}"], ch)
            print(f"{dt.datetime.now():%H:%M}  requests {n_req:6d}  reports {n_rep:6d}  "
                  f"angle samples {n_ang:5d}  (other networks ignored: {n_other})", flush=True)
            if n_req == 0 and now - last_scan > 120 and not quiet_warned:
                if n_rep:
                    print("   replies but no requests: your router most likely sends its "
                          "requests in a wide frame the board cannot read. Keep logging; "
                          "the replies are still useful.", flush=True)
                else:
                    print("   no sounding from your network yet. Make sure a phone, TV or "
                          "laptop is streaming video over it.", flush=True)
                quiet_warned = True
            send(ser, "stats", 0)
            last_beat = now

        if a.network and now - last_scan >= 3600:        # did the router change channel?
            new_ch, new_b, _ = choose(scan(ser), a.network, a.band)
            if new_ch and new_ch != ch:
                print(f"router moved: channel {ch} -> {new_ch}", flush=True)
                ch, bssids = new_ch, new_b
            tune(ser, ch)
            last_scan = now

    rot.close(); ser.close()
    print(f"\nstopped. requests {n_req}, reports {n_rep}, angle samples {n_ang}, "
          f"other networks ignored {n_other}. Files are in {a.outdir}/")


if __name__ == "__main__":
    main()
