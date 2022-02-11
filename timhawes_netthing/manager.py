# SPDX-FileCopyrightText: 2022 Tim Hawes
#
# SPDX-License-Identifier: MIT

import json
import time


class SocketManager:
    def __init__(self, socketpool):
        self.socketpool = socketpool

        # configuration
        self._host = None
        self._port = None
        self._sslcontext = None
        self.reconnect_interval = 10
        self.debug_connection = False
        self.debug_raw = False

        # state
        self.sock = None
        self.connected = False
        self.paused = True
        self.last_connect_attempt = time.monotonic() - self.reconnect_interval

        # workspace
        self.buffer = bytearray(1500)

    @property
    def host(self):
        return self._host

    @host.setter
    def host(self, value):
        self._host = value

    @property
    def port(self):
        return self._port

    @port.setter
    def port(self, value):
        self._port = int(value)

    @property
    def ssl_context(self):
        return self._ssl_context

    @ssl_context.setter
    def ssl_context(self, value):
        self._ssl_context = value

    def reconnect(self):
        """Drop the current connection and reconnect."""
        if self.connected:
            self.connected = False
            self.sock.close()
            if callable(self.disconnect_hook):
                self.disconnect_hook()
        if not self.paused:
            self.retry()

    def retry(self):
        """If not already connected, attempt to connect now."""
        self.paused = False
        self.last_connect_attempt = time.monotonic() - self.reconnect_interval
        self._try_connect()

    def pause(self):
        """Stop attempting to connect. Call retry() to resume."""
        self.paused = True

    def _try_connect(self):
        if self.connected:
            return
        if self.paused:
            return
        if not (self._host and self._port):
            return
        sock = self.socketpool.socket()
        try:
            if self._ssl_context:
                sock = self._ssl_context.wrap_socket(sock, server_hostname=self._host)
            if self.debug_connection:
                print(f"NetThing: connecting to {self._host}:{self._port}")
            sock.connect((self._host, self._port))
            sock.setblocking(False)
            self.sock = sock
            self.connected = True
            if self.debug_connection:
                print("NetThing: connected")
            self.connect_hook()
        except OSError as exc:
            print("NetThing: connect failed:", exc)
            sock.close()

    def connect_hook(self):
        """Called immediately after a connection is made."""

    def disconnect_hook(self):
        """Called immediately after a disconnection."""

    def loop(self):
        if self.connected:
            pass
        else:
            if time.monotonic() - self.last_connect_attempt > self.reconnect_interval:
                self.last_connect_attempt = time.monotonic()
                self._try_connect()

    def send_raw(self, data):
        self.loop()
        if self.connected:
            try:
                sent = self.sock.send(data)
                if self.debug_raw:
                    print("NetThing: send", data[:sent])
                if sent != len(data):
                    raise RuntimeError("Send truncated")
                return sent
            except Exception as e:
                print("NetThing: Exception during send", e)
                self.connected = False
                self.sock.close()
                self.disconnect_hook()
                return 0
        else:
            return 0

    def receive_raw(self):
        self.loop()
        if self.connected:
            try:
                while True:
                    received = self.sock.recv_into(self.buffer)
                    if received == 0:
                        print("NetThing: disconnected (eof)")
                        self.connected = False
                        self.sock.close()
                        self.disconnect_hook()
                        return
                    if self.debug_raw:
                        print("NetThing: recv", self.buffer[0:received])
                    yield self.buffer[0:received]
            except OSError as e:
                if e.errno == 11:
                    # EAGAIN
                    return
                print(f"NetThing: disconnected ({e})")
                self.connected = False
                self.sock.close()
                self.disconnect_hook()


class PacketManager(SocketManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.length_bytes = 2
        self.debug_packet = False

        # workspace
        self.current = b""
        self._send_queue = []
        self._in_receive = False

    def send_packet_null(self):
        packet = b"\x00" * self.length_bytes
        sent = self.send_raw(packet)
        if sent == len(packet):
            return True
        else:
            print("NetThing: Only sent {} out of {} bytes".format(sent, len(packet)))
            return False

    def send_packet(self, data):
        data_length = len(data)
        if self.length_bytes == 1:
            if data_length > 255:
                raise ValueError("Maximum packet size is 255")
            packet = bytearray(self.length_bytes + data_length)
            packet[0] = data_length
            packet[1:] = data[0:]
        else:
            if data_length > 65535:
                raise ValueError("Maximum packet size is 65535")
            packet = bytearray(self.length_bytes + data_length)
            packet[0] = data_length >> 8
            packet[1] = data_length & 255
            packet[2:] = data[0:]
        if self.debug_packet:
            print("NetThing: send", data)
        sent = self.send_raw(packet)
        if sent == len(packet):
            return True
        else:
            print("NetThing: Only sent {} out of {} bytes".format(sent, len(packet)))
            return False

    def receive_packet(self):
        self._in_receive = True
        packets = []
        for received in self.receive_raw():
            self.current += received
            while len(self.current) >= 2:
                length = int.from_bytes(self.current[0:2], "big")
                if len(self.current) >= length + 2:
                    packet = self.current[2 : 2 + length]
                    self.current = self.current[2 + length :]
                    if self.debug_packet:
                        print(f"NetThing: recv", packet)
                    packets.append(packet)
                else:
                    break
        self._in_receive = False
        yield from packets


class JsonPacketManager(PacketManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.debug_json = False

    def send_json(self, data):
        if self.debug_json:
            print("NetThing: send", data)
        return self.send_packet(json.dumps(data).encode("utf-8"))

    def receive_json(self):
        for data in self.receive_packet():
            decoded = json.loads(data)
            if self.debug_json:
                print("NetThing: recv", decoded)
            yield decoded
