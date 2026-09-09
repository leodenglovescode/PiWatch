"""A compact watchdog page using the same snapshot as the web panel."""
import json
from pathlib import Path
import time


def page_watchdog(device, font, title_font, duration, refresh):
    from luma.core.render import canvas
    end = time.monotonic() + duration
    while time.monotonic() < end:
        rows = ['Status unavailable']
        try:
            path = Path('/run/piwatch/status.json')
            with path.open() as stream:
                status = json.load(stream)
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            age = time.monotonic() - status.get('generated_monotonic', -1e12)
            if status.get('boot_id') != boot or age < 0 or age > status.get('stale_after_seconds', 10):
                rows = ['Status stale']
            else:
                rows = [f"{value.get('name', name)[:11]}: {value.get('state', '?')}"
                        for name, value in list(status.get('targets', {}).items())[:3]]
                if not rows:
                    rows = ['No active targets']
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        with canvas(device) as draw:
            draw.text((0, 0), 'WATCHDOG', font=title_font, fill='white')
            for index, row in enumerate(rows):
                draw.text((0, 19 + 14 * index), row, font=font, fill='white')
        time.sleep(refresh)
