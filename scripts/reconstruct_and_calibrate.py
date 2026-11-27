"""Sequential X3 reconstruction and optional Vicon lever-arm evaluation."""

import argparse
import csv
import fcntl
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from checked_single_base import checked_base
from dataset_layout import DatasetLayout
from match_estimate import matches_anchors, recording
from reconstruction_reports import publish_reports, read_table
from specialist_dataset import schema
from specialist_dataset.trim import TRIM_CONFIG

SCRIPTS = Path(__file__).resolve().parent


def args_from_cli():
  parser = argparse.ArgumentParser(
    prog="reconstruct_and_calibrate.sh",
    description="串行重建 X3、生成训练 state，可选在 state 窗口内标定并验证杆臂。",
  )
  parser.add_argument(
    "--dataset", type=Path, required=True, help="含 single/ 的数据集目录"
  )
  parser.add_argument(
    "--trials", nargs="+", help="三位试次号，默认使用配对汇总中的全部试次"
  )
  parser.add_argument(
    "--anchor-ids", required=True, help="锚点 ID，例如 0,1,2，第一个为参考锚点"
  )
  parser.add_argument("--anchor-size", required=True, help="锚点内方形边长，单位米")
  parser.add_argument(
    "--calibration", metavar="NNN", help="开启 Vicon 评估，仅用该试次拟合杆臂"
  )
  parser.add_argument(
    "--overwrite-state",
    action="store_true",
    help="重新生成所选模式的 state，成功后覆盖旧文件",
  )
  parser.add_argument(
    "--pairing-dir", type=Path, help="配对结果，默认为数据集/single/pairing"
  )
  parser.add_argument(
    "--output-dir", type=Path, help="运行记录父目录，默认为数据集/single/reconstruction"
  )
  parser.add_argument(
    "--state-boundary",
    choices=("hold-tap", "audio"),
    default="hold-tap",
    help="训练窗口的起止标记，默认 hold-tap，audio 使用 X3 双音",
  )
  args = parser.parse_args()
  ids = args.anchor_ids.split(",")
  if any(not re.fullmatch(r"0|[1-9][0-9]*", tag) for tag in ids) or len(
    set(ids)
  ) != len(ids):
    parser.error("--anchor-ids 需要互异的非负整数")
  if (
    not re.fullmatch(r"(?:[0-9]+(?:[.][0-9]+)?|[.][0-9]+)", args.anchor_size)
    or not 0 < float(args.anchor_size) < math.inf
  ):
    parser.error("--anchor-size 需要大于零的有限十进制数")
  if args.trials and (
    len(set(args.trials)) != len(args.trials)
    or any(not re.fullmatch("[0-9]{3}", n) for n in args.trials)
  ):
    parser.error("--trials 需要互异的三位试次号")
  return args


def paired_inputs(pairing, args, run):
  # Snapshot the summary and matching layers under the pairing publisher's lock.
  fd = os.open(pairing, os.O_RDONLY)
  try:
    fcntl.flock(fd, fcntl.LOCK_SH)
    if (pairing / ".publishing").is_symlink():
      raise ValueError("配对结果发布中断，请先完成配对发布")
    with (pairing / "summary.csv").open(newline="") as f:
      reader = csv.DictReader(f)
      required = {"trial", "session_id", "status", "signal", "offset_s", "corr"}
      if not required.issubset(reader.fieldnames or []):
        raise ValueError(f"配对汇总字段不完整：{pairing / 'summary.csv'}")
      ledger = {}
      for row in reader:
        n = row["trial"]
        if not re.fullmatch(r"[0-9]{3}", n) or n in ledger:
          raise ValueError(f"配对汇总中的试次号无效或重复：{n}")
        ledger[n] = row
    trials = sorted(args.trials or ledger)
    if not trials or set(trials) - ledger.keys():
      raise ValueError("所选试次必须在 pairing/summary.csv 中有记录")
    if args.calibration and args.calibration not in trials:
      raise ValueError("--calibration 必须是所选试次之一")
    if args.calibration and ledger[args.calibration]["status"] == "pass":
      directory = run / "paired_mocap"
      directory.mkdir()
      for n in trials:
        row = ledger[n]
        if row["status"] != "pass":
          continue
        session = row["session_id"]
        if not session or Path(session).name != session:
          raise ValueError(f"{n} 配对记录中的会话 ID 无效")
        source = pairing / "paired_mocap" / f"{session}.rrd"
        dest = directory / f"{n}.rrd"
        if not source.is_file() or not source.stat().st_size:
          if n == args.calibration:
            raise ValueError(f"标定试次缺少配对轨迹：{source}")
          continue  # Recorded individually in the evaluation summary.
        try:
          dest.hardlink_to(source)
        except OSError:
          shutil.copy2(source, dest)
    return {n: ledger[n] for n in trials}
  finally:
    os.close(fd)


