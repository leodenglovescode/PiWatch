"""Read-only system values for OLED pages."""
import socket
import subprocess
import time
import psutil

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
