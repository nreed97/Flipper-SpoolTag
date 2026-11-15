"""Spoolman client, speaking the conventions the U1 extended firmware's SpoolLink uses.

- Tag UIDs: uppercase hex, no separators.
- Default store: spool extra field `card_uids`, a JSON-encoded comma-separated
  string ("\\"AABBCCDD,11223344\\""). A UID belongs to at most one spool.
- Alternative store: Spoolman's native tags (v0.27+), POST /spool/{id}/tag.
- Filament variant (Silk, Matte, ...) lives in filament extra field `variant`,
  same JSON-encoded string convention.
See docs/design/spoolman.md in paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

FIELDS = {
    ("spool", "card_uids"): {"name": "Card UIDs", "field_type": "text", "order": 1, "default_value": '""'},
    ("filament", "variant"): {"name": "Variant", "field_type": "text", "order": 1, "default_value": '""'},
}


class SpoolmanError(RuntimeError):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def norm_uid(uid):
    return re.sub(r"[^0-9A-Fa-f]", "", uid or "").upper()


def decode_extra_str(value):
    """Spoolman extra values are JSON-encoded; tolerate raw strings too."""
    if value is None:
        return ""
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, str) else str(decoded)
    except (TypeError, ValueError):
        return str(value)


def encode_extra_str(value):
    return json.dumps(value or "")


def card_uids(spool):
    raw = decode_extra_str((spool.get("extra") or {}).get("card_uids"))
    return [u for u in (norm_uid(x) for x in raw.split(",")) if u]


def spool_uids(spool, store):
    if store == "native":
        return [norm_uid(t.get("uid")) for t in spool.get("tags") or []]
    return card_uids(spool)


def hex_distance(a, b):
    """0 (same) .. 1 (opposite) color distance between two hex colors."""
    try:
        ca = [int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        cb = [int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    except (ValueError, AttributeError, IndexError):
        return 1.0
    return (sum((x - y) ** 2 for x, y in zip(ca, cb)) ** 0.5) / 441.7


def match_score(filament, want):
    """How well a Spoolman filament matches decoded tag fields. Higher is better."""
    score = 0.0
    if want.get("type") and (filament.get("material") or "").upper() == str(want["type"]).upper():
        score += 3
    vendor = ((filament.get("vendor") or {}).get("name") or "").lower()
    if want.get("brand") and vendor == str(want["brand"]).lower():
        score += 2
    if want.get("color_hex") and filament.get("color_hex"):
        score += 3 * max(0.0, 1 - hex_distance(filament["color_hex"], want["color_hex"]) * 4)
    variant = decode_extra_str((filament.get("extra") or {}).get("variant")).lower()
    if want.get("subtype") and variant == str(want["subtype"]).lower():
        score += 0.5
    return round(score, 2)


class Spoolman:
    def __init__(self, base_url, store="card_uids", timeout=10):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.store = store
        self.timeout = timeout
        self._fields_ok = False

    # ---- HTTP -----------------------------------------------------------

    def request(self, method, path, body=None, query=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"message": raw.decode("utf-8", "replace")}
            msg = parsed.get("message") or parsed.get("detail") or str(parsed)
            raise SpoolmanError(f"Spoolman {method} {path}: {e.code} {msg}", e.code, parsed) from None
        except (urllib.error.URLError, OSError) as e:
            raise SpoolmanError(f"Can't reach Spoolman at {self.base}: {e}") from None

    # ---- setup ----------------------------------------------------------

    def info(self):
        return self.request("GET", "/info")

    def ensure_fields(self):
        """Create the card_uids / variant extra fields if missing (same definitions as SpoolLink)."""
        if self._fields_ok:
            return
        for (entity, key), definition in FIELDS.items():
            if key == "card_uids" and self.store == "native":
                continue  # native tags don't need the extra field
            existing = {f["key"] for f in self.request("GET", f"/field/{entity}") or []}
            if key not in existing:
                self.request("POST", f"/field/{entity}/{key}", definition)
        self._fields_ok = True

    # ---- reads ----------------------------------------------------------

    def vendors(self):
        return self.request("GET", "/vendor")

    def filaments(self):
        return self.request("GET", "/filament")

    def spools(self, allow_archived=True):
        return self.request("GET", "/spool", query={"allow_archived": str(allow_archived).lower(), "limit": 1000})

    def spool(self, spool_id):
        return self.request("GET", f"/spool/{spool_id}")

    def materials(self):
        return self.request("GET", "/material")

    def locations(self):
        return self.request("GET", "/location")

    def external_search(self, query, limit=20):
        return self.request("GET", "/external/filament/search", query={"query": query, "limit": limit})

    def spools_with_uid(self, uid, spools=None):
        uid = norm_uid(uid)
        if self.store == "native":
            return self.request("GET", "/spool", query={"tag": uid, "allow_archived": "true"}) or []
        return [s for s in (spools or self.spools()) if uid in card_uids(s)]

    def suggestions(self, want, filaments=None, limit=8):
        scored = []
        for f in filaments or self.filaments():
            s = match_score(f, want)
            if s > 0:
                scored.append({"filament": f, "score": s})
        scored.sort(key=lambda x: -x["score"])
        return scored[:limit]

    # ---- writes ---------------------------------------------------------

    def find_or_create_vendor(self, name, created):
        name = (name or "").strip()
        if not name:
            return None
        for v in self.vendors():
            if v["name"].lower() == name.lower():
                return v["id"]
        v = self.request("POST", "/vendor", {"name": name})
        created.append(("vendor", v["id"]))
        return v["id"]

    def save(self, payload):
        """Create or update vendor / filament / spool in one go.

        payload = {
          "spool_id": 12 | None,           # existing spool to update
          "filament_id": 5 | None,         # existing filament to use
          "filament": {...} | None,        # new filament fields, or changed fields of filament_id
          "vendor_name": "Hatchbox",       # used when creating a filament
          "spool": {...},                  # spool fields to set (only changed ones on update)
        }
        Anything created before a failure is deleted again, so a failed save
        doesn't leave orphan records behind.
        """
        self.ensure_fields()
        created = []
        try:
            filament_id = payload.get("filament_id")
            fil = dict(payload.get("filament") or {})
            if "variant" in fil:
                fil.setdefault("extra", {})["variant"] = encode_extra_str(fil.pop("variant"))
            if filament_id is None:
                if not fil.get("material"):
                    raise SpoolmanError("A new filament needs at least a material.", 400)
                vid = self.find_or_create_vendor(payload.get("vendor_name"), created)
                if vid is not None:
                    fil["vendor_id"] = vid
                fil.setdefault("density", 1.24)
                fil.setdefault("diameter", 1.75)
                f = self.request("POST", "/filament", fil)
                created.append(("filament", f["id"]))
                filament_id = f["id"]
            elif fil:
                if payload.get("vendor_name") is not None:
                    vid = self.find_or_create_vendor(payload["vendor_name"], created)
                    fil["vendor_id"] = vid
                self.request("PATCH", f"/filament/{filament_id}", fil)

            spool_fields = dict(payload.get("spool") or {})
            spool_fields.pop("extra", None)  # card_uids is only touched by link/unlink
            if payload.get("spool_id"):
                spool_fields["filament_id"] = filament_id
                spool = self.request("PATCH", f"/spool/{payload['spool_id']}", spool_fields)
            else:
                spool_fields["filament_id"] = filament_id
                spool = self.request("POST", "/spool", spool_fields)
                created.append(("spool", spool["id"]))
            return spool
        except Exception:
            for kind, obj_id in reversed(created):
                try:
                    self.request("DELETE", f"/{kind}/{obj_id}")
                except SpoolmanError:
                    pass
            raise

    def link(self, spool_id, uid, move=False, tag_format=None):
        """Attach a tag UID to a spool, removing it from any other spool.

        Without move=True, raises a 409 SpoolmanError listing the other spools,
        so the page can ask before moving the tag.
        """
        uid = norm_uid(uid)
        if not uid:
            raise SpoolmanError("No UID given.", 400)
        self.ensure_fields()
        others = [s for s in self.spools_with_uid(uid) if s["id"] != spool_id]
        if others and not move:
            raise SpoolmanError("Tag is linked to another spool.", 409,
                                {"conflict": [{"id": s["id"], "name": spool_label(s)} for s in others]})
        if self.store == "native":
            for s in others:
                self.request("DELETE", f"/spool/{s['id']}/tag/{uid}")
            target = self.spool(spool_id)
            if uid not in spool_uids(target, "native"):
                body = {"uid": uid}
                if tag_format:
                    body["format"] = tag_format
                self.request("POST", f"/spool/{spool_id}/tag", body)
            # Migrate: drop this UID from any card_uids list left over from the other store.
            for s in self.spools():
                if uid in card_uids(s):
                    self._set_card_uids(s["id"], [u for u in card_uids(s) if u != uid])
        else:
            target = self.spool(spool_id)
            uids = card_uids(target)
            if uid not in uids:
                uids.append(uid)
                self._set_card_uids(spool_id, uids)
            for s in others:
                self._set_card_uids(s["id"], [u for u in card_uids(s) if u != uid])
        return {"spool": self.spool(spool_id), "moved_from": [s["id"] for s in others]}

    def unlink(self, spool_id, uid):
        uid = norm_uid(uid)
        if self.store == "native":
            self.request("DELETE", f"/spool/{spool_id}/tag/{uid}")
        else:
            s = self.spool(spool_id)
            self._set_card_uids(spool_id, [u for u in card_uids(s) if u != uid])
        return self.spool(spool_id)

    def _set_card_uids(self, spool_id, uids):
        self.request("PATCH", f"/spool/{spool_id}",
                     {"extra": {"card_uids": encode_extra_str(",".join(uids))}})


def spool_label(s):
    f = s.get("filament") or {}
    vendor = (f.get("vendor") or {}).get("name") or ""
    return f"#{s['id']} {vendor} {f.get('material') or ''} {f.get('name') or ''}".replace("  ", " ").strip()


def openspool_fields(spool):
    """OpenSpool tag fields for a Spoolman spool (what gets written to the tag)."""
    f = spool.get("filament") or {}
    fields = {
        "type": f.get("material") or "",
        "color_hex": f.get("color_hex") or ((f.get("multi_color_hexes") or "").split(",")[0]) or "FFFFFF",
        "brand": (f.get("vendor") or {}).get("name") or "Generic",
        "subtype": decode_extra_str((f.get("extra") or {}).get("variant")),
        "weight": f.get("weight"),
        "diameter": f.get("diameter"),
        "spool_id": spool.get("id"),
    }
    if f.get("settings_extruder_temp"):
        t = f["settings_extruder_temp"]
        fields["min_temp"], fields["max_temp"] = t - 10, t + 10
    if f.get("settings_bed_temp"):
        t = f["settings_bed_temp"]
        fields["bed_min_temp"], fields["bed_max_temp"] = t - 5, t + 5
    multi = [c for c in (f.get("multi_color_hexes") or "").split(",") if c]
    if len(multi) > 1:
        fields["additional_color_hexes"] = multi[1:5]
    return fields
