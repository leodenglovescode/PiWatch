"""Bounded network probes; no notification or hardware side effects."""
import asyncio
import logging

log = logging.getLogger("piwatch")

async def ping(host):
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping",
            "-n",
            "-c", "1",
            "-W", "1",
            "--", host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            await asyncio.wait_for(
                proc.wait(),
                timeout=1.5,
            )

        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False

        return proc.returncode == 0

    except Exception as exc:
        log.error(
            "ping(%s) exception: %s",
            host,
            exc,
        )
        return False


async def tcp_check(host, port, timeout=1.0):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
            ),
            timeout=timeout,
        )

        writer.close()

        try:
            await writer.wait_closed()
        except Exception:
            pass

        return True

    except Exception:
        return False


async def http_check(
    host,
    port,
    path="/",
    timeout=1.5,
):
    writer = None

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
            ),
            timeout=timeout,
        )

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "User-Agent: piwatchd/1.4\r\n"
            "Connection: close\r\n"
            "\r\n"
        )

        writer.write(
            request.encode("ascii")
        )

        await writer.drain()

        first_line = await asyncio.wait_for(
            reader.readline(),
            timeout=timeout,
        )

        try:
            parts = first_line.decode(
                "ascii",
                errors="replace",
            ).split()

            status = int(parts[1])

            return 200 <= status < 400

        except (IndexError, ValueError):
            return False

    except Exception:
        return False

    finally:
        if writer is not None:
            writer.close()

            try:
                await writer.wait_closed()
            except Exception:
                pass


async def check_target(target):
    tasks, names = [], []
    for check in target['checks']:
        kind = check['type']
        names.append('ssh' if kind == 'tcp' and check.get('label') == 'SSH' else kind)
        if kind == 'ping':
            tasks.append(ping(target['host']))
        elif kind == 'tcp':
            tasks.append(tcp_check(target['host'], check['port']))
        else:
            tasks.append(http_check(target['host'], check['port'], check['path']))
    values = await asyncio.gather(*tasks)
    return {'healthy': all(values), **dict(zip(names, values))}


async def check_all(config):
    targets = [target for target in config['targets'] if target['enabled']]
    values = await asyncio.gather(*(check_target(target) for target in targets))
    return dict(zip((target['id'] for target in targets), values))


async def dns_check(name, timeout=2.0):
    """Exercise the Pi's system resolver in a cancellable child process.

    getent includes local hosts/cache/NSS configuration. This tests whether the
    Pi can resolve names, not whether a specific authoritative DNS server is up.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            'getent', 'ahostsv4', name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode == 0 and bool(output.strip())
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


class ExternalChecks:
    """Cache slow probes but mark only newly collected results as samples."""
    def __init__(self, config):
        self.config = config
        self.next_check = 0
        self.results = {}

    async def collect(self):
        import time
        if time.monotonic() < self.next_check:
            return {key: {**value, 'sampled': False} for key, value in self.results.items()}
        work, names = [], []
        if self.config['internet']['enabled']:
            names.append('internet')
            work.append(self.internet())
        if self.config['dns']['enabled']:
            names.append('dns')
            work.append(self.dns())
        values = await asyncio.gather(*work)
        self.results = dict(zip(names, values))
        self.next_check = time.monotonic() + self.config['external_interval_seconds']
        return {key: {**value, 'sampled': True} for key, value in self.results.items()}

    async def internet(self):
        import time
        targets = self.config['internet']['targets']
        results = await asyncio.gather(*(tcp_check(t['host'], t['port'], timeout=2) for t in targets))
        return {'healthy': any(results), 'tcp': any(results), 'checked_at': time.time(),
                'endpoints': [{'target': f"{t['host']}:{t['port']}", 'ok': ok}
                              for t, ok in zip(targets, results)]}

    async def dns(self):
        import time
        names = self.config['dns']['names']
        results = await asyncio.gather(*(dns_check(name) for name in names))
        return {'healthy': any(results), 'resolve': any(results), 'checked_at': time.time(),
                'endpoints': [{'target': name, 'ok': ok} for name, ok in zip(names, results)]}
