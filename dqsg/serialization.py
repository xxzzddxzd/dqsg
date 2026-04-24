import struct


class BytesWriter:
    def __init__(self):
        self.buf = bytearray()

    def write_bool(self, v: bool):
        self.buf.append(1 if v else 0)

    def write_int(self, v: int):
        self.buf.extend(struct.pack("<i", v))

    def write_long(self, v: int):
        self.buf.extend(struct.pack("<q", v))

    def write_string(self, v: str):
        b = v.encode("utf-8")
        self.write_int(len(b))
        self.buf.extend(b)

    def write_bytes(self, v: bytes):
        self.write_int(len(v))
        self.buf.extend(v)

    def write_nullable_bool(self, v):
        if v is None:
            self.write_bool(False)
        else:
            self.write_bool(True)
            self.write_bool(v)

    def write_nullable_string(self, v):
        if v is None:
            self.write_bool(False)
        else:
            self.write_bool(True)
            self.write_string(v)

    def to_bytes(self) -> bytes:
        return bytes(self.buf)


class BytesReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_bool(self) -> bool:
        v = self.data[self.pos] != 0
        self.pos += 1
        return v

    def read_int(self) -> int:
        v = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_long(self) -> int:
        v = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return v

    def read_string(self) -> str:
        length = self.read_int()
        s = self.data[self.pos:self.pos + length].decode("utf-8")
        self.pos += length
        return s

    def read_bytes(self) -> bytes:
        length = self.read_int()
        b = self.data[self.pos:self.pos + length]
        self.pos += length
        return b

    def read_bytes_fixed(self, length: int) -> bytes:
        b = self.data[self.pos:self.pos + length]
        self.pos += length
        return b

    def read_nullable_bool(self):
        if not self.read_bool():
            return None
        return self.read_bool()

    def read_nullable_string(self):
        if not self.read_bool():
            return None
        return self.read_string()

    def read_nullable_long(self):
        if not self.read_bool():
            return None
        return self.read_long()

    def read_nullable_int(self):
        if not self.read_bool():
            return None
        return self.read_int()

    def remaining(self) -> int:
        return len(self.data) - self.pos
