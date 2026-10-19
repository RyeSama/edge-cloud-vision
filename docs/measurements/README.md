# 实测数据

`metrics-1000.json` 由以下命令生成（Windows，Python 3.12，MobileNetV1 INT8，合成源）：

```bash
python -m ecvision.edge --source synthetic --max-frames 1000 --fps 15 --metrics-out data/metrics-1000.json --log-every 200
```

```json
{
  "frames": 1000,
  "accepted": 0,
  "review": 1000,
  "elapsed_s": 66.594,
  "fps": 15.02,
  "json_payload_bytes": 563944,
  "raw_rgb_equivalent_bytes": 230400000,
  "jpeg_15kb_reference_bytes": 15000000,
  "reduction_vs_raw_rgb": 0.997552,
  "reduction_vs_jpeg_15kb": 0.962404
}
```

## 口径

- `raw_rgb_equivalent_bytes` = 320 × 240 × 3 × 1000，代表"每帧都传原始 RGB"的上界基线。
- `jpeg_15kb_reference_bytes` = 15 KB × 1000，代表常见 320×240 MJPEG 码率参考。
- `json_payload_bytes` **只统计 MQTT payload**，不含 TCP/TLS/MQTT 固定头；因此这是偏乐观的应用层数字。
- 合成源（移动方块 + 文字）对 ImageNet 预训练模型是分布外输入，置信度约 0.1%，所以 1000 帧全部进入 `review`。这组数据用于验证"带宽 + 兜底路径压力"，**不代表真实场景的 accept 比例**。统计 accept 比例请用真实视频/摄像头。

复现时如果使用真实视频，请把 `--source synthetic` 换成 `--source path/to/video.mp4 --loop`，其余参数不变。
