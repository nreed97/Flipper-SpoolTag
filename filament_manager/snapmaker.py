"""Snapmaker filament tags (Mifare Classic 1K, the U1's native format).

How the U1 reads them (Snapmaker's open-source u1-klipper, fm175xx_reader.py
and filament_protocol.py; also implemented by OpenRFID):

* Every sector has its own key A and key B, derived from the tag's UID with
  HKDF-SHA256: PRK = HMAC(salt, uid[0:4]); key = HMAC(PRK, "key_a_<n>" | 0x01)[:6].
  Key A uses the first 25 bytes of the salt, key B the whole salt. The salt is
  published in u1-klipper, so any reader that knows the UID can unlock the tag.
* Sectors 0-9 hold the filament data (little-endian numbers, offsets below).
* The first 48 bytes of sectors 10-15 hold an RSA-2048 PKCS#1 v1.5 SHA-256
  signature over bytes 0-639 (sectors 0-9, trailers included). Snapmaker's
  public keys are listed below by key version.

The Flipper's NFC CLI `dump -p mfc` only reads with keys from its key cache
(/ext/nfc/.cache/<UID>.keys), so flipper.py writes the derived keys there
first. `key_cache_file` builds that file.
"""

import base64
import hashlib
import hmac

SALT = b"Snapmaker_qwertyuiop[,.;]_1q2w3e"
SECTORS = 16
SIZE = 1024

MAIN_TYPES = {1: "PLA", 2: "PETG", 3: "ABS", 4: "TPU", 5: "PVA"}
SUB_TYPES = {1: "Basic", 2: "Matte", 3: "SnapSpeed", 4: "Silk", 5: "Support",
             6: "HF", 7: "95A", 8: "95A HF"}

