#!/usr/bin/env python3

from contextlib import closing
import glob
import hashlib
import sqlite3
import signal
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import meshtastic.serial_interface
from meshtastic.protobuf import mesh_pb2, portnums_pb2
from pubsub import pub


DEST = os.environ.get("LORA_DEST", "!00000001")
TOKEN = os.environ["LORA_TOKEN"]
BIND = os.environ.get("LORA_BIND", "127.0.0.1")
PORT = int(os.environ.get("LORA_PORT", "8989"))
ACK_TIMEOUT = float(os.environ.get("LORA_ACK_TIMEOUT", "60"))

DEST_NUM = int(DEST[-8:], 16)

stopping = threading.Event()
radio = None
radio_lock = threading.Lock()

pending_acks = {}
pending_lock = threading.Lock()
# Only packets arriving during send/registration can be early ACKs.
early_acks = []
registering_ack = False
send_slot = threading.Lock()
CACHE_PATH = os.environ.get("LORA_CACHE_PATH", "/var/lib/lora-notifyd/delivery.sqlite3")


def cached_send(message, mode, ack, request_id=None):
    # No hidden queue: callers retry busy responses through their durable outbox.
    if not send_slot.acquire(blocking=False):
        return 503, {"ok": False, "status": "busy", "retry_after_s": 5}
    try:
        if request_id is None:
            return send_notification(message, mode, ack)
        digest = hashlib.sha256(json.dumps(
            [DEST, message, mode, ack], ensure_ascii=False
        ).encode()).hexdigest()
        with closing(sqlite3.connect(CACHE_PATH)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS deliveries "
                       "(id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT)")
            row = db.execute("SELECT digest, result FROM deliveries WHERE id=?",
                             (request_id,)).fetchone()
            if row and row[0] != digest:
                return 409, {"ok": False, "status": "request_id_conflict"}
            if row and row[1]:
                return 200, json.loads(row[1])
            db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?, NULL)",
                       (request_id, digest))
            db.commit()
            status, result = send_notification(message, mode, ack)
            if status == 200:
                db.execute("UPDATE deliveries SET result=? WHERE id=?",
                           (json.dumps(result), request_id))
                db.commit()
            return status, result
    finally:
        send_slot.release()


def find_device():
    configured = os.environ.get("LORA_DEVICE", "auto")

    if configured != "auto" and os.path.exists(configured):
        return configured

    patterns = [
        "/dev/serial/by-id/*Espressif*",
        "/dev/ttyACM*",
    ]

    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]

    raise RuntimeError("No Meshtastic USB serial device found")


def radio_connected():
    r = radio

    if r is None:
        return False

    try:
        return r.isConnected.is_set()
    except Exception:
        return False


def disconnect_radio():
    global radio

    old = radio
    radio = None

    if old is not None:
        try:
            old.close()
        except Exception:
            pass


def connect_radio():
    global radio

    if radio_connected():
        return radio

    disconnect_radio()

    device = find_device()

    print(
        f"[radio] connecting to {device}",
        flush=True,
    )

    radio = meshtastic.serial_interface.SerialInterface(device)

    print(
        f"[radio] connected; notification destination={DEST}",
        flush=True,
    )

    return radio


