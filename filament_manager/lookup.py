"""Find filament data for a scanned tag or a UPC barcode.

Sources, in order:
  1. Your Spoolman: spools holding the tag UID, filaments whose article_number is the UPC.
  2. upcitemdb.com (UPC only): product title / brand, parsed for material, color, weight.
  3. SpoolmanDB, via Spoolman's /external/filament/search: density, temps, spool weight.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .spoolman import SpoolmanError, card_uids, decode_extra_str, hex_distance, match_score, spool_uids

UPCITEMDB = "https://api.upcitemdb.com/prod/trial/lookup?upc={upc}"

MATERIALS = ["PLA+", "PLA-CF", "PETG-CF", "PA-CF", "PA6-CF", "PAHT-CF", "ABS-GF", "PETG", "PCTG", "PLA",
             "ABS", "ASA", "TPU", "TPE", "PA6", "PA12", "PA", "PC", "PET", "PVA", "HIPS", "PP", "PVB", "PEEK"]
MATERIAL_ALIASES = {"NYLON": "PA", "PLA PLUS": "PLA+", "CARBON FIBER PLA": "PLA-CF", "FLEXIBLE": "TPU"}
VARIANTS = ["Silk", "Matte", "Wood", "Marble", "Glow", "Sparkle", "Galaxy", "Rainbow", "Dual Color",
            "Tri Color", "Translucent", "Transparent", "Metal", "Carbon Fiber", "High Speed", "HF", "Rapid",
            "Basic", "Pro", "Tough", "Lite", "Glitter", "Gradient"]
COLORS = {
    "black": "000000", "white": "FFFFFF", "grey": "808080", "gray": "808080", "silver": "C0C0C0",
    "red": "D32F2F", "orange": "FF6A13", "yellow": "FFD500", "gold": "D4AF37", "green": "2E7D32",
    "lime": "9ACD32", "olive": "708238", "mint": "98FF98", "teal": "008080", "cyan": "00BCD4",
    "blue": "1565C0", "navy": "1A237E", "sky blue": "87CEEB", "light blue": "90CAF9", "purple": "7B1FA2",
    "violet": "8F00FF", "magenta": "FF00FF", "pink": "F48FB1", "brown": "795548", "beige": "F5F5DC",
    "bronze": "CD7F32", "copper": "B87333", "ivory": "FFFFF0", "transparent": "F0F0F0", "clear": "F0F0F0",
    "natural": "F3EBD3", "wood": "A0522D", "jade": "00A86B", "coral": "FF7F50", "peach": "FFCBA4",
}


class LookupError_(RuntimeError):
    pass


def norm_upc(code):
    digits = re.sub(r"\D", "", code or "")
    if len(digits) not in (8, 12, 13, 14):
        raise LookupError_("A UPC/EAN barcode has 8, 12, 13 or 14 digits.")
    return digits


def parse_title(title, brand=None, color=None, weight=None):
    """Guess filament fields from a product title like
    'HATCHBOX PLA 3D Printer Filament, 1.75mm, 1kg Spool, Silk Gold'."""
    t = " " + (title or "") + " "
    up = t.upper()
    out = {}
    if brand:
        out["brand"] = brand.strip()
    for alias, mat in MATERIAL_ALIASES.items():
        if alias in up:
            out["type"] = mat
            break
    for mat in MATERIALS:  # longest/most specific first
        if re.search(r"(?<![A-Z])" + re.escape(mat) + r"(?![A-Z])", up):
            out["type"] = mat
            break
    for v in VARIANTS:
        if re.search(r"\b" + re.escape(v) + r"\b", t, re.I):
            out["subtype"] = v
            break
    color_text = (color or "") + " " + t
    best = None
    for name in sorted(COLORS, key=len, reverse=True):
        m = re.search(r"\b" + re.escape(name) + r"\b", color_text, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    if best:
        out["color_name"] = best[1]
        out["color_hex"] = COLORS[best[1]]
    m = re.search(r"(?<![\d.])(1\.75|2\.85|3(?:\.0)?)\s*mm", t, re.I)
    if m:
        out["diameter"] = float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g|lbs?)\b", t + " " + (weight or ""), re.I)
    if m:
        n, unit = float(m.group(1)), m.group(2).lower()
        grams = n * 1000 if unit == "kg" else n * 453.6 if unit.startswith("lb") else n
        if 100 <= grams <= 10000:
            out["weight"] = round(grams / 50) * 50
    return out


def upcitemdb(upc, timeout=10):
    req = urllib.request.Request(UPCITEMDB.format(upc=urllib.parse.quote(upc)),
                                 headers={"Accept": "application/json", "User-Agent": "filament-manager"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise LookupError_("upcitemdb's free daily limit is used up. Try again tomorrow.") from None
        raise LookupError_(f"upcitemdb returned {e.code}.") from None
    except (urllib.error.URLError, OSError) as e:
        raise LookupError_(f"Can't reach upcitemdb: {e}") from None
    items = data.get("items") or []
    return items[0] if items else None


def external_to_fields(ext):
    """SpoolmanDB filament -> form fields."""
    f = {
        "brand": ext.get("manufacturer"), "type": ext.get("material"), "name": ext.get("name"),
        "density": ext.get("density"), "weight": ext.get("weight"), "spool_weight": ext.get("spool_weight"),
        "diameter": ext.get("diameter"), "color_hex": ext.get("color_hex") or (ext.get("color_hexes") or [None])[0],
        "extruder_temp": ext.get("extruder_temp"), "bed_temp": ext.get("bed_temp"),
        "external_id": ext.get("id"),
    }
    if ext.get("color_hexes") and len(ext["color_hexes"]) > 1:
        f["multi_color_hexes"] = ",".join(ext["color_hexes"])
    for flag, label in (("glow", "Glow"), ("translucent", "Translucent")):
        if ext.get(flag):
            f.setdefault("subtype", label)
    if ext.get("finish") and ext["finish"] != "glossy":
        f["subtype"] = str(ext["finish"]).title()
    if ext.get("pattern"):
        f["subtype"] = str(ext["pattern"]).title()
    return {k: v for k, v in f.items() if v not in (None, "")}


def external_score(ext, want):
    score = 0.0
    if want.get("type") and str(ext.get("material", "")).upper() == str(want["type"]).upper():
        score += 3
    if want.get("brand") and str(ext.get("manufacturer", "")).lower() == str(want["brand"]).lower():
        score += 2
    color = ext.get("color_hex") or (ext.get("color_hexes") or [None])[0]
    if want.get("color_hex") and color:
        score += 3 * max(0.0, 1 - hex_distance(color, want["color_hex"]) * 4)
    name = str(ext.get("name", "")).lower()
    if want.get("color_name") and want["color_name"] in name:
        score += 1
    if want.get("subtype") and str(want["subtype"]).lower() in name:
        score += 0.5
    return round(score, 2)


def search_external(sm, want, limit=6):
    """Rank SpoolmanDB filaments for the wanted fields. Quietly empty if unavailable."""
    words = [want.get("brand"), want.get("type"), want.get("subtype"), want.get("color_name")]
    query = " ".join(str(w) for w in words if w)
    if not query:
        return []
    results = []
    # Narrow queries first; fall back to broader ones if nothing matches word-by-word.
    for q in dict.fromkeys([query, " ".join(str(w) for w in words[:2] if w)]):
        try:
            results = sm.external_search(q, limit=50) or []
        except SpoolmanError:
            return []
        if results:
            break
    ranked = sorted(({"external": e, "score": external_score(e, want), "fields": external_to_fields(e)}
                     for e in results), key=lambda x: -x["score"])
    return [r for r in ranked if r["score"] > 0][:limit]


def filament_to_fields(fil):
    """Spoolman filament -> form fields."""
    f = {
        "brand": (fil.get("vendor") or {}).get("name"), "type": fil.get("material"), "name": fil.get("name"),
        "density": fil.get("density"), "weight": fil.get("weight"), "spool_weight": fil.get("spool_weight"),
        "diameter": fil.get("diameter"), "color_hex": fil.get("color_hex"),
        "multi_color_hexes": fil.get("multi_color_hexes"),
        "extruder_temp": fil.get("settings_extruder_temp"), "bed_temp": fil.get("settings_bed_temp"),
        "subtype": decode_extra_str((fil.get("extra") or {}).get("variant")),
        "upc": fil.get("article_number"), "price": fil.get("price"),
    }
    return {k: v for k, v in f.items() if v not in (None, "")}


def tag_lookup(sm, tag):
    """Everything the page needs after a scan: linked spools and filament suggestions."""
    out = {"linked_spools": [], "suggestions": [], "external": []}
    spools = sm.spools()
    out["linked_spools"] = sm.spools_with_uid(tag["uid"], spools)
    if not out["linked_spools"] and sm.store == "native":
        # Linked earlier through the card_uids field? Move it to a native tag now.
        old = [s for s in spools if tag["uid"] in card_uids(s)]
        if len(old) == 1:
            out["linked_spools"] = [sm.link(old[0]["id"], tag["uid"], tag_format=tag.get("format"))["spool"]]
            out["migrated"] = True
    want = tag.get("filament") or {}
    if want and not out["linked_spools"]:
        filaments = sm.filaments()
        out["suggestions"] = [{"filament": s["filament"], "score": s["score"],
                               "fields": filament_to_fields(s["filament"])}
                              for s in sm.suggestions(want, filaments)]
        # A legacy OpenSpool spool_id pointing at a real spool is a strong hint.
        sid = want.get("spool_id")
        if sid:
            out["legacy_spool"] = next((s for s in spools if s["id"] == int(sid)), None)
        out["partner_spools"] = partner_spools(sm, spools, want)
        out["external"] = search_external(sm, want)
    return out


PARTNER_MIN_SCORE = 7  # material + brand + a near-identical color


def partner_spools(sm, spools, want, limit=3):
    """Spools this tag may be the second tag of.

    Some spools carry two tags (Snapmaker puts one on each side) with the same
    data but different UIDs. Offer spools that match the tag's filament closely
    and have exactly one tag linked so far, newest first.
    """
    found = []
    for s in spools:
        if s.get("archived") or len(spool_uids(s, sm.store)) != 1:
            continue
        score = match_score(s.get("filament") or {}, want)
        if score >= PARTNER_MIN_SCORE:
            found.append((score, s["id"], s))
    found.sort(key=lambda x: (-x[0], -x[1]))
    return [s for _, _, s in found[:limit]]


def upc_lookup(sm, code, online=True):
    upc = norm_upc(code)
    out = {"upc": upc, "filaments": [], "product": None, "fields": {}, "external": [], "notes": []}
    if sm is not None:
        try:
            out["filaments"] = [{"filament": f, "fields": filament_to_fields(f)}
                                for f in sm.filaments() if re.sub(r"\D", "", f.get("article_number") or "") == upc]
        except SpoolmanError as e:
            out["notes"].append(str(e))
    if out["filaments"]:
        out["fields"] = dict(out["filaments"][0]["fields"])
        return out
    if online:
        try:
            item = upcitemdb(upc)
        except LookupError_ as e:
            item = None
            out["notes"].append(str(e))
        if item:
            out["product"] = {k: item.get(k) for k in ("title", "brand", "color", "size", "weight", "images")}
            out["fields"] = parse_title(item.get("title"), item.get("brand"), item.get("color"),
                                        item.get("size") or item.get("weight"))
        elif not out["notes"]:
            out["notes"].append("upcitemdb doesn't know this barcode.")
    if sm is not None and out["fields"]:
        out["external"] = search_external(sm, out["fields"])
        if out["external"]:
            best = out["external"][0]["fields"]
            out["fields"] = {**best, **{k: v for k, v in out["fields"].items() if v}}
    out["fields"]["upc"] = upc
    return out
