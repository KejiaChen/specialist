"""Hold crop planning and the shared detection interval."""

import numpy as np
import numpy.typing as npt
import pytest
from specialist_dataset.state import HOLD_HEAD, hold_interval
from specialist_dataset.trim import Hold, hold_trim_window

Float64Array = npt.NDArray[np.float64]


def _approach(at: tuple[float, float]) -> tuple[Float64Array, ...]:
  t = np.arange(0.0, 100.0, 0.05)
  speed = np.full_like(t, 1.0)
  speed[(t >= at[0]) & (t <= at[1])] = 0.05
  distance = np.full_like(t, 2.0)
  distance[(t >= at[0]) & (t <= at[1])] = 0.3
  return t, distance, speed


def test_late_hold_detection_keeps_the_interval_and_reports_its_clock_time() -> None:
  gesture = Hold(*hold_interval(*_approach((50.0, 52.0))))
  assert gesture == Hold(50.0, 52.0)
  assert gesture.fraction((0.0, 100.0)) == pytest.approx(0.52)
  with pytest.raises(ValueError, match=r"52.00%.*absolute video_time=52.000000 s"):
    gesture.validate_head((0.0, 100.0))
  with pytest.raises(ValueError, match="capture span"):
    gesture.fraction((1.0, 1.0))


def test_hold_trim_uses_one_cut_and_the_actual_imu_clock_for_both_hands() -> None:
  # Nonzero time origin, and two asynchronous sample clocks.
  times = {"a": np.arange(100.0, 201.0, 0.01), "b": np.arange(100.004, 200.9, 0.013)}
  gestures = [Hold(148.0, 150.0), Hold(150.0, 152.0)]
  start, end = hold_trim_window(gestures, times)
  effective = min(t[t >= start][0] for t in times.values())
  assert start > 100.0
  assert effective <= min(h.start for h in gestures)
  assert end == max(t[-1] for t in times.values())
  for gesture in gestures:
    assert gesture.fraction((effective, end)) <= HOLD_HEAD
  # Removing any less than the analytical boundary fails for the latest Hold.
  boundary = (gestures[-1].end - HOLD_HEAD * end) / (1 - HOLD_HEAD)
  assert start == pytest.approx(boundary, abs=2e-9)


@pytest.mark.parametrize(
  "gestures", [[Hold(49.0, 95.0)], [Hold(9.0, 10.0), Hold(49.0, 50.0)]]
)
def test_hold_trim_refuses_to_cut_away_a_detected_hold(gestures: list[Hold]) -> None:
  with pytest.raises(ValueError, match="preserving every full Hold"):
    hold_trim_window(gestures, {"a": np.arange(0.0, 100.0, 0.01)})
