# Reliability validation

Run `.venvs/meshtastic/bin/python -m unittest discover -s tests -v`.
Tests import the API with a synthetic token, mock radio/network delivery, and use
SQLite databases in temporary directories. They do not open a radio or send
notifications. Bubblewrap may prevent asyncio cross-thread wakeups; run on the
host if tests stall there.

Syntax: `python3 -m compileall -q oled lora watchdog tests`.
