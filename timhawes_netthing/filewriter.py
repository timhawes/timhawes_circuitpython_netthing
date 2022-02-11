# SPDX-FileCopyrightText: 2022 Tim Hawes
#
# SPDX-License-Identifier: MIT

import os

try:
    import adafruit_hashlib as hashlib
except ImportError:
    import hashlib


def send_file_info(stream, filename, root="/"):
    reply = {
        "cmd": "file_info",
        "filename": filename,
    }
    path = root + filename
    try:
        if os.stat(path):
            h = hashlib.md5()
            with open(path, "rb") as f:
                for chunk in f:
                    h.update(chunk)
                reply["size"] = f.tell()
            reply["md5"] = h.hexdigest()
        else:
            reply["md5"] = None
            reply["size"] = None
    except FileNotFoundError:
        reply["md5"] = None
        reply["size"] = None
    stream.send(reply)


class FileWriter:
    def __init__(self, filename, size, md5, root="/"):
        self.filename = root + filename
        self.tmp_filename = self.filename + ".new"
        self.size = size
        self.md5 = md5
        self.handle = None
        self.hash = None
        self.position = 0

    def open(self):
        self.handle = open(self.tmp_filename, "wb")
        self.hash = hashlib.md5()
        self.position = 0

    def write(self, data):
        if self.handle is None:
            self.open()
        self.handle.write(data)
        self.hash.update(data)
        self.position += len(data)

    def abort(self):
        if self.handle:
            self.handle.close()
            self.handle = None
            os.unlink(self.tmp_filename)

    def commit(self):
        if self.size != self.position:
            print("NetThing: bad file size")
            self.abort()
            return
        if self.hash.hexdigest() != self.md5:
            print("NetThing: bad md5")
            self.abort()
            return
        self.handle.close()
        self.handle = None
        os.rename(self.tmp_filename, self.filename)
