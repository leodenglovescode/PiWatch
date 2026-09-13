"""Pico command pulses and strict low-battery shutdown qualification."""
import json
import math
import os
from pathlib import Path
import subprocess
import time
import uuid

from .battery import BatteryMonitor
from .mains import MainsMonitor

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
TEST_REQUEST_PATH = Path(os.environ.get(
    'PIWATCH_UPS_TEST_REQUEST_PATH', '/run/piwatch/ups-test-request.json'))
TEST_RESULT_PATH = Path(os.environ.get(
    'PIWATCH_UPS_TEST_RESULT_PATH', '/run/piwatch/ups-test-result.json'))


def valid_battery_voltage(battery):
    if not isinstance(battery, dict):
        return None
    voltage = battery.get('voltage_v')
    if (not isinstance(voltage, (int, float)) or isinstance(voltage, bool)
            or not math.isfinite(voltage) or battery.get('available') is not True
            or battery.get('sampled') is not True):
        return None
    return float(voltage)


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

    def __init__(self, clock=time.monotonic,
                 shutdown_voltage_v=SHUTDOWN_VOLTAGE_V,
                 qualification_seconds=SHUTDOWN_QUALIFICATION_SECONDS):
        if (not math.isfinite(shutdown_voltage_v)
                or not math.isfinite(qualification_seconds)
                or qualification_seconds <= 0):
            raise ValueError('Invalid shutdown qualification')
        self.clock = clock
        self.shutdown_voltage_v = float(shutdown_voltage_v)
        self.qualification_seconds = float(qualification_seconds)
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
        voltage = valid_battery_voltage(battery)
        valid = voltage is not None
        self.last_voltage_v = voltage

        if (self.mains_present is not False or not valid
                or voltage > self.shutdown_voltage_v):
            self.reset()
            return False

        now = self.clock()
        if self.low_since is None:
            self.low_since = now
            self.action_state = 'QUALIFYING'
            return False
        if now - self.low_since < self.qualification_seconds:
            return False

        self.latched = True
        self.action_state = 'TRIGGERED'
        return True

    def trigger_test(self, voltage_v):
        if self.latched:
            return False
        self.latched = True
        self.last_voltage_v = float(voltage_v)
        self.mains_present = False
        self.low_since = self.clock()
        self.action_state = 'TEST_TRIGGERED'
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
            'shutdown_voltage_v': self.shutdown_voltage_v,
            'qualification_seconds': self.qualification_seconds,
            'low_for_seconds': round(min(elapsed, self.qualification_seconds), 1),
            'remaining_seconds': round(max(0.0, self.qualification_seconds - elapsed), 1),
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


def live_sample(settings, battery_monitor=None, mains_monitor=None):
    """Read the two authoritative shutdown inputs without changing GPIO outputs."""
    battery_monitor = battery_monitor or BatteryMonitor()
    mains_monitor = mains_monitor or MainsMonitor()
    battery = battery_monitor.sample(settings)
    voltage = battery.get('voltage_v') if battery else None
    mains = mains_monitor.sample(settings, voltage)
    return battery, mains


def dry_run(settings, threshold, qualification_seconds=30.0,
            timeout_seconds=120.0, output=print, clock=time.monotonic,
            sleeper=time.sleep):
    """Exercise real inputs and countdown without GPIO2 or shutdown side effects."""
    monitor = LowBatteryShutdown(
        clock=clock, shutdown_voltage_v=threshold,
        qualification_seconds=qualification_seconds)
    battery_monitor = BatteryMonitor()
    mains_monitor = MainsMonitor()
    deadline = clock() + timeout_seconds
    output('DRY RUN: GPIO2 and poweroff are disabled; production settings are unchanged.')
    while clock() < deadline:
        cycle = clock()
        battery, mains = live_sample(settings, battery_monitor, mains_monitor)
        mains_present = (mains.get('gpio_level')
                         if mains.get('available') is True else None)
        triggered = monitor.update(mains_present, battery)
        snapshot = monitor.snapshot()
        voltage = (f"{snapshot['battery_voltage_v']:.3f} V"
                   if snapshot['battery_voltage_v'] is not None else 'invalid')
        mains_text = ('present' if mains_present is True else 'absent'
                      if mains_present is False else 'unavailable')
        output(f"mains={mains_text} battery={voltage} state={snapshot['state']} "
               f"remaining={snapshot['remaining_seconds']:.1f}s")
        if triggered:
            output('DRY RUN PASSED: the shutdown condition would trigger now.')
            return True
        sleeper(max(0.05, 1 - (clock() - cycle)))
    output('DRY RUN ENDED: the condition did not remain true for the full interval.')
    return False


def pico_command_test(settings, output=print, sleeper=time.sleep):
    """ARM then reliably DISARM the Pico without requesting shutdown."""
    battery, mains = live_sample(settings)
    mains_present = (mains.get('gpio_level')
                     if mains.get('available') is True else None)
    if mains_present is not True:
        raise RuntimeError('Pico command test requires GPIO17 HIGH (mains present)')
    voltage = battery.get('voltage_v') if battery.get('available') is True else None
    output('PICO TEST: mains is present; no shutdown will be requested.')
    if isinstance(voltage, (int, float)):
        output(f'Battery voltage: {voltage:.3f} V')
    pico = PicoSupervisor(sleeper=sleeper)
    try:
        pico.arm()
        output('ARM pulse sent; waiting two seconds for the Pico indication.')
        sleeper(2.0)
    finally:
        pico.disarm()
        output('DISARM pulse sent; GPIO2 released to input.')


def create_shutdown_test_request(path=TEST_REQUEST_PATH):
    """Create one root/operator request for the running daemon."""
    path = Path(path)
    request_id = uuid.uuid4().hex
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o644)
    try:
        payload = json.dumps({'id': request_id, 'action': 'shutdown'}).encode()
        if os.write(descriptor, payload) != len(payload):
            raise OSError('Incomplete UPS test request write')
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return request_id


def take_shutdown_test_request(path=TEST_REQUEST_PATH):
    """Consume a valid one-shot request before starting any side effects."""
    path = Path(path)
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    try:
        request = json.loads(raw)
    finally:
        path.unlink(missing_ok=True)
    request_id = request.get('id') if isinstance(request, dict) else None
    if (not isinstance(request, dict) or request.get('action') != 'shutdown'
            or not isinstance(request_id, str)
            or len(request_id) != 32
            or any(character not in '0123456789abcdef' for character in request_id)):
        raise ValueError('Invalid UPS test request')
    return request


def write_shutdown_test_result(request_id, accepted, reason,
                               path=TEST_RESULT_PATH):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps({
        'id': request_id, 'accepted': bool(accepted), 'reason': reason,
    }), encoding='utf-8')
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def wait_for_shutdown_test_result(request_id, request_path=TEST_REQUEST_PATH,
                                  result_path=TEST_RESULT_PATH,
                                  timeout_seconds=5.0, clock=time.monotonic,
                                  sleeper=time.sleep):
    deadline = clock() + timeout_seconds
    result_path = Path(result_path)
    while clock() < deadline:
        try:
            result = json.loads(result_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            sleeper(0.1)
            continue
        if result.get('id') == request_id:
            result_path.unlink(missing_ok=True)
            return result
        sleeper(0.1)
    Path(request_path).unlink(missing_ok=True)
    raise TimeoutError('The running PiWatch daemon did not accept the request')
