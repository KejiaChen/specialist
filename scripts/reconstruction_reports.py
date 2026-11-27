"""Publish per-trial reconstruction and scoring ledgers from completed batch reports."""

import csv
import fcntl
import os
import re
import tempfile
from pathlib import Path

SCORES = (
  "trial",
  "stage",
  "sync",
  "offset_s",
  "corr",
  "calibration_trial",
  "status",
  "rejected_scale",
  "full_frames",
  "full_p50_mm",
  "full_p90_mm",
  "full_p99_mm",
)
TABLES = ("reconstruction.csv", "summary.csv")


def read_table(path):
  with path.open(newline="") as stream:
    reader = csv.DictReader(stream)
    fields = reader.fieldnames or []
    if "trial" not in fields or len(set(fields)) != len(fields):
      raise ValueError(f"无效的表头：{path}")
    rows = {}
    for row in reader:
      trial = row["trial"]
      if (
        not re.fullmatch(r"[0-9]{3}", trial or "")
        or trial in rows
        or None in row
        or None in row.values()
      ):
        raise ValueError(f"试次行无效或重复：{path} {trial}")
      rows[trial] = row
  return fields, rows


def merge_reports(parent, run, target):
  """Preserve prior row order. Append new trials together in numerical order."""
  for name in TABLES:
    old = parent / name
    fields, rows = read_table(old) if old.exists() else ([], {})
    incoming = run / name
    if name == "reconstruction.csv" or incoming.is_file():
      columns, updates = read_table(incoming)
      fields = list(dict.fromkeys([*columns, *fields]))
      rows.update((n, updates[n]) for n in sorted(updates))
    if name == "reconstruction.csv":
      trials = list(rows)
    else:
      fields = list(dict.fromkeys([*SCORES, *fields]))
      for trial in trials:
        rows.setdefault(trial, {"trial": trial, "status": "not_scored"})
    with (target / name).open("w", newline="") as stream:
      writer = csv.DictWriter(stream, fieldnames=fields)
      writer.writeheader()
      # Keep both tables in trial order even when only some trials get scores.
      writer.writerows(rows[n] for n in dict.fromkeys([*trials, *rows]))


def finish_publication(parent):
  # A stopped publisher may already have replaced one table. Finish the others
  # under the same directory lock before reading either ledger for the next run.
  pending = parent / ".pending_reports"
  if pending.is_dir():
    for name in (*TABLES, "latest"):
      item = pending / name
      if os.path.lexists(item):
        item.replace(parent / name)
    pending.rmdir()


def publish_reports(run, parent):
  fd = os.open(parent, os.O_RDONLY)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    latest = parent / "latest"
    if latest.exists() and not latest.is_symlink():
      raise ValueError(f"latest 已被普通文件或目录占用：{latest}")
    finish_publication(parent)
    with tempfile.TemporaryDirectory(dir=parent, prefix=".reports_") as staging:
      ready = Path(staging) / "ready"
      ready.mkdir()
      merge_reports(parent, run, ready)
      (ready / "latest").symlink_to(run.relative_to(parent), target_is_directory=True)
      ready.rename(parent / ".pending_reports")
      finish_publication(parent)
  finally:
    os.close(fd)
