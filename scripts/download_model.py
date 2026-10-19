#!/usr/bin/env python3
"""Download and verify the demo model.

The demo intentionally uses a stock MobileNetV1 INT8 model instead of a trained
custom model: the point of the project is the edge/cloud architecture, and a
well-known model makes the results reproducible. Swap in your own ``.tflite``
and labels later; the runtime only depends on the tensor shape/dtype.

Usage:
    python scripts/download_model.py
    python scripts/download_model.py --force
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"

MODEL_URL = (
    "https://storage.googleapis.com/download.tensorflow.org/models/"
    "mobilenet_v1_2018_08_02/mobilenet_v1_1.0_224_quant.tgz"
)
MODEL_SHA256 = "d32432d28673a936b2d6281ab0600c71cf7226dfe4cdcef3012555f691744166"
MODEL_NAME = "mobilenet_v1_1.0_224_quant.tflite"

LABELS_URL = "https://raw.githubusercontent.com/pytorch/hub/master/imagenet_classes.txt"
EXPECTED_LABELS = 1000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)


def extract_tflite(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.endswith(".tflite")]
        if len(members) != 1:
            raise RuntimeError(f"expected exactly one .tflite in archive, found {len(members)}")
        extracted = tar.extractfile(members[0])
        if extracted is None:
            raise RuntimeError(f"cannot read {members[0].name}")
        with extracted, destination.open("wb") as out:
            shutil.copyfileobj(extracted, out)


def write_labels(destination: Path) -> None:
    with urllib.request.urlopen(LABELS_URL, timeout=30) as response:
        lines = [line.strip() for line in response.read().decode("utf-8").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) != EXPECTED_LABELS:
        raise RuntimeError(f"expected {EXPECTED_LABELS} ImageNet labels, got {len(lines)}")
    # MobileNetV1 quant has 1001 output classes: index 0 is the background class.
    destination.write_text("\n".join(["background", *lines]) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download even if files exist")
    parser.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    args = parser.parse_args(argv)

    args.models_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.models_dir / MODEL_NAME
    labels_path = args.models_dir / "labels.txt"

    if model_path.exists() and not args.force:
        actual = sha256(model_path)
        if actual != MODEL_SHA256:
            raise SystemExit(
                f"existing model has wrong checksum:\n  expected {MODEL_SHA256}\n  actual   {actual}\n"
                "re-run with --force"
            )
        print(f"model already present and verified: {model_path}")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "model.tgz"
            download(MODEL_URL, archive)
            actual = sha256(archive)
            if actual != MODEL_SHA256:
                raise SystemExit(
                    f"checksum mismatch:\n  expected {MODEL_SHA256}\n  actual   {actual}"
                )
            extract_tflite(archive, model_path)
        print(f"wrote {model_path} ({model_path.stat().st_size:,} bytes, sha256 verified)")

    if labels_path.exists() and not args.force:
        print(f"labels already present: {labels_path}")
    else:
        write_labels(labels_path)
        print(f"wrote {labels_path}")

    print("done. try: python -m ecvision.edge --source synthetic --max-frames 5 --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
