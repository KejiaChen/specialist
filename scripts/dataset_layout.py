"""Resolve mode-specific paths for a task dataset, including unmigrated trials."""

import sys
from pathlib import Path


class DatasetLayout:
  def __init__(self, dataset: Path, mode: str):
    self.mode = mode
    branch = dataset / mode
    self.legacy = not (branch / "raw").exists() and any(
      branch.glob("trial_[0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9]")
    )
    self.raw = branch if self.legacy else branch / "raw"
    root = dataset if self.legacy else branch
    self.vicon = dataset / "vicon" if self.legacy else self.raw / "vicon"
    if mode == "single" and not self.legacy:
      x3 = self.raw / "x3"
      if x3.is_dir() or not any(self.raw.glob("trial_??????_???")):
        self.raw = x3
    self.pairing = root / "pairing"
    self.reconstruction = root / (
      "reconstruction_runs" if self.legacy else "reconstruction"
    )
    self.processed = root / "processed"

  def trials_in(self, processed_root: Path) -> Path:
    return processed_root / self.mode if self.legacy else processed_root


if __name__ == "__main__":
  dataset, mode = sys.argv[1:]
  layout = DatasetLayout(Path(dataset), mode)
  # One value per line for the two Bash entry points.
  for value in (
    layout.raw,
    layout.vicon,
    layout.pairing,
    layout.processed,
    "legacy" if layout.legacy else "current",
  ):
    print(value)
