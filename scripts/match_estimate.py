"""Check the recording and anchor configuration of an existing X3 trajectory."""

import math
import sys
from pathlib import Path

from rerun.chunk import RrdReader


def recording(path: Path, session: str) -> RrdReader:
  reader = RrdReader(str(path))
  if [r.recording_id for r in reader.recordings()] != [session]:
    raise ValueError(f"RRD 会话 ID 与 base 不匹配：{path}")
  return reader


def matches_anchors(path: Path, reader: RrdReader, ids: str, size: str) -> bool:
  if not (ids or size):
    return True
  report = path.parent.parent / "reports/reconstruction_anchors.txt"
  if report.is_file():
    saved_ids, saved_size = report.read_text().split()
    tags = list(map(int, saved_ids.split(",")))
    refs, sizes = [tags[0]], [float(saved_size)]
  else:
    store = reader.store()
    tags, refs, sizes = [], [], []
    for entity in store.schema().entity_paths():
      if str(entity).rpartition("/")[0] != "/world/anchor":
        continue
      data = {}
      for chunk in (
        store.stream().filter(content=str(entity), is_static=True).to_chunks()
      ):
        data.update(chunk.to_record_batch().to_pydict())
      tag = data["tag_id"][0][0]
      tags.append(tag)
      if data["is_reference"][0][0]:
        refs.append(tag)
      sizes.append(data["tag_size"][0][0])
  if ids:
    wanted = list(map(int, ids.split(",")))
    if set(tags) != set(wanted) or refs != [wanted[0]]:
      return False
  return not size or bool(
    sizes and all(math.isclose(v, float(size), rel_tol=1e-6) for v in sizes)
  )


if __name__ == "__main__":
  ids, size, session, *paths = sys.argv[1:]
  for path in map(Path, paths):
    if path.exists():
      if matches_anchors(path, recording(path, session), ids, size):
        print(path)
        break
      print(f"跳过锚点配置不符的轨迹：{path}", file=sys.stderr)
