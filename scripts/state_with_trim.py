"""Prepare an optional confirmed Hold crop, then call the original state writer.

Usage:
    uv run python scripts/state_with_trim.py --base dataset/base/<session-id>.rrd \
        --output-dir dataset

Arguments are the original state.py arguments. state.py remains the strict writer.
"""

import fcntl
import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import numpy.typing as npt
import tyro
from specialist_dataset import schema
from specialist_dataset.read import Recording, read_anchors
from specialist_dataset.state import (
  HOLD_HEAD,
  TAP_TAIL,
  TapBurst,
  hold_interval,
  tap_burst,
)
from specialist_dataset.trim import (
  TRIM_CONFIG,
  Hold,
  hold_trim_window,
  materialize_layers,
)

if __package__ in (None, ""):
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import state as writer
from specialist.record.asset import resolve_urdf_path
from specialist.record.session import SessionRecording

Float64Array = npt.NDArray[np.float64]


def publish_state(target: Path, args: writer.Args, session_id: str) -> None:
  """Install only the state layer and its replay manifest in a populated trial."""
  destination = args.output_dir.resolve()
  relative_state = schema.layer_rrd(
    Path(), schema.state_layer(args.scale, args.f_row), session_id
  )
  relative_manifest = Path(TRIM_CONFIG.report_directory) / TRIM_CONFIG.report_name
  state = destination / relative_state
  manifest = destination / relative_manifest
  staged_manifest = target / relative_manifest
  destination.mkdir(exist_ok=True)
  lock = os.open(destination, os.O_RDONLY)
  try:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if state.exists() and not args.overwrite:
      raise SystemExit(f"{state} exists; pass --overwrite to replace it")
    state.parent.mkdir(exist_ok=True)
    previous = json.loads(manifest.read_text()) if manifest.exists() else None
    proposed = (
      json.loads(staged_manifest.read_text()) if staged_manifest.exists() else None
    )
    # A non-cropped session can share an output directory with a different session.
    if not proposed and (
      not previous or Path(previous["source_layers"]["base"]).stem != session_id
    ):
      (target / relative_state).replace(state)
      return
    if previous and any(
      p != state
      for p in destination.glob(
        "state_s*_r*/" + Path(previous["source_layers"]["base"]).name
      )
    ):
      if not proposed or any(
        previous[k] != proposed[k] for k in ("window_s", "source_layers")
      ):
        raise SystemExit(f"其他训练 state 正在使用原裁剪清单：{manifest}")
    backup = target / "previous-manifest.json"
    if previous:
      os.link(manifest, backup)
    if proposed:
      manifest.parent.mkdir(exist_ok=True)
      staged_manifest.replace(manifest)
    else:
      manifest.unlink()
    try:
      (target / relative_state).replace(state)
    except OSError:
      if previous:
        backup.replace(manifest)
      else:
        manifest.unlink()
      raise
  finally:
    os.close(lock)


