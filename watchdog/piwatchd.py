#!/usr/bin/env python3
"""Entry point for the PiWatch monitoring service."""
import argparse
import os


def bounded_float(minimum, maximum):
    def parse(value):
        number = float(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f'must be between {minimum:g} and {maximum:g}')
        return number
    return parse


def load_battery_settings():
    from piwatch.config import load_config
    from piwatch.config_store import SettingsStore

    store = SettingsStore()
    return (store.read()[1] if store.path.exists() else load_config())['battery']


def ups_test(arguments):
    from piwatch.ups_supervisor import (
        TEST_RESULT_PATH, create_shutdown_test_request, dry_run,
        pico_command_test, wait_for_shutdown_test_result,
    )

    if arguments.test == 'dry-run':
        settings = load_battery_settings()
        passed = dry_run(
            settings, arguments.threshold, arguments.seconds, arguments.timeout)
        return 0 if passed else 1
    if arguments.test == 'pico':
        if arguments.confirm != 'PULSE-GPIO2':
            raise SystemExit('Refusing GPIO2 test: add --confirm PULSE-GPIO2')
        pico_command_test(load_battery_settings())
        return 0
    if arguments.confirm != 'POWER-OFF':
        raise SystemExit('Refusing real shutdown test: add --confirm POWER-OFF')
    if os.geteuid() != 0:
        raise SystemExit('Run the real shutdown test with sudo')
    TEST_RESULT_PATH.unlink(missing_ok=True)
    request_id = create_shutdown_test_request()
    print('Shutdown test submitted; waiting for the running PiWatch daemon...')
    result = wait_for_shutdown_test_result(request_id)
    if not result['accepted']:
        raise SystemExit(f"Shutdown test rejected: {result['reason']}")
    print('Accepted. PiWatch will notify, ARM the Pico, and power off now.')
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command')
    tests = commands.add_parser('ups-test', help='run a guarded UPS supervisor test')
    modes = tests.add_subparsers(dest='test', required=True)

    dry = modes.add_parser('dry-run', help='test real inputs and countdown only')
    dry.add_argument('--threshold', required=True, type=bounded_float(2.5, 4.2),
                     help='temporary dry-run threshold in volts')
    dry.add_argument('--seconds', type=bounded_float(1, 120), default=30.0,
                     help='continuous qualification time (default: 30)')
    dry.add_argument('--timeout', type=bounded_float(5, 900), default=120.0,
                     help='stop waiting after this many seconds (default: 120)')

    pico = modes.add_parser('pico', help='send ARM followed by DISARM; never power off')
    pico.add_argument('--confirm', help='must be PULSE-GPIO2')

    shutdown = modes.add_parser(
        'shutdown', help='perform a real notification, ARM, poweroff, and wake-cycle test')
    shutdown.add_argument('--confirm', help='must be POWER-OFF')
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_args(argv)
    if arguments.command == 'ups-test':
        try:
            return ups_test(arguments)
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise SystemExit(f'UPS test failed: {exc}') from None
    from piwatch.daemon import main as daemon_main
    daemon_main()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
