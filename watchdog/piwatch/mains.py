"""Read and debounce the dedicated mains-presence GPIO input."""
import time

GPIO_CHIP = '/dev/gpiochip0'
GPIO_LINE = 17


def read_gpio(chip=GPIO_CHIP, line=GPIO_LINE):
    """Return True when the active-high mains-presence input is HIGH."""
    import gpiod
    from gpiod.line import Direction, Value

    settings = gpiod.LineSettings(direction=Direction.INPUT)
    with gpiod.request_lines(chip, consumer='piwatch',
                             config={line: settings}) as request:
        return request.get_value(line) == Value.ACTIVE


class MainsMonitor:
    """Accept a GPIO transition only after it remains stable for the debounce period."""

    def __init__(self, reader=read_gpio, stable_state=None, clock=time.monotonic):
        self.reader = reader
        self.stable_state = stable_state
        self.clock = clock
        self.candidate_state = None
        self.candidate_since = None

    def sample(self, settings, voltage_v=None):
        base = {'checked_at': time.time(), 'gpio': GPIO_LINE,
                'physical_pin': 11, 'debounce_seconds': settings['mains_debounce_seconds'],
                'source': 'gpio'}
        if isinstance(voltage_v, (int, float)):
            base['voltage_v'] = round(voltage_v, 3)
        try:
            raw_state = bool(self.reader(GPIO_CHIP, GPIO_LINE))
        except (ImportError, OSError) as exc:
            return {**base, 'available': False, 'sampled': False,
                    'healthy': None, 'error': type(exc).__name__}

        now = self.clock()
        if raw_state == self.stable_state:
            self.candidate_state = None
            self.candidate_since = None
        elif raw_state != self.candidate_state:
            self.candidate_state = raw_state
            self.candidate_since = now
        elif now - self.candidate_since >= settings['mains_debounce_seconds']:
            self.stable_state = raw_state
            self.candidate_state = None
            self.candidate_since = None

        debouncing = self.candidate_state is not None
        return {**base, 'available': True, 'sampled': self.stable_state is not None,
                'healthy': self.stable_state, 'outage': self.stable_state is False,
                'gpio_level': raw_state, 'debouncing': debouncing,
                'pending_state': self.candidate_state if debouncing else None}
