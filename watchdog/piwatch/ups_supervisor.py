"""Pico command pulses and strict low-battery shutdown qualification."""
import math
import subprocess
import time

GPIO_CHIP = '/dev/gpiochip0'
PICO_COMMAND_GPIO = 2
PICO_COMMAND_PHYSICAL_PIN = 3
PICO_WAKE_GPIO = 3
PICO_WAKE_PHYSICAL_PIN = 5
DISARM_PULSE_SECONDS = 0.150
ARM_PULSE_SECONDS = 1.0
PICO_REGISTER_SECONDS = 0.250
SHUTDOWN_VOLTAGE_V = 3.05
SHUTDOWN_QUALIFICATION_SECONDS = 30.0
NOTIFICATION_START_TIMEOUT_SECONDS = 3.0
POWEROFF_COMMAND = (
    '/usr/bin/sudo', '-n', '/usr/bin/systemctl', '--no-block', 'poweroff',
)


class PicoSupervisor:
    """Signal the Pico by pulling GPIO2 LOW, then returning it to high impedance."""

    def __init__(self, chip=GPIO_CHIP, line=PICO_COMMAND_GPIO, sleeper=time.sleep):
        self.chip = chip
        self.line = line
        self.sleeper = sleeper

    def pulse_low(self, duration):
        import gpiod
        from gpiod.line import Direction, Value

        output_low = gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.INACTIVE)
        released = gpiod.LineSettings(direction=Direction.INPUT)
        request = gpiod.request_lines(
            self.chip, consumer='piwatch-pico', config={self.line: output_low})
        try:
            self.sleeper(duration)
        finally:
            try:
                request.reconfigure_lines({self.line: released})
            finally:
                request.release()

    def disarm(self):
        self.pulse_low(DISARM_PULSE_SECONDS)

    def arm(self):
        self.pulse_low(ARM_PULSE_SECONDS)


class LowBatteryShutdown:
    """Require uninterrupted fresh low-voltage evidence while mains is absent."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.low_since = None
        self.latched = False
        self.action_state = 'IDLE'
        self.error = None
        self.startup_disarmed = None
        self.startup_error = None
        self.last_voltage_v = None
        self.mains_present = None

    def reset(self):
        if not self.latched:
            self.low_since = None
            self.action_state = 'IDLE'

    def update(self, mains_present, battery):
        """Return True once after 30 continuous seconds at or below 3.05 V."""
        if self.latched:
            return False

        self.mains_present = mains_present if isinstance(mains_present, bool) else None
        voltage = battery.get('voltage_v') if isinstance(battery, dict) else None
        valid = (isinstance(voltage, (int, float)) and not isinstance(voltage, bool)
                 and math.isfinite(voltage) and battery.get('available') is True
                 and battery.get('sampled') is True)
        self.last_voltage_v = float(voltage) if valid else None

        if self.mains_present is not False or not valid or voltage > SHUTDOWN_VOLTAGE_V:
            self.reset()
            return False

        now = self.clock()
        if self.low_since is None:
            self.low_since = now
            self.action_state = 'QUALIFYING'
            return False
        if now - self.low_since < SHUTDOWN_QUALIFICATION_SECONDS:
            return False

        self.latched = True
        self.action_state = 'TRIGGERED'
        return True

    def mark(self, state, error=None):
        self.action_state = state
        self.error = error

    def mark_startup_disarm(self, succeeded, error=None):
        self.startup_disarmed = bool(succeeded)
        self.startup_error = error

    def snapshot(self):
        now = self.clock()
        elapsed = (max(0.0, now - self.low_since)
                   if self.low_since is not None else 0.0)
        return {
            'state': self.action_state,
            'latched': self.latched,
            'mains_present': self.mains_present,
            'battery_voltage_v': self.last_voltage_v,
            'shutdown_voltage_v': SHUTDOWN_VOLTAGE_V,
            'qualification_seconds': SHUTDOWN_QUALIFICATION_SECONDS,
            'low_for_seconds': round(min(elapsed, SHUTDOWN_QUALIFICATION_SECONDS), 1),
            'remaining_seconds': round(max(0.0, SHUTDOWN_QUALIFICATION_SECONDS - elapsed), 1),
            'command_gpio': PICO_COMMAND_GPIO,
            'command_physical_pin': PICO_COMMAND_PHYSICAL_PIN,
            'wake_gpio': PICO_WAKE_GPIO,
            'wake_physical_pin': PICO_WAKE_PHYSICAL_PIN,
            'startup_disarmed': self.startup_disarmed,
            'startup_error': self.startup_error,
            'error': self.error,
        }


def request_poweroff():
    """Request a clean shutdown through the narrowly authorized systemctl command."""
    subprocess.run(POWEROFF_COMMAND, check=True, timeout=10)
