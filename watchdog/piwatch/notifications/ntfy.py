"""Private ntfy transport with verified TLS and a single total I/O deadline."""
import base64
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

CA_PATH = '/etc/piwatch-ntfy-native/server.crt'
ACTIVE_PATH = '/etc/piwatch-ntfy-native/active'


class NtfyError(Exception):
    """Only fixed, nonsecret error codes may cross the transport boundary."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def transient_error(exc):
    if not isinstance(exc, NtfyError):
        return False
    return exc.code in ('Timeout', 'TransportError', 'HTTP429') or (
        exc.code.startswith('HTTP5') and len(exc.code) == 7)


def activated(path=None):
    return Path(path or os.environ.get('PIWATCH_NTFY_ACTIVE_PATH', ACTIVE_PATH)).is_file()


class DeadlineReader(io.RawIOBase):
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


class ResponseSocket:
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def makefile(self, mode):
        return io.BufferedReader(DeadlineReader(self.connection, self.deadline))


class NtfyClient:
    def __init__(self, settings):
        self.settings = dict(settings)
        self.context = ssl.create_default_context(
            cafile=CA_PATH if settings.get('tls_trust') == 'private_ca' else None)

    def publish(self, topic, message, title=None, priority=None, timeout=3.0):
        settings = self.settings
        url = urlsplit(settings.get('url', ''))
        try:
            ipaddress.ip_address(url.hostname)
        except (ValueError, TypeError):
            raise NtfyError('NotConfigured') from None
        if url.scheme != 'https' or url.path not in ('', '/') or url.username or url.password or url.query or url.fragment:
            raise NtfyError('NotConfigured')
        if not settings.get('username') or not settings.get('password'):
            raise NtfyError('NotConfigured')
        payload = {'topic': topic, 'message': message}
        if title is not None:
            payload['title'] = title
        if priority is not None:
            payload['priority'] = priority
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        authorization = base64.b64encode(
            (settings['username'] + ':' + settings['password']).encode()).decode('ascii')
        host = url.hostname
        port = url.port or 443
        authority = ('[' + host + ']' if ':' in host else host) + ':' + str(port)
        request = (f'POST / HTTP/1.1\r\nHost: {authority}\r\n'
                   f'Authorization: Basic {authorization}\r\nContent-Type: application/json\r\n'
                   f'Content-Length: {len(data)}\r\nConnection: close\r\n\r\n').encode('ascii') + data
        deadline = time.monotonic() + timeout
        connection = None
        try:
            # Literal IPs avoid unbounded DNS. Direct sockets bypass ambient proxies.
            connection = socket.create_connection((host, port), timeout=timeout)
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection = self.context.wrap_socket(connection, server_hostname=host)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            connection.settimeout(remaining)
            connection.sendall(request)
            response = http.client.HTTPResponse(ResponseSocket(connection, deadline))
            response.begin()
            if response.status != 200:
                # Redirects are rejected, never followed with credentials.
                raise NtfyError('HTTP' + str(response.status))
            body = response.read(65537)
            if len(body) > 65536:
                raise NtfyError('InvalidResponse')
            try:
                result = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                raise NtfyError('InvalidResponse') from None
            if not isinstance(result, dict) or not isinstance(result.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', result['id']):
                raise NtfyError('InvalidResponse')
            if time.monotonic() > deadline:
                raise TimeoutError()
            return {'id': result['id'], 'time': result.get('time')}
        except NtfyError:
            raise
        except ssl.SSLCertVerificationError:
            raise NtfyError('TLSVerificationFailed') from None
        except (TimeoutError, socket.timeout):
            raise NtfyError('Timeout') from None
        except (OSError, ValueError, http.client.HTTPException):
            raise NtfyError('TransportError') from None
        finally:
            if connection is not None:
                connection.close()


def send_ntfy(title, message, severity, incident_id, settings):
    client = NtfyClient(settings)
    return client.publish(settings['topic'], message + '\nIncident: ' + incident_id,
                          title=title, priority=5 if severity == 'critical' else 3)
