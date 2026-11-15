"""Minimal NDEF encode/decode for NFC Forum Type 2 tags (NTAG213/215/216).

Type 2 tag memory layout (4-byte pages):
  pages 0-2   UID, lock bytes
  page  3     Capability Container (CC): E1 10 <size/8> 00
  page  4+    user data, as a series of TLVs. The NDEF message lives in a
              TLV with type 0x03, and 0xFE marks the end.
"""

from dataclasses import dataclass

TLV_NULL, TLV_NDEF, TLV_TERMINATOR = 0x00, 0x03, 0xFE

TNF_WELL_KNOWN, TNF_MIME = 0x01, 0x02

URI_PREFIXES = [
    "", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:",
    "ftp://anonymous:anonymous@", "ftp://ftp.", "ftps://", "sftp://", "smb://",
    "nfs://", "ftp://", "dav://", "news:", "telnet://", "imap:", "rtsp://", "urn:",
    "pop:", "sip:", "sips:", "tftp:", "btspp://", "btl2cap://", "btgoep://",
    "tcpobex://", "irdaobex://", "file://", "urn:epc:id:", "urn:epc:tag:",
    "urn:epc:pat:", "urn:epc:raw:", "urn:epc:", "urn:nfc:",
]


class NdefError(ValueError):
    pass


@dataclass
class NdefRecord:
    tnf: int
    type: bytes
    payload: bytes
    id: bytes = b""

    @property
    def mime_type(self):
        return self.type.decode("ascii", "replace") if self.tnf == TNF_MIME else None

    def describe(self):
        """A JSON-friendly summary of the record."""
        out = {"tnf": self.tnf, "type": self.type.decode("ascii", "replace"),
               "payload_hex": self.payload.hex().upper()}
        if self.tnf == TNF_MIME:
            try:
                out["text"] = self.payload.decode("utf-8")
            except UnicodeDecodeError:
                pass
        elif self.tnf == TNF_WELL_KNOWN and self.type == b"U" and self.payload:
            prefix = URI_PREFIXES[self.payload[0]] if self.payload[0] < len(URI_PREFIXES) else ""
            out["uri"] = prefix + self.payload[1:].decode("utf-8", "replace")
        elif self.tnf == TNF_WELL_KNOWN and self.type == b"T" and self.payload:
            lang_len = self.payload[0] & 0x3F
            enc = "utf-16" if self.payload[0] & 0x80 else "utf-8"
            out["text"] = self.payload[1 + lang_len:].decode(enc, "replace")
        return out


def parse_message(data):
    """Split raw NDEF message bytes into records."""
    records, i = [], 0
    while i < len(data):
        header = data[i]
        mb, me, sr, il = header & 0x80, header & 0x40, header & 0x10, header & 0x08
        tnf = header & 0x07
        if header & 0x20:
            raise NdefError("chunked NDEF records are not supported")
        i += 1
        type_len = data[i]; i += 1
        if sr:
            payload_len = data[i]; i += 1
        else:
            payload_len = int.from_bytes(data[i:i + 4], "big"); i += 4
        id_len = 0
        if il:
            id_len = data[i]; i += 1
        rtype = data[i:i + type_len]; i += type_len
        rid = data[i:i + id_len]; i += id_len
        payload = data[i:i + payload_len]; i += payload_len
        if len(payload) != payload_len:
            raise NdefError("NDEF record is truncated")
        records.append(NdefRecord(tnf, bytes(rtype), bytes(payload), bytes(rid)))
        if me:
            break
        del mb
    return records


def build_message(records):
    out = bytearray()
    for n, rec in enumerate(records):
        short = len(rec.payload) < 256
        header = rec.tnf & 0x07
        if n == 0:
            header |= 0x80
        if n == len(records) - 1:
            header |= 0x40
        if short:
            header |= 0x10
        if rec.id:
            header |= 0x08
        out.append(header)
        out.append(len(rec.type))
        out += len(rec.payload).to_bytes(1 if short else 4, "big")
        if rec.id:
            out.append(len(rec.id))
        out += rec.type + rec.id + rec.payload
    return bytes(out)


def find_ndef_tlv(user_data):
    """Return the NDEF message bytes from Type 2 user memory, or None if absent."""
    i = 0
    while i < len(user_data):
        t = user_data[i]
        if t == TLV_NULL:
            i += 1
            continue
        if t == TLV_TERMINATOR:
            return None
        if i + 1 >= len(user_data):
            return None
        length = user_data[i + 1]
        hdr = 2
        if length == 0xFF:
            length = int.from_bytes(user_data[i + 2:i + 4], "big")
            hdr = 4
        start = i + hdr
        if t == TLV_NDEF:
            value = user_data[start:start + length]
            if len(value) < length:
                raise NdefError("NDEF TLV runs past the end of the data read")
            return bytes(value)
        i = start + length
    return None


def ndef_tlv_end(user_data):
    """How many bytes of user memory hold meaningful TLVs (through the terminator).

    Lets the reader stop early instead of reading the whole tag. Returns None
    if more data is needed to tell.
    """
    i = 0
    while i < len(user_data):
        t = user_data[i]
        if t == TLV_NULL:
            i += 1
            continue
        if t == TLV_TERMINATOR:
            return i + 1
        if i + 1 >= len(user_data):
            return None
        length = user_data[i + 1]
        hdr = 2
        if length == 0xFF:
            if i + 3 >= len(user_data):
                return None
            length = int.from_bytes(user_data[i + 2:i + 4], "big")
            hdr = 4
        i += hdr + length
        if i > len(user_data):
            return None
    return None


def wrap_tlv(message):
    """Wrap an NDEF message in a TLV plus terminator, ready to write at page 4."""
    n = len(message)
    if n < 0xFF:
        head = bytes([TLV_NDEF, n])
    else:
        head = bytes([TLV_NDEF, 0xFF]) + n.to_bytes(2, "big")
    return head + message + bytes([TLV_TERMINATOR])


def mime_record(mime_type, payload):
    return NdefRecord(TNF_MIME, mime_type.encode("ascii"), payload)
