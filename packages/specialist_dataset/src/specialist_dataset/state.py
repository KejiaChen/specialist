"""Numpy state-stream helpers: kept frames, row lattice, opening, hands and gestures."""

import math
from collections import Counter
from dataclasses import dataclass
from functools import reduce
from typing import NamedTuple

import numpy as np
import numpy.typing as npt

Float64Array = npt.NDArray[np.float64]
Int64Array = npt.NDArray[np.int64]

__all__ = [
  "HOLD_HEAD",
  "HOLD_MIN_DURATION",
  "TAP_TAIL",
  "TapBurst",
  "finger",
  "hands",
  "hold",
  "hold_interval",
  "image_frames",
  "lattice_brackets",
  "lerp",
  "nearest",
  "normalize_opening",
  "opening",
  "tap",
  "tap_burst",
]

_MAX_BRACKET = 0.0025
_PAIR_TOLERANCE = 0.05
_MIN_ANGLE = 3.0
_MIN_AGREEMENT = 0.75
_HOLD_SPEED = 0.15
HOLD_MIN_DURATION = 0.4
HOLD_HEAD = 0.1
_TAP_Z = 12.0
_TAP_MIN_GAP = 0.08  # Merge closely spaced peaks from the same impact (seconds).
_TAP_STRIKE_SPACING = 0.20  # Minimum peak spacing when counting separate knocks.
_TAP_BURST_GAP = 0.7
TAP_TAIL = 0.8  # Earliest allowed first peak, as a fraction of the capture span.


def hold_interval(
  times: Float64Array,
  anchor_distance: Float64Array,
  speed: Float64Array,
) -> tuple[float, float]:
  """The full closest slow anchor approach; refuses a hold that is too short."""
  slow = speed <= _HOLD_SPEED
  if not slow.any():
    raise ValueError("the gripper never slows near an anchor")
  i = int(np.argmin(np.where(slow, anchor_distance, np.inf)))
  lo, hi = i, i
  while lo > 0 and slow[lo - 1]:
    lo -= 1
  while hi < len(slow) - 1 and slow[hi + 1]:
    hi += 1
  if times[hi] - times[lo] < HOLD_MIN_DURATION:
    raise ValueError(
      f"the closest slow anchor approach lasts {times[hi] - times[lo]:.2f} s, "
      f"under the {HOLD_MIN_DURATION} s hold"
    )
  return float(times[lo]), float(times[hi])


def hold(
  times: Float64Array,
  anchor_distance: Float64Array,
  speed: Float64Array,
  span: tuple[float, float],
) -> float:
  """When the start hold ends; refuses one too short or ending past the first tenth."""
  _, end = hold_interval(times, anchor_distance, speed)
  at = (end - span[0]) / (span[1] - span[0])
  if at > HOLD_HEAD:
    raise ValueError(f"the hold ends at {at:.0%} of the capture, past its first tenth")
  return end


@dataclass(frozen=True)
class TapBurst:
  start: float
  strike: float  # First peak, used for the final-20% check.
  end: float


class _Spike(NamedTuple):
  peak: float
  slot: str
  start: float
  end: float


def _spikes(times: Float64Array, accel: Float64Array, slot: str) -> list[_Spike]:
  """First peak and full sample bounds of each distinct impact on one camera."""
  norm = np.linalg.norm(accel, axis=1)
  baseline = np.median(norm)
  spread = 1.4826 * np.median(np.abs(norm - baseline))
  above = np.concatenate(([False], norm > baseline + _TAP_Z * spread, [False]))
  edges = np.flatnonzero(np.diff(above.astype(np.int8)))
  events: list[_Spike] = []
  previous_peak = -math.inf
  for lo, hi in zip(edges[::2], edges[1::2], strict=True):
    peak = float(times[lo + int(np.argmax(norm[lo:hi]))])
    end = float(times[hi - 1])
    if peak - previous_peak < _TAP_MIN_GAP:
      events[-1] = events[-1]._replace(end=end)
    else:
      events.append(_Spike(peak, slot, float(times[lo]), end))
    previous_peak = peak
  return events


