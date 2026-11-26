"""Copy synchronized reconstruction layers into a shorter session-clock window."""

import errno
import json
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as pc
from rerun.chunk import Chunk, RrdReader

from specialist_dataset import schema
from specialist_dataset.state import HOLD_HEAD, HOLD_MIN_DURATION, TAP_TAIL, TapBurst

Float64Array = npt.NDArray[np.float64]


@dataclass(frozen=True)
class TrimConfig:
  output_suffix: str = "_hold_trim"
  video_sample: str = "VideoStream:sample"
  video_keyframe: str = "VideoStream:is_keyframe"
  chunk_id_metadata: bytes = b"rerun:id"
  recording_count: int = 1
  copy_directories: tuple[str, ...] = ("assets", "blueprints", "reports")
  report_directory: str = "state_trim_reports"
  report_name: str = "hold_trim.json"
  accepted_answers: tuple[str, ...] = ("y", "yes", "是")


TRIM_CONFIG = TrimConfig()


@dataclass(frozen=True)
class Hold:
  """A detected gesture on the original session clock, in seconds."""

  start: float
  end: float

  def fraction(self, span: tuple[float, float]) -> float:
    if not all(math.isfinite(t) for t in span) or span[1] <= span[0]:
      raise ValueError(f"invalid capture span {span}")
    return (self.end - span[0]) / (span[1] - span[0])

  def validate_head(self, span: tuple[float, float]) -> None:
    at = self.fraction(span)
    if at > HOLD_HEAD:
      raise ValueError(
        f"the hold ends at {at:.2%} of the capture "
        f"(absolute {schema.TIMELINE}={self.end:.6f} s), past its first tenth"
      )


def hold_trim_window(
  holds: list[Hold], times: dict[str, Float64Array], burst: TapBurst | None = None
) -> tuple[float, float]:
  """Find the widest sampled IMU interval satisfying both gesture constraints."""
  if not holds or not times:
    raise ValueError("a hold trim needs gestures and IMU times")
  for t in times.values():
    if t.ndim != 1 or not t.size or not np.isfinite(t).all() or (np.diff(t) <= 0).any():
      raise ValueError("IMU times must be finite and strictly increasing")
  clock = np.unique(np.concatenate(list(times.values())))
  latest_hold = max(h.end for h in holds)
  # For each start, the latest admissible end preserves the most data and gives
  # the Hold the most time. That end can only move earlier as the start advances.
  for start in clock:
    if any(h.end - max(start, h.start) < HOLD_MIN_DURATION for h in holds):
      break
    end = clock[-1]
    if burst is not None:
      if start > burst.start:
        break
      limit = start + (burst.strike - start) / TAP_TAIL
      idx = int(np.searchsorted(clock, np.nextafter(limit, np.inf), side="right")) - 1
      end = clock[idx]
      if burst.strike < start + TAP_TAIL * (end - start):
        end = clock[idx - 1]
      if end < burst.end:
        break
    if end > start and (latest_hold - start) / (end - start) <= HOLD_HEAD:
      return float(start), float(end)
  raise ValueError(
    f"the common crop must retain at least {HOLD_MIN_DURATION} s of every Hold "
    f"in the first tenth and the full strongest Tap, with its first peak in the final {1 - TAP_TAIL:.0%}"
  )


def video_preroll(reader: RrdReader, start_ns: int) -> dict[str, int]:
  """The latest decode keyframe at/before the cut, separately for every lens."""
  starts: dict[str, int | None] = {}
  first_samples: dict[str, int] = {}
  first_keyframes: dict[str, int] = {}
  for chunk in reader.stream().filter(
    components=[TRIM_CONFIG.video_sample, TRIM_CONFIG.video_keyframe], is_static=False
  ):
    batch = chunk.to_record_batch()
    if schema.TIMELINE not in batch.schema.names:
      raise ValueError(f"video has no {schema.TIMELINE}: {chunk.entity_path}")
    best = starts.setdefault(chunk.entity_path, None)
    times = batch.column(schema.TIMELINE).cast(pa.int64()).to_pylist()
    # RRD compaction may store packets and their markers in separate chunks.
    flags = (
      batch.column(TRIM_CONFIG.video_keyframe).to_pylist()
      if TRIM_CONFIG.video_keyframe in batch.schema.names
      else [None] * len(times)
    )
    for t, flag in zip(times, flags, strict=True):
      if t is None:
        continue
      first_samples[chunk.entity_path] = min(first_samples.get(chunk.entity_path, t), t)
      if flag and flag[0]:
        first_keyframes[chunk.entity_path] = min(
          first_keyframes.get(chunk.entity_path, t), t
        )
        if t <= start_ns:
          best = t if best is None else max(best, t)
    starts[chunk.entity_path] = best
  # A lens that only starts after the cut needs no earlier packets.
  for entity, best in starts.items():
    if (
      best is None
      and first_samples[entity] >= start_ns
      and first_keyframes.get(entity) == first_samples[entity]
    ):
      starts[entity] = first_samples[entity]
  missing = [entity for entity, start in starts.items() if start is None]
  if missing:
    raise ValueError(f"no decode keyframe at or before the crop start: {missing}")
  return {entity: start for entity, start in starts.items() if start is not None}


