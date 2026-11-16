import pathlib
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from filament_manager import lookup  # noqa: E402
from filament_manager.fake import FakeFlipperSerial, FakeTag  # noqa: E402
from filament_manager.flipper import Flipper  # noqa: E402
from filament_manager.spoolman import card_uids, decode_extra_str, encode_extra_str, match_score  # noqa: E402


class TitleParsing(unittest.TestCase):
    def test_hatchbox(self):
        f = lookup.parse_title("HATCHBOX PLA 3D Printer Filament, Dimensional Accuracy +/- 0.03 mm, 1 kg Spool, 1.75 mm, Silk Gold", "HATCHBOX")
        self.assertEqual(f["type"], "PLA")
        self.assertEqual(f["subtype"], "Silk")
        self.assertEqual(f["color_hex"], lookup.COLORS["gold"])
        self.assertEqual(f["weight"], 1000)
        self.assertEqual(f["diameter"], 1.75)

    def test_specific_material_wins(self):
        self.assertEqual(lookup.parse_title("Overture PETG-CF Filament 1.75mm Black")["type"], "PETG-CF")
        self.assertEqual(lookup.parse_title("eSUN PLA+ 1.75mm 1KG Cool White")["type"], "PLA+")
        self.assertEqual(lookup.parse_title("Polymaker Nylon PA6 2.2 lbs")["weight"], 1000)

    def test_upc_validation(self):
        self.assertEqual(lookup.norm_upc("8 19042-02004 3"), "819042020043")
        with self.assertRaises(lookup.LookupError_):
            lookup.norm_upc("123")


class FakeSM:
    def __init__(self, filaments=(), external=()):
        self._f, self._e = list(filaments), list(external)

    def filaments(self):
        return self._f

    def external_search(self, query, limit=20):
        words = query.lower().split()
        return [e for e in self._e if all(w in f"{e['manufacturer']} {e['name']} {e['material']}".lower() for w in words)]


class UpcLookup(unittest.TestCase):
    EXT = [{"id": "hatchbox_pla_silkgold", "manufacturer": "Hatchbox", "name": "Silk Gold", "material": "PLA",
            "density": 1.24, "weight": 1000, "spool_weight": 225, "diameter": 1.75, "color_hex": "D4AF37",
            "extruder_temp": 210, "bed_temp": 60, "finish": "glossy"},
           {"id": "hatchbox_pla_black", "manufacturer": "Hatchbox", "name": "Black", "material": "PLA",
            "density": 1.24, "weight": 1000, "diameter": 1.75, "color_hex": "000000"}]

    def setUp(self):
        self._orig = lookup.upcitemdb
        lookup.upcitemdb = lambda upc: {"title": "HATCHBOX PLA 3D Printer Filament, 1 kg Spool, 1.75 mm, Silk Gold",
                                        "brand": "Hatchbox"}

    def tearDown(self):
        lookup.upcitemdb = self._orig

    def test_online_then_spoolmandb(self):
        r = lookup.upc_lookup(FakeSM(external=self.EXT), "819042020043")
        self.assertEqual(r["external"][0]["external"]["id"], "hatchbox_pla_silkgold")
        self.assertEqual(r["fields"]["spool_weight"], 225)
        self.assertEqual(r["fields"]["extruder_temp"], 210)
        self.assertEqual(r["fields"]["subtype"], "Silk")
        self.assertEqual(r["fields"]["upc"], "819042020043")

    def test_own_spoolman_first(self):
        mine = {"id": 7, "material": "PETG", "article_number": "819042020043", "vendor": {"name": "Acme"}}
        r = lookup.upc_lookup(FakeSM(filaments=[mine]), "819042020043")
        self.assertEqual(r["filaments"][0]["filament"]["id"], 7)
        self.assertIsNone(r["product"])


class SpoolmanEncoding(unittest.TestCase):
    def test_card_uids(self):
        self.assertEqual(card_uids({"extra": {"card_uids": encode_extra_str("aabbccdd,04:11:22:33:44:55:66")}}),
                         ["AABBCCDD", "04112233445566"])
        self.assertEqual(card_uids({"extra": {"card_uids": "AABBCCDD"}}), ["AABBCCDD"])  # raw, not encoded
        self.assertEqual(card_uids({"extra": {}}), [])
        self.assertEqual(decode_extra_str('"Silk"'), "Silk")

    def test_match_score(self):
        fil = {"material": "PLA", "vendor": {"name": "Hatchbox"}, "color_hex": "FF6A13", "extra": {"variant": '"Wood"'}}
        self.assertGreater(match_score(fil, {"type": "PLA", "brand": "hatchbox", "color_hex": "#FF6A13"}), 7.9)
        self.assertLess(match_score(fil, {"type": "PETG", "color_hex": "0000FF"}), 1)


class PartnerSpools(unittest.TestCase):
    def test_second_tag_of_a_snapmaker_spool(self):
        sm = type("SM", (), {"store": "native"})()
        fil = {"material": "PETG", "vendor": {"name": "Snapmaker"}, "color_hex": "223622"}
        other = {"material": "PLA", "vendor": {"name": "Hatchbox"}, "color_hex": "223622"}
        spools = [
            {"id": 1, "filament": fil, "extra": {}, "tags": [{"uid": "607A25E4"}]},
            {"id": 2, "filament": fil, "extra": {}, "tags": [{"uid": "AA"}, {"uid": "BB"}]},  # already has both
            {"id": 3, "filament": other, "extra": {}, "tags": [{"uid": "CC"}]},              # different filament
            {"id": 4, "filament": fil, "extra": {}, "tags": [{"uid": "DD"}], "archived": True},
            {"id": 5, "filament": fil, "extra": {}, "tags": [{"uid": "EE"}]},
        ]
        want = {"brand": "Snapmaker", "type": "PETG", "color_hex": "223622"}
        self.assertEqual([s["id"] for s in lookup.partner_spools(sm, spools, want)], [5, 1])


class WaitForTag(unittest.TestCase):
    def test_waits_until_tag_arrives(self):
        ser = FakeFlipperSerial(None)
        f = Flipper(ser)
        f.start()
        threading.Timer(0.5, lambda: setattr(ser, "tag", FakeTag())).start()
        t0 = time.monotonic()
        # first scanner call blocks; it times out per call, so loop with short timeouts like the page does
        tag = None
        while tag is None and time.monotonic() - t0 < 5:
            tag = f.wait_for_tag(timeout=0.3)
        self.assertEqual(tag.uid, "04A1B2C3D4E5F6")

    def test_skip_uid_and_cancel(self):
        f = Flipper(FakeFlipperSerial())
        f.start()
        cancel = threading.Event()
        threading.Timer(0.8, cancel.set).start()
        self.assertIsNone(f.wait_for_tag(timeout=5, skip_uid="04A1B2C3D4E5F6", cancel=cancel))


if __name__ == "__main__":
    unittest.main()
