"""Validated monitoring settings; legacy JSON is supported for first startup."""
import copy
import ipaddress
import json
import math
import os
import re
from urllib.parse import urlsplit

CONFIG_PATH = os.environ.get('PIWATCH_CONFIG_PATH', '/etc/piwatch/config.json')
DEFAULT_INTERNET = {'enabled': True, 'targets': [
    {'host': '1.1.1.1', 'port': 443}, {'host': '223.5.5.5', 'port': 53}]}
DEFAULT_DNS = {'enabled': True, 'names': ['example.com', 'www.baidu.com']}
DEFAULT_BATTERY = {'enabled': True, 'bus': 3, 'address': 0x40,
                   'low_voltage_v': 3.30, 'recover_voltage_v': 3.50,
                   'mains_detection_enabled': True, 'mains_drop_v': 0.05,
                   'mains_recover_rise_v': 0.05, 'mains_window_seconds': 10.0,
                   'discharge_polarity': 'auto', 'discharge_shunt_mv': 0.50}
SECRET_FIELDS = {'lora': ('token',), 'smtp': ('password',), 'home_assistant': ('webhook_id',)}
GATEWAY_FIELDS = {
    'lora': {'enabled', 'url', 'token'},
    'smtp': {'enabled', 'host', 'port', 'security', 'username', 'password', 'sender', 'recipient'},
    'home_assistant': {'enabled', 'url', 'webhook_id'},
}


def load_config(path=CONFIG_PATH):
    with open(path, encoding='utf-8') as stream:
        return validate_config(json.load(stream))


def host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or value.startswith('-') or any(c.isspace() for c in value):
        raise ValueError('Host must be an IP address or hostname')
    return value


def port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    return value