def run_logged(command, logs, env):
  with subprocess.Popen(["tee", "-a", "--", *logs], stdin=subprocess.PIPE) as tee:
    result = subprocess.run(
      command, cwd=SCRIPTS.parent, env=env, stdout=tee.stdin, stderr=subprocess.STDOUT
    )
  if tee.returncode:
    raise OSError("写入日志失败")
  return result


def state_result(estimate, boundary="hold-tap"):
  folder = estimate.parent.parent
  datasets = (
    (folder / "audio_state",)
    if boundary == "audio"
    else (folder, folder.with_name(folder.name + TRIM_CONFIG.output_suffix))
  )
  for dataset in datasets:
    path = dataset / schema.state_layer(8, 100) / estimate.name
    if path.is_file():
      reader = recording(path, estimate.stem)
      # The batch is single-camera. Read the actual first/last training pose rows
      # on the original video clock, including for a confirmed crop.
      store = reader.store()
      entities = {str(p).strip("/") for p in store.schema().entity_paths()}
      (pose,) = [
        schema.state_pose(h) for h in schema.HANDS if schema.state_pose(h) in entities
      ]
      chunks = store.stream().filter(content="/" + pose, is_static=False).to_chunks()
      spans = []
      for chunk in chunks:
        times = schema.seconds(chunk.to_record_batch().column(schema.TIMELINE))
        if len(times):
          spans.append((float(times.min()), float(times.max())))
      if not spans:
        raise ValueError(f"state 缺少训练位姿：{path}")
      lo, hi = min(start for start, _ in spans), max(end for _, end in spans)
      if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        raise ValueError(f"state 时间窗无效：{path}")
      return path, (lo, hi)
  return None


def prepare_state(raw, estimate, reused, log, boundary="hold-tap", overwrite=False):
  folder = estimate.parent.parent
  fd = os.open(raw, os.O_RDONLY)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    if not overwrite:
      existing = state_result(estimate, boundary)
      if existing:
        return ("reused" if reused else "built"), *existing
      if not reused and boundary == "hold-tap":
        raise ValueError(f"重建已保存，未生成 state。见 {folder / 'reports/state.log'}")
    report = folder / "reports/state.log"
    report.parent.mkdir(exist_ok=True)
    destination = folder / "audio_state" if boundary == "audio" else folder
    command = [
      sys.executable,
      "-u",
      str(SCRIPTS / "state.py"),
      "--base",
      folder / "base" / estimate.name,
      "--estimate",
      estimate,
      "--output-dir",
      destination,
    ]
    if overwrite:
      command.append("--overwrite")
    if boundary == "audio":
      masters = list(raw.glob("*_00_*.insv"))
      if len(masters) != 1:
        raise ValueError(f"{raw}: 音频模式需要唯一的 _00_ 原片")
      command.extend(["--audio-source", masters[0]])
    env = dict(
      os.environ,
      TMPDIR=str(folder),
      PATH=str(SCRIPTS.parent / "tools/ffmpeg/bin") + os.pathsep + os.environ["PATH"],
    )
    run_logged(command, [log.name, report], env).check_returncode()
    result = state_result(estimate, boundary)
    if not result:
      raise ValueError(f"未找到生成的训练 state：{folder}")
    return "built", *result
  finally:
    os.close(fd)


