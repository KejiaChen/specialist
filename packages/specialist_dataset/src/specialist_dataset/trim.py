"""Copy synchronized reconstruction layers into a shorter session-clock window."""

import json
import math
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
from specialist_dataset.state import HOLD_HEAD

Float64Array = npt.NDArray[np.float64]


@dataclass(frozen=True)
class TrimConfig:
  output_suffix: str = "_hold_trim"
  video_sample: str = "VideoStream:sample"
  video_keyframe: str = "VideoStream:is_keyframe"
  chunk_id_metadata: bytes = b"rerun:id"
  recording_count: int = 1
  copy_directories: tuple[str, ...] = ("assets", "blueprints", "reports")
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
  holds: list[Hold], times: dict[str, Float64Array]
) -> tuple[float, float]:
  """Remove only a prefix, keeping every full Hold and placing its end in the head."""
  if not holds or not times:
    raise ValueError("a hold trim needs gestures and IMU times")
  for t in times.values():
    if t.ndim != 1 or not t.size or not np.isfinite(t).all() or (np.diff(t) <= 0).any():
      raise ValueError("IMU times must be finite and strictly increasing")
  end = max(float(t[-1]) for t in times.values())
  start = min(float(t[0]) for t in times.values())
  head = HOLD_HEAD
  boundary = max(start, (max(h.end for h in holds) - head * end) / (1.0 - head))
  # Round upward by one clock tick so boundary rounding cannot leave a late Hold.
  start = (math.ceil(boundary * schema.NS_PER_SECOND) + 1) / schema.NS_PER_SECOND
  first = [int(np.searchsorted(t, start)) for t in times.values()]
  if any(i >= len(t) for i, t in zip(first, times.values(), strict=True)):
    raise ValueError("the hold trim leaves a camera with no IMU samples")
  effective_start = min(float(t[i]) for i, t in zip(first, times.values(), strict=True))
  if effective_start > min(h.start for h in holds):
    raise ValueError(
      "cannot put every Hold in the first tenth while preserving every full Hold; "
      "the gestures are too long or too far apart"
    )
  for gesture in holds:
    gesture.validate_head((effective_start, end))
  return start, end


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
  start_ns = math.ceil(window[0] * schema.NS_PER_SECOND)
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
      shutil.copytree(source_dir, target / name)
  preroll = {}
  for layer, source in layers.items():
    preroll.update(
      trim_recording(
        source, schema.layer_rrd(target, layer, session_id), window, session_id
      )
    )
    print(f"Trimmed {layer}: {window[0]:.6f} .. {window[1]:.6f} s", flush=True)
  reports = target / "reports"
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
