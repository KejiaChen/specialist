"""Confirmed whole-dataset Hold crops around the unchanged state writer."""

import io
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import numpy as np
import pytest
from rerun.chunk import RrdReader
from scripts.state import Args, gripper_state
from scripts.state import main as strict_main
from scripts.state_with_trim import main
from specialist_dataset import schema
from specialist_dataset.frames import LensVideo, decode_samples
from specialist_dataset.read import Recording
from specialist_dataset.state import HOLD_HEAD, hold
from specialist_dataset.trim import TRIM_CONFIG, trim_dataset

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
  accel[np.abs(imu_t - _STRIKE) < 0.003, 2] += 200.0
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


class _Terminal(io.StringIO):
  def isatty(self) -> bool:
    return True


def _fingerprints(root: Path) -> dict[str, str]:
  return {
    layer: sha256(schema.layer_rrd(root, layer, "sess").read_bytes()).hexdigest()
    for layer in (schema.BASE, schema.ESTIMATE, schema.VIZ)
  }


def test_original_writer_still_refuses_a_late_hold(
  late_dataset: Path,
  tmp_path: Path,
) -> None:
  with pytest.raises(SystemExit, match="past its first tenth"):
    strict_main(
      Args(
        base=schema.layer_rrd(late_dataset, schema.BASE, "sess"),
        output_dir=tmp_path,
        urdf_path=late_dataset / "robot.urdf",
        margin=0.0,
      )
    )
  assert not list(tmp_path.iterdir())


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


def test_confirmed_hold_trim_keeps_sources_and_writes_a_synchronized_dataset(
  late_dataset: Path,
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  capfd: pytest.CaptureFixture[str],
) -> None:
  originals = _fingerprints(late_dataset)
  monkeypatch.setattr("sys.stdin", _Terminal("y\n"))
  output = tmp_path / "output"
  _state(late_dataset, output, margin=0.0)
  trimmed = output.with_name(output.name + TRIM_CONFIG.output_suffix)
  assert _fingerprints(late_dataset) == originals
  assert not output.exists()
  report = json.loads((trimmed / "reports" / TRIM_CONFIG.report_name).read_text())
  assert report["window_s"][0] > report["original_span_s"][0]
  assert report["video_decode_preroll_start_s"]
  stdout = capfd.readouterr().out
  assert "absolute video_time=" in stdout
  assert "%" in stdout
  assert "[y/N]" in stdout
  with (
    Recording(schema.layer_rrd(trimmed, schema.BASE, "sess")) as base,
    Recording(schema.layer_rrd(trimmed, schema.ESTIMATE, "sess")) as estimate,
    Recording(
      schema.layer_rrd(trimmed, schema.state_layer(_SCALE, _F_ROW), "sess")
    ) as state,
  ):
    imu = {slot: base.scalars(schema.imu_signal(slot, "gyro"))[0] for slot in _SHIFT}
    span = (min(t[0] for t in imu.values()), max(t[-1] for t in imu.values()))
    for slot in _SHIFT:
      gripper = gripper_state(estimate, slot)
      end = hold(
        gripper[0],
        np.linalg.norm(gripper[1], axis=1),
        np.linalg.norm(gripper[3], axis=1),
        span,
      )
      assert (end - span[0]) / (span[1] - span[0]) <= HOLD_HEAD
    assert base.property("RecordingInfo")["start_time"]
    assert estimate.property(schema.ESTIMATE)["git_sha"]
    assert _frames(state, "left")[3]
  assert schema.layer_rrd(trimmed, schema.VIZ, "sess").exists()


@pytest.mark.parametrize("answer", ["n\n", "\n", "", "maybe\n"])
def test_declined_hold_trim_publishes_nothing(
  late_dataset: Path,
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  answer: str,
) -> None:
  originals = _fingerprints(late_dataset)
  monkeypatch.setattr("sys.stdin", _Terminal(answer))
  with pytest.raises(SystemExit, match="已取消裁剪"):
    _state(late_dataset, tmp_path / "output", margin=0.0)
  assert not list(tmp_path.iterdir())
  assert _fingerprints(late_dataset) == originals


def test_noninteractive_late_hold_requires_explicit_confirmation(
  late_dataset: Path,
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
  with pytest.raises(SystemExit, match="交互终端"):
    _state(late_dataset, tmp_path / "output", margin=0.0)
  assert not list(tmp_path.iterdir())


def test_failed_trimmed_state_publishes_nothing(
  late_dataset: Path,
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  originals = _fingerprints(late_dataset)
  monkeypatch.setattr("sys.stdin", _Terminal("yes\n"))
  with pytest.raises(SystemExit, match="window"):
    _state(late_dataset, tmp_path / "output", margin=100.0)
  assert not list(tmp_path.iterdir())
  assert _fingerprints(late_dataset) == originals


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