def tap_burst(
  times: dict[str, Float64Array],
  accel: dict[str, Float64Array],
  *,
  require_tail: bool = False,
) -> TapBurst:
  """Locate the last burst with at least three distinct strikes on every camera."""
  bursts: list[list[_Spike]] = [[]]
  for event in sorted(
    e for slot in times for e in _spikes(times[slot], accel[slot], slot)
  ):
    if bursts[-1] and event.peak - bursts[-1][-1].peak > _TAP_BURST_GAP:
      bursts.append([])
    bursts[-1].append(event)
  for burst in reversed(bursts):
    counts: Counter[str] = Counter()
    last_counted: dict[str, float] = {}
    for event in burst:
      if event.peak >= last_counted.get(event.slot, -math.inf) + _TAP_STRIKE_SPACING:
        counts[event.slot] += 1
        last_counted[event.slot] = event.peak
    if burst and all(counts[slot] >= 3 for slot in times):
      result = TapBurst(
        min(e.start for e in burst), burst[0].peak, max(e.end for e in burst)
      )
      break
  else:
    raise ValueError(
      "no tap burst with at least three distinct strikes on every camera"
    )
  if require_tail:
    t0 = min(float(t[0]) for t in times.values())
    t1 = max(float(t[-1]) for t in times.values())
    if result.strike < t0 + TAP_TAIL * (t1 - t0):
      raise ValueError(
        f"the last repeated tap burst starts at {(result.strike - t0) / (t1 - t0):.0%} of "
        f"the recording, before its final {1 - TAP_TAIL:.0%}"
      )
  return result


def tap(times: dict[str, Float64Array], accel: dict[str, Float64Array]) -> float:
  """First peak of the closing gesture in the final 20%."""
  return tap_burst(times, accel, require_tail=True).strike


def nearest(
  times: Float64Array, queries: Float64Array
) -> tuple[Int64Array, Float64Array]:
  """The index into ascending ``times`` nearest each query, and the gap."""
  if not len(times):
    raise ValueError("no times to search")
  hi = np.clip(np.searchsorted(times, queries), 0, len(times) - 1)
  lo = np.clip(hi - 1, 0, len(times) - 1)
  index = np.where(np.abs(times[hi] - queries) < np.abs(times[lo] - queries), hi, lo)
  return index.astype(np.int64), np.abs(times[index] - queries)


def image_frames(
  times: dict[str, Float64Array], fps: float, window: tuple[float, float]
) -> dict[str, Int64Array]:
  """Each lens's indices of the session frames every lens has, inside ``window``."""
  session = {s: np.rint(np.asarray(t) * fps).astype(np.int64) for s, t in times.items()}
  wanted = reduce(np.intersect1d, session.values())
  wanted = wanted[
    (wanted >= math.ceil(window[0] * fps - 0.5))
    & (wanted <= math.floor(window[1] * fps + 0.5))
  ]
  if not wanted.size:
    raise ValueError(f"no frame of every lens falls in the window {window} s")
  return {
    s: np.searchsorted(grid, wanted).astype(np.int64) for s, grid in session.items()
  }


def lattice_brackets(
  times: Float64Array, f_row: float, span: tuple[float, float]
) -> tuple[Int64Array, Int64Array, Float64Array]:
  """``(rows, before, weight)`` per lattice row in ``span``; refuses wide gaps."""
  t = np.asarray(times, dtype=np.float64)
  if len(t) < 2 or (np.diff(t) <= 0.0).any():
    raise ValueError("sample times are not strictly increasing")
  rows = np.arange(
    math.ceil(max(t[0], span[0]) * f_row), math.floor(min(t[-1], span[1]) * f_row) + 1
  )
  if not rows.size:
    raise ValueError(f"no row falls in the window {span} s")
  instants = rows / f_row
  before = np.clip(np.searchsorted(t, instants, side="right") - 1, 0, len(t) - 2)
  width = t[before + 1] - t[before]
  if (wide := width > _MAX_BRACKET).any():
    raise ValueError(
      f"rows {rows[wide][:6].tolist()} fall in a gap of {width.max() * 1e3:.1f} ms "
      "between samples"
    )
  return rows, before, (instants - t[before]) / width


