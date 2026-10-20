from __future__ import annotations

import socket
import struct

from ecvision.control.interlock import (
    ControlAction,
    ControlCommand,
    InterlockConfig,
    InterlockController,
    InterlockInput,
    InterlockState,
)
from ecvision.control.modbus import ModbusTcpServer, RegisterMap


def _sample(seq: int, detected: bool, *, ts: float = 0.0) -> InterlockInput:
    return InterlockInput(
        seq=seq,
        detected=detected,
        label="person",
        label_id=1,
        confidence=0.95,
        latency_ms=12.0,
        ts_mono_s=ts,
    )


def test_interlock_requires_consecutive_detections() -> None:
    controller = InterlockController(
        InterlockConfig(assert_after=3, clear_after_s=5, stale_after_s=30),
        clock=lambda: 0.0,
    )
    assert controller.update(_sample(0, True), now_s=0.0).state == InterlockState.ARMED
    assert controller.update(_sample(1, True), now_s=0.1).state == InterlockState.ARMED
    third = controller.update(_sample(2, True), now_s=0.2)
    assert third.state == InterlockState.ALARM
    assert third.alarm is True
    assert third.alarm_count == 1


def test_interlock_holds_alarm_after_last_detection() -> None:
    controller = InterlockController(
        InterlockConfig(assert_after=1, clear_after_s=5, stale_after_s=30),
        clock=lambda: 0.0,
    )
    controller.update(_sample(0, True), now_s=0.0)
    assert controller.update(_sample(1, True), now_s=4.0).alarm is True
    # Last detection was at 4.0; clearing before 9.0 must still hold.
    assert controller.update(_sample(2, False), now_s=8.0).alarm is True
    assert controller.update(_sample(3, False), now_s=9.1).alarm is False


def test_interlock_faults_on_stale_and_resets() -> None:
    controller = InterlockController(
        InterlockConfig(assert_after=1, clear_after_s=1, stale_after_s=2, watchdog_timeout_s=100),
        clock=lambda: 0.0,
    )
    controller.update(_sample(0, True), now_s=0.0)
    state = controller.check(now_s=3.0)
    assert state is not None
    assert state.state == InterlockState.FAULT
    assert state.fail_safe is True
    reset = controller.command(ControlCommand(seq=7, action=ControlAction.RESET))
    assert reset is not None
    assert reset.state == InterlockState.IDLE
    assert reset.command_ack == 7


def test_modbus_server_read_write_roundtrip() -> None:
    registers = RegisterMap()
    with ModbusTcpServer("127.0.0.1", 0, register_map=registers) as server:
        with socket.create_connection((server.host, server.port), timeout=2.0) as sock:
            sock.sendall(_request(struct.pack(">BHH", 5, 0, 0xFF00)))
            _read_response(sock)
            sock.sendall(_request(struct.pack(">BHH", 1, 0, 1)))
            response = _read_response(sock)
            assert response[0] == 1
            assert response[1] == 1
            assert response[2] & 0x01

            registers.write_register(3, 1234)
            sock.sendall(_request(struct.pack(">BHH", 3, 3, 1)))
            response = _read_response(sock)
            assert response[0] == 3
            assert struct.unpack(">H", response[2:4])[0] == 1234


def _request(pdu: bytes, transaction: int = 1) -> bytes:
    return struct.pack(">HHHB", transaction, 0, len(pdu) + 1, 1) + pdu


def _read_response(sock: socket.socket) -> bytes:
    header = _recv_exact(sock, 7)
    _transaction, _protocol, length, _unit = struct.unpack(">HHHB", header)
    return _recv_exact(sock, length - 1)


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise AssertionError("connection closed")
        buffer.extend(chunk)
    return bytes(buffer)
