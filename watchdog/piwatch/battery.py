"""Read INA219 battery voltage and apply the low-voltage hysteresis."""
import fcntl
import os
import time

I2C_SLAVE = 0x0703
BUS_VOLTAGE_REGISTER = 0x02


def _read_register(descriptor, register):
    if os.write(descriptor, bytes((register,))) != 1:
        raise OSError('Incomplete INA219 register selection')
    payload = os.read(descriptor, 2)
    if len(payload) != 2:
        raise OSError('Incomplete INA219 register reading')
    return int.from_bytes(payload, 'big')


def read_ina219(bus, address):
    """Return INA219 bus voltage using only the standard library."""
    descriptor = os.open(f'/dev/i2c-{bus}', os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.ioctl(descriptor, I2C_SLAVE, address)
        bus_raw = _read_register(descriptor, BUS_VOLTAGE_REGISTER)
    finally:
        os.close(descriptor)
    return {'voltage_v': (bus_raw >> 3) * 0.004}


class BatteryMonitor:
    """Keep alarm hysteresis across samples; tracker counters handle debounce."""

    def __init__(self, reader=read_ina219, battery_low=False):
        self.reader = reader
        self.low = battery_low

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
            return {**base, 'healthy': None, 'error': type(exc).__name__}
        voltage = reading['voltage_v']
        if self.low:
            self.low = voltage < settings['recover_voltage_v']
        else:
            self.low = voltage <= settings['low_voltage_v']
        return {**base, 'available': True, 'sampled': True,
                'healthy': not self.low, 'low': self.low,
                'voltage_v': round(voltage, 3)}