def reconstruct(ds, layout, n, args, log):
  folders = list(layout.raw.glob(f"trial_[0-9][0-9][0-9][0-9][0-9][0-9]_{n}"))
  if len(folders) != 1:
    raise ValueError(f"{n} 应对应一个原片目录，找到 {len(folders)} 个")
  raw = folders[0]
  processed = layout.processed
  primary = layout.trials_in(processed) / raw.name
  variant_root = processed / f"anchors_{args.anchor_ids}_{args.anchor_size}"
  variant = layout.trials_in(variant_root) / raw.name
  # A complete reconstruction is reusable only with the same raw capture and anchors.
  for folder in dict.fromkeys(
    [
      primary,
      variant,
      *(layout.trials_in(p) / raw.name for p in sorted(processed.glob("anchors_*"))),
    ]
  ):
    if not (folder / "estimate").is_dir():
      continue
    fronts = list(raw.glob("*_00_*.insv"))
    if len(fronts) != 1:
      raise ValueError(f"{raw} 应有唯一主镜头")
    front = fronts[0]
    base = checked_base(
      folder, front, front.with_name(front.name.replace("_00_", "_10_", 1))
    )
    estimate = folder / "estimate" / base.name
    reader = recording(estimate, base.stem)
    if not matches_anchors(estimate, reader, args.anchor_ids, args.anchor_size):
      continue
    required = [folder / "viz" / base.name, folder / "blueprints/specialist.rbl"]
    assets = list((folder / "assets").glob("gripper-*.rrd"))
    if len(assets) != 1 or not all(
      p.is_file() and p.stat().st_size for p in required + assets
    ):
      raise ValueError(f"已有重建不完整：{folder}")
    print(f"复用 {estimate}", file=log)
    return "reused", estimate, raw

  root = variant_root if (primary / "estimate").exists() else processed
  command = [
    str(SCRIPTS / "process_one_trial.sh"),
    ds.name,
    "single",
    raw.name,
    "--anchor-ids",
    args.anchor_ids,
    "--anchor-size",
    args.anchor_size,
  ]
  if args.state_boundary == "hold-tap" and not args.overwrite_state:
    command.append("--state")
  env = dict(
    os.environ, SPECIALIST_DATA_ROOT=str(ds.parent), SPECIALIST_PROCESSED_ROOT=str(root)
  )
  result = run_logged(command, [log.name], env)
  if result.returncode not in (0, 3):
    result.check_returncode()
  (estimate,) = (layout.trials_in(root) / raw.name / "estimate").glob("*.rrd")
  return "built", estimate, raw


