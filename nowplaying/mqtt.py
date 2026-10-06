"""Home Assistant, through MQTT: what's playing, published to a broker.

A small MQTT 3.1.1 client, standard library only: the status goes to
<MQTT_TOPIC>/state (JSON) and the artwork's address to <MQTT_TOPIC>/artwork,
both kept by the broker (retained); <MQTT_TOPIC>/availability says "online",
and the broker says "offline" for the bridge when it goes away (its will).
Home Assistant finds the entities by itself (MQTT discovery): title,
artist, album, source, quality, playing, and the artwork as an image.
"""
import ipaddress
import json
import re
import select
import socket
import ssl
import struct
import threading
import time

from . import VERSION
from .audio import describe_badges

CONNECT, CONNACK, PUBLISH, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 12, 13, 14
KEEPALIVE = 60          # seconds: the broker drops a client silent for 1.5 times that
REFUSED = {1: 'it does not speak MQTT 3.1.1', 2: 'it refused the client name', 3: 'it is unavailable',
           4: 'wrong user name or password', 5: 'not authorized (user name or password?)'}


class MqttError(Exception):
    pass


def _string(text):
    data = text.encode()
    return struct.pack('!H', len(data)) + data


def _length(n):
    # MQTT's "remaining length": 7 bits a byte, the high bit for "more"
    out = b''
    while True:
        n, digit = divmod(n, 128)
        out += bytes([digit | (0x80 if n else 0)])
        if not n:
            return out


def packet(kind, body=b'', flags=0):
    return bytes([kind << 4 | flags]) + _length(len(body)) + body


def connect_packet(client_id, user='', password='', will=None, keepalive=KEEPALIVE):
    # CONNECT, with a clean session; will: (topic, message), retained
    flags = 0x02
    payload = _string(client_id)
    if will:
        flags |= 0x04 | 0x20
        payload += _string(will[0]) + _string(will[1])
    if user:
        flags |= 0x80
        payload += _string(user)
        if password:
            flags |= 0x40
            payload += _string(password)
    return packet(CONNECT, _string('MQTT') + bytes([4, flags]) + struct.pack('!H', keepalive) + payload)


def publish_packet(topic, message, retain=True):
    data = message if isinstance(message, bytes) else message.encode()
    return packet(PUBLISH, _string(topic) + data, flags=1 if retain else 0)  # QoS 0


def read_packet(sock):
    # (kind, body) of the next packet from the broker
    def read(n):
        data = b''
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise MqttError('the broker closed the connection')
            data += chunk
        return data
    kind = read(1)[0] >> 4
    length, shift = 0, 0
    while True:
        digit = read(1)[0]
        length += (digit & 0x7F) << shift
        shift += 7
        if not digit & 0x80:
            break
    return kind, read(length) if length else b''


def open_connection(host, port, use_tls, client_id, user='', password='', will=None, timeout=10):
    # A connection the broker accepted, or MqttError / OSError
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        if use_tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.sendall(connect_packet(client_id, user, password, will))
        kind, body = read_packet(sock)
        if kind != CONNACK or len(body) != 2:
            raise MqttError('unexpected answer from the broker')
        if body[1]:
            raise MqttError(f'the broker said no: {REFUSED.get(body[1], f"code {body[1]}")}')
    except BaseException:
        sock.close()
        raise
    return sock


def node_id(topic):
    return re.sub(r'[^a-zA-Z0-9_-]+', '_', topic).strip('_') or 'nowplaying'


def base_address(sock, scheme, port):
    # The bridge's address, as Home Assistant can reach it: the one this
    # machine uses to talk to the broker (the network's, when the broker
    # runs here too)
    address = sock.getsockname()[0]
    if ipaddress.ip_address(address).is_loopback:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(('192.0.2.1', 9))  # no packet sent: only picks the route
                address = probe.getsockname()[0]
        except OSError:
            pass
    host = f'[{address}]' if ':' in address else address
    return f'{scheme}://{host}:{port}'


def state_payload(status, base):
    # What Home Assistant gets: the status, with absolute addresses and
    # the badges the page shows
    art = status.get('art_url', '')
    if art.startswith('/'):
        art = base + art
    badges = describe_badges(status.get('format', ''), status.get('codec', ''), status.get('lossless', False)) \
        if status.get('state') in ('play', 'pause') else []
    return {
        'state': 'error' if status.get('error') else status.get('state', 'stop'),
        'error': status.get('error', ''),
        'title': status.get('title', ''),
        'artist': status.get('artist', '') if status.get('artist') != '—' else '',
        'album': status.get('album', ''),
        'source': status.get('source', ''),
        'sender': status.get('sender', ''),
        'station': status.get('station', ''),
        'codec': status.get('codec', ''),
        'format': status.get('format', ''),
        'badges': badges,
        'quality': ' · '.join(badges),
        'hires': 'Hi-Res' in badges,
        'duration': round(status.get('duration') or 0, 1),
        'elapsed': round(status.get('elapsed') or 0, 1),
        'art_url': art,
        'next_title': status.get('next_title', ''),
        'next_artist': status.get('next_artist', ''),
    }


