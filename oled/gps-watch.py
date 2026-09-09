import serial
import time
import os

PORT = "/dev/serial0"
BAUD = 9600

gps = serial.Serial(PORT, BAUD, timeout=2)

fix = False
fix_quality = 0
sats_used = 0
sats_seen = 0
hdop = "N/A"
utc = "N/A"
lat = "N/A"
lon = "N/A"

def coord(value, hemi):
    if not value:
        return "N/A"

    try:
        v = float(value)
        deg = int(v // 100)
        minutes = v - deg * 100
        result = deg + minutes / 60

        if hemi in ("S", "W"):
            result = -result

        return f"{result:.6f}"
    except:
        return "N/A"

while True:
    try:
        line = gps.readline().decode(
            "ascii", errors="ignore"
        ).strip()

        if not line.startswith("$"):
            continue

        parts = line.split(",")

        # RMC: valid fix + UTC
        if line.startswith(("$GPRMC", "$GNRMC")):
            if len(parts) > 2:
                utc = parts[1] or "N/A"
                fix = parts[2] == "A"

            if len(parts) > 6:
                lat = coord(parts[3], parts[4])
                lon = coord(parts[5], parts[6])

        # GGA: fix quality + satellites used + HDOP
        elif line.startswith(("$GPGGA", "$GNGGA")):
            try:
                fix_quality = int(parts[6] or 0)
                sats_used = int(parts[7] or 0)
                hdop = parts[8] or "N/A"
            except:
                pass

        # GSV: satellites visible
        elif "GSV" in parts[0]:
            try:
                sats_seen = int(parts[3] or 0)
            except:
                pass

        os.system("clear")

        print("=== PiWatch GPS ===")
        print()

        if fix or fix_quality > 0:
            print("FIX:        YES ✓")
        else:
            print("FIX:        NO")

        print(f"Quality:    {fix_quality}")
        print(f"Sats used:  {sats_used}")
        print(f"Sats seen:  {sats_seen}")
        print(f"HDOP:       {hdop}")
        print(f"UTC:        {utc}")
        print()
        print(f"Latitude:   {lat}")
        print(f"Longitude:  {lon}")
        print()
        print("Waiting for GPS data... Ctrl+C to stop")

        time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nStopped.")
        break
