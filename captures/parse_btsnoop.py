#!/usr/bin/env python3
"""Summarise what a phone app and a 51 78 "cat" thermal printer (X6h, service AE30) exchange.

Input: an Android Bluetooth HCI snoop log (btsnoop_hci.log, datalink 1001/1002/1003 H4/HCI).
Only the standard library is used.

How to record (Android):
  1. Settings > Developer options > "Enable Bluetooth HCI snoop log" = Enabled (or "Filtered"
     is NOT enough: choose the full one), then toggle Bluetooth off and on.
  2. Open the printer's own app, connect, print ONE short line, wait until paper stops.
  3. Turn Bluetooth HCI snoop log off. Get the file: `adb bugreport out.zip` and take
     FS/data/misc/bluetooth/logs/btsnoop_hci.log (older phones: /sdcard/btsnoop_hci.log),
     or `adb pull /data/misc/bluetooth/logs/btsnoop_hci.log` if rooted.
  4. Put it in ~/xp/kettle-xiaomi/captures/ and run:
       python3 captures/parse_btsnoop.py captures/btsnoop_hci.log

Handles are mapped to characteristics from the Read By Type (0x2803) answers in the capture;
if the app used a cached GATT table, the X6h defaults below are applied (override with
--map 6=AE01,8=AE02).
"""

import argparse
import statistics
import struct
import sys
from collections import Counter
from dataclasses import dataclass

BTSNOOP_EPOCH_US = 0x00DCDDB30F2F8000
DEFAULT_HANDLES = {
    6: "AE01", 8: "AE02", 11: "AE03", 13: "AE04", 16: "AE05", 19: "AE10", 66: "AE3B", 68: "AE3C",
}
COMMANDS = {
    0xA0: "retract", 0xA1: "feed", 0xA2: "row", 0xA3: "state", 0xA4: "quality", 0xA6: "control",
    0xA8: "info", 0xAA: "?AA", 0xAE: "flow", 0xAF: "energy", 0xB1: "?B1", 0xB2: "?B2",
    0xBA: "battery", 0xBB: "id", 0xBC: "?BC", 0xBD: "speed", 0xBE: "mode", 0xBF: "rle-row",
    0xCE: "lzo-row", 0xCF: "gray-lzo-row", 0xD1: "auth", 0xA9: "update",
}
STATE_BITS = ((0x01, "out_of_paper"), (0x02, "cover_open"), (0x04, "overheating"),
              (0x08, "low_battery"), (0x10, "charging"), (0x80, "printing"))


@dataclass
class Att:
    t: float
    sent: bool  # True: phone -> printer
    op: int
    handle: int
    value: bytes


def crc8(data: bytes) -> int:
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (0x07 if value & 0x80 else 0)) & 0xFF
    return value


def records(blob: bytes):
    if blob[:8] != b"btsnoop\0":
        raise SystemExit("not a btsnoop file")
    datalink = struct.unpack(">I", blob[12:16])[0]
    if datalink not in (1001, 1002):
        raise SystemExit(f"unsupported datalink {datalink} (need HCI UART/H4 = 1002)")
    pos = 16
    first = None
    while pos + 24 <= len(blob):
        _orig, incl, flags, _drops, ts = struct.unpack(">IIIIq", blob[pos : pos + 24])
        data = blob[pos + 24 : pos + 24 + incl]
        pos += 24 + incl
        if first is None:
            first = ts
        yield (ts - first) / 1e6, bool(flags & 1) is False, data, datalink


def att_events(blob: bytes):
    partial: dict[tuple[int, bool], bytearray] = {}
    for t, sent, data, datalink in records(blob):
        if datalink == 1002:
            if not data or data[0] != 2:
                continue
            data = data[1:]
        if len(data) < 4:
            continue
        handle_flags, length = struct.unpack("<HH", data[:4])
        handle, pb = handle_flags & 0x0FFF, (handle_flags >> 12) & 3
        payload = data[4 : 4 + length]
        key = (handle, sent)
        if pb in (0, 2):
            partial[key] = bytearray(payload)
        elif pb == 1 and key in partial:
            partial[key].extend(payload)
        else:
            continue
        buf = partial[key]
        if len(buf) < 4:
            continue
        l2len, cid = struct.unpack("<HH", bytes(buf[:4]))
        if len(buf) < 4 + l2len:
            continue
        del partial[key]
        if cid != 4 or l2len < 1:
            continue
        pdu = bytes(buf[4 : 4 + l2len])
        op = pdu[0]
        if op in (0x52, 0x12, 0x1B, 0x1D, 0x0A) and len(pdu) >= 3:
            yield Att(t, sent, op, struct.unpack("<H", pdu[1:3])[0], pdu[3:])
        elif op in (0x09, 0x11, 0x03, 0x02, 0x13, 0x0B):
            yield Att(t, sent, op, 0, pdu[1:])


def handle_map(events: list[Att], overrides: dict[int, str]) -> dict[int, str]:
    mapping = dict(DEFAULT_HANDLES)
    for e in events:
        if e.op == 0x09 and not e.sent and e.value and e.value[0] in (7, 21):
            size = e.value[0]
            for i in range(1, len(e.value) - size + 1, size):
                entry = e.value[i : i + size]
                value_handle = struct.unpack("<H", entry[3:5])[0]
                uuid = entry[5:]
                if len(uuid) == 2:
                    mapping[value_handle] = f"{struct.unpack('<H', uuid)[0]:04X}"
    mapping.update(overrides)
    return mapping


