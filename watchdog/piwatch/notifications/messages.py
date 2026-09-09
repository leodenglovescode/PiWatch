"""Human-readable reports, including bounded radio messages."""
from datetime import datetime

DISPLAY_NAMES = {
    "router": "Router",
    "super_server": "Super Server",
    "home_assistant": "Home Assistant",
    "internet": "Internet",
    "dns": "DNS",
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
    return [f"{label}: {'OK' if result[key] else 'FAIL'}"
            for key, label in labels.items() if key in result]


def build_message(name, new_state, results, trackers):
    down = new_state == "DOWN"
    title = f"{'🚨' if down else '✅'} PiWatch: {getattr(trackers[name], 'display', DISPLAY_NAMES.get(name, name))} {'DOWN' if down else 'RECOVERED'}"
    lines = ["Node: watchdog-pi", f"Time: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}", ""]
    lines.extend(probe_lines(results[name]))
    lines.extend(["", *[f"{getattr(tracker, 'display', DISPLAY_NAMES.get(key, key))} state: {tracker.state}"
                          for key, tracker in trackers.items()]])
    if not down:
        lines.extend(["", f"Outage duration: {format_duration(trackers[name].last_outage_seconds)}"])
    return title, "\n".join(lines)


def build_lora_messages(name, new_state, results, trackers):
    down = new_state == "DOWN"
    lines = [f"{'🚨' if down else '✅'} PiWatch: {getattr(trackers[name], 'display', DISPLAY_NAMES.get(name, name))} {'DOWN' if down else 'RECOVERED'}",
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
