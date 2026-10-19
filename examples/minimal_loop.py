"""最小闭环：采集 -> 224x224 预处理 -> TFLite INT8 推理 -> 门限 -> JSON。

这是模块 A 的“骨架版”，故意保持在一屏以内。生产版还包含：
    - 可选摄像头/视频源（本文件用确定性合成源，便于无硬件复现）
    - 指标统计（JSON 字节数、FPS、推理延迟）
    - MQTT 发布、心跳、离线缓冲
分别见 src/ecvision/edge/pipeline.py 与 src/ecvision/transport/mqtt.py。

运行：
    python scripts/download_model.py          # 只需一次
    python examples/minimal_loop.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ecvision.edge.classifier import TFLiteClassifier  # noqa: E402
from ecvision.edge.gate import GateConfig, evaluate  # noqa: E402
from ecvision.edge.sources import build_source  # noqa: E402

MODEL = ROOT / "models" / "mobilenet_v1_1.0_224_quant.tflite"
LABELS = ROOT / "models" / "labels.txt"


def main() -> None:
    # 1) 模拟 MCU 的低分辨率采集：320x240，15 FPS。
    #    换成摄像头只需 build_source("0", ...)，换成视频文件传路径即可。
    source = build_source("synthetic", width=320, height=240, fps=15.0)

    # 2) 加载 INT8 量化模型。TFLiteClassifier 会读 input shape/dtype，
    #    uint8/int8/float32 都能处理，并负责反量化。
    classifier = TFLiteClassifier(MODEL, LABELS, num_threads=2)

    # 3) 门限是纯函数：>0.90 直接接受，0.50~0.90 标记 review，
    #    <0.50 也标记 review（安全场景不静默丢弃）。
    gate = GateConfig(accept_threshold=0.90, review_threshold=0.50)

    try:
        for seq in range(5):
            frame = source.read()
            if frame is None:
                break

            prediction = classifier.predict(frame, top_k=3)
            top1 = prediction.top1
            result = evaluate(top1.confidence, gate)

            # 只把结构化字段送上云；没有图像、没有 base64。
            event = {
                "schema_version": "1.0",
                "device_id": "edge-cam-01",
                "seq": seq,
                "source": {"width": frame.shape[1], "height": frame.shape[0], "fps": 15.0},
                "inference": {
                    "model": classifier.model_name,
                    "label": top1.label,
                    "label_id": top1.label_id,
                    "confidence": round(top1.confidence, 6),
                    "latency_ms": round(prediction.inference_ms, 2),
                    "top_k": [
                        {"label": c.label, "confidence": round(c.confidence, 6)}
                        for c in prediction.candidates
                    ],
                },
                "gate": {
                    "decision": result.decision.value,
                    "reason": result.reason.value,
                    "accept_threshold": gate.accept_threshold,
                    "review_threshold": gate.review_threshold,
                },
            }
            print(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    finally:
        source.close()


if __name__ == "__main__":
    main()
