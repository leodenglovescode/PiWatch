#!/usr/bin/env python3
"""Publish PiWatch measurements to Home Assistant through MQTT Discovery."""
from piwatch.mqtt_bridge import main


if __name__ == '__main__':
    main()

