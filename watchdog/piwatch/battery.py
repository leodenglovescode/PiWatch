"""Read INA219 voltage data and infer battery and mains state."""
import fcntl
import os
import time
from collections import deque

I2C_SLAVE = 0x0703
BUS_VOLTAGE_REGISTER = 0x02
SHUNT_VOLTAGE_REGISTER = 0x01


def _read_register(descriptor, register):
    if os.write(descriptor, bytes((register,))) != 1:
        raise OSError('Incomplete INA219 register selection')
    payload = os.read(descriptor, 2)
    if len(payload) != 2:
        raise OSError('Incomplete INA219 register reading')
    return int.from_bytes(payload, 'big')


def read_ina219(bus, address):
    """Return bus and signed shunt voltage using only the standard library."""
    descriptor = os.open(f'/dev/i2c-{bus}', os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.ioctl(descriptor, I2C_SLAVE, address)
        bus_raw = _read_register(descriptor, BUS_VOLTAGE_REGISTER)
        shunt_raw = _read_register(descriptor, SHUNT_VOLTAGE_REGISTER)
    finally:
        os.close(descriptor)
    if shunt_raw & 0x8000:
        shunt_raw -= 0x10000
    return {'voltage_v': (bus_raw >> 3) * 0.004,
            'shunt_voltage_mv': shunt_raw * 0.01}


class BatteryMonitor:
    """Keep alarm hysteresis across samples; tracker counters handle debounce."""

    def __init__(self, reader=read_ina219, battery_low=False, mains_out=False,
                 learned_polarity=None):
        self.reader = reader
        self.low = battery_low
        self.mains_out = mains_out
        self.voltage_history = deque()
        self.outage_minimum = None
        self.detected_drop = None
        self.learned_polarity = {'positive': 1, 'negative': -1}.get(learned_polarity)
        self.detection_method = None

    def mains_sample(self, voltage, shunt_mv, settings):
        now = time.monotonic()
        cutoff = now - settings['mains_window_seconds']
        while self.voltage_history and self.voltage_history[0][0] < cutoff:
            self.voltage_history.popleft()
        prior_peak = max((value for _, value in self.voltage_history), default=voltage)
        current_drop = max(0, prior_peak - voltage)
        configured_polarity = {'positive': 1, 'negative': -1}.get(settings['discharge_polarity'])
        polarity = configured_polarity or self.learned_polarity
        discharging = (polarity is not None and
                       shunt_mv * polarity >= settings['discharge_shunt_mv'])
        if self.mains_out:
            self.outage_minimum = min(self.outage_minimum if self.outage_minimum is not None else voltage,
                                      voltage)
            direction_recovered = polarity is not None and not discharging
            voltage_recovered = (polarity is None and
                                 voltage - self.outage_minimum >= settings['mains_recover_rise_v'])
            if direction_recovered or voltage_recovered:
                self.mains_out = False
                self.outage_minimum = None
                self.detected_drop = None
                self.detection_method = None
                self.voltage_history.clear()
        elif discharging or current_drop >= settings['mains_drop_v']:
            self.mains_out = True
            self.outage_minimum = voltage
            self.detected_drop = current_drop
            self.detection_method = 'shunt-direction' if discharging else 'voltage-drop'
            if settings['discharge_polarity'] == 'auto' and abs(shunt_mv) >= settings['discharge_shunt_mv']:
                self.learned_polarity = 1 if shunt_mv > 0 else -1
                polarity = self.learned_polarity
                discharging = True
        if self.mains_out and discharging:
            self.detection_method = 'shunt-direction'
        self.voltage_history.append((now, voltage))
        return {'available': True, 'sampled': True, 'healthy': not self.mains_out,
                'outage': self.mains_out, 'inferred': True,
                'voltage_v': round(voltage, 3),
                'shunt_voltage_mv': round(shunt_mv, 3),
                'discharging': discharging,
                'discharge_polarity': ('positive' if polarity == 1 else
                                       'negative' if polarity == -1 else 'unlearned'),
                'detection_method': self.detection_method,
                'voltage_drop_v': round(self.detected_drop if self.mains_out else current_drop, 3),
                'drop_threshold_v': settings['mains_drop_v'],
                'recover_rise_v': settings['mains_recover_rise_v'],
                'window_seconds': settings['mains_window_seconds']}

    def sample(self, settings):
        base = {
            'available': False,
            'sampled': False,
            'checked_at': time.time(),
            'bus': settings['bus'],
            'address': f"0x{settings['address']:02x}",
            'low_voltage_v': settings['low_voltage_v'],
            'recover_voltage_v': settings['recover_voltage_v'],
        }
        try:
            reading = self.reader(settings['bus'], settings['address'])
        except OSError as exc:
            unavailable = {**base, 'healthy': None, 'error': type(exc).__name__}
            return {'battery': unavailable,
                    'mains': {**unavailable, 'inferred': True}}
        voltage = reading['voltage_v']
        shunt_mv = reading['shunt_voltage_mv']
        if self.low:
            self.low = voltage < settings['recover_voltage_v']
        else:
            self.low = voltage <= settings['low_voltage_v']
        battery = {**base, 'available': True, 'sampled': True,
                   'healthy': not self.low, 'low': self.low,
                   'voltage_v': round(voltage, 3),
                   'shunt_voltage_mv': round(shunt_mv, 3)}
        return {'battery': battery,
                'mains': {**base, **self.mains_sample(voltage, shunt_mv, settings)}}