def lerp(
  values: Float64Array, before: Int64Array, weight: Float64Array
) -> Float64Array:
  """``values`` linearly interpolated at the instants ``lattice_brackets`` located."""
  v = np.asarray(values, dtype=np.float64)
  w = weight.reshape(-1, *(1,) * (v.ndim - 1))
  out: Float64Array = v[before] + w * (v[before + 1] - v[before])
  return out


def finger(
  times: Float64Array, angles: Float64Array
) -> tuple[Float64Array, Float64Array]:
  """One finger's ``(times, angles)`` cut to the finite rows, where its tag is seen."""
  t = np.asarray(times, dtype=np.float64)
  a = np.asarray(angles, dtype=np.float64)
  keep = np.isfinite(a)
  if not keep.any():
    raise ValueError("the finger was never seen")
  return t[keep], a[keep]


# Pad contact and open limit used in specialist_sim.gripper (radians).
OPENING_CLOSED_RAD = -0.1518
OPENING_OPEN_RAD = 0.6981


def normalize_opening(angle_rad: Float64Array) -> Float64Array:
  """Map the coupled joint angle to the same unit interval across recordings."""
  if not np.isfinite(angle_rad).all():
    raise ValueError("gripper opening contains a nonfinite angle")
  return np.clip(
    (angle_rad - OPENING_CLOSED_RAD) / (OPENING_OPEN_RAD - OPENING_CLOSED_RAD),
    0.0,
    1.0,
  )


def opening(
  left: tuple[Float64Array, Float64Array],
  right: tuple[Float64Array, Float64Array],
  at: Float64Array,
) -> Float64Array:
  """Coupled angle ``(left - right) / 2`` at ``at``, refusing rows past either span."""
  (t_left, a_left), (t_right, a_right) = left, right
  if at[0] < max(t_left[0], t_right[0]) or at[-1] > min(t_left[-1], t_right[-1]):
    raise ValueError("a row falls outside the solved fingers' span")
  return (np.interp(at, t_left, a_left) - np.interp(at, t_right, a_right)) / 2.0


def hands(
  times: dict[str, Float64Array], translations: dict[str, Float64Array]
) -> tuple[str, str]:
  """``(left, right)`` slots, left counter-clockwise seen from the world origin."""
  if len(times) != 2:
    raise ValueError(f"hand detection needs two cameras, got {sorted(times)}")
  a, b = sorted(times)
  index, gap = nearest(np.asarray(times[b], float), np.asarray(times[a], float))
  paired = gap <= _PAIR_TOLERANCE
  pa = np.asarray(translations[a], dtype=np.float64)[paired, :2]
  pb = np.asarray(translations[b], dtype=np.float64)[index[paired], :2]
  norm = np.linalg.norm(pa, axis=1) * np.linalg.norm(pb, axis=1)
  sine = (pa[:, 0] * pb[:, 1] - pa[:, 1] * pb[:, 0])[norm > 0] / norm[norm > 0]
  if not sine.size:
    raise ValueError(f"{a} and {b} share no keyframes to compare")
  mean = float(sine.mean())
  angle = math.degrees(math.asin(max(-1.0, min(1.0, mean))))
  agreement = float(np.mean(np.sign(sine) == np.sign(mean)))
  if abs(angle) < _MIN_ANGLE or agreement < _MIN_AGREEMENT:
    raise ValueError(
      f"cannot tell the hands apart: seen from the anchor {b} sits {angle:+.1f} deg "
      f"from {a}, and the keyframes agree on the side {agreement:.0%} of the time"
    )
  return (b, a) if mean > 0.0 else (a, b)
