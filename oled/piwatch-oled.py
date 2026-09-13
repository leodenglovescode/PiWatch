import time
from metrics import get_ip, get_temp, get_uptime, get_time_source
from gps_status import gps, gpsd_reader
from power_overlay import PowerOverlay, read_status
from watchdog_page import page_watchdog as draw_watchdog
import psutil
import signal
import subprocess
import threading

from PIL import ImageFont


# --------------------------------------------------
# OLED
# --------------------------------------------------

REFRESH = 0.5
PAGE_TIME = 5.0
STOP = threading.Event()
POWER_OVERLAY = PowerOverlay()
DISPLAY_HIDDEN = False


# --------------------------------------------------
# Fonts
# --------------------------------------------------

def find_font(name):
    return subprocess.check_output(
        ["fc-match", "-f", "%{file}", name],
        text=True
    ).strip()


def init_display():
    global device, canvas, FONT, FONT_BIG, FONT_CLOCK, FONT_SMALL
    from luma.core.interface.serial import i2c
    from luma.oled.device import ssd1306
    from luma.core.render import canvas
    serial = i2c(port=6, address=0x3C)
    device = ssd1306(serial, width=128, height=64)

    FONT_PATH = find_font("DejaVu Sans")
    BOLD_PATH = find_font("DejaVu Sans:style=Bold")

    FONT = ImageFont.truetype(FONT_PATH, 12)
    FONT_BIG = ImageFont.truetype(BOLD_PATH, 16)
    FONT_CLOCK = ImageFont.truetype(BOLD_PATH, 22)
    FONT_SMALL = ImageFont.truetype(FONT_PATH, 9)


def stop(_signum=None, _frame=None):
    STOP.set()


def wait_refresh():
    STOP.wait(REFRESH)


def centered(draw, y, text, font):
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    draw.text((max(0, (128 - width) // 2), y), text, font=font, fill='white')


def render_power_overlay():
    """Draw a transient power event, or put the SSD1306 into sleep mode."""
    global DISPLAY_HIDDEN
    overlay = POWER_OVERLAY.update(read_status())
    if overlay and overlay.get('display_off'):
        if not DISPLAY_HIDDEN:
            device.hide()
            DISPLAY_HIDDEN = True
        return True
    if DISPLAY_HIDDEN:
        device.show()
        DISPLAY_HIDDEN = False
    if not overlay:
        return False
    with canvas(device) as draw:
        centered(draw, 3, overlay['title'], FONT)
        centered(draw, 24, overlay['message'], FONT_BIG)
        voltage = overlay.get('voltage_v')
        if voltage is not None:
            centered(draw, 50, f'BAT {voltage:.3f} V', FONT_SMALL)
    return True




# --------------------------------------------------
# Pages
# --------------------------------------------------

def page_system():
    end = time.monotonic() + PAGE_TIME

    while time.monotonic() < end and not STOP.is_set():
        if render_power_overlay():
            wait_refresh()
            continue
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        temp = get_temp()

        with canvas(device) as draw:
            draw.text(
                (0, 0),
                "SYSTEM",
                font=FONT_BIG,
                fill="white"
            )

            draw.text(
                (0, 21),
                f"CPU  {cpu:4.1f}%",
                font=FONT,
                fill="white"
            )

            draw.text(
                (0, 37),
                f"TEMP {temp:4.1f}C",
                font=FONT,
                fill="white"
            )

            draw.text(
                (0, 52),
                f"RAM  {ram:4.1f}%",
                font=FONT,
                fill="white"
            )

        wait_refresh()


def page_network():
    end = time.monotonic() + PAGE_TIME

    while time.monotonic() < end and not STOP.is_set():
        if render_power_overlay():
            wait_refresh()
            continue
        disk = psutil.disk_usage("/").percent
        ip = get_ip()

        with canvas(device) as draw:
            draw.text(
                (0, 0),
                "NETWORK",
                font=FONT_BIG,
                fill="white"
            )

            draw.text(
                (0, 23),
                f"Disk {disk:4.1f}%",
                font=FONT,
                fill="white"
            )

            draw.text(
                (0, 42),
                ip,
                font=FONT,
                fill="white"
            )

        wait_refresh()


def page_gps():
    end = time.monotonic() + PAGE_TIME

    while time.monotonic() < end and not STOP.is_set():
        if render_power_overlay():
            wait_refresh()
            continue
        mode = gps["mode"]

        if mode >= 3:
            fix = "3D FIX"
        elif mode == 2:
            fix = "2D FIX"
        else:
            fix = "NO FIX"

        seen = gps["seen"]
        used = gps["used"]
        hdop = gps["hdop"]

        if hdop is None:
            hdop_text = "--"
        else:
            hdop_text = f"{hdop:.2f}"

        with canvas(device) as draw:
            draw.text(
                (0, 0),
                "GPS",
                font=FONT_BIG,
                fill="white"
            )

            draw.text(
                (0, 21),
                fix,
                font=FONT,
                fill="white"
            )

            draw.text(
                (0, 37),
                f"SAT {used}/{seen}",
                font=FONT,
                fill="white"
            )

            draw.text(
                (0, 52),
                f"HDOP {hdop_text}",
                font=FONT,
                fill="white"
            )

        wait_refresh()


def page_time():
    end = time.monotonic() + PAGE_TIME

    while time.monotonic() < end and not STOP.is_set():
        if render_power_overlay():
            wait_refresh()
            continue
        clock = time.strftime("%H:%M:%S")
        date = time.strftime("%Y-%m-%d")
        source = get_time_source()
        uptime = get_uptime()

        with canvas(device) as draw:
            draw.text(
                (0, 0),
                clock,
                font=FONT_CLOCK,
                fill="white"
            )

            draw.text(
                (0, 29),
                date,
                font=FONT,
                fill="white"
            )

            draw.text(
                (0, 48),
                f"SRC {source}",
                font=FONT_SMALL,
                fill="white"
            )

            draw.text(
                (70, 48),
                f"UP {uptime}",
                font=FONT_SMALL,
                fill="white"
            )

        wait_refresh()


# --------------------------------------------------
# Main
# --------------------------------------------------

def page_watchdog():
    end = time.monotonic() + PAGE_TIME
    while time.monotonic() < end and not STOP.is_set():
        if render_power_overlay():
            wait_refresh()
            continue
        draw_watchdog(device, FONT, FONT_BIG,
                      min(REFRESH, end - time.monotonic()), REFRESH)


def main():
    init_display()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=gpsd_reader, daemon=True, name='gps-reader').start()
    psutil.cpu_percent(interval=None)
    try:
        while not STOP.is_set():
            page_system()
            page_network()
            page_watchdog()
            page_gps()
            page_time()
    finally:
        device.hide()


if __name__ == '__main__':
    main()
