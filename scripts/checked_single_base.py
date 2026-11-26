"""Print the path of a single-camera base after checking its source and recording ID."""

import hashlib
import sys
from pathlib import Path

from rerun.chunk import RrdReader
from specialist_dataset import schema


def checked_base(folder: Path, front: Path, rear: Path) -> Path:
  bases = list((folder / "base").glob("*.rrd"))
  if len(bases) != 1:
    raise SystemExit(f"应有唯一的 base RRD：{folder / 'base'}")
  base = bases[0]
  reader = RrdReader(str(base))
  if [r.recording_id for r in reader.recordings()] != [base.stem]:
    raise SystemExit(f"base 的会话 ID 不匹配：{base}")
  camera = "/" + schema.camera("cam00").strip("/")
  fields = {}
  for chunk in reader.store().stream().filter(content=camera, is_static=True).to_chunks():
    fields.update(chunk.to_record_batch().to_pydict())
  if fields.get("source") != [[front.name]]:
    raise SystemExit(f"base 的原始镜头文件名不匹配：{front.name}")

  checks = folder / "reports/raw_sha256.txt"
  if not checks.exists():
    checks = base.parent / "raw_sha256.txt"
  if checks.exists():
    digests = {}
    for line in checks.read_text().splitlines():
      digest, path = line.split(maxsplit=1)
      digests[Path(path).name] = digest
    for raw in (front, rear):
      with raw.open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != digests.get(raw.name):
          raise SystemExit(f"原片校验不符：{raw.name}")
  return base


if __name__ == "__main__":
  folder, front, rear = map(Path, sys.argv[1:])
  print(checked_base(folder, front, rear))
