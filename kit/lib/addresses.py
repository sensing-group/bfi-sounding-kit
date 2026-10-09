"""Find the device addresses in a recording, wherever they appear.

Addresses appear in two forms: as text (`a4:6b:40:11:22:33`) in the activity
lines, logs and metadata, and as raw bytes inside the hex of every frame
record. A router also writes its own address with the lowest bit of the first
byte set when it signals its bandwidth in a sounding request (`a5:6b:40:...`).
Both forms of every address have to be found, or one of them survives the
replacement.
"""
import re

TEXT = re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b")
# the firmware writes hex in lower case; "F" starts a record, also when the USB
# console glued it onto the previous one or a prompt precedes it
FRAME = re.compile(r"F -?\d+ (\d+) (\d+) -?\d+ \d+ \d+ [0-9a-f]{32} ([0-9a-f]+)")
STANDIN = "02:00:00:00:00:{:02x}"
STANDIN_RE = re.compile(r"^0[23]:00:00:00:00:[0-9a-f]{2}$")
GROUP = ("ff:ff:ff:ff:ff:ff", "01:00:5e", "33:33", "01:80:c2")


def colon(h):
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def signalled(addr):
    """the bandwidth-signalling form of an address: lowest bit of byte one set"""
    return f"{int(addr[:2], 16) | 1:02x}" + addr[2:]


def fields(line):
    """(address, sent_by_router_in_a_control_frame) for every address field of
    every frame record on a line, glued records included"""
    out = []
    for m in FRAME.finditer(line):
        typ, h = int(m.group(1)), m.group(3)
        if typ == 0:                              # management: three addresses
            spans = [(8, False), (20, False), (32, False)]
        elif typ == 1:                            # control: receiver, transmitter
            spans = [(8, False), (20, True)]
        else:
            spans = []
        for start, ta in spans:
            if len(h) >= start + 12:
                out.append((colon(h[start:start + 12]), ta))
    return out


def is_group(addr):
    return addr.startswith(GROUP)


def base(addr, ta):
    """the device address behind a field, or None for a group address"""
    if is_group(addr):
        return None
    if int(addr[:2], 16) & 1:
        if not ta:
            return None                           # multicast, not a device
        addr = f"{int(addr[:2], 16) & 0xFE:02x}" + addr[2:]
    return addr


def beacons(line):
    """spans of the router announcements (beacons) recorded on a line: they
    carry the network's name and often the router's make and model"""
    return [m.span() for m in FRAME.finditer(line)
            if m.group(1) == "0" and m.group(2) == "8"]
