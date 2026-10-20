"""Markdown report for a planar metrology run."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ecvision.metrology.camera import CalibrationResult
from ecvision.metrology.gauge import GaugeResult
from ecvision.metrology.measurement import MeasurementResult
from ecvision.metrology.uncertainty import (
    UncertaintyComponent,
    budget_rows,
    combined_standard_uncertainty,
    expanded_uncertainty,
)


def render_report(
    *,
    title: str,
    calibration: CalibrationResult,
    measurement: MeasurementResult,
    components: Sequence[UncertaintyComponent],
    gauge: GaugeResult | None = None,
    notes: Sequence[str] = (),
) -> str:
    camera = calibration.camera
    combined = combined_standard_uncertainty(components)
    expanded = expanded_uncertainty(combined, k=2.0)

    lines: list[str] = [
        f"# {title}",
        "",
        "## Camera",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        f"| 分辨率 | {camera.image_width} × {camera.image_height} px |",
        f"| fx / fy | {camera.fx:.3f} / {camera.fy:.3f} px |",
        f"| cx / cy | {camera.cx:.3f} / {camera.cy:.3f} px |",
        f"| 畸变参数 | {', '.join(f'{value:.6f}' for value in camera.distortion)} |",
        f"| 标定视图 / 点 | {calibration.n_views} / {calibration.n_points} |",
        f"| RMS 重投影误差 | {calibration.rms_reprojection_px:.4f} px |",
        "",
        "## Planar measurement",
        "",
        "| 项目 | 数值 |",
        "|---|---:|",
        f"| 测量值 | {measurement.value_mm:.4f} mm |",
        f"| 标准不确定度 | {measurement.sigma_mm:.4f} mm |",
        f"| 95% 区间 | {measurement.ci95_low_mm:.4f} – {measurement.ci95_high_mm:.4f} mm |",
        f"| 蒙特卡洛样本 | {measurement.n_samples} |",
        f"| 像素定位 σ | {measurement.pixel_sigma_px:.3f} px |",
        f"| 标定尺度 σ | {measurement.calibration_scale_sigma_rel * 100:.3f}% |",
        "",
        "## Uncertainty budget",
        "",
        "| 来源 | 标准不确定度 / mm | 灵敏度 | 贡献 / mm | 占比 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in budget_rows(components, k=2.0):
        lines.append(
            "| {name} | {standard_uncertainty_mm:.4f} | {sensitivity:.3f} | "
            "{contribution_mm:.4f} | {share_pct:.1f}% |".format(**row)
        )
    lines += [
        "",
        f"合成标准不确定度 **{combined:.4f} mm**，扩展不确定度 **U(k=2) = {expanded:.4f} mm**。",
        "",
    ]

    if gauge is not None:
        lines += [
            "## Gauge R&R",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 零件 / 操作员 / 试验 | {gauge.parts} / {gauge.operators} / {gauge.trials} |",
            f"| 重复性 | {gauge.repeatability_std_mm:.4f} mm |",
            f"| 再现性 | {gauge.reproducibility_std_mm:.4f} mm |",
            f"| GRR | {gauge.grr_std_mm:.4f} mm |",
            f"| ndc | {gauge.ndc} |",
        ]
        if gauge.grr_pct_tolerance is not None:
            lines.append(f"| %GRR / 公差 | {gauge.grr_pct_tolerance:.2f}% |")
        lines.append("")

    if notes:
        lines += ["## Notes", ""]
        lines += [f"- {note}" for note in notes]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(path: str | Path, content: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target