# Snapmaker's RSA public keys (DER SubjectPublicKeyInfo, base64) by key version.
PUBLIC_KEYS = {
    0: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA8oEF7YuKO863TbUxnrvY"
        "H1JFrvCnMapm8Ho952KlfNWbf6IEDMlX6QJpBuvUkrkjWpLJJQurIWL3KFeLUhCh"
        "POrYdiGrdsUlp4YO037iLSlgmzo1dUdgbawAcGox1PvR/Naw5ADibubO2rN49WQR"
        "+BkxxigvoWHSFetaoMCswQ5B/niq3byhzktgmWOcv71F4yFwcxivF8R+s0gSBL4i"
        "/1zNeSUZkbvP4/T0B08i3D+e6fl9xpCnINZ3P9OWcx+p3SB2o4TdmAeKV4hkT9n7"
        "o+/OWr92fx6qbiNKJr04oMhrRsFK6w7hitp2n8RGS64w9lhtplnBgxtbgxAYyUnp"
        "qwIDAQAB"
    ),
    1: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA8nbtQNABbc5PkyzI0A5m"
        "VH/E8y23Wld0iykvTOoBYJOrPwJDmXsnSyyX84Nv6voSr8FYv3Fb2SqSdOgQLFqp"
        "BXvntXew8rPpq5Ll8gSzLRxE1VmEOVtZWCTJ4Wxwwi79rrFmpa/nAtUeYZIGiiud"
        "w2MzCHXW5G3c1FWhQ0C8vUUMfBQXmGnoHGsul6R8xld6CDCWY8ia/FvfR+KCtMRn"
        "VYyYguYsq4rODWJHiFCOef4FZconUR3RTh0ojvq78CsHk94goxidWzZoKcVnvWhh"
        "bOixTjU37W4JDECEOui3ObMMvJkzxkZo1irlAH7jTiPqhP94U/JbRDpBlHOOn67b"
        "GQIDAQAB"
    ),
    2: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxZQPYewwMFaPlcEHq+SH"
        "QS1C1NhVmAaY56qxLyHJ4aNc2iWdCx4/9ZKY4CL6xkeCD88Zndv/xzImplRdoAzo"
        "whD47Vm4iuq8+NqHUI8na6ISd+MZ/O6/eo/ggaEZBX8lR+Yf0qfWtntsI9flUOoJ"
        "mq1IXvNXqOxflUmPyffT40QSkAN4Rr3scB3ozlxuJZehWM/lUmZ1H5PQDwAqsM0T"
        "Rj6ChzVmUbSvwEvbDTwpXkpMA0C5//OW0T//IKDEBYxEl928vYbraLRDRIetgdaD"
        "o+77+ztfOv4AyP/ipikprHwIWi7yga5KUXq/XpNPy6cPISZD+/LBUJBxLELspREP"
        "rQIDAQAB"
    ),
    3: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvK8cJyeFeTkFgkSLCCAg"
        "EgR9KAvIHmvK8CRdtn+W6PiIbN04MFIg8jiYW/3fq+AcBFFMo+HtR2gym8JNVx2I"
        "RDI4WdfbR/0gaIHjOQ41OwlXmqqSkDsFmjxVI6bDRZYpHkOfkC+9Vi1Aii4l/Yq9"
        "O7s+2j4zP9GoUWWJPb3mW07Vu+EnHB/XIuaoDJVQAS+ov3xTotCeKdcdgySnNP5g"
        "kOvWUvWtwNQldCRcQ0eo3j5RO+4J4IRK2J8q7BrdV/gbJUE/BBPIOuURPLzNJJO3"
        "wgx4PEwlb5uYEUL35ARL7NzL8ZOxebzs5H4tXuWrBhALw6O33Tfg3TmTmwR2JUpv"
        "7QIDAQAB"
    ),
    4: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvafhk7Bdb3F+5B9w7YXv"
        "chrNzl09QkZc27NLxL0ViRitGQhX9KC/xVg+XkBGI8XfioAwYkJ3jYgwmci5gJOL"
        "ofPyNXcFtvtzq2NZNuDZY26krrXLORhS1o8ue92RB2gM92Rc2heWVrsvLycNl2Qz"
        "OUjUEGmWpSMo98xIsgkTZJ4aYxWVN86yqknOcHVpTmcr5SBRB90K9hTRtsaMD97O"
        "FYVc7AA/TGwqFJOnXXzWczWtg7kUY2vqCHwsvKs3G/EIFKOIe1n37V94OcxHTySC"
        "co9Kc6Y0bGFIwIruinH1WkFVt6TAzo+0ZdZy5Sq493AG9y1RZ5nYj5qUmc1PMmrD"
        "gwIDAQAB"
    ),
    5: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxWdxd7qeouSFbZ2Sldv3"
        "apDrgAupOYiDRkO85C+qkZaezOzqW0EsOV0x7nG/smw++TRfHyGIK4gXCdg1JfNR"
        "WYjqckRdnLYMzGdDk24VV5Bbrsgska0v0Oy1ucz3CYu+F22ais00OqK0MY0B96MI"
        "/B/0pRSTAIyxvC6LjhHy8DYyPdqNF9EMikKfAfcn7ytsH1PoSSGVtrZqyNe5OLrW"
        "yAw+FQsTg/VFJcYxPTQJ1ymwQmDCdKgApe3PVajyYswoIA7R0S8ujau0aAFEO3dU"
        "GDEwjOnaHfwFlg3OKMFJTxc2sl/WEB8xtWuKl0Guf0VnzWJ6noxqf/DiaN1fuHG0"
        "AwIDAQAB"
    ),
    6: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqF+YJNHLHC6c25oTDgNg"
        "liahUxWBPSkgght1/gJu5vBRDKWEn6i/RuKAFdTOsH+Hlvr5qWms7bBUHx78UMF+"
        "FF1Nq9tb4jhFuqq4HWsBBjNnU6O0JhFTjKJU2nudmphXlpdLQfcKSIYMQe795GHL"
        "izh8WsNTcTHNNBkjhi7y4c4RUqnJso0L6vrf0B3EB/9DDUJitrwfw+1/OrKOEVEP"
        "624sEa802cHfb+BG9zKBXjFwzYCYF9gWey9yeA3UA7EYmPpqA1lqNv8m0r7YjZ4n"
        "uGBDjs+AXaGtdqrW3IUtkUF2vWwNSRncbcXi3mNfzslrtPhsDVAFki4vDSw7yNht"
        "2wIDAQAB"
    ),
    7: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuKWRCTTgxPltfflWHdhu"
        "2ITxWC/LTEl7OtatNWFhMFQZF2J5SN/45bjH6xIPTcDglTSl2/UMC1D/ugiq+j0z"
        "dGSdE7xn3ZSzLTMCwgRkvXmd8aQgafBYbB7E6oAgus+6lRXZPwnMfZAe0yaJNHyt"
        "1Wd8ZUlRY7BHSPPtmG1liVEzxoTb6urB6mK49r24+oC7xa65q5NSdlZWSTeaK4Xt"
        "DVVDiwe+uubNTm59KnVAKgBMNd3qN942pH6fo/dBz++BzJVEG/qJewHUTGZAeIl+"
        "CgqhSEbmEIgolsDgaKY99ZWa2FWJdo+ohYhmjc92TyB9kWw6yIwez+tlRUkssLGt"
        "SwIDAQAB"
    ),
    8: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAt7XOTs6P2xB8v8/xWVdR"
        "wVefphRDXSuv74RObtr0pwLTc7BytkcDw8r60BNPv9hGDpW2S1szxqS8x4EaOHP7"
        "81qNpIUULlUdXxty1RvpSdfRb044kpwl7A/s4OEakkyJZF1ed+Qte1FqOFDDIZ+l"
        "g+Co8FjOwWixoSyIlR22mEP7r6Y98GL5tnSohkVoGAgEipswWb6549mssjZmES+J"
        "hB0axY6Dl/LlDYxN6jjUZwSIo7bw0GXGm9ScW2qTVaT1m2A9etpD6OIG+iQVLQqP"
        "whVBs5q0o/EM4nBN88RBsF2OmfkcZPJ2NdX6o3qx+pCZ9NDgkHjGDZdnGEnM5Lu2"
        "dwIDAQAB"
    ),
    9: (
        "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAz/d5C5FpqlcF7NbUEvBN"
        "fiDJWH0BF63PEwHPiX+cS6l+q4NqqYI167u1pGkZGJV1njgGYFTM08x2KO7/bk6o"
        "CWcGuKWNM8Tp1+tv3XioNGVCnIpHmdUx5F9qcXlPtDx74wQk/+JZLQ/sLnLvHcV3"
        "YTaz55fpyzVUHkgXusdVynSyAt3ywWWQRcjp3sspGa/udC0j6LCvrzqLACv3gMGA"
        "Id0b6REzjSn03UzkwBIwSb8DszieeNhaCOK4M/TxPFNyrhQRYcAvhiZJu+tylqJs"
        "VP+gaWFvElFeFkxcHvYXHdJPlJLjYeT51hm/pdll26yYLhpeBa0inHwSqv4D3jFZ"
        "PQIDAQAB"
    ),
}


