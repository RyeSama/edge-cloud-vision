"""Frame acquisition abstractions.

The runner only knows ``FrameSource``. Replacing a webcam with a CSI camera on
an embedded board therefore does not touch inference, gating, or transport.
"""

from __future__ import annotations

import platform
import time
from typing import Protocol, runtime_checkable

import cv2
import numpy as np


@runtime_checkable
class FrameSource(Protocol):
    @property
    def description(self) -> str: ...

    def read(self) -> np.ndarray | None: ...

    def close(self) -> None: ...


class OpenCVSource:
    """Webcam, video file, or image sequence through one OpenCV path."""

    def __init__(
        self,
        source: int | str,
        width: int = 320,
        height: int = 240,
        fps: float = 15.0,
        loop: bool = False,
    ) -> None:
        self._is_file = isinstance(source, str)
        self._loop = loop
        self._path = source
        backend = cv2.CAP_DSHOW if platform.system() == "Windows" and not self._is_file else 0
        self._cap = cv2.VideoCapture(source, backend) if backend else cv2.VideoCapture(source)
        if not self._cap.isOpened():
            self._cap.release()
            raise RuntimeError(f"cannot open video source: {source!r}")
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._cap.set(cv2.CAP_PROP_FPS, fps)
        self._width = width
        self._height = height
        self._fps = fps

    @property
    def description(self) -> str:
        return f"opencv:{self._path}"

    def read(self) -> np.ndarray | None:
        ok, frame = self._cap.read()
        if not ok:
            if self._loop and self._is_file:
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._cap.read()
            if not ok:
                return None
        if frame.shape[1] != self._width or frame.shape[0] != self._height:
            frame = cv2.resize(frame, (self._width, self._height), interpolation=cv2.INTER_AREA)
        return frame

    def close(self) -> None:
        self._cap.release()


class SyntheticSource:
    """Camera-free source for CI, demos, and broker/integration tests.

    It is deliberately obvious that this is not real perception: the moving
    square is only a deterministic frame generator.
    """

    def __init__(self, width: int = 320, height: int = 240, fps: float = 15.0) -> None:
        self._width = width
        self._height = height
        self._fps = fps
        self._n = 0
        self._next = time.monotonic()

    @property
    def description(self) -> str:
        return f"synthetic:{self._width}x{self._height}@{self._fps:g}"

    def read(self) -> np.ndarray | None:
        # Pace the generator like a camera; avoids a 100%-CPU busy loop.
        now = time.monotonic()
        wait = self._next - now
        if wait > 0:
            time.sleep(wait)
        self._next = max(self._next + 1.0 / self._fps, time.monotonic())

        frame = np.zeros((self._height, self._width, 3), dtype=np.uint8)
        frame[:, :, 0] = 24
        frame[:, :, 1] = 28
        frame[:, :, 2] = 32
        x = 20 + (self._n * 7) % max(1, self._width - 100)
        y = self._height // 2 - 40
        cv2.rectangle(frame, (x, y), (x + 80, y + 80), (40, 180, 240), -1)
        cv2.putText(
            frame,
            f"SYNTHETIC #{self._n:05d}",
            (10, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (230, 230, 230),
            1,
            cv2.LINE_AA,
        )
        self._n += 1
        return frame

    def close(self) -> None:
        return None


def build_source(
    spec: str,
    width: int,
    height: int,
    fps: float,
    loop: bool = False,
) -> FrameSource:
    """``spec`` is ``synthetic``, a camera index (``0``), or a file path."""
    if spec.lower() == "synthetic":
        return SyntheticSource(width=width, height=height, fps=fps)
    if spec.isdigit():
        return OpenCVSource(int(spec), width=width, height=height, fps=fps)
    return OpenCVSource(spec, width=width, height=height, fps=fps, loop=loop)
