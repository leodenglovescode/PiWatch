"""Coordinate monitoring, live settings, durable delivery, and health sampling."""
import asyncio
import json
import logging
import signal
import time

from .checks import check_all, ExternalChecks
from .battery import BatteryMonitor
from .config import load_config
from .config_store import SettingsStore
from .health import health_worker, SystemSampler
from .mains import MainsMonitor
from .notifications.outbox import Outbox
from .state import StateTracker
from .status import write_status

log = logging.getLogger('piwatch')


def process_maintenance(store, outbox):
    try:
        requests = store.pending_maintenance()
    except Exception as exc:
        log.error('Could not read maintenance requests: %s', type(exc).__name__)
        return
    for request in requests:
        try:
            result = outbox.clear_records(request['action'])
        except Exception as exc:
            try:
                store.finish_maintenance(request['id'], {'error': type(exc).__name__},
                                         failed=True)
            except Exception:
                # The idempotent request stays pending and will retry next cycle.
                pass
            log.error('Maintenance request failed: action=%s error=%s',
                      request['action'], type(exc).__name__)
            continue
        try:
            store.finish_maintenance(request['id'], result)
        except Exception as exc:
            # Clearing is idempotent; keep the request pending for the next cycle.
            log.error('Could not record maintenance completion: %s', type(exc).__name__)
            continue
        log.info('Maintenance completed: action=%s deliveries=%d incidents=%d actor=%s',
                 request['action'], result['deliveries'], result['incidents'],
                 request['actor'])


def reconcile_trackers(config, previous, trackers):
    definitions = {t['id']: t for t in config['targets'] if t['enabled']}
    definitions.update({name: {'name': name.upper() if name == 'dns' else 'Internet', **config[name]}
                        for name in ('internet', 'dns') if config[name]['enabled']})
    if config['battery']['enabled']:
        definitions['battery'] = {'name': 'UPS battery', **config['battery']}
    if config['battery']['mains_detection_enabled']:
        definitions['mains'] = {'name': 'Mains power', 'gpio': 17,
                                'debounce_seconds': config['battery']['mains_debounce_seconds']}
    old_definitions = {t['id']: t for t in previous.get('targets', [])}
    old_definitions.update({name: previous.get(name) for name in ('internet', 'dns')})
    old_definitions['battery'] = previous.get('battery')
    old_battery = previous.get('battery') or {}
    old_definitions['mains'] = ({'name': 'Mains power', 'gpio': 17,
                                'debounce_seconds': old_battery.get('mains_debounce_seconds')}
                               if old_battery.get('mains_detection_enabled') else None)
    updated = {}
    for name, definition in definitions.items():
        old = old_definitions.get(name)
        # Changing a host/probe starts new evidence; label/threshold edits keep state.
        comparable = lambda value: {k: v for k, v in (value or {}).items() if k != 'name'}
        if name in trackers and comparable(old) == comparable(definition):
            tracker = trackers[name]
        else:
            tracker = StateTracker(name, config['suspect_after_failures'], config['down_after_failures'], config['recover_after_successes'])
        tracker.suspect_after = 1 if name == 'mains' else config['suspect_after_failures']
        tracker.down_after = 1 if name == 'mains' else config['down_after_failures']
        tracker.recover_after = 1 if name == 'mains' else config['recover_after_successes']
        tracker.display = definition['name']
        tracker.definition = json.dumps(comparable(definition), sort_keys=True)
        updated[name] = tracker
    return updated


