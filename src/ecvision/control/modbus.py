"""Minimal Modbus TCP server used as a PLC/interlock simulator.

This is intentionally a small, dependency-free implementation of the subset an
instrument demo needs (coils, holding registers, input registers). It is not a
production Modbus stack: no serial RTU, no diagnostics, no authentication. The
value is that the edge process can expose a *real* industrial protocol instead
of a JSON-only mock, and a PLC client can poll it without extra Python packages.

Register map (defaults, all 16-bit):
  coils[0] alarm, coils[1] fail_safe, coils[2] interlock_ok, coils[3] heartbeat
  holding[0] class_id, [1] confidence_x1000, [2] latency_ms, [3] state_code,
  [4] event_seq, [5] alarm_count
  input[0..5] mirror of holding
"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
from dataclasses import dataclass, field


class ModbusError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class RegisterMap:
    coils: list[bool] = field(default_factory=lambda: [False] * 64)
    holding: list[int] = field(default_factory=lambda: [0] * 64)
    input_registers: list[int] = field(default_factory=lambda: [0] * 64)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def write_coil(self, address: int, value: bool) -> None:
        with self.lock:
            self._check(address, len(self.coils))
            self.coils[address] = bool(value)

    def write_register(self, address: int, value: int) -> None:
        with self.lock:
            self._check(address, len(self.holding))
            self.holding[address] = int(value) & 0xFFFF
            self.input_registers[address] = int(value) & 0xFFFF

    def read_coils(self, address: int, count: int) -> list[bool]:
        with self.lock:
            self._check_range(address, count, len(self.coils))
            return self.coils[address : address + count]

    def read_holding(self, address: int, count: int) -> list[int]:
        with self.lock:
            self._check_range(address, count, len(self.holding))
            return self.holding[address : address + count]

    def read_input(self, address: int, count: int) -> list[int]:
        with self.lock:
            self._check_range(address, count, len(self.input_registers))
            return self.input_registers[address : address + count]

    @staticmethod
    def _check(address: int, size: int) -> None:
        if not 0 <= address < size:
            raise ModbusError(2, f"address {address} out of range")

    @staticmethod
    def _check_range(address: int, count: int, size: int) -> None:
        if count < 1 or address < 0 or address + count > size:
            raise ModbusError(2, "read range out of bounds")


class _ModbusServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, register_map: RegisterMap) -> None:
        self.register_map = register_map
        super().__init__(address, handler)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            header = _recv_exact(self.request, 7)
            if header is None:
                return
            transaction_id, protocol_id, length, unit_id = struct.unpack(">HHHB", header)
            if protocol_id != 0 or length < 2:
                return
            body = _recv_exact(self.request, length - 1)
            if body is None or not body:
                return
            function = body[0]
            payload = body[1:]
            try:
                response_pdu = _dispatch(function, payload, self.server.register_map)
            except ModbusError as exc:
                response_pdu = bytes([function | 0x80, exc.code])
            except (struct.error, ValueError):
                response_pdu = bytes([function | 0x80, 3])
            response = struct.pack(">HHHB", transaction_id, 0, len(response_pdu) + 1, unit_id)
            try:
                self.request.sendall(response + response_pdu)
            except OSError:
                return


def _dispatch(function: int, payload: bytes, registers: RegisterMap) -> bytes:
    if function == 0x01:
        address, count = struct.unpack(">HH", payload[:4])
        values = registers.read_coils(address, count)
        packed = bytearray((count + 7) // 8)
        for index, value in enumerate(values):
            if value:
                packed[index // 8] |= 1 << (index % 8)
        return bytes([function, len(packed)]) + bytes(packed)
    if function in (0x03, 0x04):
        address, count = struct.unpack(">HH", payload[:4])
        values = (
            registers.read_holding(address, count)
            if function == 0x03
            else registers.read_input(address, count)
        )
        body = b"".join(struct.pack(">H", value & 0xFFFF) for value in values)
        return bytes([function, len(body)]) + body
    if function == 0x05:
        address, value = struct.unpack(">HH", payload[:4])
        if value not in (0x0000, 0xFF00):
            raise ModbusError(3, "coil value must be 0x0000 or 0xFF00")
        registers.write_coil(address, value == 0xFF00)
        return struct.pack(">BHH", function, address, value)
    if function == 0x06:
        address, value = struct.unpack(">HH", payload[:4])
        registers.write_register(address, value)
        return struct.pack(">BHH", function, address, value)
    if function == 0x0F:
        address, count, byte_count = struct.unpack(">HHB", payload[:5])
        raw = payload[5 : 5 + byte_count]
        if len(raw) != byte_count:
            raise ModbusError(3, "short coil payload")
        for index in range(count):
            registers.write_coil(address + index, bool(raw[index // 8] & (1 << (index % 8))))
        return struct.pack(">BHH", function, address, count)
    if function == 0x10:
        address, count, byte_count = struct.unpack(">HHB", payload[:5])
        raw = payload[5 : 5 + byte_count]
        if len(raw) != byte_count or byte_count != count * 2:
            raise ModbusError(3, "bad register payload")
        for index in range(count):
            registers.write_register(
                address + index, struct.unpack(">H", raw[index * 2 : index * 2 + 2])[0]
            )
        return struct.pack(">BHH", function, address, count)
    raise ModbusError(1, f"unsupported function {function}")


class ModbusTcpServer:
    """Threaded Modbus TCP server bound to localhost by default."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 15020,
        *,
        register_map: RegisterMap | None = None,
    ) -> None:
        self.register_map = register_map or RegisterMap()
        self._server = _ModbusServer((host, port), _Handler, self.register_map)
        self.host, self.port = self._server.server_address[:2]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._server.serve_forever, name="modbus-tcp")
        self._thread.daemon = True
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)
        self._thread = None

    def __enter__(self) -> ModbusTcpServer:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def _recv_exact(sock: socket.socket, length: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)