def discovery(topic, prefix, base):
    # Home Assistant's MQTT discovery: [(config topic, config)]
    node = node_id(topic)
    device = {'identifiers': [node], 'name': 'Now Playing' if node == 'nowplaying' else f'Now Playing {node}',
              'manufacturer': 'nowplaying', 'model': 'mpd bridge', 'sw_version': VERSION,
              'configuration_url': base + '/settings'}
    common = {'availability_topic': f'{topic}/availability', 'device': device}
    state = f'{topic}/state'
    sensors = [('title', 'Title', 'mdi:music'), ('artist', 'Artist', 'mdi:account-music'),
               ('album', 'Album', 'mdi:album'), ('source', 'Source', 'mdi:speaker'),
               ('quality', 'Quality', 'mdi:quality-high'), ('state', 'State', 'mdi:play-pause')]
    configs = []
    for key, name, icon in sensors:
        config = dict(common, name=name, unique_id=f'{node}_{key}', icon=icon, state_topic=state,
                      value_template=f'{{{{ value_json.{key} }}}}')
        if key == 'title':
            config['json_attributes_topic'] = state  # everything, for templates and cards
        configs.append((f'{prefix}/sensor/{node}/{key}/config', config))
    configs.append((f'{prefix}/binary_sensor/{node}/playing/config', dict(
        common, name='Playing', unique_id=f'{node}_playing', icon='mdi:play-circle', state_topic=state,
        value_template="{{ 'ON' if value_json.state == 'play' else 'OFF' }}")))
    configs.append((f'{prefix}/image/{node}/artwork/config', dict(
        common, name='Artwork', unique_id=f'{node}_artwork', url_topic=f'{topic}/artwork')))
    return configs


class Client:
    # Keeps the broker up to date with the status, for good: reconnects
    # after a lost connection, waiting longer each time (up to a minute)
    def __init__(self, host, port, use_tls, user, password, topic, prefix, public_url, scheme, bridge_port,
                 current, log, client_id=None):
        self.host, self.port, self.use_tls = host, port, use_tls
        self.user, self.password = user, password
        self.topic, self.prefix = topic.strip('/'), prefix.strip('/')
        self.public_url, self.scheme, self.bridge_port = public_url.rstrip('/'), scheme, bridge_port
        self.current = current  # () -> the status now, when connecting
        self.log = log
        self.client_id = client_id or f'nowplaying-{socket.gethostname()}'[:23]
        self.lock = threading.Lock()
        self.latest = None      # the status to publish
        self.wake = threading.Event()

    def publish_status(self, status):
        # A new status, from the bridge: sent from the client's own thread
        with self.lock:
            self.latest = status
        self.wake.set()

    def run(self):
        delay, last_problem = 5, None
        while True:
            try:
                sock = open_connection(self.host, self.port, self.use_tls, self.client_id, self.user, self.password,
                                       will=(f'{self.topic}/availability', 'offline'))
            except (OSError, MqttError) as e:
                if str(e) != last_problem:  # once per problem
                    self.log.warning('MQTT: cannot connect to %s:%s: %s', self.host, self.port, e)
                    last_problem = str(e)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            self.log.info('MQTT: connected to %s:%s, publishing to %s/', self.host, self.port, self.topic)
            delay, last_problem = 5, None
            try:
                self.serve(sock)
            except (OSError, MqttError) as e:
                self.log.warning('MQTT: connection to %s:%s lost: %s', self.host, self.port, e)
            except Exception:
                self.log.exception('MQTT: failed, connecting again')
            finally:
                sock.close()
            time.sleep(delay)

    def serve(self, sock):
        sock.settimeout(10)
        base = self.public_url or base_address(sock, self.scheme, self.bridge_port)
        sock.sendall(publish_packet(f'{self.topic}/availability', 'online'))
        if self.prefix:
            for config_topic, config in discovery(self.topic, self.prefix, base):
                sock.sendall(publish_packet(config_topic, json.dumps(config)))
        with self.lock:
            if self.latest is None:
                self.latest = self.current()
        self.wake.set()
        sent = artwork = None
        last_ping = last_heard = time.monotonic()
        while True:
            if self.wake.wait(1):
                self.wake.clear()
                with self.lock:
                    status = self.latest
                state = state_payload(status, base)
                payload = json.dumps(state)
                if payload != sent:
                    sock.sendall(publish_packet(f'{self.topic}/state', payload))
                    sent = payload
                art = state['art_url'] or base + '/icon-512.png'  # the app's icon when there's none
                if art != artwork:
                    sock.sendall(publish_packet(f'{self.topic}/artwork', art))
                    artwork = art
            now = time.monotonic()
            while (isinstance(sock, ssl.SSLSocket) and sock.pending()) or select.select([sock], [], [], 0)[0]:
                read_packet(sock)  # PINGRESP (nothing else comes: no subscription)
                last_heard = now
            # A ping every 30 s: keeps the connection up, and finds out when
            # the broker is gone without a word
            if now - last_ping >= KEEPALIVE / 2:
                sock.sendall(packet(PINGREQ))
                last_ping = now
            if now - last_heard > KEEPALIVE * 1.5:
                raise MqttError('the broker stopped answering')


def check(host, port, use_tls, user, password, client_id):
    # For --check: None when the broker accepts the bridge, else why not
    try:
        sock = open_connection(host, port, use_tls, client_id, user, password, timeout=5)
    except (OSError, MqttError) as e:
        return str(e) or e.__class__.__name__
    try:
        sock.sendall(packet(DISCONNECT))
    finally:
        sock.close()
    return None
