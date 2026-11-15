"""Anycubic ACE tag format (raw NTAG pages, no NDEF).

Written by the Spool Programmer iOS app and DnG-Crafts ACE-RFID. Layout,
with offsets counted from the start of page 4 (checked against ACE-RFID's
Android readTag):

  0   magic 7B 00 65 00
  4   SKU, 16 bytes ASCII
  24  brand, 16 bytes ASCII
  44  material type, 16 bytes ASCII
  64  alpha, then color as B G R
  80  nozzle min / max temp, uint16 little-endian each
  100 bed min / max temp
  104 diameter (x100), length in meters
  108 weight in grams (uint16 LE)
"""

MAGIC = bytes([0x7B, 0x00, 0x65, 0x00])
LENGTH = 112  # bytes needed from page 4 (pages 4-31)


def is_ace(user_data):
    return user_data[:4] == MAGIC


def _text(b):
    return b.split(b"\x00")[0].decode("ascii", "replace").strip()


def _u16(b, off):
    return int.from_bytes(b[off:off + 2], "little")


def parse(user_data):
    """Return a dict of filament fields, or None if this is not an ACE tag."""
    if not is_ace(user_data) or len(user_data) < LENGTH:
        return None
    d = user_data
    color = d[65:68][::-1].hex().upper()
    if color == "010101":  # ACE writes black as 010101
        color = "000000"
    out = {
        "sku": _text(d[4:20]),
        "brand": _text(d[24:40]),
        "type": _text(d[44:60]),
        "color_hex": color,
        "alpha": f"{d[64]:02X}",
        "min_temp": _u16(d, 80),
        "max_temp": _u16(d, 82),
        "bed_min_temp": _u16(d, 100),
        "bed_max_temp": _u16(d, 102),
        "diameter": _u16(d, 104) / 100 or None,
        "length_m": _u16(d, 106),
        "weight": _u16(d, 108),
    }
    return {k: v for k, v in out.items() if v not in ("", None, 0)}
