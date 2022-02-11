# SPDX-FileCopyrightText: 2022 Tim Hawes
#
# SPDX-License-Identifier: MIT

from binascii import a2b_base64, hexlify
import espidf
import gc
import json
import microcontroller
import os
import rtc
import ssl
import supervisor
import time


from .filewriter import FileWriter, send_file_info
from .manager import JsonPacketManager


class NetThing(JsonPacketManager):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clientid = None
        self.password = None
        self.root = "/"
        self.enable_file_management = True
        self.enable_firmware_management = False
        self.enable_rtc_update = True
        self.filewriter = None
        self.connected_callback = None
        self.disconnected_callback = None
        self.last_receive = None
        self.receive_timeout = 65
        self.last_send = None

        self._count_sent = 0
        self._count_received = 0
        self._count_send_errors = 0
        self._count_connect = 0

    def reload(self, config_file):
        """Load configuration from JSON file and reconnect."""
        try:
            with open(config_file, "rb") as file:
                config = json.load(file)
                for k in ["clientid", "password", "host", "port"]:
                    if k in config:
                        setattr(self, k, config[k])
                if config.get("tls") is True:
                    if config.get("ca"):
                        self.ssl_context = ssl.SSLContext()
                        self.ssl_context.load_verify_locations(cadata=config["ca"])
                        self.ssl_context.check_hostname = False
                    else:
                        self.ssl_context = ssl.create_default_context()
                else:
                    self.ssl_context = None
            if not self.paused:
                self.reconnect()
        except OSError:
            print(f"NetThing: {config_file} not found")

    def connect_hook(self):
        self._count_connect += 1
        self.send(
            {"cmd": "hello", "clientid": self.clientid, "password": self.password}
        )
        if callable(self.connected_callback):
            self.connected_callback()

    def disconnect_hook(self):
        if callable(self.disconnected_callback):
            self.disconnected_callback()

    def send_null(self):
        self.send_packet_null()
        self.last_send = time.monotonic()

    def send(self, data):
        status = self.send_json(data)
        self.last_send = time.monotonic()
        if status:
            self._count_sent += 1
        else:
            self._count_send_errors += 1
        return status

    def cmd_ping(self, data):
        received_time = time.time()
        data["cmd"] = "pong"
        self.send(data)
        if "timestamp" in data:
            # don't use float() because we lose too much precision
            sent_time = int(data["timestamp"].split(".")[0])
            print(f"NetThing: ping received with {received_time-sent_time}s delay")

    def cmd_pong(self, data):
        if "millis" in data:
            rtt = (time.monotonic_ns() // 1000000) - data["millis"]
            print(f"NetThing: received ping response with rtt {rtt}ms")

    def cmd_file_query(self, data):
        if not self.enable_file_management:
            return
        send_file_info(self, data["filename"], root=self.root)

    def cmd_file_write(self, data):
        if not self.enable_file_management:
            return
        self.filewriter = FileWriter(
            data["filename"], size=data["size"], md5=data["md5"], root=self.root
        )
        self.send(
            {
                "cmd": "file_continue",
                "filename": data["filename"],
                "position": 0,
            }
        )

    def cmd_file_data(self, data):
        if not self.enable_file_management:
            return
        payload = a2b_base64(data["data"].encode())
        try:
            self.filewriter.write(payload)
            if data.get("eof"):
                self.filewriter.commit()
                self.send({"cmd": "file_write_ok", "filename": data["filename"]})
            else:
                self.send(
                    {
                        "cmd": "file_continue",
                        "filename": data["filename"],
                        "position": data["position"] + len(payload),
                    }
                )
        except OSError as exc:
            self.send(
                {
                    "cmd": "file_write_error",
                    "filename": data["filename"],
                    "error": str(exc),
                }
            )

    def cmd_time(self, data):
        if self.enable_rtc_update:
            rtc.RTC().datetime = time.localtime(int(data["time"]))

    def cmd_net_metrics_query(self, data):
        reply = {
            "cmd": "net_metrics_info",
            "millis": time.monotonic_ns() // 1000000,
            "time": time.time(),
            "espidf_heap_caps_total_size": espidf.heap_caps_get_total_size(),
            "espidf_heap_caps_free_size": espidf.heap_caps_get_free_size(),
            "espidf_heap_caps_largest_free_block": espidf.heap_caps_get_largest_free_block(),
            "gc_mem_alloc": gc.mem_alloc(),
            "gc_mem_free": gc.mem_free(),
            "net_tcp_reconns": self._count_connect,
            "cpu_temperature": microcontroller.cpu.temperature,
            "supervisor_runtime_usb_connected": supervisor.runtime.usb_connected,
            "supervisor_runtime_serial_connected": supervisor.runtime.serial_connected,
            # "net_wifi_reconns": wifi_reconnections,
            # "net_wifi_rssi": WiFi.RSSI(),
        }
        self.send(reply)

    def cmd_system_query(self, data):
        reply = {
            "cmd": "system_info",
            "millis": time.monotonic_ns() // 1000000,
            "time": time.time(),
            "microcontroller_cpu_uid": hexlify(microcontroller.cpu.uid).decode("ascii"),
            "microcontroller_cpu_reset_reason": str(microcontroller.cpu.reset_reason),
            "os_uname_machine": os.uname().machine,
            "os_uname_nodename": os.uname().nodename,
            "os_uname_release": os.uname().release,
            "os_uname_sysname": os.uname().sysname,
            "os_uname_version": os.uname().version,
            "supervisor_runtime_run_reason": str(supervisor.runtime.run_reason),
            "supervisor_get_previous_traceback": supervisor.get_previous_traceback(),
        }
        self.send(reply)

    def receive(self):
        for data in self.receive_json():
            self.last_receive = time.monotonic()
            self._count_received += 1
            if "cmd" in data:
                if hasattr(self, "cmd_" + data["cmd"]):
                    method = getattr(self, "cmd_" + data["cmd"])
                    if callable(method):
                        method(data)
                        continue
            yield data
        if self.connected and self.last_receive is not None:
            if time.monotonic() - self.last_receive > self.receive_timeout:
                print(
                    f"NetThing: no messages received for >{self.receive_timeout} seconds, reconnecting"
                )
                self.last_receive = None
                self.reconnect()
