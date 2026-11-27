"""Print X3 marker candidates in seconds from the start of the decoded audio."""

import argparse
import json
import subprocess
from pathlib import Path
from typing import Literal, TypedDict

import numpy as np
import numpy.typing as npt

SAMPLE_RATE = 8000
FRAME = 160  # 20 ms, giving FFT bins spaced 50 Hz apart.
START_HZ = 1000
END_HIGH_HZ = 1600
END_LOW_HZ = 1200
FREQUENCIES = (START_HZ, END_HIGH_HZ, END_LOW_HZ)


class Marker(TypedDict):
  type: Literal["start", "end"]
  time_s: float


def detect_activity(path: str | Path, ffmpeg: str = "ffmpeg") -> npt.NDArray[np.bool_]:
  command = [
    ffmpeg,
    "-nostdin",
    "-v",
    "error",
    "-i",
    path,
    "-map",
    "0:a:0",
    "-ac",
    "1",
    "-ar",
    str(SAMPLE_RATE),
    "-f",
    "s16le",
    "pipe:1",
  ]
  window = np.hanning(FRAME)
  chunks = []
  with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
    assert process.stdout is not None
    while raw := process.stdout.read(FRAME * 2 * 500):
      samples = np.frombuffer(raw, dtype="<i2")
      frames = len(samples) // FRAME
      if not frames:
        break
      data = samples[: frames * FRAME].reshape(frames, FRAME)
      power = np.abs(np.fft.rfft(data * window, axis=1)) ** 2
      flags = []
      for freq in FREQUENCIES:
        k = freq * FRAME // SAMPLE_RATE
        peak = power[:, k]
        nearby = power[:, [k - 5, k - 4, k - 3, k - 2, k + 2, k + 3, k + 4, k + 5]]
        floor = np.maximum(np.median(nearby, axis=1), FRAME)
        centered = peak > 1.4 * np.maximum(power[:, k - 1], power[:, k + 1])
        flags.append(np.where(centered & (peak > 10 * floor), peak, 0.0))
      chunks.append(np.column_stack(flags))
    if process.wait():
      raise ValueError("FFmpeg could not decode the audio stream.")
  if not chunks:
    raise ValueError("Empty audio stream.")
  tone_power = np.concatenate(chunks)
  # A quiet reverberant tail can still dominate its frequency bin. Use a
  # local reference so changes in playback volume between markers are fine.
  padded = np.pad(tone_power, ((10, 10), (0, 0)), mode="edge")
  neighborhoods = np.lib.stride_tricks.sliding_window_view(padded, 21, axis=0)
  active: npt.NDArray[np.bool_] = tone_power > neighborhoods.max(axis=-1) * 0.063
  return active  # 12 dB below local peak


def short_pulses(
  active: npt.NDArray[np.bool_], frequency: int, pulse_frames: range, max_dropout: int
) -> list[tuple[int, int, int]]:
  # Bridge a single noisy frame inside a tone.
  active = active.copy()
  active[1:-1] |= active[:-2] & active[2:]
  if max_dropout == 2:
    # The longer pulse can also tolerate a 40 ms dropout inside the tone.
    bounded = active[:-3] & active[3:]
    active[1:-2] |= bounded
    active[2:-1] |= bounded
  edges = np.flatnonzero(np.diff(active.astype(int), prepend=0, append=0))
  return [
    (int(start), int(end), frequency)
    for start, end in edges.reshape(-1, 2)
    if end - start in pulse_frames
  ]


def find_markers(activity: npt.NDArray[np.bool_]) -> list[Marker]:
  markers: list[Marker] = []
  # Pulse and gap lengths in 20 ms frames, for the 0.55 s and 0.95 s markers.
  profiles = [(range(7, 14), range(5, 11), 1), (range(14, 22), range(9, 17), 2)]
  for pulse_frames, gap_frames, max_dropout in profiles:
    pulses = {
      hz: short_pulses(activity[:, i], hz, pulse_frames, max_dropout)
      for i, hz in enumerate(FREQUENCIES)
    }
    for kind, first_hz, second_hz in (
      ("start", START_HZ, START_HZ),
      ("end", END_HIGH_HZ, END_LOW_HZ),
    ):
      candidates = pulses[first_hz]
      if first_hz != second_hz:
        candidates = sorted(candidates + pulses[second_hz])
      for first, second in zip(candidates, candidates[1:], strict=False):
        start1, end1, hz1 = first
        start2, end2, hz2 = second
        if (hz1, hz2) == (first_hz, second_hz) and (start2 - end1) in gap_frames:
          boundary = end2 if kind == "start" else start1
          markers.append(
            {
              "type": "start" if kind == "start" else "end",
              "time_s": boundary * FRAME / SAMPLE_RATE,
            }
          )
  return sorted(markers, key=lambda marker: marker["time_s"])


def video_marker_window(source: Path) -> tuple[float, float]:
  """One unambiguous marker pair, in seconds relative to the first video frame."""
  import av

  from specialist.loader.audio_sync import resolve_ffmpeg

  with av.open(str(source)) as media:
    if not media.streams.audio or not media.streams.video:
      raise ValueError(f"{source}: 需要带音轨的原始录像")
    audio, video = media.streams.audio[0], media.streams.video[0]
    if (
      audio.start_time is None
      or audio.time_base is None
      or video.start_time is None
      or video.time_base is None
    ):
      raise ValueError(f"{source}: 缺少音视频时间戳")
    offset = float(
      audio.start_time * audio.time_base - video.start_time * video.time_base
    )
  markers = find_markers(detect_activity(source, resolve_ffmpeg()))
  if [m["type"] for m in markers] != ["start", "end"]:
    raise ValueError(
      f"{source}: 需要唯一一组先开始后结束的音频标记，检测结果 {markers}"
    )
  start, end = (m["time_s"] + offset for m in markers)
  if end <= start:
    raise ValueError(f"{source}: 音频标记窗口为空")
  return start, end


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
    "recording", help="Video or audio file containing the recorded markers"
  )
  args = parser.parse_args()
  try:
    print(json.dumps(find_markers(detect_activity(args.recording)), indent=2))
  except (OSError, ValueError) as exc:
    parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
  main()
