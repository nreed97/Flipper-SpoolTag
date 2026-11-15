"""Local HTTP service behind the filament manager web page.

Run:   python -m filament_manager.server                 (auto-detect the Flipper)
       python -m filament_manager.server --port COM5
       python -m filament_manager.server --fake          (no Flipper: simulated tags)
Then open http://localhost:8765

Settings (Spoolman URL, tag store) are kept in config.json next to this
package and can be changed from the page.

API (JSON):
  GET  /api/status                     Flipper + Spoolman state, settings
  POST /api/settings                   {"spoolman_url", "tag_store"}
  POST /api/tag/read                   read the tag on the Flipper now
  POST /api/tag/wait                   {"timeout", "skip_uid"} wait for a tag, then read it
  POST /api/tag/cancel                 stop a running wait
  POST /api/tag/write                  {"fields" | "spool_id", "expect_uid"} write OpenSpool + verify
  POST /api/lookup/tag                 {tag} -> linked spools, Spoolman + SpoolmanDB suggestions
  POST /api/lookup/upc                 {"upc"} -> filament fields from Spoolman / upcitemdb / SpoolmanDB
  GET  /api/catalog                    vendors, filaments, spools, materials, locations
  POST /api/save                       create/update vendor, filament, spool (see spoolman.Spoolman.save)
  POST /api/link                       {"spool_id", "uid", "move"}; 409 lists spools already holding it
  POST /api/unlink                     {"spool_id", "uid"}
"""

import argparse
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import lookup, openspool
from .flipper import Flipper, FlipperError, NoTag
from .spoolman import Spoolman, SpoolmanError, card_uids, openspool_fields, spool_uids

STATIC = pathlib.Path(__file__).parent / "static"
CONFIG = pathlib.Path(__file__).parent.parent / "config.json"
DEFAULTS = {"spoolman_url": "http://localhost:7912", "tag_store": "native"}


