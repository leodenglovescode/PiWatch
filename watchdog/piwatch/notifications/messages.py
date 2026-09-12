"""Human-readable reports, including bounded radio messages."""
from datetime import datetime

DISPLAY_NAMES = {
    "router": "Router",
    "super_server": "Super Server",
    "home_assistant": "Home Assistant",
    "internet": "Internet",
    "dns": "DNS",
    "battery": "UPS battery",
    "mains": "Mains power",
}


def format_duration(seconds):
    if seconds is None:
        return "unknown"
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"


def probe_lines(result):
    labels = {"ping": "Ping", "ssh": "SSH", "http": "HTTP",
              "tcp": "Public TCP", "resolve": "Resolution"}
    lines = [f"{label}: {'OK' if result[key] else 'FAIL'}"
             for key, label in labels.items() if key in result]
    if isinstance(result.get('voltage_v'), (int, float)):
        label = 'Battery voltage' if result.get('source') == 'gpio' else 'Voltage'
        lines.append(f"{label}: {result['voltage_v']:.3f} V")
    if isinstance(result.get('low_voltage_v'), (int, float)):
        lines.append(f"Low threshold: {result['low_voltage_v']:.2f} V")
    if isinstance(result.get('gpio_level'), bool):
        lines.append(f"GPIO{result.get('gpio', 17)}: {'HIGH' if result['gpio_level'] else 'LOW'}")
    return lines


def build_message(name, new_state, results, trackers):
    down = new_state == "DOWN"
    event = ('LOW' if down and name == 'battery' else 'lost' if down and name == 'mains'
             else 'DOWN' if down else 'restored' if name == 'mains' else 'RECOVERED')
    title = f"{'🚨' if down else '✅'} PiWatch: {getattr(trackers[name], 'display', DISPLAY_NAMES.get(name, name))} {event}"
    lines = ["Node: watchdog-pi", f"Time: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}", ""]
    lines.extend(probe_lines(results[name]))
    lines.extend(["", *[f"{getattr(tracker, 'display', DISPLAY_NAMES.get(key, key))} state: {tracker.state}"
                          for key, tracker in trackers.items()]])
    if not down:
        lines.extend(["", f"Outage duration: {format_duration(trackers[name].last_outage_seconds)}"])
    return title, "\n".join(lines)


def build_lora_messages(name, new_state, results, trackers):
    down = new_state == "DOWN"
    event = ('LOW' if down and name == 'battery' else 'lost' if down and name == 'mains'
             else 'DOWN' if down else 'restored' if name == 'mains' else 'RECOVERED')
    lines = [f"{'🚨' if down else '✅'} PiWatch: {getattr(trackers[name], 'display', DISPLAY_NAMES.get(name, name))} {event}",
             f"Time: {datetime.now().astimezone():%H:%M:%S %Z}"]
    if not down:
        lines.append(f"Outage: {format_duration(trackers[name].last_outage_seconds)}")
    lines.extend(probe_lines(results[name]))
    primary = "\n".join(lines)
    secondary = "Other device states:\n" + "\n".join(
        f"{getattr(tracker, 'display', DISPLAY_NAMES.get(key, key))}: {tracker.state}" for key, tracker in trackers.items() if key != name
    )
    def bounded(message):
        if len(message.encode('utf-8')) <= 200:
            return message
        return message.encode('utf-8')[:197].decode('utf-8', errors='ignore') + '…'
    return bounded(primary), bounded(secondary)