def batch(args):
  ds = args.dataset.resolve(strict=True)
  layout = DatasetLayout(ds, "single")
  pairing = (args.pairing_dir or layout.pairing).resolve(strict=True)
  parent = (args.output_dir or layout.reconstruction).resolve()
  parent.mkdir(parents=True, exist_ok=True)
  run = Path(
    tempfile.mkdtemp(prefix=datetime.now().strftime("run_%Y%m%d_%H%M%S_"), dir=parent)
  )
  print(f"运行目录 {run}", flush=True)
  (run / "logs").mkdir()
  rows = paired_inputs(pairing, args, run)
  calibration = args.calibration
  excluded = set()
  exclusions_file = parent / "exclusions.csv"
  if calibration and exclusions_file.is_file():
    fields, entries = read_table(exclusions_file)
    if "status" not in fields:
      raise ValueError(f"排除表缺少 status 列：{exclusions_file}")
    excluded = {
      n for n, row in entries.items() if row["status"] == "excluded"
    } & rows.keys()
    if calibration in excluded:
      raise ValueError(
        f"指定标定试次 {calibration} 在 {exclusions_file} 中标为 excluded"
      )
  if calibration and rows[calibration]["status"] != "pass":
    print(
      f"指定标定试次 {calibration} 的配对状态为 {rows[calibration]['status']}，"
      "本批仅重建，跳过杆臂拟合和验证。",
      flush=True,
    )
    calibration = None
  (run / "parameters.txt").write_text(
    f"dataset={ds}\npairing={pairing}\ntrials={' '.join(rows)}\nanchor_ids={args.anchor_ids}\n"
    f"anchor_size_m={args.anchor_size}\nrequested_calibration={args.calibration or ''}\n"
    f"calibration={calibration or ''}\nstate_boundary={args.state_boundary}\n"
    f"overwrite_state={args.overwrite_state}\nexcluded={' '.join(sorted(excluded))}\n"
  )
  layers, sync, records, windows = {}, {}, [], {}
  errors = False
  with (run / "reconstruction.csv").open("w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(
      (
        "trial",
        "pairing_status",
        "reconstruction",
        "estimate",
        "state_status",
        "state_rrd",
        "state_start_s",
        "state_end_s",
        "state_boundary",
      )
    )
    for n, row in rows.items():
      estimate = state = None
      state_status = "skipped"
      window = ("", "")
      print(f"处理 {n}", flush=True)
      with (run / "logs" / f"{n}_reconstruction.log").open("a") as log:
        try:
          status, estimate, raw = reconstruct(ds, layout, n, args, log)
        except (
          OSError,
          RuntimeError,
          ValueError,
          SystemExit,
          subprocess.CalledProcessError,
        ) as exc:
          status = "reconstruction_error"
          print(exc, file=log)
          errors = True
        if estimate is not None:
          log.flush()
          try:
            state_status, state, window = prepare_state(
              raw,
              estimate,
              status == "reused",
              log,
              args.state_boundary,
              args.overwrite_state,
            )
            windows[n] = window
          except (
            OSError,
            RuntimeError,
            ValueError,
            subprocess.CalledProcessError,
          ) as exc:
            state_status = "state_error"
            print(exc, file=log)
            errors = True
      writer.writerow(
        (
          n,
          row["status"],
          status,
          estimate or "",
          state_status,
          state or "",
          *window,
          args.state_boundary,
        )
      )
      f.flush()
      print(f"{n} {status}, state {state_status}", flush=True)
      if not calibration:
        continue
      if n in excluded:
        records.append(dict(trial=n, status="excluded"))
        continue
      sync[n] = dict(sync=row["signal"], offset_s=row["offset_s"], corr=row["corr"])
      if estimate is None:
        reason = status
      elif row["status"] != "pass":
        reason = f"pairing_{row['status']}"
      elif state is None:
        reason = state_status
      else:
        mocap = run / "paired_mocap" / f"{n}.rrd"
        try:
          if estimate.stem != row["session_id"]:
            raise ValueError("配对会话与重建会话不符")
          recording(mocap, estimate.stem)
          layers[n] = estimate, mocap
          continue
        except (OSError, RuntimeError, ValueError) as exc:
          reason = "mocap_error"
          print(f"{n} {exc}", file=sys.stderr)
          errors = True
      stage = "candidate" if n == calibration else "holdout"
      records.append(
        dict(
          trial=n,
          stage=stage,
          status=reason,
          calibration_trial=calibration,
          **sync[n],
        )
      )
  if calibration:
    sys.path.insert(0, str(SCRIPTS.parents[1] / "vicon/processing"))
    from align_right_batch import evaluate_layers

    (run / "scores").mkdir()
    try:
      evaluate_layers(
        run, layers, sync, records, calibration, keep_going=True, windows=windows
      )
    except SystemExit:
      # A rejected calibration still produces a complete table with the reasons.
      if (run / "summary.csv").is_file():
        publish_reports(run, parent)
      raise
    errors |= any(r["status"] == "score_error" for r in records)
  publish_reports(run, parent)
  print(f"最新重建清单 {parent / 'reconstruction.csv'}", flush=True)
  return int(errors)


if __name__ == "__main__":
  try:
    sys.exit(batch(args_from_cli()))
  except (ValueError, OSError, RuntimeError) as exc:
    sys.exit(str(exc))
