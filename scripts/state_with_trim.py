"""Compatibility entry point for writing state from the original session."""

import sys
from pathlib import Path

import tyro

if __package__ in (None, ""):
  sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.state import Args, main

if __name__ == "__main__":
  main(tyro.cli(Args))