def trim_recording(
  source: Path, target: Path, window: tuple[float, float], session_id: str
) -> dict[str, float]:
  """Stream a filtered RRD copy; retain statics, properties and video decode preroll."""
  if target.exists():
    raise ValueError(f"refusing to overwrite {target}")
  if not all(math.isfinite(t) for t in window) or window[0] >= window[1]:
    raise ValueError(f"invalid trim window {window}")
  reader = RrdReader(str(source))
  recordings = reader.recordings()
  if (
    len(recordings) != TRIM_CONFIG.recording_count
    or recordings[0].recording_id != session_id
  ):
    raise ValueError(f"unexpected recording identity in {source}")
  start_ns = round(window[0] * schema.NS_PER_SECOND)
  end_ns = round(window[1] * schema.NS_PER_SECOND)
  starts = video_preroll(reader, start_ns)

  def keep(chunk: Chunk) -> list[Chunk]:
    if chunk.is_static or schema.TIMELINE not in chunk.timeline_names:
      return [chunk]
    batch = chunk.to_record_batch()
    times = batch.column(schema.TIMELINE).cast(pa.int64())
    lower = starts.get(chunk.entity_path, start_ns)
    filtered = batch.filter(
      pc.and_(pc.greater_equal(times, lower), pc.less_equal(times, end_ns))
    )
    if not filtered.num_rows:
      return []
    # A filtered chunk is a new chunk, and must not reuse its source's identity.
    metadata = dict(filtered.schema.metadata or {})
    metadata.pop(TRIM_CONFIG.chunk_id_metadata, None)
    return Chunk.from_record_batch(filtered.replace_schema_metadata(metadata))

  target.parent.mkdir(parents=True, exist_ok=True)
  partial = target.with_suffix(target.suffix + ".partial")
  try:
    reader.stream().flat_map(keep).write_rrd(
      partial, application_id=recordings[0].application_id, recording_id=session_id
    )
    partial.replace(target)
  finally:
    partial.unlink(missing_ok=True)
  return {entity: start / schema.NS_PER_SECOND for entity, start in starts.items()}


def _copy_support_file(source: str, target: str) -> str:
  path = Path(source)
  if (path.parent.name == "assets" and path.match("gripper-*.rrd")) or (
    path.parent.name == "blueprints" and path.name == "specialist.rbl"
  ):
    try:
      os.link(path.resolve(strict=True), target)
      return target
    except OSError as exc:
      if exc.errno not in (
        errno.EXDEV,
        errno.EOPNOTSUPP,
        errno.EMLINK,
        errno.EPERM,
        errno.EACCES,
      ):
        raise
  return shutil.copy2(source, target)


def materialize_layers(
  sources: dict[str, Path], target: Path, window: tuple[float, float], session_id: str
) -> dict[str, float]:
  preroll = {}
  for layer, source in sources.items():
    preroll.update(
      trim_recording(
        source, schema.layer_rrd(target, layer, session_id), window, session_id
      )
    )
    print(f"Trimmed {layer}: {window[0]:.6f} .. {window[1]:.6f} s", flush=True)
  return preroll


def trim_manifest_path(dataset: Path) -> Path:
  path = dataset / TRIM_CONFIG.report_directory / TRIM_CONFIG.report_name
  legacy = dataset / "reports" / TRIM_CONFIG.report_name
  return legacy if not path.exists() and legacy.is_file() else path


def read_trim_plan(dataset: Path) -> tuple[dict[str, Path], tuple[float, float]]:
  manifest = json.loads(trim_manifest_path(dataset).read_text())
  start, end = manifest["window_s"]
  if not math.isfinite(start) or not math.isfinite(end) or start >= end:
    raise ValueError(f"裁剪时间窗无效：{dataset}")
  sources = {
    layer: (dataset / manifest["source_layers"][layer]).resolve(strict=True)
    for layer in (schema.BASE, schema.ESTIMATE, schema.VIZ)
    if layer != schema.VIZ or layer in manifest["source_layers"]
  }
  if len({source.name for source in sources.values()}) != 1:
    raise ValueError(f"裁剪源会话不一致：{dataset}")
  return sources, (start, end)


def trim_dataset(
  base: Path,
  estimate: Path,
  target: Path,
  window: tuple[float, float],
  session_id: str,
  report: dict[str, Any],
) -> None:
  """Copy one session's base, estimate, optional viz and supporting files."""
  root = base.parent.parent
  layers = {schema.BASE: base, schema.ESTIMATE: estimate}
  viz = schema.layer_rrd(estimate.parent.parent, schema.VIZ, session_id)
  if viz.exists():
    layers[schema.VIZ] = viz
  for name in TRIM_CONFIG.copy_directories:
    source_dir = root / name
    if source_dir.is_dir():
      shutil.copytree(source_dir, target / name, copy_function=_copy_support_file)
  preroll = materialize_layers(layers, target, window, session_id)
  reports = target / TRIM_CONFIG.report_directory
  reports.mkdir(exist_ok=True)
  manifest = {
    **report,
    "source_layers": {layer: str(source.resolve()) for layer, source in layers.items()},
    "window_s": window,
    "timestamps": "original session clock preserved",
    "video_decode_preroll_start_s": preroll,
  }
  (reports / TRIM_CONFIG.report_name).write_text(
    json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
  )