def _hkdf6(salt, ikm, info):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info.encode() + b"\x01", hashlib.sha256).digest()[:6]


def sector_keys(uid):
    """[(key_a, key_b)] for the 16 sectors of the tag with this UID (bytes)."""
    ikm = bytes(uid[:4])
    return [(_hkdf6(SALT[:25], ikm, f"key_a_{n}"), _hkdf6(SALT, ikm, f"key_b_{n}"))
            for n in range(SECTORS)]


def key_cache_file(uid):
    """Text of a Flipper NFC key cache file holding this tag's keys."""
    h = lambda b: " ".join(f"{x:02X}" for x in b)  # noqa: E731
    lines = ["Filetype: Flipper NFC keys", "Version: 1", "Mifare Classic type: 1K",
             "Key A map: 000000000000FFFF", "Key B map: 000000000000FFFF"]
    for n, (a, b) in enumerate(sector_keys(uid)):
        lines += [f"Key A sector {n}: {h(a)}", f"Key B sector {n}: {h(b)}"]
    return "\n".join(lines) + "\n"


# ---- signature ------------------------------------------------------------

def _der(data, pos):
    """Read one DER element: (tag, value start, value end)."""
    tag, n = data[pos], data[pos + 1]
    pos += 2
    if n & 0x80:
        size = n & 0x7F
        n = int.from_bytes(data[pos:pos + size], "big")
        pos += size
    return tag, pos, pos + n


