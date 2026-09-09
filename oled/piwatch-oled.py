import time
import socket
import json
import psutil
import subprocess
import threading

from PIL import ImageFont
from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas


# --------------------------------------------------
# OLED
# --------------------------------------------------

serial = i2c(port=1, address=0x3C)
device = ssd1306(serial, width=128, height=64)

REFRESH = 0.5
PAGE_TIME = 5.0


# --------------------------------------------------
# Fonts
# --------------------------------------------------

def find_font(name):
    return subprocess.check_output(
        ["fc-match", "-f", "%{file}", name],
        text=True
    ).strip()


FONT_PATH = find_font("DejaVu Sans")
BOLD_PATH = find_font("DejaVu Sans:style=Bold")

FONT = ImageFont.truetype(FONT_PATH, 12)
FONT_BIG = ImageFont.truetype(BOLD_PATH, 16)
FONT_CLOCK = ImageFont.truetype(BOLD_PATH, 22)
FONT_SMALL = ImageFont.truetype(FONT_PATH, 9)


# --------------------------------------------------
# GPSD data
# --------------------------------------------------

gps = {
    "mode": 0,
    "lat": None,
    "lon": None,
    "time": None,
    "hdop": None,
    "seen": 0,
    "used": 0,
}


def gpsd_reader():
    while True:
        try:
            s = socket.create_connection(
                ("127.0.0.1", 2947),
                timeout=5
            )

            s.sendall(
                b'?WATCH={"enable":true,"json":true};\n'
            )

            f = s.makefile()

            for line in f:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                cls = data.get("class")

                if cls == "TPV":
                    gps["mode"] = data.get("mode", 0)
                    gps["lat"] = data.get("lat")
                    gps["lon"] = data.get("lon")
                    gps["time"] = data.get("time")

                elif cls == "SKY":
                # Some gpsd SKY reports don't include satellite counts.
                # Only update values when they are actually present.
                    if "nSat" in data:
                        gps["seen"] = data["nSat"]

                    if "uSat" in data:
                        gps["used"] = data["uSat"]

                    if "hdop" in data:
                        gps["hdop"] = data["hdop"]


        except Exception:
            gps["mode"] = 0
            gps["seen"] = 0
            gps["used"] = 0
            gps["hdop"] = None
            time.sleep(2)


threading.Thread(
    target=gpsd_reader,
    daemon=True
).start()


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "No network"


def get_temp():
    try:
        with open(
            "/sys/class/thermal/thermal_zone0/temp"
        ) as f:
            return float(f.read()) / 1000
    except Exception:
        return 0.0


def get_uptime():
    seconds = int(time.time() - psutil.boot_time())

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    if days:
        return f"{days}d {hours}h"

    return f"{hours}h {minutes}m"


def get_time_source():
    try:
        result = subprocess.check_output(
            ["chronyc", "tracking"],
            text=True,
            timeout=2
        )

        for line in result.splitlines():
            if line.startswith("Reference ID"):
                if "(PPS)" in line:
                    return "PPS"

                if "203.107.6.88" in line:
                    return "ALIYUN"

                if "106.55.184.199" in line:
                    return "TENCENT"

        return "NTP"

    except Exception:
        return "UNKNOWN"


# --------------------------------------------------
# Pages
# --------------------------------------------------

def page_system():
    end = time.monotonic() + PAGE_TIME

    while time.monotonic() < end:
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

        time.sleep(REFRESH)


def page_network():
    end = time.monotonic() + PAGE_TIME

    while time.monotonic() < end:
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

        time.sleep(REFRESH)


def page_gps():
    end = time.monotonic() + PAGE_TIME

    while time.monotonic() < end:
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

        time.sleep(REFRESH)


def page_time():
    end = time.monotonic() + PAGE_TIME

    while time.monotonic() < end:
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

        time.sleep(REFRESH)


# --------------------------------------------------
# Main
# --------------------------------------------------

psutil.cpu_percent(interval=None)

while True:
    page_system()
    page_network()
    page_gps()
    page_time()
