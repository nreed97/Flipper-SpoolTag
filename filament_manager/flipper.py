"""Drive a Flipper Zero's NFC CLI (Momentum mntm-012 / new OFW NFC CLI) over USB serial.

Syntax checked against the mntm-012 source (applications/main/nfc/cli).

Reading uses one fast NFC operation plus a file read:
  [main]  storage remove /ext/nfc/filament_manager.nfc
  [nfc]   dump -p mfu -f /ext/nfc/filament_manager.nfc -t 3000   (NTAG / Ultralight)
  [nfc]   dump -p 14_3a -f ... -t 3000      (Mifare Classic: get the UID first,
  [main]  storage write_chunk /ext/nfc/.cache/<UID>.keys N   put Snapmaker's derived
  [nfc]   dump -p mfc -f ... -t 8000         keys in the key cache, then dump)
  [main]  storage read /ext/nfc/filament_manager.nfc              (Flipper .nfc text file)

Writing, inside the nfc shell:
  mfu rdbl -b N               "Block: N Data: AA BB CC DD"
  mfu wrbl -b N -d AABBCCDD   prints "Success"

We avoid the `raw` command: in mntm-012 a second `raw` call reuses freed
memory and can hang the CLI for several seconds.

Only one program can hold the serial port: close qFlipper and the web CLI.
"""

import re
import threading
import time
from dataclasses import dataclass, field

from . import ace, ndef, openspool, snapmaker

FLIPPER_VID, FLIPPER_PID = 0x0483, 0x5740
NFC_PROMPT = "[nfc]>: "
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
HEX_BYTES = r"((?:[0-9A-Fa-f?]{2}[ ]?)+)"
BLOCK_RE = re.compile(r"Block:\s*(\d+)\s*Data:\s*" + HEX_BYTES)
DUMP_PATH = "/ext/nfc/filament_manager.nfc"
FIRST_USER_PAGE = 4
KEY_CACHE_DIR = "/ext/nfc/.cache"
CLASSIC_SAKS = {"08", "18", "09", "19", "28", "38", "88", "98"}


class FlipperError(RuntimeError):
    pass


class NoTag(FlipperError):
    pass


def hex_to_bytes(text):
    return bytes.fromhex(re.sub(r"[^0-9A-Fa-f]", "", text))