async def run(config=None):
    store = SettingsStore()
    revision = 0
    if config is None:
        revision, config = store.read() if store.path.exists() else (0, load_config())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    trackers = reconcile_trackers(config, {}, {})
    outbox = Outbox(is_running=lambda: not stop.is_set())
    outbox.restore(trackers)
    external = ExternalChecks(config)
    health = {}
    current = {'config': config, 'revision': revision}
    started = time.monotonic()
    system_sampler = SystemSampler()
    battery_monitor = BatteryMonitor(
        battery_low=(trackers.get('battery') is not None and
                     trackers['battery'].state in ('SUSPECT', 'DOWN')))
    mains_monitor = MainsMonitor(stable_state=(
        trackers['mains'].state == 'UP' if trackers.get('mains') and
        trackers['mains'].state in ('UP', 'DOWN') else None))
    workers = [asyncio.create_task(outbox.work(channel))
               for channel in ('ip', 'ha', 'smtp', 'lora')]
    workers.append(asyncio.create_task(health_worker(health, stop, current)))
    next_probe = 0
    results = {}
    local_results = {}
    battery_value = None
    config_error = None
    log.info('piwatchd starting; targets=%s', ','.join(trackers))
    try:
        while not stop.is_set():
            cycle = time.monotonic()
            if store.path.exists():
                try:
                    new_revision, new_config = store.read()
                    if new_revision != revision:
                        trackers = reconcile_trackers(new_config, config, trackers)
                        config, revision = new_config, new_revision
                        current.update(config=config, revision=revision)
                        external = ExternalChecks(config)
                        battery_monitor = BatteryMonitor(
                            battery_low=(trackers.get('battery') is not None and
                                         trackers['battery'].state in ('SUSPECT', 'DOWN')))
                        mains_monitor = MainsMonitor(stable_state=(
                            trackers['mains'].state == 'UP' if trackers.get('mains') and
                            trackers['mains'].state in ('UP', 'DOWN') else None))
                        next_probe = 0
                        results = {}
                        local_results = {}
                        log.info('Applied settings revision %d', revision)
                    config_error = None
                except Exception as exc:
                    # Keep the last validated config. Never log supplied values/credentials.
                    config_error = type(exc).__name__
            sample_local = time.monotonic() >= next_probe
            async def local_sample():
                nonlocal battery_value
                if sample_local:
                    values = await check_all(config)
                    if (config['battery']['enabled'] or
                            config['battery']['mains_detection_enabled']):
                        battery_value = await asyncio.to_thread(
                            battery_monitor.sample, config['battery'])
                    if config['battery']['enabled']:
                        values['battery'] = battery_value
                    return {name: {'checked_at': time.time(), 'sampled': True, **value}
                            for name, value in values.items()}
                return {name: {**value, 'sampled': False} for name, value in local_results.items()}
            local_results, remote = await asyncio.gather(local_sample(), external.collect())
            if config['battery']['mains_detection_enabled']:
                voltage = battery_value.get('voltage_v') if battery_value else None
                mains_value = await asyncio.to_thread(
                    mains_monitor.sample, config['battery'], voltage)
                local_results['mains'] = {
                    'checked_at': time.time(), 'sampled': True, **mains_value}
            if sample_local:
                next_probe = cycle + config['interval_seconds']
            results = {**local_results, **remote}
            transitions = []
            for name, tracker in trackers.items():
                if results[name].get('sampled', True):
                    transition = tracker.update(results[name]['healthy'])
                    if transition:
                        transitions.append((name, transition))
            if transitions:
                incidents = [(name, new, results) for name, (old, new) in transitions
                             if new == 'DOWN' or (old == 'DOWN' and new == 'UP')]
                outbox.record(trackers, incidents, config['gateways'])
            for worker in workers:
                if worker.done():
                    worker.result()
                    if not stop.is_set():
                        raise RuntimeError('background worker exited unexpectedly')
            try:
                metrics = system_sampler.sample()
            except (OSError, ValueError, IndexError):
                metrics = {'checked_at': time.time(), 'state': 'UNKNOWN'}
            process_maintenance(store, outbox)
            outbox.maintain()
            write_status(results, trackers, outbox, health, config, started,
                         revision=revision, config_error=config_error, metrics=metrics)
            try:
                await asyncio.wait_for(stop.wait(), max(.05, 1 - (time.monotonic() - cycle)))
            except asyncio.TimeoutError:
                pass
    finally:
        stop.set()
        await asyncio.gather(*workers, return_exceptions=True)
        outbox.db.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        log.info('piwatchd stopped')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(run())