def on_mesh_receive(packet, interface):
    """
    Watch all packets coming from LHMG and correlate ROUTING_APP
    ACK/NAK packets with reliable notifications currently waiting.
    """

    try:
        decoded = packet.get("decoded") or {}

        if decoded.get("portnum") != "ROUTING_APP":
            return

        request_id = decoded.get("requestId")

        if request_id is None:
            return

        request_id = int(request_id)

        with pending_lock:
            state = pending_acks.get(request_id)

            if state is None:
                if registering_ack and len(early_acks) < 128:
                    early_acks.append((packet, interface))
                return

            if state["event"].is_set():
                return

            routing = decoded.get("routing") or {}
            error_reason = routing.get("errorReason", "NONE")

            from_num = int(packet.get("from", 0))

            elapsed_ms = round(
                (time.monotonic() - state["started"]) * 1000,
                1,
            )

            local_num = 0

            try:
                local_num = int(interface.localNode.nodeNum)
            except Exception:
                pass

            # Any real NAK is terminal.
            if error_reason != "NONE":
                state["result"] = {
                    "kind": "nak",
                    "error_reason": error_reason,
                    "from_num": from_num,
                    "response_ms": elapsed_ms,
                }

                print(
                    f"[ack] packet={request_id} "
                    f"NAK={error_reason} after {elapsed_ms}ms",
                    flush=True,
                )

                state["event"].set()
                return

            # An ACK generated by our own LHMG is only an implicit ACK.
            # Useful information, but NOT proof LPMN got the DM.
            if from_num == local_num:
                if state["implicit_ack_ms"] is None:
                    state["implicit_ack_ms"] = elapsed_ms

                print(
                    f"[ack] packet={request_id} "
                    f"implicit ACK after {elapsed_ms}ms; "
                    f"still waiting for destination",
                    flush=True,
                )

                return

            # For this service we only accept the configured destination's
            # ACK as final delivery confirmation.
            if from_num != state["dest_num"]:
                print(
                    f"[ack] packet={request_id} "
                    f"ignoring ACK from !{from_num:08x}",
                    flush=True,
                )
                return

            state["result"] = {
                "kind": "ack",
                "from_num": from_num,
                "ack_ms": elapsed_ms,
            }

            print(
                f"[ack] packet={request_id} "
                f"EXPLICIT ACK from !{from_num:08x} "
                f"after {elapsed_ms}ms",
                flush=True,
            )

            state["event"].set()

    except Exception as e:
        print(
            f"[ack] receive handler error: "
            f"{type(e).__name__}: {e}",
            flush=True,
        )


# Subscribing to the parent topic receives all packet types,
# including ROUTING_APP ACKs.
pub.subscribe(on_mesh_receive, "meshtastic.receive")


def send_packet(r, message, mode, ack):
    if mode == "alert":
        if ack:
            # sendAlert() itself hardcodes wantAck=False, so reproduce
            # its ALERT_APP/ALERT priority behavior while requesting ACK.
            return r.sendData(
                message.encode("utf-8"),
                destinationId=DEST,
                portNum=portnums_pb2.PortNum.ALERT_APP,
                wantAck=True,
                priority=mesh_pb2.MeshPacket.Priority.ALERT,
            )

        return r.sendAlert(
            message,
            destinationId=DEST,
        )

    return r.sendText(
        message,
        destinationId=DEST,
        wantAck=ack,
    )


