"""OpenSpool tag payloads, including the U1 extended firmware's extra fields.

Format: an NDEF record with MIME type application/json holding e.g.
  {"protocol": "openspool", "version": "1.0", "type": "PLA",
   "color_hex": "#FF0000", "brand": "Generic", "min_temp": 190, ...}
See https://openspool.io/rfid.html and the extended firmware's
docs/design/openspool.md.
"""

import json

from . import ndef

MIME = "application/json"
REQUIRED = ("type", "color_hex")
INT_FIELDS = ("min_temp", "max_temp", "bed_min_temp", "bed_max_temp", "weight")
FLOAT_FIELDS = ("diameter",)
STR_FIELDS = ("brand", "subtype", "alpha")
# spool_id is a legacy field: SpoolLink apps link a tag carrying it to that spool.
KNOWN = REQUIRED + INT_FIELDS + FLOAT_FIELDS + STR_FIELDS + ("additional_color_hexes", "spool_id")


def _hex_color(value):
    v = str(value).strip().lstrip("#").upper()
    if len(v) != 6 or any(c not in "0123456789ABCDEF" for c in v):
        raise ValueError(f"color must be 6 hex digits, got {value!r}")
    return v


def build_payload(fields):
    """Validate user fields and return the OpenSpool dict to write."""
    missing = [k for k in REQUIRED if not fields.get(k)]
    if missing:
        raise ValueError("missing required field(s): " + ", ".join(missing))
    out = {"protocol": "openspool", "version": "1.0",
           "type": str(fields["type"]).strip().upper(),
           "color_hex": "#" + _hex_color(fields["color_hex"])}
    for k in STR_FIELDS:
        if fields.get(k) not in (None, ""):
            out[k] = str(fields[k]).strip()
    for k in INT_FIELDS:
        if fields.get(k) not in (None, ""):
            out[k] = int(float(fields[k]))
    for k in FLOAT_FIELDS:
        if fields.get(k) not in (None, ""):
            out[k] = float(fields[k])
    extra = fields.get("additional_color_hexes") or []
    if extra:
        out["additional_color_hexes"] = [_hex_color(c) for c in extra][:4]
    if fields.get("spool_id") not in (None, ""):
        out["spool_id"] = int(fields["spool_id"])
    return out


def encode(fields):
    """Fields -> bytes to write to the tag starting at page 4."""
    payload = json.dumps(build_payload(fields), separators=(",", ":")).encode()
    return ndef.wrap_tlv(ndef.build_message([ndef.mime_record(MIME, payload)]))


def find_in_records(records):
    """Return the OpenSpool dict from a list of NDEF records, or None."""
    for rec in records:
        if rec.mime_type != MIME:
            continue
        try:
            data = json.loads(rec.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get("protocol", "")).lower() == "openspool":
            return data
    return None
