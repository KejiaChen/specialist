"""Original-clock state generation and replay of historical crops."""

import io
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest
from rerun.chunk import RrdReader
from scripts.state import Args, warn_gesture_timing
from scripts.state import main as strict_main
from scripts.state_with_trim import main
from specialist_dataset import schema
from specialist_dataset.frames import LensVideo, decode_samples
from specialist_dataset.read import Recording
from specialist_dataset.trim import (
  TRIM_CONFIG,
  trim_dataset,
)

from specialist.record.base import calibration_chunks, imu_chunks, write_base_recording
from specialist.record.video import normalize_h264, video_chunks
from tests.record_fixtures import (
  camera_estimate,
  encode_clip,
  synthetic_insv,
  urdf_file,
  write_run,
)
from tests.unit.record.test_state import (
  _F_ROW,
  _FPS,
  _FRAMES,
  _RATE,
  _SCALE,
  _SHIFT,
  _STRIKE,
  _WINDOW,
  _frames,
)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
  return _make_dataset(tmp_path_factory.mktemp("trim-source"))


@pytest.fixture(scope="module")
def late_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
  return _make_dataset(tmp_path_factory.mktemp("late-dataset"), imu_start=-25.0)


def _make_dataset(root: Path, imu_start: float = -1.0) -> Path:
  window = (min(_WINDOW[0], imu_start), _WINDOW[1])
  imu_t = np.arange(imu_start, 30.0, 0.005)
  accel = np.zeros((len(imu_t), 3))
  accel[:, 2] = 9.81 + 0.05 * np.sin(20.0 * imu_t)
  for strike in (_STRIKE, _STRIKE + 0.25, _STRIKE + 0.5):
    accel[np.abs(imu_t - strike) < 0.003, 2] += 200.0
  insv = replace(
    synthetic_insv(),
    imu_timestamps=imu_t,
    gyro=np.zeros((len(imu_t), 3)),
    accel=accel,
  )
  clip = normalize_h264(
    encode_clip(root / "clip.mp4", _FRAMES, rate=_FPS, gop=4), root / "clean.mp4"
  )
  streams, sidecars = [], []
  for slot, shift in _SHIFT.items():
    streams.append(video_chunks(clip, schema.video(slot, "rear"), shift, window))
    sidecars += imu_chunks(slot, insv, 0.0, window) + calibration_chunks(slot, insv)
  base = schema.layer_rrd(root, schema.BASE, "sess")
  base.parent.mkdir()
  write_base_recording(base, "sess", streams, sidecars)
  cameras = [
    camera_estimate("cam00", hold=0.7, rate=_RATE["cam00"]),
    camera_estimate("cam01", offset=1.0, hold=0.7, rate=_RATE["cam01"]),
  ]
  write_run(root, "sess", cameras, urdf_path=urdf_file(root), frame_period=1.0 / _FPS)
  return root


def _state(dataset: Path, out_dir: Path, **kwargs: object) -> Path:
  base = schema.layer_rrd(dataset, schema.BASE, "sess")
  args = {
    "scale": _SCALE,
    "f_row": _F_ROW,
    "urdf_path": dataset / "robot.urdf",
    **kwargs,
  }
  main(Args(base=base, output_dir=out_dir, **args))  # type: ignore[arg-type]
  return schema.layer_rrd(out_dir, schema.state_layer(_SCALE, _F_ROW), "sess")


def _fingerprints(root: Path) -> dict[str, str]:
  return {
    layer: sha256(schema.layer_rrd(root, layer, "sess").read_bytes()).hexdigest()
    for layer in (schema.BASE, schema.ESTIMATE, schema.VIZ)
  }


@pytest.mark.parametrize(
  ("hold_end", "tap_peak", "messages"),
  [
    (30.0, 90.0, []),
    (30.001, 90.0, ["cam00 Hold"]),
    (30.0, 89.999, ["Tap 首峰"]),
    (31.0, 89.0, ["cam00 Hold", "Tap 首峰"]),
  ],
)
def test_timing_warnings_on_original_clock(hold_end, tap_peak, messages, capsys):
  warn_gesture_timing((10.0, 110.0), {"cam00": hold_end}, tap_peak)
  warnings = capsys.readouterr().err.splitlines()
  assert len(warnings) == len(messages)
  for warning, message in zip(warnings, messages, strict=True):
    assert message in warning


