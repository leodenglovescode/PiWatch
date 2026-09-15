#!/usr/bin/env python3

import glob
import math
import os
import threading
import time

import meshtastic.serial_interface
from meshtastic.protobuf import config_pb2, mesh_pb2, portnums_pb2
from pubsub import pub
from .command_responder import COMMANDS, CommandResponder, node_number


DEST = os.environ.get("LORA_DEST", "!00000001")
BIND = os.environ.get("LORA_BIND", "127.0.0.1")
PORT = int(os.environ.get("LORA_PORT", "8989"))
ACK_TIMEOUT = float(os.environ.get("LORA_ACK_TIMEOUT", "60"))
STATUS_ENABLED = os.environ.get("LORA_STATUS_ENABLED", "true").lower() in (
    "1", "true", "yes", "on",
)
STATUS_COMMAND = os.environ.get("LORA_STATUS_COMMAND", "status")
STATUS_SENDERS = tuple(filter(None, (
    value.strip()
    for value in os.environ.get("LORA_STATUS_SENDERS", DEST).split(",")
)))
STATUS_COOLDOWN = float(os.environ.get("LORA_STATUS_COOLDOWN", "15"))
STATUS_PATH = os.environ.get("LORA_STATUS_PATH", "/run/piwatch/status.json")

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
command_responder = None
last_rx = {}
last_rx_lock = threading.Lock()


def _enum_name(enum, value):
    try:
        return enum.Name(int(value))
    except (TypeError, ValueError):
        return None


def _packet_value(packet, *names):
    for name in names:
        if packet.get(name) is not None:
            return packet[name]
    return None


def remember_rx(packet):
    """Retain only nonsecret link metadata from the newest received packet."""
    if not isinstance(packet, dict):
        return
    rssi = _packet_value(packet, 'rxRssi', 'rx_rssi')
    snr = _packet_value(packet, 'rxSnr', 'rx_snr')
    via_mqtt = bool(_packet_value(packet, 'viaMqtt', 'via_mqtt'))
    if rssi is None and snr is None and not via_mqtt:
        return
    report = {
        'from': node_number(packet.get('from')),
        'rssi': rssi,
        'snr': snr,
        'via_mqtt': via_mqtt,
        'received_at': time.time(),
    }
    with last_rx_lock:
        last_rx.clear()
        last_rx.update(report)


def _channel_name(local_node, preset):
    try:
        name = local_node.channels[0].settings.name.strip()
    except (AttributeError, IndexError, TypeError):
        name = ''
    if name:
        return name
    return ''.join(part.title() for part in preset.split('_'))


def _channel_hash(value):
    result = 5381
    for byte in value.encode('utf-8'):
        result = ((result << 5) + result + byte) & 0xffffffff
    return result


def _center_frequency_mhz(lora_config, local_node, region, preset, bandwidth):
    """Calculate the current CN frequency without exposing channel credentials."""
    try:
        override = float(lora_config.override_frequency)
        offset = float(lora_config.frequency_offset)
    except (AttributeError, TypeError, ValueError):
        return None
    if override > 0:
        return override + offset
    # PiWatch currently operates in CN. Avoid pretending other regional plans are
    # interchangeable; add their exact firmware ranges if the installation moves.
    if region != 'CN' or not bandwidth or bandwidth <= 0:
        return None
    start, end = 470.0, 510.0
    step = bandwidth / 1000.0
    slots = int((end - start) / step)
    if slots < 1:
        return None
    try:
        slot = int(lora_config.channel_num)
    except (AttributeError, TypeError, ValueError):
        slot = 0
    if slot <= 0:
        slot = _channel_hash(_channel_name(local_node, preset)) % slots + 1
    if slot > slots:
        return None
    return start + step / 2 + (slot - 1) * step + offset