class App:
    def __init__(self, args, config_path=CONFIG):
        self.args = args
        self.config_path = pathlib.Path(config_path)
        self.flipper = None
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.config = dict(DEFAULTS)
        try:
            self.config.update(json.loads(self.config_path.read_text()))
        except (OSError, ValueError):
            pass
        if getattr(args, "spoolman", None):
            self.config["spoolman_url"] = args.spoolman

    @property
    def sm(self):
        return Spoolman(self.config["spoolman_url"], store=self.config["tag_store"])

    def save_config(self, updates):
        for k in DEFAULTS:
            if updates.get(k):
                self.config[k] = str(updates[k]).strip()
        if self.config["tag_store"] not in ("card_uids", "native"):
            raise ValueError("tag_store must be card_uids or native")
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def get_flipper(self):
        with self.lock:
            if self.flipper is None:
                if self.args.fake:
                    from .fake import FakeFlipperSerial
                    self.flipper = Flipper(FakeFlipperSerial(), debug=self.args.debug)
                    self.flipper.port = "simulated"
                    self.flipper.start()
                else:
                    self.flipper = Flipper.open(self.args.port, debug=self.args.debug)
            return self.flipper

    def reset(self):
        with self.lock:
            if self.flipper is not None:
                try:
                    self.flipper.close()
                except Exception:
                    pass
            self.flipper = None

    # ---- handlers -------------------------------------------------------

    def status(self):
        out = {"settings": self.config, "fake": bool(self.args.fake)}
        try:
            out["flipper"] = {"connected": True, "port": self.get_flipper().port}
        except Exception as e:
            out["flipper"] = {"connected": False, "error": str(e)}
        try:
            info = self.sm.info()
            out["spoolman"] = {"connected": True, "version": info.get("version")}
        except SpoolmanError as e:
            out["spoolman"] = {"connected": False, "error": str(e)}
        return out

    def tag_result(self, tag):
        data = tag.to_json()
        try:
            data["lookup"] = lookup.tag_lookup(self.sm, data)
        except SpoolmanError as e:
            data["lookup"] = {"error": str(e)}
        return data

    def catalog(self):
        sm = self.sm
        store = self.config["tag_store"]
        spools = sm.spools()
        for s in spools:
            s["uids"] = spool_uids(s, store)
        return {"vendors": sm.vendors(), "filaments": sm.filaments(), "spools": spools,
                "materials": sm.materials(), "locations": sm.locations()}

    def write(self, body):
        fields = body.get("fields")
        if not fields and body.get("spool_id"):
            fields = openspool_fields(self.sm.spool(int(body["spool_id"])))
        if not fields:
            raise ValueError("Nothing to write: give fields or spool_id.")
        self.cancel.set()  # stop any running wait so the write gets the Flipper
        res = self.get_flipper().write_openspool(fields, body.get("expect_uid"))
        self.cancel.clear()
        return res

    def post(self, path, body):
        if path == "/api/settings":
            self.save_config(body)
            return self.status()
        if path == "/api/tag/encode":
            data = openspool.encode(body.get("fields", {}))
            return {"hex": data.hex().upper(), "bytes": len(data), "payload": openspool.build_payload(body["fields"])}
        if path == "/api/tag/read":
            return self.tag_result(self.get_flipper().read_tag())
        if path == "/api/tag/wait":
            self.cancel.clear()
            tag = self.get_flipper().wait_for_tag(float(body.get("timeout", 20)), body.get("skip_uid"), self.cancel)
            return {"tag": self.tag_result(tag) if tag else None}
        if path == "/api/tag/cancel":
            self.cancel.set()
            return {"ok": True}
        if path == "/api/tag/write":
            return self.write(body)
        if path == "/api/fake/tag" and self.args.fake:
            return self.set_fake_tag(body)
        if path == "/api/lookup/tag":
            return lookup.tag_lookup(self.sm, body)
        if path == "/api/lookup/upc":
            return lookup.upc_lookup(self.sm, body.get("upc", ""), online=not self.args.offline)
        if path == "/api/save":
            return self.sm.save(body)
        if path == "/api/link":
            r = self.sm.link(int(body["spool_id"]), body["uid"], bool(body.get("move")), body.get("format"))
            r["spool"]["uids"] = spool_uids(r["spool"], self.config["tag_store"])
            return r
        if path == "/api/unlink":
            s = self.sm.unlink(int(body["spool_id"]), body["uid"])
            s["uids"] = spool_uids(s, self.config["tag_store"])
            return s
        return None

    def set_fake_tag(self, body):
        from .fake import FakeTag
        ser = self.get_flipper().ser
        kind = body.get("kind", "blank")
        if kind == "none":
            ser.tag = None
        else:
            ser.tag = FakeTag(uid=body.get("uid") or "04A1B2C3D4E5F6", classic=(kind == "classic"))
        return {"ok": True, "kind": kind}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            if app.args.debug:
                super().log_message(fmt, *a)

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            try:
                if self.path == "/api/status":
                    return self._send(200, app.status())
                if self.path == "/api/catalog":
                    return self._send(200, app.catalog())
            except SpoolmanError as e:
                return self._send(502, {"error": str(e)})
            name = "index.html" if self.path.split("?")[0] in ("/", "/index.html") else self.path.lstrip("/")
            path = (STATIC / name).resolve()
            if STATIC.resolve() in path.parents and path.is_file():
                ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}.get(
                    path.suffix.lstrip("."), "application/octet-stream")
                return self._send(200, path.read_bytes(), ctype + "; charset=utf-8")
            self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                result = app.post(self.path, self._body())
                if result is None:
                    return self._send(404, {"error": "not found"})
                self._send(200, result)
            except NoTag as e:
                self._send(409, {"error": str(e), "no_tag": True})
            except SpoolmanError as e:
                code = e.status if e.status in (400, 404, 409, 422) else 502
                self._send(code, {"error": str(e), **(e.body if isinstance(e.body, dict) else {})})
            except lookup.LookupError_ as e:
                self._send(400, {"error": str(e)})
            except (ValueError, KeyError) as e:
                self._send(400, {"error": str(e)})
            except FlipperError as e:
                self._send(502, {"error": str(e)})
            except OSError as e:  # serial port vanished: reconnect next time
                app.reset()
                self._send(502, {"error": f"Lost the Flipper connection: {e}"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Filament manager local service")
    ap.add_argument("--port", help="Flipper serial port, e.g. COM5 (auto-detected if omitted)")
    ap.add_argument("--spoolman", help="Spoolman URL (saved to config.json), e.g. http://192.168.1.100:7912")
    ap.add_argument("--listen", default="127.0.0.1", help="address to serve on")
    ap.add_argument("--http-port", type=int, default=8765)
    ap.add_argument("--fake", action="store_true", help="simulate a Flipper and tags")
    ap.add_argument("--offline", action="store_true", help="don't send UPCs to upcitemdb")
    ap.add_argument("--debug", action="store_true", help="print raw Flipper output and HTTP requests")
    args = ap.parse_args()
    app = App(args)
    if args.spoolman:
        app.save_config({"spoolman_url": args.spoolman})
    server = ThreadingHTTPServer((args.listen, args.http_port), make_handler(app))
    print(f"Filament manager on http://localhost:{args.http_port}  (Ctrl+C to quit)")
    print(f"Spoolman: {app.config['spoolman_url']}  tag store: {app.config['tag_store']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        app.cancel.set()
        app.reset()


if __name__ == "__main__":
    main()
