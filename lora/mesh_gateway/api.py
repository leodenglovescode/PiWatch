"""Bearer-authenticated HTTP API; radio ownership stays in radio.py."""
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import radio
from .delivery import cached_send

TOKEN = os.environ.get("LORA_TOKEN", "")

class Handler(BaseHTTPRequestHandler):
    server_version = "lora-notifyd/2.0"

    def log_message(self, fmt, *args):
        print(
            f"[http] {self.client_address[0]} "
            + (fmt % args),
            flush=True,
        )

    def send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")

        self.send_response(status)
        self.send_header(
            "Content-Type",
            "application/json",
        )
        self.send_header(
            "Content-Length",
            str(len(body)),
        )
        self.end_headers()

        self.wfile.write(body)

    def authenticated(self):
        return (
            self.headers.get("Authorization", "")
            == f"Bearer {TOKEN}"
        )

    def do_GET(self):
        if self.path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "service": "lora-notifyd",
                    "version": "2.0",
                    "destination": radio.DEST,
                    "radio_connected": radio.radio_connected(),
                    "ack_timeout_s": radio.ACK_TIMEOUT,
                },
            )
            return

        self.send_json(
            404,
            {"error": "not found"},
        )

    def do_POST(self):
        if self.path != "/v1/notify":
            self.send_json(
                404,
                {"error": "not found"},
            )
            return

        if not self.authenticated():
            self.send_json(
                401,
                {"error": "unauthorized"},
            )
            return

        try:
            length = int(
                self.headers.get(
                    "Content-Length",
                    "0",
                )
            )

            if length <= 0 or length > 4096:
                raise ValueError(
                    "invalid request size"
                )

            request = json.loads(
                self.rfile.read(length).decode(
                    "utf-8"
                )
            )

            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")

            message = str(
                request.get("message", "")
            ).strip()

            mode = str(
                request.get("mode", "text")
            ).lower()

            ack = request.get("ack", False)
            if not isinstance(ack, bool):
                raise ValueError("ack must be a boolean")
            request_id = request.get("request_id")
            if request_id is not None and (
                not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
            ):
                raise ValueError("request_id must be a string of 1-128 characters")

            if not message:
                raise ValueError(
                    "message is empty"
                )

            if len(message.encode("utf-8")) > 200:
                raise ValueError(
                    "message exceeds 200 UTF-8 bytes"
                )

            if mode not in (
                "text",
                "alert",
            ):
                raise ValueError(
                    "mode must be 'text' or 'alert'"
                )

            status, result = cached_send(
                message, mode, ack, request_id,
            )

            self.send_json(
                status,
                result,
            )

        except ValueError as e:
            self.send_json(
                400,
                {"error": str(e)},
            )

        except Exception as e:
            self.send_json(
                503,
                {
                    "error":
                    f"{type(e).__name__}: {e}"
                },
            )


def main():
    if not TOKEN:
        raise RuntimeError("LORA_TOKEN must be configured")
    server = ThreadingHTTPServer(
        (radio.BIND, radio.PORT),
        Handler,
    )

    print(
        f"[http] listening on "
        f"http://{radio.BIND}:{radio.PORT}",
        flush=True,
    )

    # Subscribe before opening the radio so early destination ACKs are observed.
    radio.pub.subscribe(radio.on_mesh_receive, "meshtastic.receive")
    threading.Thread(
        target=radio.radio_maintainer,
        daemon=True,
        name="radio-maintainer",
    ).start()

    def shutdown(*_):
        radio.stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        # ThreadingHTTPServer joins active requests so delivery cache commits
        # complete before systemd stops the process.
        server.server_close()
        with radio.radio_lock:
            radio.disconnect_radio()
        radio.pub.unsubscribe(radio.on_mesh_receive, "meshtastic.receive")
