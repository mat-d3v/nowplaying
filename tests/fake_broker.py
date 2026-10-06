"""A fake MQTT broker, for the bridge's Home Assistant tests.

Takes MQTT 3.1.1 clients, keeps what they publish (the retained messages
like a real broker), answers their pings, and publishes a client's will
when it goes away without saying goodbye. refuse=N answers CONNECT with
that return code instead of accepting it.
"""
import socketserver
import struct
import threading
import time


def _string(data, at):
    size = struct.unpack('!H', data[at:at + 2])[0]
    return data[at + 2:at + 2 + size].decode(), at + 2 + size


class _Handler(socketserver.BaseRequestHandler):
    def read(self, n):
        data = b''
        while len(data) < n:
            chunk = self.request.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def read_packet(self):
        first = self.read(1)[0]
        length, shift = 0, 0
        while True:
            digit = self.read(1)[0]
            length += (digit & 0x7F) << shift
            shift += 7
            if not digit & 0x80:
                break
        return first >> 4, first & 0x0F, self.read(length) if length else b''

    def handle(self):
        broker, will, goodbye = self.server, None, False
        try:
            kind, _, body = self.read_packet()
            if kind != 1:
                return
            client = self.connect(body)
            will = client['will']
            with broker.changed:
                broker.clients.append(client)
                broker.changed.notify_all()
            self.request.sendall(bytes([0x20, 2, 0, broker.refuse]))
            if broker.refuse:
                will = None
                return
            while True:
                kind, flags, body = self.read_packet()
                if kind == 3:     # PUBLISH, QoS 0
                    topic, at = _string(body, 0)
                    broker.publish(topic, body[at:].decode(), bool(flags & 1))
                elif kind == 12:  # PINGREQ
                    self.request.sendall(bytes([0xD0, 0]))
                elif kind == 14:  # DISCONNECT
                    goodbye = True
                    return
        except (EOFError, OSError):
            pass
        finally:
            if will and not goodbye:
                broker.publish(*will, retain=True)

    def connect(self, body):
        name, at = _string(body, 0)
        level, flags = body[at], body[at + 1]
        keepalive = struct.unpack('!H', body[at + 2:at + 4])[0]
        client_id, at = _string(body, at + 4)
        will = user = password = None
        if flags & 0x04:
            topic, at = _string(body, at)
            message, at = _string(body, at)
            will = (topic, message)
        if flags & 0x80:
            user, at = _string(body, at)
        if flags & 0x40:
            password, at = _string(body, at)
        return dict(protocol=(name, level), clean=bool(flags & 0x02), will_retain=bool(flags & 0x20),
                    keepalive=keepalive, client_id=client_id, will=will, user=user, password=password)


class FakeBroker(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, refuse=0):
        super().__init__(('127.0.0.1', 0), _Handler)
        self.refuse = refuse
        self.clients = []     # each CONNECT: client_id, user, password, will...
        self.published = []   # (topic, message, retained), in order
        self.retained = {}    # topic -> message
        self.changed = threading.Condition()

    @property
    def port(self):
        return self.server_address[1]

    def publish(self, topic, message, retain):
        with self.changed:
            self.published.append((topic, message, retain))
            if retain:
                self.retained[topic] = message
            self.changed.notify_all()

    def wait_for(self, check, timeout=10):
        # check() true, or AssertionError once the time is out
        deadline = time.time() + timeout
        with self.changed:
            while not check():
                left = deadline - time.time()
                if left <= 0:
                    raise AssertionError(f'timed out; retained: {sorted(self.retained)}')
                self.changed.wait(left)

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self
