"""GPS state from gpsd; importing this module starts no thread or serial I/O."""
import json
import socket
import time

gps = dict(mode=0, lat=None, lon=None, time=None, hdop=None, seen=0, used=0)


def gpsd_reader():
    while True:
        try:
            with socket.create_connection(('127.0.0.1', 2947), timeout=5) as connection:
                connection.sendall(b'?WATCH={"enable":true,"json":true};\n')
                with connection.makefile() as stream:
                    last_tpv = time.monotonic()
                    for line in stream:
                        try:
                            data = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get('class') == 'TPV':
                            last_tpv = time.monotonic()
                            gps.update(mode=data.get('mode', 0), lat=data.get('lat'), lon=data.get('lon'), time=data.get('time'))
                        elif data.get('class') == 'SKY':
                            for source, target in (('nSat', 'seen'), ('uSat', 'used'), ('hdop', 'hdop')):
                                if source in data:
                                    gps[target] = data[source]
                        if time.monotonic() - last_tpv > 10:
                            raise TimeoutError('GPS reports stale')
        except (OSError, ValueError):
            pass
        finally:
            gps.update(mode=0, lat=None, lon=None, time=None, hdop=None, seen=0, used=0)
        time.sleep(2)