def write_trimmed_state(
  args: writer.Args,
  estimate: Path,
  session_id: str,
  span: tuple[float, float],
  gestures: dict[str, Hold],
  imu_times: dict[str, Float64Array],
  burst: TapBurst,
) -> None:
  """Show the proposed common crop and publish the state and crop manifest in its trial."""
  print(f"采集区间 {span[0]:.6f} .. {span[1]:.6f} s")
  for slot, gesture in gestures.items():
    print(
      f"  {slot}: Hold 结束于 {gesture.fraction(span):.2%}, "
      f"absolute {schema.TIMELINE}={gesture.end:.6f} s "
      f"(Hold 起点 {gesture.start:.6f} s)"
    )
  print(
    f"最强 Tap 的首个峰值在 {burst.strike:.6f} s，完整脉冲区间 [{burst.start:.6f}, {burst.end:.6f}] s"
  )
  with writer.refusing():
    window = hold_trim_window(list(gestures.values()), imu_times, burst)
  destination = args.output_dir.resolve()
  print(
    f"拟保留 [{window[0]:.6f}, {window[1]:.6f}] s；"
    f"移除开头 {window[0] - span[0]:.6f} s、尾段 {span[1] - window[1]:.6f} s。\n"
    f"裁剪后各相机的 Hold 结束于前 {HOLD_HEAD:.0%}，最强 Tap 首峰位于后 {1 - TAP_TAIL:.0%}；沿用原始时间戳。\n"
    f"输出目录：{destination}"
  )
  for slot, gesture in gestures.items():
    retained_start = max(gesture.start, window[0])
    print(
      f"  {slot}: 原 Hold 区间保留 [{retained_start:.6f}, {gesture.end:.6f}] s"
      f"（{gesture.end - retained_start:.3f} s）"
    )
  auto_confirm = os.environ.get("SPECIALIST_AUTO_CONFIRM_TRIM") == "1"
  if auto_confirm:
    print("自动确认裁剪，开始生成训练 state。", flush=True)
  else:
    if not sys.stdin.isatty():
      raise SystemExit("需要在交互终端确认裁剪；未修改原始重建结果。")
    try:
      answer = input("是否裁剪并生成训练 state？[y/N] ").strip().casefold()
    except (EOFError, KeyboardInterrupt) as exc:
      raise SystemExit("已取消裁剪；未修改原始重建结果。") from exc
    if answer not in TRIM_CONFIG.accepted_answers:
      raise SystemExit("已取消裁剪；未修改原始重建结果。")

  destination.parent.mkdir(parents=True, exist_ok=True)
  # Keep incomplete crops private. A failed gesture/decode/state check publishes nothing.
  with tempfile.TemporaryDirectory(
    prefix=".state-trim-",
    dir=destination if destination.is_dir() else destination.parent,
  ) as staging:
    target = Path(staging) / "dataset"
    target.mkdir()
    inputs = Path(staging) / "inputs"
    sources = {schema.BASE: args.base, schema.ESTIMATE: estimate}
    preroll = materialize_layers(sources, inputs, window, session_id)
    viz = schema.layer_rrd(estimate.parent.parent, schema.VIZ, session_id)
    if viz.is_file():
      sources[schema.VIZ] = viz
    report = {
      "source_layers": {
        layer: os.path.relpath(path.resolve(), destination)
        for layer, path in sources.items()
      },
      "window_s": window,
      "timestamps": "original session clock preserved",
      "video_decode_preroll_start_s": preroll,
      "original_span_s": span,
      "hold_head_fraction": HOLD_HEAD,
      "tap": {"start_s": burst.start, "strike_s": burst.strike, "end_s": burst.end},
      "tap_tail_fraction": TAP_TAIL,
      "holds": {
        slot: {
          "start_s": h.start,
          "end_s": h.end,
          "retained_start_s": max(h.start, window[0]),
          "original_fraction": h.fraction(span),
        }
        for slot, h in gestures.items()
      },
      "confirmation": "automatic" if auto_confirm else "interactive",
      "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    reports = target / TRIM_CONFIG.report_directory
    reports.mkdir()
    (reports / TRIM_CONFIG.report_name).write_text(
      json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    writer.main(
      replace(
        args,
        base=schema.layer_rrd(inputs, schema.BASE, session_id),
        estimate=schema.layer_rrd(inputs, schema.ESTIMATE, session_id),
        output_dir=target,
      ),
    )
    publish_state(target, args, session_id)
  print(f"裁剪及 state 已保存：{destination}")


def main(args: writer.Args) -> None:
  sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
  session = SessionRecording(args.base)
  try:
    estimate_path = args.estimate or schema.layer_rrd(
      args.base.parent.parent, schema.ESTIMATE, session.session_id
    )
    slots = session.slots
    imus = {slot: session.imu(slot) for slot in slots}
    span = (
      min(float(imu.t[0]) for imu in imus.values()),
      max(float(imu.t[-1]) for imu in imus.values()),
    )
    anchors = np.stack(
      [pose[:3, 3] for pose in read_anchors(estimate_path).poses.values()]
    )
    gestures = {}
    with Recording(estimate_path) as estimate:
      for slot in slots:
        with writer.refusing(slot):
          gripper = writer.gripper_state(estimate, slot)
          distance = np.linalg.norm(gripper[1][:, None] - anchors[None], axis=2).min(
            axis=1
          )
          gestures[slot] = Hold(
            *hold_interval(gripper[0], distance, np.linalg.norm(gripper[3], axis=1))
          )
      times = {slot: imu.t for slot, imu in imus.items()}
      with writer.refusing():
        burst = tap_burst(times, {slot: imu.accel for slot, imu in imus.items()})
      needs_crop = any(h.fraction(span) > HOLD_HEAD for h in gestures.values()) or (
        burst.strike < span[0] + TAP_TAIL * (span[1] - span[0])
      )
      if needs_crop:
        writer.same_urdf(estimate, slots, resolve_urdf_path(args.urdf_path))
  finally:
    session.close()

  output = schema.layer_rrd(
    args.output_dir, schema.state_layer(args.scale, args.f_row), session.session_id
  )
  if output.exists() and not args.overwrite:
    raise SystemExit(f"{output} exists; pass --overwrite to replace it")
  if needs_crop:
    write_trimmed_state(
      args,
      estimate_path,
      session.session_id,
      span,
      gestures,
      times,
      burst,
    )
  else:
    destination = args.output_dir.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
      prefix=".state-", dir=destination if destination.is_dir() else destination.parent
    ) as staging:
      target = Path(staging)
      writer.main(replace(args, output_dir=target))
      publish_state(target, args, session.session_id)
    print(f"训练 state 已保存：{destination}")


if __name__ == "__main__":
  main(tyro.cli(writer.Args))
