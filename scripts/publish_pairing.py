"""Publish a completed pairing attempt while the caller holds the output directory lock."""

import csv
import errno
import filecmp
import os
import re
import shutil
import sys
from pathlib import Path

FIELDS = ("trial", "session_id", "status", "signal", "offset_s", "corr")


def read_rows(path: Path, *, backup: Path | None = None) -> dict[str, dict[str, str]]:
  with path.open(newline="") as stream:
    reader = csv.DictReader(stream)
    legacy = "sync_signal" in (reader.fieldnames or []) and "signal" not in reader.fieldnames
    if legacy and backup is not None:
      if not backup.exists():
        temp = backup.with_suffix(".tmp")
        shutil.copyfile(path, temp)
        temp.replace(backup)
    elif tuple(reader.fieldnames or ()) != FIELDS:
      raise ValueError(f"无法识别汇总表的字段：{path}")
    rows = {}
    for row in reader:
      if legacy:
        status = {"sync_pass": "pass", "speed_sync_rejected": "review"}[row["status"]]
        row["status"] = status
        row["signal"] = row["sync_signal"] if status == "pass" else ""
        if status != "pass":
          row["offset_s"] = row["corr"] = ""
      trial, session = row["trial"], row["session_id"]
      if not re.fullmatch(r"[0-9]{3}", trial) or trial in rows:
        raise ValueError(f"试次号无效或重复：{path} {trial}")
      if session and (Path(session).name != session or session in (".", "..")):
        raise ValueError(f"会话 ID 无效：{session}")
      rows[trial] = {key: row[key] for key in FIELDS}
  return rows


def publish(output: Path, run: Path) -> None:
  if run.parent != output or not run.name.startswith("run_"):
    raise ValueError(f"运行目录应在输出目录内：{run}")
  products = run / ".products"
  ledger = output / "summary.csv"
  previous = read_rows(ledger, backup=run / "previous_summary.csv") if ledger.exists() else {}
  updates = read_rows(run / "summary.csv")
  merged = previous | updates
  passed = {r["session_id"] for r in merged.values() if r["status"] == "pass"}
  files = []
  obsolete = set()
  for trial, row in updates.items():
    if row["status"] == "pass":
      rrd = products / "paired_mocap" / (row["session_id"] + ".rrd")
      if not row["session_id"] or not rrd.is_file() or not rrd.stat().st_size:
        raise ValueError(f"通过配对的试次缺少 RRD：{trial}")
      files.append(rrd)
    old_session = previous.get(trial, {}).get("session_id")
    if old_session and old_session not in passed:
      obsolete.add(output / "paired_mocap" / (old_session + ".rrd"))

    csvs = list((products / "motive_csv").glob(f"*_{trial}.csv"))
    if len(csvs) > 1 or (not csvs and row["status"] != "error"):
      raise ValueError(f"试次转换结果数量不符：{trial}")
    if csvs:
      files.extend(csvs)
      obsolete.update(p for p in (output / "motive_csv").glob(f"*_{trial}.csv") if p.name != csvs[0].name)

  pending = output / ".publishing"
  if not pending.is_symlink():
    pending.symlink_to(run.name)
  if pending.resolve() != run:
    raise ValueError(f"尚有待完成的发布：{pending.resolve()}")
  for name in ("motive_csv", "paired_mocap"):
    (output / name).mkdir(exist_ok=True)
  # Keep staged originals until the summary and latest pointer are both published.
  for source in files:
    target = output / source.parent.name / source.name
    if target.is_file() and not target.is_symlink() and (
      source.samefile(target) or filecmp.cmp(source, target, shallow=False)
    ):
      continue
    temp = run / ".publish_file"
    temp.unlink(missing_ok=True)
    try:
      os.link(source, temp)
    except OSError as exc:
      if exc.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.EPERM, errno.EMLINK):
        raise
      shutil.copy2(source, temp)
    temp.replace(target)
  for path in obsolete:
    path.unlink(missing_ok=True)

  temp = run / ".summary.tmp"
  with temp.open("w", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(merged[n] for n in sorted(merged))
  temp.replace(ledger)
  pointer = run / ".latest"
  pointer.unlink(missing_ok=True)
  pointer.symlink_to(run.name)
  pointer.replace(output / "latest")
  pending.unlink()
  shutil.rmtree(products)


if __name__ == "__main__":
  publish(*(Path(arg).resolve(strict=True) for arg in sys.argv[1:]))