def _rsa_key(b64):
    der = base64.b64decode("".join(b64))
    _, start, _ = _der(der, 0)                 # SubjectPublicKeyInfo
    _, _, end = _der(der, start)               # skip AlgorithmIdentifier
    _, bits, _ = _der(der, end)                # BIT STRING
    _, seq, _ = _der(der, bits + 1)            # RSAPublicKey (after unused-bits byte)
    _, ms, me = _der(der, seq)
    _, es, ee = _der(der, me)
    return int.from_bytes(der[ms:me], "big"), int.from_bytes(der[es:ee], "big")


_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def verify_signature(data):
    """(ok, key_version) for a 1024-byte dump."""
    version = int.from_bytes(data[138:140], "little")
    if version not in PUBLIC_KEYS:
        return False, version
    n, e = _rsa_key(PUBLIC_KEYS[version])
    sig = b"".join(data[s * 64:s * 64 + 48] for s in range(10, 16))[:256]
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(256, "big")
    digest = hashlib.sha256(bytes(data[:640])).digest()
    expected = b"\x00\x01" + b"\xff" * (256 - 3 - len(_SHA256_PREFIX) - 32) + b"\x00" + _SHA256_PREFIX + digest
    return hmac.compare_digest(em, expected), version


# ---- data -----------------------------------------------------------------

def blocks_to_bytes(blocks):
    """Flipper .nfc blocks {n: 16 bytes | None} -> 1024 bytes (unknown = zeros), all_known."""
    out = bytearray(SIZE)
    complete = True
    for n in range(SIZE // 16):
        b = blocks.get(n)
        if b is None or len(b) != 16:
            complete = False
            continue
        out[n * 16:n * 16 + 16] = b
    return bytes(out), complete


def _u16(d, off):
    return int.from_bytes(d[off:off + 2], "little")


def _text(b):
    return b.split(b"\x00")[0].decode("ascii", "replace").strip()


def looks_like(data):
    """Cheap check before decoding: a known material code and sane color count."""
    return len(data) == SIZE and _u16(data, 66) in MAIN_TYPES and data[72] <= 5


def parse(data):
    """Decode a 1024-byte Snapmaker tag into filament fields (None if it isn't one)."""
    if not looks_like(data):
        return None
    d = data
    colors = [d[80 + 3 * i:83 + 3 * i].hex().upper() for i in range(max(1, d[72]))]
    bed = _u16(d, 154)
    out = {
        "brand": _text(d[16:32]),
        "manufacturer": _text(d[32:48]),
        "type": MAIN_TYPES[_u16(d, 66)],
        "subtype": SUB_TYPES.get(_u16(d, 68), ""),
        "color_hex": colors[0],
        "additional_color_hexes": colors[1:],
        "alpha": f"{0xFF - d[73]:02X}",
        "sku": str(int.from_bytes(d[96:100], "little") or ""),
        "diameter": _u16(d, 128) / 100 or None,
        "weight": _u16(d, 130),
        "length_m": _u16(d, 132),
        "dry_temp": _u16(d, 144),
        "dry_time_h": _u16(d, 146),
        "max_temp": _u16(d, 148),
        "min_temp": _u16(d, 150),
        "bed_min_temp": bed,
        "bed_max_temp": bed,
        "first_layer_temp": _u16(d, 156),
        "other_layer_temp": _u16(d, 158),
        "mfg_date": _text(d[160:168]),
        "tray": _u16(d, 70),
    }
    return {k: v for k, v in out.items() if v not in ("", None, 0, [])}