def send_notification(message, mode="text", ack=False):
    """
    Returns (HTTP status, JSON response).
    """

    global registering_ack
    # Background maintenance owns reconnects; HTTP requests fail fast when busy
    # or disconnected instead of spending their deadline in connection setup.
    if not radio_lock.acquire(blocking=False):
        return 503, {"ok": False, "status": "radio_busy"}
    try:
        if not radio_connected():
            return 503, {"ok": False, "status": "radio_disconnected"}
        # Never blindly resend after an ambiguous transmission exception.
        last_error = None

        for attempt in range(1):
            try:
                r = radio
                started = time.monotonic()
                with pending_lock:
                    early_acks.clear()
                    registering_ack = ack
                try:
                    packet = send_packet(r, message, mode, ack)
                except Exception:
                    with pending_lock:
                        registering_ack = False
                        early_acks.clear()
                    raise

                queued_ms = round(
                    (time.monotonic() - started) * 1000,
                    1,
                )

                packet_id = int(packet.id)

                print(
                    f"[send] mode={mode} reliable={ack} "
                    f"id={packet_id} queued={queued_ms}ms "
                    f"message={message!r}",
                    flush=True,
                )

                # Fire-and-forget ends here.
                # Intentionally NO ack_ms / delivered fields.
                if not ack:
                    return 200, {
                        "ok": True,
                        "status": "queued",
                        "destination": DEST,
                        "mode": mode,
                        "reliable": False,
                        "packet_id": packet_id,
                        "queued_ms": queued_ms,
                    }

                state = {
                    "event": threading.Event(),
                    "started": started,
                    "dest_num": DEST_NUM,
                    "implicit_ack_ms": None,
                    "result": None,
                }

                with pending_lock:
                    pending_acks[packet_id] = state
                    registering_ack = False
                    buffered = list(early_acks)
                    early_acks.clear()
                for early_packet, interface in buffered:
                    on_mesh_receive(early_packet, interface)

                print(
                    f"[ack] waiting up to {ACK_TIMEOUT:.0f}s "
                    f"for explicit ACK packet={packet_id}",
                    flush=True,
                )

                # ACK budget starts at transmission, not after registration.
                deadline = started + ACK_TIMEOUT
                try:
                    while not state["event"].is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not radio_connected():
                            break
                        state["event"].wait(min(remaining, 0.25))
                    got_result = state["event"].is_set()
                finally:
                    with pending_lock:
                        pending_acks.pop(packet_id, None)
                if not got_result and not radio_connected():
                    return 503, {"ok": False, "status": "radio_disconnected",
                                 "packet_id": packet_id}

                if not got_result:
                    result = {
                        "ok": False,
                        "status": "ack_timeout",
                        "destination": DEST,
                        "mode": mode,
                        "reliable": True,
                        "delivery_confirmed": False,
                        "packet_id": packet_id,
                        "queued_ms": queued_ms,
                        "waited_ms": round(
                            ACK_TIMEOUT * 1000,
                            1,
                        ),
                    }

                    if state["implicit_ack_ms"] is not None:
                        result["implicit_ack_seen"] = True
                        result["implicit_ack_ms"] = state[
                            "implicit_ack_ms"
                        ]

                    print(
                        f"[ack] packet={packet_id} timed out "
                        f"without destination ACK",
                        flush=True,
                    )

                    return 504, result

                result = state["result"]

                if result["kind"] == "nak":
                    response = {
                        "ok": False,
                        "status": "nak",
                        "destination": DEST,
                        "mode": mode,
                        "reliable": True,
                        "delivered": False,
                        "packet_id": packet_id,
                        "queued_ms": queued_ms,
                        "error_reason": result["error_reason"],
                        "response_ms": result["response_ms"],
                    }

                    if state["implicit_ack_ms"] is not None:
                        response["implicit_ack_seen"] = True
                        response["implicit_ack_ms"] = state[
                            "implicit_ack_ms"
                        ]

                    return 502, response

                # Explicit ACK from LPMN.
                response = {
                    "ok": True,
                    "status": "delivered",
                    "destination": DEST,
                    "mode": mode,
                    "reliable": True,
                    "delivered": True,
                    "packet_id": packet_id,
                    "queued_ms": queued_ms,
                    "ack_ms": result["ack_ms"],
                    "ack_from": f"!{result['from_num']:08x}",
                }

                if state["implicit_ack_ms"] is not None:
                    response["implicit_ack_seen"] = True
                    response["implicit_ack_ms"] = state[
                        "implicit_ack_ms"
                    ]

                return 200, response

            except Exception as e:
                last_error = e

                print(
                    f"[radio] attempt {attempt + 1} failed: "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )

                disconnect_radio()

        raise last_error
    finally:
        with pending_lock:
            registering_ack = False
            early_acks.clear()
        radio_lock.release()


def radio_maintainer():
    while not stopping.is_set():
        try:
            if not radio_connected():
                with radio_lock:
                    if not radio_connected():
                        connect_radio()

        except Exception as e:
            print(
                f"[radio] background reconnect failed: {e}",
                flush=True,
            )

        stopping.wait(5)


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
                    "destination": DEST,
                    "radio_connected": radio_connected(),
                    "ack_timeout_s": ACK_TIMEOUT,
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
    server = ThreadingHTTPServer(
        (BIND, PORT),
        Handler,
    )

    print(
        f"[http] listening on "
        f"http://{BIND}:{PORT}",
        flush=True,
    )

    threading.Thread(
        target=radio_maintainer,
        daemon=True,
        name="radio-maintainer",
    ).start()

    def shutdown(*_):
        stopping.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        # ThreadingHTTPServer joins active requests so delivery cache commits
        # complete before systemd stops the process.
        server.server_close()
        with radio_lock:
            disconnect_radio()


if __name__ == "__main__":
    main()
