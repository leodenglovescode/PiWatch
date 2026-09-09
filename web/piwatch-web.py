#!/usr/bin/env python3
"""Local panel entry point and terminal-only account initialization."""
import argparse
import getpass
import os
from pathlib import Path
import sys

# Works both in the checkout and when installed next to the piwatch package.
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'watchdog' if (root / 'watchdog').is_dir() else root))
from piwatch_web.app import create_app
from piwatch.config import load_config
from piwatch.config_store import SettingsStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('serve', 'init', 'reset-account'), nargs='?', default='serve')
    parser.add_argument('--config', default='/etc/piwatch/config.json')
    args = parser.parse_args()
    if args.command != 'serve' and not sys.stdin.isatty():
        raise SystemExit('Run account setup/recovery in an interactive terminal on the Pi')
    app = create_app()
    if args.command == 'init':
        username = input('Owner username: ').strip()
        password = getpass.getpass('Owner password (15+ characters): ')
        if password != getpass.getpass('Confirm password: '):
            raise SystemExit('Passwords do not match')
        settings = SettingsStore()
        if not settings.path.exists():
            settings.initialize(load_config(args.config))
        app.extensions['auth_store'].create_owner(username, password)
        print('Owner created. Start the piwatch-web service to sign in.')
    elif args.command == 'reset-account':
        # OS access as the service user/root is the explicit recovery authority.
        auth = app.extensions['auth_store']
        username = input('Owner username: ').strip()
        password = getpass.getpass('New password (15+ characters): ')
        if password != getpass.getpass('Confirm password: '):
            raise SystemExit('Passwords do not match')
        auth.update_account(username, password, 30)
        auth.disable_factor('totp')
        auth.disable_factor('email')
        print('Account recovered. All sessions and second factors were revoked.')
    else:
        from waitress import serve
        bind = os.environ.get('PIWATCH_WEB_BIND', '127.0.0.1')
        port = int(os.environ.get('PIWATCH_WEB_PORT', '8080'))
        print(f'PiWatch panel: http://{bind}:{port}', flush=True)
        serve(app, host=bind, port=port, threads=4, max_request_body_size=65536,
              channel_timeout=30, clear_untrusted_proxy_headers=True)


if __name__ == '__main__':
    main()