def split_frames(stream: bytearray):
    """Pop complete 51 78 frames; return list of (cmd, dir, payload, ok_crc)."""
    out = []
    while True:
        start = stream.find(b"\x51\x78")
        if start < 0:
            keep = stream[-1:] if stream[-1:] == b"\x51" else b""
            stream[:] = keep  # a lone 0x51 may be the first half of the next magic
            return out
        del stream[:start]
        if len(stream) < 8:
            return out
        length = stream[4] | stream[5] << 8
        if length > 600:
            del stream[:2]
            continue
        total = length + 8
        if len(stream) < total:
            return out
        raw = bytes(stream[:total])
        del stream[:total]
        out.append((raw[2], raw[3], raw[6:-2], crc8(raw[6:-2]) == raw[-2]))


def describe(cmd: int, payload: bytes) -> str:
    if cmd == 0xAE and payload[:1] in (b"\x10", b"\x00"):
        return "buffer FULL (pause)" if payload[0] == 0x10 else "buffer EMPTY (resume)"
    if cmd == 0xA3 and payload:
        names = [n for b, n in STATE_BITS if payload[0] & b]
        return f"state flags=0x{payload[0]:02x} {names or 'idle'} rest={payload[1:].hex()}"
    if cmd == 0xAF and len(payload) == 2:
        return f"energy={int.from_bytes(payload, 'little')}"
    if cmd in (0xBD, 0xBE, 0xA4) and payload:
        return f"{COMMANDS[cmd]}=0x{payload[0]:02x}"
    if cmd == 0xA1 and len(payload) == 2:
        return f"feed={int.from_bytes(payload, 'little')} dots"
    return f"{COMMANDS.get(cmd, f'cmd 0x{cmd:02x}')} len={len(payload)} {payload[:12].hex()}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--map", default="", help="handle=NAME,... e.g. 6=AE01,8=AE02")
    ap.add_argument("--all", action="store_true", help="also list every row write")
    args = ap.parse_args()
    overrides = {int(k): v for k, v in (p.split("=") for p in args.map.split(",") if p)}
    with open(args.file, "rb") as source:
        events = list(att_events(source.read()))
    names = handle_map(events, overrides)
    data_ev = [e for e in events if e.op in (0x52, 0x12, 0x1B, 0x1D)]
    print(f"ATT data PDUs: {len(data_ev)}; handles seen: "
          + ", ".join(f"{h}={names.get(h, '?')}" for h in sorted({e.handle for e in data_ev})))
    streams = {True: bytearray(), False: bytearray()}
    rows = 0
    row_times: list[float] = []
    sizes: Counter[int] = Counter()
    writes: list[Att] = []
    for e in data_ev:
        ch = names.get(e.handle, f"h{e.handle}")
        kind = {0x52: "WRITE-CMD", 0x12: "WRITE-REQ", 0x1B: "NOTIFY", 0x1D: "INDICATE"}[e.op]
        if e.sent and e.op in (0x52, 0x12):
            writes.append(e)
            sizes[len(e.value)] += 1
        key = e.sent
        if ch in ("AE01", "AE02") or e.value[:2] == b"\x51\x78":
            streams[key].extend(e.value)
            for cmd, direction, payload, ok in split_frames(streams[key]):
                if cmd == 0xA2 or cmd in (0xBF, 0xCE, 0xCF):
                    rows += 1
                    row_times.append(e.t)
                    if not args.all:
                        continue
                tag = "" if ok else "  [BAD CRC]"
                print(f"{e.t:9.3f}s {'->' if e.sent else '<-'} {ch} {kind:9} "
                      f"{describe(cmd, payload)}{tag}")
        else:
            print(f"{e.t:9.3f}s {'->' if e.sent else '<-'} {ch} {kind:9} len={len(e.value)} "
                  f"{e.value[:24].hex()}")
    print()
    print(f"raster rows: {rows}", end="")
    if len(row_times) > 1:
        print(f"  first {row_times[0]:.3f}s last {row_times[-1]:.3f}s "
              f"({rows / max(row_times[-1] - row_times[0], 1e-6):.0f} rows/s)")
    else:
        print()
    if len(writes) > 1:
        gaps = [b.t - a.t for a, b in zip(writes, writes[1:])]
        print(f"phone writes: {len(writes)}, bytes {sum(len(w.value) for w in writes)}")
        print(f"write sizes (bytes: count): {dict(sorted(sizes.items()))}")
        print(f"inter-write gap ms: median {statistics.median(gaps)*1e3:.1f} "
              f"max {max(gaps)*1e3:.1f}; gaps >100 ms: {sum(g > 0.1 for g in gaps)}")
        mtu = [e for e in events if e.op in (0x02, 0x03)]
        if mtu:
            print("MTU exchange:", ", ".join(f"{'->' if e.sent else '<-'} {struct.unpack('<H', e.value[:2])[0]}" for e in mtu))


if __name__ == "__main__":
    sys.exit(main())
