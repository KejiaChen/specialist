"""Prepare the recorded crop in a temporary directory owned by the replay caller."""

import argparse
from pathlib import Path

from specialist_dataset import schema
from specialist_dataset.trim import materialize_layers, read_trim_plan


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("dataset", type=Path)
  parser.add_argument("temporary_dir", type=Path)
  parser.add_argument("--session")
  args = parser.parse_args()
  sources, window = read_trim_plan(args.dataset)
  session = sources[schema.BASE].stem
  if args.session and args.session != session:
    parser.error(f"裁剪清单会话为 {session}，请求的会话为 {args.session}")
  if schema.VIZ not in sources:
    parser.error("回放需要完整重建的 viz 层")
  materialize_layers(sources, args.temporary_dir, window, session)
  for name in ("blueprints", "assets"):
    (args.temporary_dir / name).symlink_to(
      sources[schema.ESTIMATE].parent.parent / name, target_is_directory=True
    )


if __name__ == "__main__":
  main()