def radio_command_snapshot():
    """Return bounded live radio/node metadata for read-only LoRa commands."""
    current = radio
    connected = radio_connected()
    result = {'connected': connected, 'nodes': []}
    if not connected or current is None:
        return result
    try:
        local_node = current.localNode
        lora_config = local_node.localConfig.lora
        region = _enum_name(config_pb2.Config.LoRaConfig.RegionCode,
                            lora_config.region)
        preset = _enum_name(config_pb2.Config.LoRaConfig.ModemPreset,
                            lora_config.modem_preset)
        bandwidth = float(lora_config.bandwidth)
        spread_factor = float(lora_config.spread_factor)
        if preset == 'LONG_FAST':
            bandwidth = bandwidth or 250
            spread_factor = spread_factor or 11
        result.update({
            'gateway': node_number(local_node.nodeNum),
            'region': region,
            'preset': preset,
            'bandwidth_khz': bandwidth or None,
            'spread_factor': spread_factor or None,
            'frequency_mhz': _center_frequency_mhz(
                lora_config, local_node, region, preset or '', bandwidth),
        })
    except (AttributeError, TypeError, ValueError):
        pass
    with last_rx_lock:
        result['last_rx'] = dict(last_rx)
    try:
        entries = list((current.nodesByNum or {}).items())
    except (AttributeError, RuntimeError):
        entries = []
    now = time.time()
    for key, value in entries:
        if not isinstance(value, dict):
            continue
        number = node_number(value.get('num')) or node_number(key)
        if number is None:
            continue
        user = value.get('user') or {}
        last_heard = value.get('lastHeard')
        age = (max(0, now - last_heard)
               if isinstance(last_heard, (int, float)) else None)
        recent = result['last_rx'] if result['last_rx'].get('from') == number else {}
        result['nodes'].append({
            'num': number,
            'long_name': user.get('longName'),
            'short_name': user.get('shortName'),
            'age_seconds': age,
            'snr': (value.get('snr') if value.get('snr') is not None
                    else recent.get('snr')),
            'rssi': (value.get('rssi') if value.get('rssi') is not None
                     else recent.get('rssi')),
            'hops': value.get('hopsAway'),
            'via_mqtt': bool(value.get('viaMqtt') or recent.get('via_mqtt')),
        })
    result['nodes'].sort(
        key=lambda item: (item['age_seconds'] is None,
                          item['age_seconds'] if item['age_seconds'] is not None else math.inf))
    return result


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

    remember_rx(packet)
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


def on_text_receive(packet, interface):
    """Queue an authorized read-only command without blocking the callback."""
    remember_rx(packet)
    try:
        local_num = node_number(interface.localNode.nodeNum)
        result = (command_responder.enqueue(packet, local_num)
                  if command_responder else "disabled")
        if result == "queued":
            sender = node_number(packet.get("from"))
            print(
                f"[command] request queued from !{sender:08x}",
                flush=True,
            )
        elif result in ("rate_limited", "busy"):
            print(
                f"[command] request {result.replace('_', ' ')}",
                flush=True,
            )
    except Exception as e:
        print(
            f"[command] receive handler error: {type(e).__name__}",
            flush=True,
        )


def send_packet(r, message, mode, ack, destination=DEST):
    if mode == "alert":
        if ack:
            # sendAlert() itself hardcodes wantAck=False, so reproduce
            # its ALERT_APP/ALERT priority behavior while requesting ACK.
            return r.sendData(
                message.encode("utf-8"),
                destinationId=destination,
                portNum=portnums_pb2.PortNum.ALERT_APP,
                wantAck=True,
                priority=mesh_pb2.MeshPacket.Priority.ALERT,
            )

        return r.sendAlert(
            message,
            destinationId=destination,
        )

    return r.sendText(
        message,
        destinationId=destination,
        wantAck=ack,
    )


def send_notification(message, mode="text", ack=False, destination=DEST):
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
                    packet = send_packet(r, message, mode, ack, destination)
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
                        "destination": destination,
                        "mode": mode,
                        "reliable": False,
                        "packet_id": packet_id,
                        "queued_ms": queued_ms,
                    }

                state = {
                    "event": threading.Event(),
                    "started": started,
                    "dest_num": int(str(destination).lstrip("!"), 16),
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
                        "destination": destination,
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
                        "destination": destination,
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
                    "destination": destination,
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


def send_command_reply(message, sender):
    """Send a lower-priority direct reply without colliding with alert delivery."""
    destination = f"!{sender:08x}"
    send_slot.acquire()
    try:
        status, result = send_notification(
            message,
            mode="text",
            ack=False,
            destination=destination,
        )
        print(
            f"[command] reply to {destination}: {result.get('status', status)}",
            flush=True,
        )
    finally:
        send_slot.release()


command_responder = CommandResponder(
    send_command_reply,
    STATUS_SENDERS,
    command=STATUS_COMMAND,
    cooldown=STATUS_COOLDOWN,
    status_path=STATUS_PATH,
    enabled=STATUS_ENABLED,
    radio_provider=radio_command_snapshot,
)


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
