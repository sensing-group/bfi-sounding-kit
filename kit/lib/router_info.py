#!/usr/bin/env python3
"""Read a router's identity and sounding capabilities out of a dumped beacon.

Input is a capture file from probe.py that contains beacon records, produced by
the firmware's `beacon <n>` command with `dump on`. Nothing here talks to the
radio, it only parses bytes we already have.

What it answers:
  name        SSID, and the make and model when the router puts a WPS element
              in its beacon (most consumer boxes do)
  type        which generations it advertises, how wide it runs, its vendor
              elements, the OUI of its own address
  capability  the parts that decide how it sounds: whether it is an SU or MU
              beamformer, how many sounding dimensions it has, how many spatial
              streams it supports

Usage:  router_info.py CAPTURE.txt [more.txt ...]
"""
import sys

# OUIs seen in these captures. Not a full list, just enough to name the common
# ones without shipping a 3 MB registry.
OUI = {
    "000c43": "MediaTek/Ralink", "00904c": "Broadcom (Epigram)",
    "001018": "Broadcom", "0050f2": "Microsoft (WPA/WPS)",
    "00173f": "Broadcom", "8cfdf0": "Qualcomm", "001374": "Atheros",
    "506f9a": "Wi-Fi Alliance (P2P)", "0017f2": "Apple", "001b2f": "Netgear",
    "0022f7": "Zyxel", "f4f5e8": "Google", "5c7dae": "TP-Link",
    "a46b40": "TP-Link", "cc28aa": "TP-Link", "58d61f": "Ubiquiti",
    "08b657": "Cisco Meraki", "e0e258": "Intel", "40d133": "Apple",
    "401a58": "Apple", "30e3a4": "Apple", "ceba bd": "randomized",
}

WPS_ATTR = {0x1021: "manufacturer", 0x1023: "model", 0x1024: "model number",
            0x1011: "device name", 0x103c: "rf bands"}


def vendor(mac_hex):
    o = mac_hex[:6].lower()
    if int(mac_hex[1], 16) & 0x02:
        return "locally administered (randomized or virtual AP)"
    return OUI.get(o, f"OUI {o}")


def ies(b, start):
    """walk the information elements of a management frame"""
    out = []
    i = start
    while i + 2 <= len(b):
        eid, ln = b[i], b[i + 1]
        if i + 2 + ln > len(b):
            break
        out.append((eid, b[i + 2:i + 2 + ln]))
        i += 2 + ln
    return out


def bits(v, lo, hi):
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def nss(mcs_map):
    """highest spatial stream with a supported MCS set (2 bits per stream)"""
    n = 0
    for s in range(8):
        if bits(mcs_map, 2 * s, 2 * s + 1) != 3:
            n = s + 1
    return n


def wps(data):
    """the WPS element is a run of 2-byte type, 2-byte length, value"""
    found, i = {}, 4                      # skip the 00:50:f2:04 OUI and type
    while i + 4 <= len(data):
        t = int.from_bytes(data[i:i + 2], "big")
        ln = int.from_bytes(data[i + 2:i + 4], "big")
        if i + 4 + ln > len(data):
            break
        if t in WPS_ATTR:                 # serial number is deliberately skipped
            found[WPS_ATTR[t]] = data[i + 4:i + 4 + ln].decode("utf8", "replace").strip("\x00")
        i += 4 + ln
    return found


def describe(frame):
    b = frame[:-4] if len(frame) > 40 else frame   # sig_len includes the FCS
    bssid = b[16:22].hex()
    cap = int.from_bytes(b[34:36], "little")
    out = {"bssid": bssid, "vendor of address": vendor(bssid),
           "beacon interval": f"{int.from_bytes(b[32:34], 'little') * 1024 / 1000:.0f} ms",
           "privacy": "on" if cap & 0x10 else "off"}
    gens, vend = [], []

    for eid, d in ies(b, 36):
        if eid == 0 and "ssid" not in out:
            out["ssid"] = d.decode("utf8", "replace") or "(hidden)"
        elif eid == 3 and d:
            out["primary channel"] = d[0]
        elif eid == 7 and len(d) >= 3:
            out["country"] = d[:2].decode("ascii", "replace")
        elif eid == 45 and len(d) >= 2:
            gens.append("HT (Wi-Fi 4)")
            info = int.from_bytes(d[:2], "little")
            out["HT width"] = "40 MHz" if info & 0x02 else "20 MHz"
        elif eid == 191 and len(d) >= 12:
            gens.append("VHT (Wi-Fi 5)")
            v = int.from_bytes(d[:4], "little")
            out["VHT SU beamformer"] = bool(v & (1 << 11))
            out["VHT SU beamformee"] = bool(v & (1 << 12))
            out["VHT MU beamformer"] = bool(v & (1 << 19))
            out["VHT MU beamformee"] = bool(v & (1 << 20))
            out["VHT sounding dimensions"] = bits(v, 16, 18) + 1
            out["VHT beamformee STS"] = bits(v, 13, 15) + 1
            out["VHT spatial streams"] = nss(int.from_bytes(d[4:6], "little"))
        elif eid == 192 and len(d) >= 3:
            out["VHT width"] = {0: "20 or 40 MHz", 1: "80 MHz",
                                2: "160 MHz", 3: "80+80 MHz"}.get(d[0], d[0])
        elif eid == 255 and d:
            ext = d[0]
            if ext == 35:
                gens.append("HE (Wi-Fi 6)")
                if len(d) >= 18:
                    phy = d[7]            # first byte of the PHY capabilities
                    w = []
                    if phy & 0x02: w.append("40 MHz (2.4 GHz)")
                    if phy & 0x04: w.append("40/80 MHz")
                    if phy & 0x08: w.append("160 MHz")
                    if w: out["HE width set"] = ", ".join(w)
            elif ext == 108:
                gens.append("EHT (Wi-Fi 7)")
        elif eid == 221 and len(d) >= 4:
            o = d[:3].hex()
            if o == "0050f2" and d[3] == 4:
                out.update(wps(d))
            else:
                tag = OUI.get(o, f"OUI {o}")
                if tag not in vend:
                    vend.append(tag)

    out["generations"] = ", ".join(gens) if gens else "legacy only"
    if vend:
        out["vendor elements"] = ", ".join(vend)
    return out


def main(paths):
    seen = set()
    for path in paths:
        for line in open(path, encoding="utf8", errors="replace"):
            p = line.split()
            if len(p) < 9 or p[0] != "F" or p[2] != "0" or p[3] != "8":
                continue
            try:
                frame = bytes.fromhex(p[8])      # p[7] is the raw rx_ctrl
            except ValueError:
                continue
            if len(frame) < 40:
                continue
            info = describe(frame)
            key = (info.get("ssid"), info["bssid"])
            if key in seen:
                continue
            seen.add(key)
            print(f"\n=== {info.pop('ssid', '?')}   ({path})")
            for k, v in info.items():
                print(f"  {k:26} {v}")
    if not seen:
        print("no beacon records in the input. Run `dump on`, then `beacon 3`.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
