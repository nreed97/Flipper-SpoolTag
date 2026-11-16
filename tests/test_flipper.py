import json
import pathlib
import sys
import threading
import unittest
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from filament_manager import ace, ndef, openspool, snapmaker  # noqa: E402
from filament_manager.fake import FakeFlipperSerial, FakeTag, snapmaker_tag  # noqa: E402
from filament_manager.flipper import Flipper, FlipperError, NoTag  # noqa: E402

FIELDS = {"type": "pla", "color_hex": "ff6a13", "brand": "Generic", "subtype": "Silk",
          "min_temp": "190", "max_temp": 220, "bed_min_temp": 50, "bed_max_temp": "",
          "weight": 1000, "diameter": "1.75"}


def make(tag="blank"):
    ser = FakeFlipperSerial(tag)
    f = Flipper(ser)
    f.start()
    return f, ser


class NdefTests(unittest.TestCase):
    def test_roundtrip_short_and_long(self):
        for size in (10, 300):
            payload = b"x" * size
            msg = ndef.build_message([ndef.mime_record("application/json", payload)])
            data = ndef.wrap_tlv(msg)
            self.assertEqual(ndef.ndef_tlv_end(data), len(data))
            recs = ndef.parse_message(ndef.find_ndef_tlv(data + b"\x00" * 8))
            self.assertEqual(recs[0].payload, payload)
            self.assertEqual(recs[0].mime_type, "application/json")

    def test_empty_tlv(self):
        self.assertEqual(ndef.find_ndef_tlv(bytes([3, 0, 0xFE])), b"")


class OpenSpoolTests(unittest.TestCase):
    def test_build(self):
        p = openspool.build_payload(FIELDS)
        self.assertEqual(p["type"], "PLA")
        self.assertEqual(p["color_hex"], "#FF6A13")
        self.assertEqual(p["min_temp"], 190)
        self.assertNotIn("bed_max_temp", p)
        self.assertEqual(p["diameter"], 1.75)

    def test_requires_type_and_color(self):
        with self.assertRaises(ValueError):
            openspool.build_payload({"type": "PLA"})
        with self.assertRaises(ValueError):
            openspool.build_payload({"type": "PLA", "color_hex": "red"})


class FlipperTests(unittest.TestCase):
    def test_blank_tag(self):
        f, _ = make()
        t = f.read_tag()
        self.assertEqual(t.uid, "04A1B2C3D4E5F6")
        self.assertEqual(t.family, "ntag")
        self.assertEqual(t.capacity, 496)
        self.assertEqual(t.format, "blank")

    def test_write_then_read_openspool(self):
        f, ser = make()
        res = f.write_openspool(FIELDS, expect_uid="04a1b2c3d4e5f6")
        self.assertGreater(res["pages"], 10)
        self.assertTrue(all(p >= 4 for p in ser.writes))
        t = f.read_tag()
        self.assertEqual(t.format, "openspool")
        self.assertEqual(t.filament["brand"], "Generic")
        self.assertEqual(t.filament["subtype"], "Silk")

    def test_wrong_uid_guard(self):
        f, ser = make()
        with self.assertRaises(FlipperError):
            f.write_openspool(FIELDS, expect_uid="DEADBEEF")
        self.assertEqual(ser.writes, [])

    def test_no_tag(self):
        f, _ = make(None)
        with self.assertRaises(NoTag):
            f.read_tag()

    def test_mifare_classic_uid_only(self):
        f, _ = make(FakeTag(uid="1D9B9E1A", classic=True))
        t = f.read_tag()
        self.assertEqual(t.uid, "1D9B9E1A")
        self.assertEqual(t.family, "mifare_classic")
        self.assertEqual(t.format, "uid_only")
        self.assertTrue(any("UID only" in n for n in t.notes))
        with self.assertRaises(FlipperError):
            f.write_openspool(FIELDS)

    def test_locked_classic_leaves_no_key_file(self):
        f, ser = make(FakeTag(uid="1D9B9E1A", classic=True))
        f.read_tag()
        self.assertNotIn("/ext/nfc/.cache/1D9B9E1A.keys", ser.files)

    def test_snapmaker_tag(self):
        f, ser = make(snapmaker_tag())
        t = f.read_tag()
        self.assertEqual(t.uid, "0C1EA85E")
        self.assertEqual(t.family, "mifare_classic")
        self.assertEqual(t.format, "snapmaker")
        fil = t.filament
        self.assertEqual((fil["brand"], fil["type"], fil["subtype"]), ("Snapmaker", "PLA", "SnapSpeed"))
        self.assertEqual(fil["color_hex"], "E72F1D")
        self.assertEqual((fil["min_temp"], fil["max_temp"], fil["weight"]), (190, 230, 500))
        self.assertFalse(fil["signature_ok"])  # the fake isn't signed
        self.assertIn("/ext/nfc/.cache/0C1EA85E.keys", ser.files)
        # Second read reuses the cache file instead of rewriting it.
        n = sum(c.startswith("storage write_chunk") for c in ser.commands)
        self.assertEqual(f.read_tag().format, "snapmaker")
        self.assertEqual(sum(c.startswith("storage write_chunk") for c in ser.commands), n)


