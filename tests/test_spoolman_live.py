"""Runs against a real (throwaway!) Spoolman. Skipped unless SPOOLMAN_TEST_URL is set:
    set SPOOLMAN_TEST_URL=http://localhost:7912 && python -m unittest tests.test_spoolman_live
It creates vendors, filaments and spools, so don't point it at your real inventory."""
import os
import secrets
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from filament_manager.spoolman import Spoolman, SpoolmanError, card_uids, spool_uids  # noqa: E402

URL = os.environ.get("SPOOLMAN_TEST_URL")


@unittest.skipUnless(URL, "set SPOOLMAN_TEST_URL to a throwaway Spoolman")
class LiveSpoolman(unittest.TestCase):
    def test_card_uids_flow(self):
        sm = Spoolman(URL)
        a = sm.save({"filament": {"material": "PLA", "name": "Test", "color_hex": "A0522D", "variant": "Wood"},
                     "vendor_name": "TestVendor", "spool": {}})
        b = sm.save({"filament_id": a["filament"]["id"], "spool": {}})
        uid = secrets.token_hex(4).upper()
        sm.link(a["id"], ":".join(uid[i:i + 2] for i in range(0, 8, 2)))
        with self.assertRaises(SpoolmanError) as cm:
            sm.link(b["id"], uid.lower())
        self.assertEqual(cm.exception.status, 409)
        r = sm.link(b["id"], uid, move=True)
        self.assertEqual(r["moved_from"], [a["id"]])
        self.assertEqual(card_uids(sm.spool(a["id"])), [])
        self.assertEqual(card_uids(r["spool"]), [uid])

    def test_native_flow(self):
        sm = Spoolman(URL, store="native")
        a = sm.save({"filament": {"material": "PETG", "color_hex": "1565C0"}, "vendor_name": "TestVendor", "spool": {}})
        b = sm.save({"filament_id": a["filament"]["id"], "spool": {}})
        uid = "04" + secrets.token_hex(6).upper()
        sm.link(a["id"], uid)
        r = sm.link(b["id"], uid, move=True)
        self.assertEqual(spool_uids(r["spool"], "native"), [uid])
        self.assertEqual(spool_uids(sm.spool(a["id"]), "native"), [])

    def test_native_link_migrates_card_uids(self):
        old, new = Spoolman(URL), Spoolman(URL, store="native")
        s = old.save({"filament": {"material": "PLA", "color_hex": "112233"}, "vendor_name": "TestVendor", "spool": {}})
        uid = "04" + secrets.token_hex(6).upper()
        old.link(s["id"], uid)
        r = new.link(s["id"], uid)
        self.assertEqual(spool_uids(r["spool"], "native"), [uid])
        self.assertEqual(card_uids(new.spool(s["id"])), [])

    def test_failed_save_cleans_up(self):
        sm = Spoolman(URL)
        before = len(sm.vendors())
        with self.assertRaises(SpoolmanError):
            sm.save({"filament": {"material": "PLA", "color_hex": "nothex"}, "vendor_name": "OrphanVendorXYZ", "spool": {}})
        self.assertEqual(len(sm.vendors()), before)


if __name__ == "__main__":
    unittest.main()