def parse_nfc_file(text):
    """Parse a Flipper .nfc file into header fields plus pages/blocks."""
    info, pages, blocks = {}, {}, {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        m = re.fullmatch(r"(Page|Block) (\d+)", key)
        if m:
            target = pages if m.group(1) == "Page" else blocks
            # Unknown Mifare Classic bytes are written as "??"
            target[int(m.group(2))] = None if "??" in value else hex_to_bytes(value)
        else:
            info[key] = value
    return info, pages, blocks


@dataclass
class TagInfo:
    uid: str            # uppercase hex, no separators (same form Spoolman/SpoolLink use)
    atqa: str = ""
    sak: str = ""
    device: str = ""    # e.g. "NTAG215", "Mifare Classic"
    family: str = ""    # ntag | mifare_classic
    capacity: int = 0   # Type 2 user bytes
    user_data: bytes = b""
    blocks: dict = field(default_factory=dict)   # Mifare Classic blocks we could read
    records: list = field(default_factory=list)
    format: str = "unknown"   # openspool | ace | snapmaker | ndef | blank | unformatted | uid_only
    filament: dict = None     # decoded filament fields, when the format carries them
    notes: list = field(default_factory=list)

    def to_json(self):
        return {
            "uid": self.uid, "atqa": self.atqa, "sak": self.sak, "device": self.device,
            "family": self.family, "capacity": self.capacity,
            "user_data_hex": self.user_data.hex().upper(),
            "blocks_read": len([b for b in self.blocks.values() if b]),
            "ndef_records": [r.describe() for r in self.records],
            "format": self.format, "filament": self.filament, "notes": self.notes,
        }


def decode_type2(tag, pages):
    """Fill in a TagInfo from NTAG/Ultralight pages (dict page -> 4 bytes)."""
    cc = pages.get(3) or b"\x00" * 4
    tag.capacity = cc[2] * 8 if cc[0] == 0xE1 else 504
    data = b""
    for p in range(FIRST_USER_PAGE, FIRST_USER_PAGE + tag.capacity // 4):
        if pages.get(p) is None:
            break
        data += pages[p]
    tag.user_data = data
    if ace.is_ace(data):
        tag.format, tag.filament = "ace", ace.parse(data)
        return tag
    if cc[0] != 0xE1:
        tag.format = "unformatted"
        tag.notes.append("Tag is not NDEF formatted (no Capability Container).")
        return tag
    try:
        msg = ndef.find_ndef_tlv(data)
        tag.records = ndef.parse_message(msg) if msg else []
    except (ndef.NdefError, IndexError) as e:
        tag.notes.append(f"Could not parse NDEF data: {e}")
    spool = openspool.find_in_records(tag.records)
    if spool:
        tag.format, tag.filament = "openspool", spool
    elif tag.records:
        tag.format = "ndef"
    else:
        tag.format = "blank"
    return tag


def decode_classic(tag, blocks):
    """Fill in a TagInfo from Mifare Classic blocks (dict block -> 16 bytes | None)."""
    data, complete = snapmaker.blocks_to_bytes(blocks)
    known = len([b for b in blocks.values() if b])
    fil = snapmaker.parse(data)
    if not fil:
        tag.notes.append(f"Read {known} of {len(blocks)} blocks, but this isn't a format the app knows.")
        return tag
    ok, version = snapmaker.verify_signature(data) if complete else (False, None)
    fil["signature_ok"] = ok
    tag.format, tag.filament = "snapmaker", fil
    if not ok:
        tag.notes.append("Snapmaker data decoded, but its signature did not check out"
                         + ("" if complete else " (some blocks could not be read)") + ".")
    return tag


class Flipper:
    """One open serial connection to the Flipper.

    `ser` is anything with pyserial's read/write/in_waiting/reset_input_buffer,
    so tests can pass a fake.
    """

    def __init__(self, ser, debug=False):
        self.ser = ser
        self.debug = debug
        self.lock = threading.Lock()
        self.shell = None  # "main" or "nfc"
        self.port = None

    @classmethod
    def open(cls, port=None, debug=False):
        import serial
        from serial.tools import list_ports
        if not port:
            for p in list_ports.comports():
                if p.vid == FLIPPER_VID and p.pid == FLIPPER_PID:
                    port = p.device
                    break
            else:
                raise FlipperError("No Flipper found. Plug it in, close qFlipper, or pass --port.")
        flipper = cls(serial.Serial(port, 230400, timeout=0.1), debug=debug)
        flipper.port = port
        flipper.start()
        return flipper

    # ---- low level -------------------------------------------------------

    def _read_prompt(self, timeout):
        """Read until either prompt appears at the end of the output."""
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk.decode("utf-8", errors="replace")
                tail = ANSI.sub("", buf[-40:])
                if tail.endswith(">: "):
                    break
        out = ANSI.sub("", buf)
        if out.endswith(NFC_PROMPT):
            self.shell = "nfc"
        elif out.endswith(">: "):
            self.shell = "main"
        return out

    def command(self, text, timeout=5.0):
        self.ser.reset_input_buffer()
        self.ser.write(text.encode() + b"\r")
        out = self._read_prompt(timeout)
        if self.debug:
            print(f"--- [{self.shell}] {text}\n{out.strip()}\n---")
        return out

    def start(self):
        self.ser.write(b"\x03")
        self._read_prompt(3)
        self.ser.reset_input_buffer()
        if self.shell is None:
            raise FlipperError("No prompt from the Flipper. Is qFlipper or another terminal open?")

    def ensure_nfc(self):
        if self.shell != "nfc":
            self.command("nfc")
            if self.shell != "nfc":
                raise FlipperError("Flipper did not enter the NFC shell. Is the NFC app open on the Flipper?")

    def ensure_main(self):
        if self.shell != "main":
            self.command("exit")
            if self.shell != "main":
                raise FlipperError("Could not leave the NFC shell.")

    def close(self):
        try:
            self.ser.write(b"\x03")
            if self.shell == "nfc":
                self.ser.write(b"exit\r")
        except Exception:
            pass
        self.ser.close()

    # ---- reading --------------------------------------------------------

    def _dump_cmd(self, proto, ms):
        self.ensure_nfc()
        return self.command(f"dump -p {proto} -f {DUMP_PATH} -t {ms}", timeout=ms / 1000 + 5)

    def _read_dump_file(self):
        self.ensure_main()
        text = self.command(f"storage read {DUMP_PATH}", timeout=10)
        if "Size:" not in text:
            raise FlipperError("Could not read the dump file back: " + text.strip()[-200:])
        return parse_nfc_file(text)

    def _write_file(self, path, text):
        """Create `path` on the Flipper with `text` (main shell, write_chunk)."""
        self.ensure_main()
        self.command(f"storage remove {path}")
        data = text.encode()
        self.ser.reset_input_buffer()
        self.ser.write(f"storage write_chunk {path} {len(data)}\r".encode())
        deadline, buf = time.monotonic() + 5, ""
        while "Ready" not in buf and time.monotonic() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            buf += chunk.decode("utf-8", errors="replace")
            if ANSI.sub("", buf).endswith(">: "):
                raise FlipperError(f"Could not write {path}: {ANSI.sub('', buf).strip()[-160:]}")
        if "Ready" not in buf:
            raise FlipperError(f"Could not write {path}: no Ready from the Flipper.")
        self.ser.write(data)
        out = self._read_prompt(5)
        if "error" in out.lower():
            raise FlipperError(f"Could not write {path}: {out.strip()[-160:]}")

    def _dump_classic(self, outputs):
        """Mifare Classic: UID via ISO14443-3A, then a keyed dump.

        The CLI's mfc dump only uses keys from the Flipper's key cache. If
        there is no cache file for this UID, we write Snapmaker's derived keys
        there (and remove them again if they turn out to be wrong). If no keys
        work we still return the UID so the spool can be linked by UID.
        """
        out = self._dump_cmd("14_3a", 3000)
        outputs.append(out)
        if "Dump saved" not in out:
            return None
        info, _, _ = self._read_dump_file()
        uid = hex_to_bytes(info.get("UID", ""))
        if not uid:
            return None
        self.command(f"storage remove {DUMP_PATH}")
        cache = f"{KEY_CACHE_DIR}/{uid.hex().upper()}.keys"
        had_cache = "File, size" in self.command(f"storage stat {cache}")
        if not had_cache:
            self.command(f"storage mkdir {KEY_CACHE_DIR}")
            self._write_file(cache, snapmaker.key_cache_file(uid))
        out = self._dump_cmd("mfc", 8000)
        outputs.append(out)
        if "Dump saved" in out:
            return self._read_dump_file()
        self.ensure_main()
        if not had_cache:
            self.command(f"storage remove {cache}")
            note = "Sectors are locked with keys this app doesn't know (not a Snapmaker tag?). Linked by UID only."
        else:
            note = (f"The Flipper's saved keys for this tag ({cache}) did not work. "
                    "Delete that file to let the app try Snapmaker's keys. Linked by UID only.")
        info["_locked_note"] = note
        return info, {}, {}

    def _dump(self, protos=("mfu", "mfc")):
        """Dump the tag in range to DUMP_PATH and return the parsed file."""
        self.ensure_main()
        self.command(f"storage remove {DUMP_PATH}")
        outputs = []
        for proto in protos:
            if proto == "mfc":
                result = self._dump_classic(outputs)
                if result:
                    return result
                continue
            out = self._dump_cmd(proto, 3000)
            outputs.append(out)
            if "Dump saved" in out:
                return self._read_dump_file()
        joined = "\n".join(outputs)
        if re.search(r"timeout|not present|no card", joined, re.I):
            raise NoTag("No tag in range")
        raise FlipperError("Dump failed: " + joined.strip()[-300:])

    def read_tag(self):
        """Read UID, type and contents of the tag in range."""
        with self.lock:
            info, pages, blocks = self._dump()
        return self._tag_from_dump(info, pages, blocks)

    def wait_for_tag(self, timeout=20.0, skip_uid=None, cancel=None):
        """Block until a tag is in range, then read it. Returns None on timeout/cancel.

        Uses the CLI `scanner` command, which waits for a tag and is aborted
        with Ctrl+C. A tag whose UID equals `skip_uid` (the one just handled)
        is ignored until a different tag shows up.
        """
        deadline = time.monotonic() + timeout
        stopped = lambda: time.monotonic() >= deadline or (cancel is not None and cancel.is_set())  # noqa: E731
        while not stopped():
            with self.lock:
                self.ensure_nfc()
                self.ser.reset_input_buffer()
                self.ser.write(b"scanner\r")
                buf = ""
                while not stopped():
                    chunk = self.ser.read(self.ser.in_waiting or 1)
                    if chunk:
                        buf += chunk.decode("utf-8", errors="replace")
                        if ANSI.sub("", buf).endswith(NFC_PROMPT):
                            break
                out = ANSI.sub("", buf)
                if not out.endswith(NFC_PROMPT):
                    self.ser.write(b"\x03")
                    self._read_prompt(3)
                    return None
                m = re.search(r"Protocols detected:([^\r\n]*)", out)
                found = m.group(1) if m else ""
                if "Ultralight" in found or "NTAG" in found:
                    protos = ("mfu",)
                elif "Classic" in found:
                    protos = ("mfc",)
                else:
                    protos = ("mfu", "mfc")
                try:
                    info, pages, blocks = self._dump(protos)
                except NoTag:
                    continue  # tag left before the dump finished
            tag = self._tag_from_dump(info, pages, blocks)
            if skip_uid and tag.uid == skip_uid.upper():
                time.sleep(0.5)
                continue
            return tag
        return None

    def _tag_from_dump(self, info, pages, blocks):
        uid = hex_to_bytes(info.get("UID", "")).hex().upper()
        if not uid:
            raise FlipperError("Dump file had no UID line.")
        device = info.get("Device type", "")
        tag = TagInfo(uid=uid, atqa=info.get("ATQA", "").replace(" ", ""),
                      sak=info.get("SAK", "").replace(" ", ""), device=device)
        if pages:
            tag.family = "ntag"
            return decode_type2(tag, pages)
        classic = "Classic" in device or tag.sak.upper() in CLASSIC_SAKS
        tag.family = "mifare_classic" if classic else "other"
        if classic and not blocks:
            tag.device = "Mifare Classic"
        tag.blocks = blocks
        tag.format = "uid_only"
        if "_locked_note" in info:
            tag.notes.append(info["_locked_note"])
            return tag
        if classic:
            decode_classic(tag, blocks)
        return tag

    # ---- writing --------------------------------------------------------

    def _rdbl(self, page):
        out = self.command(f"mfu rdbl -b {page}")
        m = BLOCK_RE.search(out)
        if not m or int(m.group(1)) != page:
            if re.search(r"not present|timeout", out, re.I):
                raise NoTag("No tag in range")
            raise FlipperError(f"Reading page {page} failed: {out.strip()[-120:]}")
        return hex_to_bytes(m.group(2))

    def _wrbl(self, page, data):
        out = self.command(f"mfu wrbl -b {page} -d {data.hex().upper()}")
        if "Success" not in out:
            raise FlipperError(f"Writing page {page} failed: {out.strip()[-160:]}")

    def write_user_data(self, data, expect_uid=None):
        """Write bytes to the tag's user area starting at page 4, then verify.

        `expect_uid` guards against writing to a different tag than the one
        the user scanned. Pages 0-3 (UID, lock bits, CC) are never written.
        """
        with self.lock:
            self.ensure_nfc()
            p0, p1, cc = self._rdbl(0), self._rdbl(1), self._rdbl(3)
            uid = (p0[:3] + p1).hex().upper()
            if expect_uid and uid != expect_uid.upper():
                raise FlipperError(f"Tag in range is {uid}, expected {expect_uid}. Nothing written.")
            if cc[0] != 0xE1:
                raise FlipperError("Tag has no NDEF Capability Container (page 3); refusing to write.")
            capacity = cc[2] * 8
            if len(data) > capacity:
                raise FlipperError(f"Data is {len(data)} bytes but the tag only holds {capacity}.")
            padded = data + b"\x00" * (-len(data) % 4)
            for n in range(0, len(padded), 4):
                self._wrbl(FIRST_USER_PAGE + n // 4, padded[n:n + 4])
        tag = self.read_tag()
        if tag.uid != uid or tag.user_data[: len(padded)] != padded:
            raise FlipperError("Verification failed: data read back does not match what was written.")
        return {"uid": uid, "bytes_written": len(padded), "pages": len(padded) // 4,
                "format": tag.format, "filament": tag.filament}

    def write_openspool(self, fields, expect_uid=None):
        return self.write_user_data(openspool.encode(fields), expect_uid=expect_uid)
