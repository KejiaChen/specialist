"""Prepare an optional confirmed Hold crop, then call the original state writer.

Usage:
    uv run python scripts/state_with_trim.py --base dataset/base/<session-id>.rrd \
        --output-dir dataset

Arguments are the original state.py arguments. state.py remains the strict writer.
"""

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
from specialist_dataset.state import HOLD_HEAD, hold_interval
from specialist_dataset.trim import TRIM_CONFIG, Hold, hold_trim_window, trim_dataset

if __package__ in (None, ""):
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import state as writer
from specialist.record.asset import resolve_urdf_path
from specialist.record.session import SessionRecording

Float64Array = npt.NDArray[np.float64]


def write_trimmed_state(
  args: writer.Args,
  estimate: Path,
  session_id: str,
  span: tuple[float, float],
  gestures: dict[str, Hold],
  imu_times: dict[str, Float64Array],
) -> None:
  """Show the proposed common crop and publish a new dataset only after confirmation."""
  print(f"Hold 超出前 {HOLD_HEAD:.0%}；采集区间 {span[0]:.6f} .. {span[1]:.6f} s")
  for slot, gesture in gestures.items():
    print(
      f"  {slot}: Hold 结束于 {gesture.fraction(span):.2%}, "
      f"absolute {schema.TIMELINE}={gesture.end:.6f} s "
      f"(Hold 起点 {gesture.start:.6f} s)"
    )
  with writer.refusing():
    window = hold_trim_window(list(gestures.values()), imu_times)
  destination = args.output_dir.resolve().with_name(
    args.output_dir.resolve().name + TRIM_CONFIG.output_suffix
  )
  if destination.exists():
    raise SystemExit(f"裁剪目标已存在：{destination}；请使用新的 --output-dir")
  print(
    f"拟同步裁剪 base/estimate/viz，保留 [{window[0]:.6f}, {window[1]:.6f}] s；"
    f"移除开头 {window[0] - span[0]:.6f} s。\n"
    f"裁剪后各相机的 Hold 均在前 {HOLD_HEAD:.0%}，保留完整 Hold 和原始时间戳。\n"
    f"输出目录：{destination}"
  )
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
    prefix=f".{destination.name}.", dir=destination.parent
  ) as staging:
    target = Path(staging) / "dataset"
    target.mkdir()
    trim_dataset(
      args.base,
      estimate,
      target,
      window,
      session_id,
      {
        "original_span_s": span,
        "hold_head_fraction": HOLD_HEAD,
        "holds": {
          slot: {
            "start_s": h.start,
            "end_s": h.end,
            "original_fraction": h.fraction(span),
          }
          for slot, h in gestures.items()
        },
        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
      },
    )
    writer.main(
      replace(
        args,
        base=schema.layer_rrd(target, schema.BASE, session_id),
        estimate=schema.layer_rrd(target, schema.ESTIMATE, session_id),
        output_dir=target,
      ),
    )
    target.rename(destination)
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
      late = any(h.fraction(span) > HOLD_HEAD for h in gestures.values())
      if late:
        writer.same_urdf(estimate, slots, resolve_urdf_path(args.urdf_path))
  finally:
    session.close()

  if late:
    output = schema.layer_rrd(
      args.output_dir, schema.state_layer(args.scale, args.f_row), session.session_id
    )
    if output.exists() and not args.overwrite:
      raise SystemExit(f"{output} exists; pass --overwrite to replace it")
    write_trimmed_state(
      args,
      estimate_path,
      session.session_id,
      span,
      gestures,
      {slot: imu.t for slot, imu in imus.items()},
    )
  else:
    writer.main(args)


if __name__ == "__main__":
  main(tyro.cli(writer.Args))
