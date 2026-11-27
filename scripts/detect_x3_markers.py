"""Standalone entry point for the Specialist X3 audio marker detector."""

from specialist.loader.audio_markers import (
  detect_activity,
  find_markers,
  main,
  short_pulses,
)

__all__ = ["detect_activity", "find_markers", "short_pulses"]

if __name__ == "__main__":
  main()
