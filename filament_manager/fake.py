"""A pretend Flipper serial port with a tag on it, for tests and --fake mode.

It answers the CLI commands the real Flipper does (main shell: storage;
nfc shell: dump, mfu rdbl, mfu wrbl), using the output formats from the
mntm-012 source.
"""

import re


class FakeTag:
    def __init__(self, uid="04A1B2C3D4E5F6", classic=False, pages=135):
        self.uid = bytes.fromhex(uid)
        self.classic = classic
        if classic:
            self.blocks = {i: (self.uid + b"\x00" * 16)[:16] if i == 0 else None for i in range(64)}
            return
        self.mem = bytearray(pages * 4)
        self.mem[0:3] = self.uid[0:3]
        self.mem[4:8] = self.uid[3:7]
        self.mem[12:16] = bytes([0xE1, 0x10, 0x3E, 0x00])  # NTAG215 CC
        self.mem[16:19] = bytes([0x03, 0x00, 0xFE])           # empty NDEF TLV

    def page(self, n):
        return bytes(self.mem[n * 4:n * 4 + 4])

    def nfc_file(self):
        h = lambda b: " ".join(f"{x:02X}" for x in b)  # noqa: E731
        if self.classic:
            lines = ["Filetype: Flipper NFC device", "Version: 4", "Device type: Mifare Classic",
                     f"UID: {h(self.uid)}", "ATQA: 00 04", "SAK: 08", "Mifare Classic type: 1K"]
            lines += [f"Block {i}: " + (h(b) if b else " ".join(["??"] * 16)) for i, b in self.blocks.items()]
            return "\n".join(lines) + "\n"
        n = len(self.mem) // 4
        lines = ["Filetype: Flipper NFC device", "Version: 4", "Device type: NTAG215",
                 f"UID: {h(self.uid)}", "ATQA: 00 44", "SAK: 00", f"Pages total: {n}", f"Pages read: {n}"]
        lines += [f"Page {i}: {h(self.page(i))}" for i in range(n)]
        return "\n".join(lines) + "\n"


class FakeFlipperSerial:
    def __init__(self, tag="blank"):
        self.tag = FakeTag() if tag == "blank" else tag  # None = no tag in range
        self.out = bytearray()
        self.line = ""
        self.in_nfc = False
        self.files = {}
        self.writes = []
        self.commands = []
        self.scanning = False

    # pyserial surface
    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, n=1):
        chunk = bytes(self.out[:n])
        del self.out[:n]
        return chunk

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        pass

    def write(self, data):
        for ch in data.decode():
            if ch == "\x03":
                self.line = ""
                if self.scanning:
                    self.scanning = False
                    self._emit("Protocols detected: \r\n")
                self._emit("\r\n" + self._prompt())
            elif ch == "\r":
                line, self.line = self.line.strip(), ""
                self.commands.append(line)
                if self.in_nfc and line == "scanner" and self.tag is None:
                    self.scanning = True  # blocks until Ctrl+C, like the real one
                    self._emit(line + "\r\nPress Ctrl+C to abort\r\n\n")
                    continue
                self._emit(line + "\r\n" + self._run(line) + "\r\n" + self._prompt())
            else:
                self.line += ch
        return len(data)

    def _prompt(self):
        return "[nfc]>: " if self.in_nfc else ">: "

    def _emit(self, s):
        self.out += s.encode()

    # command emulation
    def _run(self, line):
        if not self.in_nfc:
            if line == "nfc":
                self.in_nfc = True
                return "Welcome to NFC Command Line Interface!"
            m = re.fullmatch(r"storage remove (\S+)", line)
            if m:
                return "" if self.files.pop(m.group(1), None) else "Storage error: file/dir not exist"
            m = re.fullmatch(r"storage read (\S+)", line)
            if m:
                if m.group(1) not in self.files:
                    return "Storage error: file/dir not exist"
                body = self.files[m.group(1)]
                return f"Size: {len(body)}\r\n{body}"
            return "could not find command"
        if line == "exit":
            self.in_nfc = False
            return ""
        if line == "scanner":
            name = "Mifare Classic" if self.tag.classic else "Mifare Ultralight"
            return f"Press Ctrl+C to abort\r\n\nProtocols detected: ISO14443-3A, {name}"
        m = re.fullmatch(r"dump -p (mfu|mfc) -f (\S+) -t \d+", line)
        if m:
            return self._dump(m.group(1), m.group(2))
        m = re.fullmatch(r"mfu rdbl -b (\d+)", line)
        if m:
            return self._rdbl(int(m.group(1)))
        m = re.fullmatch(r"mfu wrbl -b (\d+) -d ([0-9A-Fa-f]{8})", line)
        if m:
            return self._wrbl(int(m.group(1)), bytes.fromhex(m.group(2)))
        return "\x1b[31mError: Action not found\x1b[0m"

    def _dump(self, proto, path):
        name = {"mfu": "Mifare Ultralight", "mfc": "Mifare Classic"}[proto]
        out = f'Dumping as "{name}"\r\n'
        if path in self.files:
            return f"\x1b[31mFile '{path}' already exists\r\n\x1b[0m"
        if self.tag is None or (proto == "mfc") != self.tag.classic:
            return out + "\x1b[31mError: timeout\r\n\x1b[0m"
        self.files[path] = self.tag.nfc_file()
        return out + f"Dump saved to '{path}'"

    def _hex(self, b):
        return "".join(f"{x:02X} " for x in b)

    def _ntag(self):
        return self.tag is not None and not self.tag.classic

    def _rdbl(self, page):
        if not self._ntag():
            return "\x1b[31mError: Card not present\x1b[0m"
        return f"\r\nBlock: {page} Data: {self._hex(self.tag.page(page))}"

    def _wrbl(self, page, data):
        if not self._ntag():
            return "\x1b[31mError: Card not present\x1b[0m"
        if page < 4:
            return "\x1b[31mError: Protocol\x1b[0m"
        self.tag.mem[page * 4:page * 4 + 4] = data
        self.writes.append(page)
        return f"\x1b[92m\r\nSuccess\r\n\x1b[0mBlock: {page} Data: {self._hex(data)}"
