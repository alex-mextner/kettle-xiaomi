"""captures/parse_btsnoop.py on a synthetic HCI snoop log (no phone needed)."""

import contextlib
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "captures"))
import parse_btsnoop as parser  # noqa: E402


def frame(command: int, payload: bytes, direction: int = 0) -> bytes:
    return (
        b"\x51\x78"
        + bytes((command, direction))
        + struct.pack("<H", len(payload))
        + payload
        + bytes((parser.crc8(payload), 0xFF))
    )


def acl(sent: bool, att: bytes, timestamp: int) -> bytes:
    l2cap = struct.pack("<HH", len(att), 4) + att
    data = b"\x02" + struct.pack("<HH", 0x2040, len(l2cap)) + l2cap
    return struct.pack(">IIIIq", len(data), len(data), 0 if sent else 1, 0, timestamp) + data


class ParseBtsnoop(unittest.TestCase):
    def test_summarises_stream_flow_control_and_state(self):
        stream = frame(0xA4, b"\x33") + frame(0xAF, struct.pack("<H", 8000))
        stream += b"".join(frame(0xA2, bytes([i]) * 48) for i in range(10)) + frame(0xA3, b"\x00")
        log = b"btsnoop\0" + struct.pack(">II", 1, 1002)
        stamp = 1_000_000
        for offset in range(0, len(stream), 100):
            log += acl(True, b"\x52" + struct.pack("<H", 6) + stream[offset : offset + 100], stamp)
            stamp += 20_000
        log += acl(False, b"\x1b" + struct.pack("<H", 8) + frame(0xAE, b"\x10", 1), stamp)
        log += acl(False, b"\x1b" + struct.pack("<H", 8) + frame(0xA3, b"\x80\x00\x27", 1), stamp)
        with tempfile.NamedTemporaryFile(suffix=".log") as handle:
            handle.write(log)
            handle.flush()
            argv, sys.argv = sys.argv, ["parse_btsnoop", handle.name]
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    parser.main()
            finally:
                sys.argv = argv
        text = out.getvalue()
        self.assertIn("energy=8000", text)
        self.assertIn("buffer FULL (pause)", text)
        self.assertIn("'printing'", text)
        self.assertIn("raster rows: 10", text)
        self.assertIn("gaps >100 ms: 0", text)
        self.assertNotIn("BAD CRC", text)


if __name__ == "__main__":
    unittest.main()
