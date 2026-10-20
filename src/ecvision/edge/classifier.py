"""TFLite inference wrapper.

Runtime notes for Windows: PyPI's ``tflite-runtime`` has no Windows wheels, so
this module tries LiteRT (``ai-edge-litert``) first, then ``tflite-runtime``
(Linux/edge), then full TensorFlow as a last resort. Install exactly one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from ecvision.edge.preprocess import to_rgb_input


@dataclass(frozen=True, slots=True)
class Candidate:
    label_id: int
    label: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Prediction:
    candidates: tuple[Candidate, ...]
    preprocess_ms: float
    inference_ms: float
    probabilities: tuple[float, ...] | None = None

    @property
    def top1(self) -> Candidate:
        return self.candidates[0]


def _interpreter_class():
    errors: list[str] = []
    for module, attr in (
        ("ai_edge_litert.interpreter", "Interpreter"),
        ("tflite_runtime.interpreter", "Interpreter"),
        ("tensorflow.lite.python.interpreter", "Interpreter"),
    ):
        try:
            imported = __import__(module, fromlist=[attr])
            return getattr(imported, attr)
        except (ImportError, OSError) as exc:  # DLL load failures surface as OSError
            errors.append(f"{module}: {exc}")
    raise RuntimeError(
        "no TFLite runtime found. On Windows run `pip install ai-edge-litert`; "
        "on Linux `pip install tflite-runtime`; or install `tensorflow`. "
        f"Tried: {errors}"
    )


def load_labels(path: str | Path) -> list[str]:
    labels = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()]
    labels = [label for label in labels if label]
    if not labels:
        raise ValueError(f"empty labels file: {path}")
    return labels


class TFLiteClassifier:
    def __init__(
        self,
        model_path: str | Path,
        labels_path: str | Path,
        *,
        num_threads: int = 2,
        float_normalize: Literal["symmetric", "unit", "none"] = "symmetric",
    ) -> None:
        model_path = Path(model_path)
        if not model_path.is_file():
            raise FileNotFoundError(
                f"model not found: {model_path}. Run `python scripts/download_model.py` first."
            )
        self.model_name = model_path.stem
        self.labels = load_labels(labels_path)
        self._normalize = float_normalize
        self._lock = threading.Lock()

        interpreter_cls = _interpreter_class()
        self._interpreter = interpreter_cls(model_path=str(model_path), num_threads=num_threads)
        self._interpreter.allocate_tensors()
        self._input = self._interpreter.get_input_details()[0]
        self._output = self._interpreter.get_output_details()[0]

        shape = tuple(int(v) for v in self._input["shape"])
        if len(shape) != 4:
            raise ValueError(f"expected 4-D input tensor, got {shape}")
        if shape[-1] == 3:
            self._layout = "nhwc"
            self._height, self._width = shape[1], shape[2]
        elif shape[1] == 3:
            self._layout = "nchw"
            self._height, self._width = shape[2], shape[3]
        else:
            raise ValueError(f"cannot infer image layout from input shape {shape}")
        if self._width <= 0 or self._height <= 0:
            raise ValueError(f"model has dynamic spatial input {shape}; export a fixed shape")

    @property
    def input_size(self) -> tuple[int, int]:
        return self._width, self._height

    def _make_tensor(self, rgb: np.ndarray) -> np.ndarray:
        dtype = np.dtype(self._input["dtype"])
        if dtype == np.uint8:
            data = rgb.astype(np.uint8, copy=False)
        elif dtype == np.int8:
            scale, zero_point = self._input["quantization"]
            if not scale:
                raise ValueError("int8 input has no quantization scale")
            data = np.clip(np.round(rgb.astype(np.float32) / scale + zero_point), -128, 127)
            data = data.astype(np.int8)
        elif dtype == np.float32:
            if self._normalize == "symmetric":
                data = rgb.astype(np.float32) / 127.5 - 1.0
            elif self._normalize == "unit":
                data = rgb.astype(np.float32) / 255.0
            else:
                data = rgb.astype(np.float32)
        else:
            raise TypeError(f"unsupported input dtype: {dtype}")

        if self._layout == "nchw":
            data = np.transpose(data, (2, 0, 1))
        return np.expand_dims(data, axis=0)

    def _decode_scores(self, raw: np.ndarray) -> np.ndarray:
        dtype = np.dtype(self._output["dtype"])
        scores = raw.astype(np.float32).reshape(-1)
        if dtype in (np.uint8, np.int8):
            scale, zero_point = self._output["quantization"]
            if scale:
                scores = (scores - zero_point) * scale

        # Some exports emit logits; others emit softmax probabilities.
        total = float(scores.sum())
        if scores.min() < 0.0 or scores.max() > 1.0 or not 0.98 <= total <= 1.02:
            shifted = scores - scores.max()
            exp = np.exp(shifted)
            scores = exp / exp.sum()
        return scores

    def _label_for(self, index: int, count: int) -> str:
        if len(self.labels) == count:
            return self.labels[index]
        if len(self.labels) == count - 1:
            return "background" if index == 0 else self.labels[index - 1]
        return f"class_{index}"

    def predict(self, frame: np.ndarray, top_k: int = 3) -> Prediction:
        started = time.perf_counter()
        rgb = to_rgb_input(frame, self.input_size)
        tensor = self._make_tensor(rgb)
        preprocessed = time.perf_counter()

        with self._lock:
            self._interpreter.set_tensor(self._input["index"], tensor)
            self._interpreter.invoke()
            raw = self._interpreter.get_tensor(self._output["index"])
        finished = time.perf_counter()

        scores = self._decode_scores(raw)
        order = np.argsort(scores)[-top_k:][::-1]
        candidates = tuple(
            Candidate(
                label_id=int(index),
                label=self._label_for(int(index), len(scores)),
                confidence=float(np.clip(scores[index], 0.0, 1.0)),
            )
            for index in order
        )
        return Prediction(
            candidates=candidates,
            preprocess_ms=(preprocessed - started) * 1000.0,
            inference_ms=(finished - preprocessed) * 1000.0,
            probabilities=tuple(float(value) for value in scores),
        )