def _validate_config(raw):
    config = copy.deepcopy(raw)
    if not isinstance(config, dict):
        raise ValueError('Settings must be an object')
    for key, default in (('interval_seconds', 1), ('external_interval_seconds', 15), ('health_interval_seconds', 15)):
        value = float(config.get(key, default))
        if not math.isfinite(value) or not 1 <= value <= 3600:
            raise ValueError(f'{key} must be between 1 and 3600 seconds')
        config[key] = value
    for key, default in (('suspect_after_failures', 3), ('down_after_failures', 5), ('recover_after_successes', 3)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
            raise ValueError(f'{key} must be an integer from 1 to 1000')
        config[key] = value
    if config['suspect_after_failures'] > config['down_after_failures']:
        raise ValueError('Suspect threshold must not exceed down threshold')
    if 'targets' not in config:
        config['targets'] = [
            {'id': 'router', 'name': 'Router', 'host': config['router']['host'], 'enabled': True, 'checks': [{'type': 'ping'}]},
            {'id': 'super_server', 'name': 'Super Server', 'host': config['super_server']['host'], 'enabled': True,
             'checks': [{'type': 'ping'}, {'type': 'tcp', 'port': config['super_server']['ssh_port'], 'label': 'SSH'}]},
            {'id': 'home_assistant', 'name': 'Home Assistant', 'host': config['home_assistant']['host'], 'enabled': True,
             'checks': [{'type': 'ping'}, {'type': 'http', 'port': config['home_assistant']['port'], 'path': config['home_assistant']['path']}]},
        ]
    targets = config['targets']
    if not isinstance(targets, list) or len(targets) > 32:
        raise ValueError('Configure at most 32 monitored targets')
    ids = set()
    for target in targets:
        ident = target.get('id', '')
        if not isinstance(ident, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', ident) or ident in ids or ident in ('internet', 'dns'):
            raise ValueError('Each target needs a unique ID; internet and dns are reserved')
        ids.add(ident)
        if not isinstance(target.get('name'), str) or not 1 <= len(target['name']) <= 40:
            raise ValueError('Target name must contain 1–40 characters')
        host(target['host'])
        target.setdefault('enabled', True)
        if not isinstance(target['enabled'], bool):
            raise ValueError('Target enabled must be a boolean')
        checks = target.get('checks', [])
        if not isinstance(checks, list) or not 1 <= len(checks) <= 3:
            raise ValueError('Each target requires 1–3 checks')
        kinds = set()
        for check in checks:
            kind = check.get('type')
            if kind not in ('ping', 'tcp', 'http') or kind in kinds:
                raise ValueError('Select each check type at most once: ping, tcp, http')
            kinds.add(kind)
            if kind != 'ping':
                port(check['port'])
            if kind == 'http':
                path = check.get('path', '/')
                if not isinstance(path, str) or not path.startswith('/') or len(path) > 1024 or any(ord(c) <= 32 or ord(c) > 126 for c in path):
                    raise ValueError('HTTP path must start with / and contain no whitespace')
                check['path'] = path
    for name, defaults in (('internet', DEFAULT_INTERNET), ('dns', DEFAULT_DNS)):
        config[name] = {**defaults, **config.get(name, {})}
        if not isinstance(config[name]['enabled'], bool):
            raise ValueError(f'{name}.enabled must be a boolean')
    targets = config['internet']['targets']
    if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
        raise ValueError('Internet requires 1–8 IP targets')
    for target in targets:
        ipaddress.ip_address(target['host'])
        port(target['port'])
    names = config['dns']['names']
    if not isinstance(names, list) or not 1 <= len(names) <= 8:
        raise ValueError('DNS requires 1–8 names')
    for name in names:
        host(name)
    battery = config.setdefault('battery', copy.deepcopy(DEFAULT_BATTERY))
    if not isinstance(battery, dict) or set(battery) - DEFAULT_BATTERY.keys():
        raise ValueError('Unknown battery setting')
    battery = config['battery'] = {**DEFAULT_BATTERY, **battery}
    if not isinstance(battery['enabled'], bool):
        raise ValueError('battery.enabled must be a boolean')
    if isinstance(battery['bus'], bool) or not isinstance(battery['bus'], int) or not 0 <= battery['bus'] <= 255:
        raise ValueError('Battery I2C bus must be an integer from 0 to 255')
    if isinstance(battery['address'], bool) or not isinstance(battery['address'], int) or not 0x40 <= battery['address'] <= 0x4f:
        raise ValueError('INA219 address must be between 0x40 and 0x4f')
    for key in ('low_voltage_v', 'recover_voltage_v'):
        value = battery[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'battery.{key} must be a finite number')
        battery[key] = float(value)
    if not 2.5 <= battery['low_voltage_v'] <= 4.0:
        raise ValueError('Battery low voltage must be between 2.50 and 4.00 V')
    if not battery['low_voltage_v'] + 0.04 <= battery['recover_voltage_v'] <= 4.2:
        raise ValueError('Battery recovery voltage must be at least 0.04 V above low and no more than 4.20 V')
    if not isinstance(battery['mains_detection_enabled'], bool):
        raise ValueError('battery.mains_detection_enabled must be a boolean')
    if battery['discharge_polarity'] not in ('auto', 'positive', 'negative'):
        raise ValueError('Battery discharge polarity must be auto, positive, or negative')
    for key in ('mains_drop_v', 'mains_recover_rise_v', 'mains_window_seconds',
                'discharge_shunt_mv'):
        value = battery[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'battery.{key} must be a finite number')
        battery[key] = float(value)
    if not 0.04 <= battery['mains_drop_v'] <= 0.80:
        raise ValueError('Mains detection drop must be between 0.04 and 0.80 V')
    if not 0.04 <= battery['mains_recover_rise_v'] <= 0.80:
        raise ValueError('Mains recovery rise must be between 0.04 and 0.80 V')
    if not 2 <= battery['mains_window_seconds'] <= 60:
        raise ValueError('Mains detection window must be between 2 and 60 seconds')
    if not 0.05 <= battery['discharge_shunt_mv'] <= 100:
        raise ValueError('Discharge shunt threshold must be between 0.05 and 100 mV')
    gateways = config.setdefault('gateways', {})
    if not isinstance(gateways, dict) or set(gateways) - GATEWAY_FIELDS.keys():
        raise ValueError('Unknown message gateway')
    for name, allowed in GATEWAY_FIELDS.items():
        gateway = gateways.setdefault(name, {})
        if not isinstance(gateway, dict) or set(gateway) - allowed:
            raise ValueError(f'Unknown {name} setting')
        gateway.setdefault('enabled', True)
        if not isinstance(gateway['enabled'], bool):
            raise ValueError('Gateway enabled must be a boolean')
        for key, value in gateway.items():
            if key not in ('enabled', 'port') and (not isinstance(value, str) or len(value) > 4096 or '\n' in value or '\r' in value):
                raise ValueError(f'Invalid {name} {key}')
        if gateway.get('url'):
            url = urlsplit(gateway['url'])
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('Gateway URL must be HTTP(S), without credentials, query, or fragment')
            if url.port is not None:
                port(url.port)
            gateway['url'] = gateway['url'].rstrip('/')
        if name == 'smtp' and gateway.get('host'):
            host(gateway['host'])
        if 'port' in gateway:
            port(gateway['port'])
        if 'security' in gateway and gateway['security'] not in ('ssl', 'starttls'):
            raise ValueError('SMTP requires SSL or STARTTLS')
    return {key: config[key] for key in ('targets', 'internet', 'dns', 'battery', 'gateways', 'interval_seconds',
            'external_interval_seconds', 'health_interval_seconds', 'suspect_after_failures',
            'down_after_failures', 'recover_after_successes')}


def validate_config(raw):
    try:
        return _validate_config(raw)
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError('Invalid settings structure: check required fields and value types') from exc