def test_wrapper_preserves_the_original_state_for_an_on_time_hold(
  dataset: Path,
  tmp_path: Path,
) -> None:
  args = Args(
    base=schema.layer_rrd(dataset, schema.BASE, "sess"),
    output_dir=tmp_path / "original",
    urdf_path=dataset / "robot.urdf",
    margin=0.0,
    scale=_SCALE,
    f_row=_F_ROW,
  )
  strict_main(args)
  main(replace(args, output_dir=tmp_path / "wrapper"))
  layer = schema.state_layer(_SCALE, _F_ROW)
  with (
    Recording(schema.layer_rrd(args.output_dir, layer, "sess")) as original,
    Recording(schema.layer_rrd(tmp_path / "wrapper", layer, "sess")) as wrapped,
  ):
    for hand in schema.HANDS:
      for component, width in (
        (schema.POSE_TRANSLATION, 3),
        (schema.POSE_QUATERNION, 4),
      ):
        for actual, expected in zip(
          wrapped.values(schema.state_pose(hand), component, width),
          original.values(schema.state_pose(hand), component, width),
          strict=True,
        ):
          np.testing.assert_array_equal(actual, expected)
      for actual, expected in zip(
        _frames(wrapped, hand), _frames(original, hand), strict=True
      ):
        np.testing.assert_array_equal(actual, expected)


def test_late_hold_builds_once_without_crop_or_confirmation(
  late_dataset, tmp_path, monkeypatch, capfd
):
  import scripts.state as writer

  calls = []
  detect = writer.tap_burst

  def track_detection(*args, **kwargs):
    calls.append(1)
    return detect(*args, **kwargs)

  monkeypatch.setattr(writer, "tap_burst", track_detection)
  monkeypatch.setattr("sys.stdin", io.StringIO(""))
  originals = _fingerprints(late_dataset)
  state = _state(late_dataset, tmp_path, margin=0.0)
  assert calls == [1]
  assert state.is_file()
  assert "cam00 Hold" in capfd.readouterr().err
  assert list(tmp_path.iterdir()) == [state.parent]
  assert _fingerprints(late_dataset) == originals


def test_overwrite_retires_old_crop_only_after_success(late_dataset, tmp_path):
  state = _state(late_dataset, tmp_path, margin=0.0)
  manifest = tmp_path / TRIM_CONFIG.report_directory / TRIM_CONFIG.report_name
  manifest.parent.mkdir()
  manifest.write_text('{"source_layers": {"base": "base/sess.rrd"}}')
  old_bytes = state.read_bytes()
  with pytest.raises(SystemExit, match="window"):
    _state(late_dataset, tmp_path, margin=100.0, overwrite=True)
  assert state.read_bytes() == old_bytes and manifest.exists()
  with pytest.raises(SystemExit, match="overwrite"):
    _state(late_dataset, tmp_path, margin=0.0)
  _state(late_dataset, tmp_path, margin=0.0, overwrite=True)
  assert state.is_file() and not manifest.exists()


def test_synchronized_crop_preserves_video_keyframe_preroll(
  dataset: Path,
  tmp_path: Path,
) -> None:
  originals = _fingerprints(dataset)
  window = (0.15, 0.28)
  trim_dataset(
    schema.layer_rrd(dataset, schema.BASE, "sess"),
    schema.layer_rrd(dataset, schema.ESTIMATE, "sess"),
    tmp_path,
    window,
    "sess",
    {},
  )
  assert _fingerprints(dataset) == originals
  with Recording(schema.layer_rrd(tmp_path, schema.BASE, "sess")) as base:
    for slot in _SHIFT:
      t, _ = base.scalars(schema.imu_signal(slot, "gyro"))
      assert t[0] >= window[0] and t[-1] <= window[1]
      video = LensVideo(base, slot, "rear", tmp_path)
      assert video.times[0] < window[0]
      assert video.times[-1] <= window[1]
      assert video.sync_samples[0] == 0
      assert len(list(decode_samples(video, np.arange(len(video.times))))) == len(
        video.times
      )
      video.close()
  for layer in (schema.ESTIMATE, schema.VIZ):
    for chunk in RrdReader(str(schema.layer_rrd(tmp_path, layer, "sess"))).stream():
      if not chunk.is_static and schema.TIMELINE in chunk.timeline_names:
        t = schema.seconds(chunk.to_record_batch().column(schema.TIMELINE))
        assert np.min(t) >= window[0] and np.max(t) <= window[1]
