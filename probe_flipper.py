#!/usr/bin/env python3
"""Read-only check of the Flipper commands the filament manager relies on.

Hold a tag on the back of the Flipper, then run:
    python probe_flipper.py            (auto-detect the port)
    python probe_flipper.py --port COM5

Nothing is written to the tag (a scratch dump file is saved on the Flipper's
SD card at /ext/nfc/filament_manager.nfc). Everything is also saved to
probe_log.txt so you can paste it back.
"""

import argparse
import contextlib
import io
import json
import time
import traceback

from filament_manager.flipper import Flipper


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    args = ap.parse_args()

    log = io.StringIO()

    class Tee(io.TextIOBase):
        def write(self, s):
            print(s, end="", flush=True, file=real)
            log.write(s)
            return len(s)

    import sys
    real = sys.stdout
    with contextlib.redirect_stdout(Tee()):
        f = Flipper.open(args.port, debug=True)
        print(f"Connected on {f.port}")
        try:
            for n in (1, 2):
                print(f"\n### read {n}")
                t0 = time.monotonic()
                try:
                    tag = f.read_tag()
                    print(f"### read {n} took {time.monotonic() - t0:.1f}s")
                    print(json.dumps(tag.to_json(), indent=2))
                except Exception:
                    print(f"### read {n} failed after {time.monotonic() - t0:.1f}s")
                    print(traceback.format_exc())
        finally:
            f.close()
    with open("probe_log.txt", "w", encoding="utf-8") as fh:
        fh.write(log.getvalue())
    print("\nSaved probe_log.txt")


if __name__ == "__main__":
    main()
