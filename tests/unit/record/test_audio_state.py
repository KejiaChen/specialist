"""Exercise the state-only batch step with an X3-style marker track and RRD layers."""

import subprocess
from pathlib import Path

import numpy as np
import pytest
from specialist_dataset import schema
from specialist_dataset.read import Recording

from specialist.loader.audio_markers import video_marker_window
from specialist.loader.audio_sync import resolve_ffmpeg
from specialist.record.asset import resolve_urdf_path
from specialist.record.base import capture_chunk, write_base_recording
from specialist.record.video import video_chunks
from tests.record_fixtures import (
  camera_estimate,
  encode_clip,
  synthetic_insv,
  write_run,
)


def marker_movie(folder: Path, events: tuple[tuple[int, float], ...]) -> Path:
  clip = encode_clip(folder / "video.mp4", 90)
  source = folder / "VID_20261127_133232_00_196.insv"
  pcm = np.zeros(3 * 8000, dtype="<i2")
  t = np.arange(2800) / 8000
  for frequency, start in events:
    i = round(start * 8000)
    pcm[i : i + len(t)] = 8000 * np.sin(2 * np.pi * frequency * t)
  subprocess.run(
    [
      resolve_ffmpeg(),
      "-v",
      "error",
      "-i",
      str(clip),
      "-f",
      "s16le",
      "-ar",
      "8000",
      "-ac",
      "1",
      "-i",
      "pipe:0",
      "-c:v",
      "copy",
      "-c:a",
      "aac",
      "-f",
      "mp4",
      str(source),
    ],
    input=pcm.tobytes(),
    check=True,
  )
  return source


@pytest.mark.parametrize(
  "frequencies", [(1000, 1000), (1600, 1200, 1000, 1000), (1000, 1000, 1000, 1000)]
)
def test_refuses_incomplete_reversed_or_repeated_markers(tmp_path, frequencies):
  source = marker_movie(
    tmp_path, tuple((f, 0.1 + i * 0.6) for i, f in enumerate(frequencies))
  )
  with pytest.raises(ValueError, match="需要唯一一组"):
    video_marker_window(source)


def test_batch_builds_and_reuses_audio_state(tmp_path, monkeypatch):
  scripts = Path(__file__).resolve().parents[3] / "scripts"
  monkeypatch.syspath_prepend(str(scripts))
  from reconstruct_and_calibrate import prepare_state, state_result

  raw = tmp_path / "raw"
  raw.mkdir()
  source = marker_movie(raw, ((1000, 0.1), (1000, 0.7), (1600, 1.7), (1200, 2.3)))
  root = tmp_path / "processed"
  base = root / "base/sess.rrd"
  base.parent.mkdir(parents=True)
  write_base_recording(
    base,
    "sess",
    [video_chunks(raw / "video.mp4", schema.video("cam00", "rear"), 0, (0, 3))],
    [capture_chunk("cam00", synthetic_insv(), source.name, 0.0, float("inf"))],
  )
  camera = camera_estimate("cam00", rate=10000)
  camera.keyframe_times *= 10
  camera.highrate_times *= 10
  estimate, _ = write_run(
    root, "sess", [camera], urdf_path=resolve_urdf_path(None), frame_period=1 / 30
  )
  with (tmp_path / "log").open("a") as log:
    status, path, window = prepare_state(raw, estimate, False, log, "audio")
    assert status == "built"
    assert window == pytest.approx((1.05, 1.7), abs=0.025)
    assert path == root / "audio_state/state_s8_r100/sess.rrd"
    with Recording(path) as state:
      assert state.property(schema.STATE)["audio_source"] == [source.name]
    assert state_result(estimate) is None
    assert prepare_state(raw, estimate, True, log, "audio") == ("reused", path, window)
    path.write_bytes(b"invalid old state")
    assert prepare_state(raw, estimate, True, log, "audio", overwrite=True) == (
      "built",
      path,
      window,
    )
