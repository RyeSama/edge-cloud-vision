"""CLI entry point for the edge node.

Examples:
    python -m ecvision.edge --source synthetic --dry-run
    python -m ecvision.edge --source 0
    python -m ecvision.edge --source samples/warehouse.mp4 --loop
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from ecvision.config import MqttConfig
from ecvision.edge.classifier import TFLiteClassifier
from ecvision.edge.gate import GateConfig
from ecvision.edge.pipeline import EdgeRunner, RunnerConfig
from ecvision.edge.sources import build_source
from ecvision.transport.base import DryRunPublisher

DEFAULT_MODEL = Path("models/mobilenet_v1_1.0_224_quant.tflite")
DEFAULT_LABELS = Path("models/labels.txt")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ecv-edge",
        description="Edge node: low-resolution capture -> INT8 TFLite -> confidence gate -> MQTT JSON.",
    )
    p.add_argument(
        "--source", default="synthetic", help="synthetic | camera index (0) | video path"
    )
    p.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    p.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    p.add_argument("--device-id", default=os.getenv("ECV_DEVICE_ID", "edge-cam-01"))
    p.add_argument("--width", type=int, default=320, help="simulated MCU capture width")
    p.add_argument("--height", type=int, default=240, help="simulated MCU capture height")
    p.add_argument("--fps", type=float, default=15.0)
    p.add_argument("--top-k", type=int, default=3, choices=range(1, 6))
    p.add_argument("--accept-threshold", type=float, default=0.90)
    p.add_argument("--review-threshold", type=float, default=0.50)
    p.add_argument("--max-frames", type=int, default=None)
    p.add_argument("--loop", action="store_true", help="loop a video file")
    p.add_argument("--preview", action="store_true", help="show an OpenCV window (needs a GUI)")
    p.add_argument("--dry-run", action="store_true", help="print JSONL instead of publishing MQTT")
    p.add_argument("--mqtt-host", default=None)
    p.add_argument("--mqtt-port", type=int, default=None)
    p.add_argument("--client-id", default=None)
    p.add_argument("--num-threads", type=int, default=2, help="TFLite CPU threads")
    p.add_argument(
        "--float-normalize",
        choices=["symmetric", "unit", "none"],
        default="symmetric",
        help="only used by float32 models; INT8 models ignore it",
    )
    p.add_argument("--metrics-out", type=Path, default=None)
    p.add_argument("--log-every", type=int, default=30)
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("ecv.edge")
    load_dotenv()

    gate_config = GateConfig(
        accept_threshold=args.accept_threshold,
        review_threshold=args.review_threshold,
    )
    source = build_source(args.source, args.width, args.height, args.fps, loop=args.loop)

    publisher = DryRunPublisher()
    if not args.dry_run:
        # Imported lazily so `--dry-run` works even before paho-mqtt is installed.
        from ecvision.transport.mqtt import MqttPublisher

        mqtt_config = MqttConfig.from_env(client_id=args.client_id or f"ecv-edge-{args.device_id}")
        mqtt_config = replace(
            mqtt_config,
            host=args.mqtt_host or mqtt_config.host,
            port=args.mqtt_port or mqtt_config.port,
        )
        publisher = MqttPublisher(mqtt_config, args.device_id)

    try:
        classifier = TFLiteClassifier(
            args.model,
            args.labels,
            num_threads=args.num_threads,
            float_normalize=args.float_normalize,
        )
        log.info(
            "edge=%s source=%s model=%s input=%dx%d qos=1 dry_run=%s",
            args.device_id,
            source.description,
            classifier.model_name,
            classifier.input_size[0],
            classifier.input_size[1],
            args.dry_run,
        )
        runner = EdgeRunner(
            source=source,
            classifier=classifier,
            publisher=publisher,
            config=RunnerConfig(
                device_id=args.device_id,
                fps=args.fps,
                gate=gate_config,
                top_k=args.top_k,
                log_every=max(1, args.log_every),
            ),
        )
        runner.run(
            max_frames=args.max_frames,
            preview=args.preview,
            metrics_out=args.metrics_out,
        )
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