class SnapmakerTests(unittest.TestCase):
    def test_key_derivation(self):
        # Sector 0 keys from a real Snapmaker tag with UID C1EA85E3 (trailer bytes 0-5 and 10-15).
        a, b = snapmaker.sector_keys(bytes.fromhex("C1EA85E3"))[0]
        self.assertEqual(a.hex().upper(), "E67C7116F60E")
        self.assertEqual(b.hex().upper(), "20131608C3ED")

    def test_cache_file_format(self):
        text = snapmaker.key_cache_file(bytes.fromhex("C1EA85E3"))
        self.assertTrue(text.startswith("Filetype: Flipper NFC keys\nVersion: 1\n"))
        self.assertIn("Key A map: 000000000000FFFF", text)
        self.assertIn("Key A sector 0: E6 7C 71 16 F6 0E", text)
        self.assertIn("Key B sector 15: ", text)

    def test_not_snapmaker(self):
        self.assertIsNone(snapmaker.parse(bytes(1024)))

    def test_repeated_reads_and_no_raw(self):
        f, ser = make()
        for _ in range(3):
            self.assertEqual(f.read_tag().format, "blank")
        self.assertFalse(any(c.startswith("raw") for c in ser.commands))

    def test_too_big(self):
        f, _ = make(FakeTag(pages=45))
        f.ser.tag.mem[14] = 0x12          # CC says 144 bytes (NTAG213)
        with self.assertRaises(FlipperError):
            f.write_user_data(b"x" * 200)

    def test_ace_tag(self):
        tag = FakeTag()
        d = bytearray(112)
        d[0:4] = ace.MAGIC
        d[4:12] = b"AHPLBK-1"
        d[24:32] = b"AnyCubic"
        d[44:47] = b"PLA"
        d[64:68] = bytes([0xFF, 0x13, 0x6A, 0xFF])  # alpha, B, G, R
        d[80:84] = (190).to_bytes(2, "little") + (230).to_bytes(2, "little")
        d[100:104] = (50).to_bytes(2, "little") + (60).to_bytes(2, "little")
        d[104:108] = (175).to_bytes(2, "little") + (330).to_bytes(2, "little")
        d[108:110] = (1000).to_bytes(2, "little")
        tag.mem[16:16 + 112] = d
        f, _ = make(tag)
        t = f.read_tag()
        self.assertEqual(t.format, "ace")
        self.assertEqual(t.filament["color_hex"], "FF6A13")
        self.assertEqual(t.filament["brand"], "AnyCubic")
        self.assertEqual(t.filament["diameter"], 1.75)
        self.assertEqual(t.filament["weight"], 1000)


class RealTagTests(unittest.TestCase):
    # User data from a real tag written by the Spool Programmer iOS app (ACE format), set to blue.
    REAL_ACE = ("7B0065004148504C412D3030310000000000000000000000416E796375626963000000000000000000000000"
                "504C410000000000000000000000000000000000FFFF9100000000000000000000000000C800DC0000000000"
                "00000000000000000000000032003C00AF004A01E8030000")

    def test_real_ace_tag(self):
        tag = FakeTag(uid="1D9B9E1A8D0000")
        data = bytes.fromhex(self.REAL_ACE)
        tag.mem[16:16 + len(data)] = data
        f, _ = make(tag)
        t = f.read_tag()
        self.assertEqual(t.uid, "1D9B9E1A8D0000")
        self.assertEqual(t.format, "ace")
        self.assertEqual(t.filament["color_hex"], "0091FF")  # written as blue
        self.assertEqual(t.filament["min_temp"], 200)


class ServerTests(unittest.TestCase):
    def test_http_roundtrip(self):
        from http.server import ThreadingHTTPServer
        from types import SimpleNamespace
        from filament_manager.server import App, make_handler
        import tempfile
        app = App(SimpleNamespace(fake=True, port=None, debug=False, offline=True, spoolman="http://127.0.0.1:9"),
                  config_path=pathlib.Path(tempfile.mkdtemp()) / "config.json")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, body=None):
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())

        try:
            st = call("/api/status")
            self.assertTrue(st["flipper"]["connected"])
            self.assertFalse(st["spoolman"]["connected"])
            uid = call("/api/tag/read", {})["uid"]
            call("/api/tag/write", {"fields": FIELDS, "expect_uid": uid})
            self.assertEqual(call("/api/tag/read", {})["filament"]["type"], "PLA")
            self.assertIn("error", call("/api/tag/read", {})["lookup"])  # Spoolman down: read still works
            self.assertEqual(call("/api/tag/wait", {"timeout": 2})["tag"]["uid"], uid)
            call("/api/fake/tag", {"kind": "none"})
            self.assertIsNone(call("/api/tag/wait", {"timeout": 1})["tag"])
            with urllib.request.urlopen(base + "/") as r:
                self.assertIn(b"Filament Manager", r.read())
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
