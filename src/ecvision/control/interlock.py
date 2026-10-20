"""Debounced interlock and fail-safe state machine.

The vision model is a sensor, not a safety PLC. This controller adds the
deterministic layer a real installation needs:

* assert only after ``assert_after`` consecutive detections;
* hold the alarm for ``clear_after_s`` after the last detection;
* fail safe on stale input or a missing heartbeat;
* expose a reset/silence/request-capture command with a sequence number and ACK.

The default output is a Modbus register map, so a PLC simulator can poll it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ecvision.control.modbus import RegisterMap


class InterlockState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"
    ALARM = "alarm"
    FAULT = "fault"


class ControlAction(str, Enum):
    RESET = "reset"
    SILENCE = "silence"
    REQUEST_CAPTURE = "request_capture"


@dataclass(frozen=True, slots=True)
class InterlockConfig:
    assert_after: int = 3
    clear_after_s: float = 5.0
    stale_after_s: float = 2.0
    watchdog_timeout_s: float = 10.0
    alarm_coil: int = 0
    fail_safe_coil: int = 1
    interlock_ok_coil: int = 2
    heartbeat_coil: int = 3
    class_register: int = 0
    confidence_register: int = 1
    latency_register: int = 2
    state_register: int = 3
    seq_register: int = 4
    alarm_count_register: int = 5

    def __post_init__(self) -> None:
        if self.assert_after < 1:
            raise ValueError("assert_after must be >= 1")
        if self.clear_after_s < 0 or self.stale_after_s <= 0:
            raise ValueError("invalid timing configuration")


@dataclass(frozen=True, slots=True)
class InterlockInput:
    seq: int
    detected: bool
    label: str
    label_id: int
    confidence: float
    latency_ms: float
    ts_mono_s: float


@dataclass(frozen=True, slots=True)
class InterlockOutput:
    seq: int
    state: InterlockState
    alarm: bool
    audible_alarm: bool
    fail_safe: bool
    interlock_ok: bool
    consecutive: int
    reason: str
    class_id: int
    confidence: float
    latency_ms: float
    alarm_count: int
    command_ack: int | None = None
    capture_request: int | None = None


@dataclass(frozen=True, slots=True)
class ControlCommand:
    seq: int
    action: ControlAction


class ControlSink(Protocol):
    def write(self, output: InterlockOutput) -> None: ...


class ModbusRegisterSink:
    """Write the interlock state to the simulated PLC register map."""

    def __init__(self, registers: RegisterMap, config: InterlockConfig | None = None) -> None:
        self.registers = registers
        self.config = config or InterlockConfig()

    def write(self, output: InterlockOutput) -> None:
        cfg = self.config
        self.registers.write_coil(cfg.alarm_coil, output.alarm)
        self.registers.write_coil(cfg.fail_safe_coil, output.fail_safe)
        self.registers.write_coil(cfg.interlock_ok_coil, output.interlock_ok)
        self.registers.write_coil(cfg.heartbeat_coil, not output.fail_safe)
        self.registers.write_register(cfg.class_register, output.class_id)
        self.registers.write_register(cfg.confidence_register, round(output.confidence * 1000))
        self.registers.write_register(cfg.latency_register, round(output.latency_ms))
        self.registers.write_register(cfg.state_register, _STATE_CODE[output.state])
        self.registers.write_register(cfg.seq_register, output.seq & 0xFFFF)
        self.registers.write_register(cfg.alarm_count_register, output.alarm_count & 0xFFFF)


class InterlockController:
    def __init__(
        self,
        config: InterlockConfig | None = None,
        *,
        sink: ControlSink | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.config = config or InterlockConfig()
        self._sink = sink
        self._clock = clock or time.monotonic
        now = self._clock()
        self._last_event_s = now
        self._last_heartbeat_s = now
        self._alarm_since: float | None = None
        self._last_detect_s: float | None = None
        self._state = InterlockState.IDLE
        self._consecutive = 0
        self._silenced = False
        self._alarm_count = 0
        self._last: InterlockOutput | None = None
        self._last_input: InterlockInput | None = None
        self._ack: int | None = None
        self._capture_request: int | None = None

    @property
    def state(self) -> InterlockState:
        return self._state

    @property
    def last(self) -> InterlockOutput | None:
        return self._last

    def update(self, sample: InterlockInput, *, now_s: float | None = None) -> InterlockOutput:
        now = self._clock() if now_s is None else now_s
        self._last_event_s = now

        if self._state == InterlockState.FAULT:
            return self._emit(sample, now, "fault latched; reset required")

        self._check_watchdog(now)
        if self._state == InterlockState.FAULT:
            return self._emit(sample, now, "watchdog timeout")

        if sample.detected:
            self._consecutive += 1
            self._last_detect_s = now
            if self._consecutive >= self.config.assert_after:
                if self._state != InterlockState.ALARM:
                    self._alarm_count += 1
                    self._alarm_since = now
                    self._silenced = False
                self._state = InterlockState.ALARM
            elif self._state != InterlockState.ALARM:
                self._state = InterlockState.ARMED
        else:
            self._consecutive = 0
            if (
                self._state == InterlockState.ALARM
                and self._last_detect_s is not None
                and now - self._last_detect_s < self.config.clear_after_s
            ):
                pass
            elif self._state in {InterlockState.ALARM, InterlockState.ARMED}:
                self._state = InterlockState.IDLE
                self._alarm_since = None
        return self._emit(sample, now, "detection" if sample.detected else "clear")

    def heartbeat(self, *, now_s: float | None = None) -> InterlockOutput | None:
        now = self._clock() if now_s is None else now_s
        self._last_heartbeat_s = now
        self._check_watchdog(now)
        if self._last is None:
            return None
        return self._last

    def check(self, *, now_s: float | None = None) -> InterlockOutput | None:
        now = self._clock() if now_s is None else now_s
        self._check_watchdog(now)
        if self._last is None:
            return None
        if self._state == InterlockState.FAULT and self._last_input is not None:
            return self._emit(self._last_input, now, "watchdog/stale timeout")
        return self._last

    def command(self, command: ControlCommand) -> InterlockOutput | None:
        if command.action == ControlAction.RESET:
            self._state = InterlockState.IDLE
            self._consecutive = 0
            self._alarm_since = None
            self._last_detect_s = None
            self._silenced = False
            self._ack = command.seq
        elif command.action == ControlAction.SILENCE:
            self._silenced = True
            self._ack = command.seq
        elif command.action == ControlAction.REQUEST_CAPTURE:
            self._capture_request = command.seq
            self._ack = command.seq
        if self._last is None:
            return None
        if self._last_input is None:
            return self._last
        return self._emit(self._last_input, self._clock(), "command")

    def _check_watchdog(self, now: float) -> None:
        if now - self._last_heartbeat_s > self.config.watchdog_timeout_s:
            self._state = InterlockState.FAULT
            return
        if now - self._last_event_s > self.config.stale_after_s:
            self._state = InterlockState.FAULT

    def _emit(self, sample: InterlockInput, now: float, reason: str) -> InterlockOutput:
        self._last_input = sample
        alarm = self._state == InterlockState.ALARM
        fail_safe = self._state == InterlockState.FAULT
        output = InterlockOutput(
            seq=sample.seq,
            state=self._state,
            alarm=alarm,
            audible_alarm=alarm and not self._silenced,
            fail_safe=fail_safe,
            interlock_ok=not fail_safe and not alarm,
            consecutive=self._consecutive,
            reason=reason,
            class_id=sample.label_id,
            confidence=round(sample.confidence, 6),
            latency_ms=round(sample.latency_ms, 3),
            alarm_count=self._alarm_count,
            command_ack=self._ack,
            capture_request=self._capture_request,
        )
        self._last = output
        if self._sink is not None:
            self._sink.write(output)
        return output


_STATE_CODE = {
    InterlockState.IDLE: 0,
    InterlockState.ARMED: 1,
    InterlockState.ALARM: 2,
    InterlockState.FAULT: 3,
}
