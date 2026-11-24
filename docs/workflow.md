# Specialist 使用指南

在能访问外置盘和 NVIDIA GPU 的 Linux 宿主机上使用。桌面回放还需要图形会话。

## 使用环境

从终端进入 `specialist` 仓库，并确认 `uv`、`ffprobe` 和 GPU 可用。下文的命令都从这个目录执行。

```bash
cd ~/specialist-reconstruction/specialist
uv --version
command -v ffprobe
nvidia-smi -L
```

如果终端找不到已安装的 `uv`，可运行 `source "$HOME/.local/bin/env"` 加载其路径。

初次安装环境或锁定的依赖更新后，如果同级目录有 `specialist_description` 仓库，运行以下命令。脚本从该仓库读取锁定的夹爪模型版本。有 GitHub SSH 访问权限时，也可以直接执行 `uv sync --locked --python 3.14`。

```bash
./scripts/sync-env.sh
```

要在当前终端直接使用项目的 Python，先激活虚拟环境。

```bash
source .venv/bin/activate
```

激活后，可以检查 MEI、夹爪模型和 GPU 求解环境。检查通过会更新同级目录中的 `../agent_logs/environment-check.json`。

```bash
python scripts/check_environment.py
```

本机的回放使用 Python 依赖 `imageio-ffmpeg` 附带的 FFmpeg。如果新检出仓库的系统 FFmpeg 无法解码回放视频，安装环境后可将随包提供的二进制复制到脚本优先查找的路径。

```bash
ffmpeg_path=$(.venv/bin/python -c 'from imageio_ffmpeg import get_ffmpeg_exe; print(get_ffmpeg_exe())')
install -Dm755 -- "$ffmpeg_path" tools/ffmpeg/bin/ffmpeg
```

## 数据存放结构

仓库里的 `datasets` 是指向外置盘 `specialist_data/` 的本地软链接。首次配置时，把下面的挂载路径替换成实际位置。

```bash
ln -s /media/umi/bd261add-2cbf-47e8-9592-bd5aca3d1314/specialist_data datasets
```

同一任务更换采集布置时另建目录。下面是两套当前使用的布置，`pick_cup_01` 为双机，`pick_cup_02` 为单机。

```text
datasets/
├── pick_cup_01/
│   ├── dual/
│   │   └── trial_261116_001/
│   │       ├── left/
│   │       └── right/
│   └── processed/
│       └── recollect_20261116_1720/
└── pick_cup_02/
    ├── single/
    │   ├── trial_261117_001/
    │   └── trial_261117_002/
    └── processed/
        └── single_hand_201117/
```

- **试次命名**采用 `trial_YYMMDD_NNN`，例如 `trial_261116_001` 表示 2026 年 11 月 16 日的第 001 条。同一布置的 `single` 和 `dual` 各自按采集日期编号。
- **原始录像**放在 `single/<trial>/` 或 `dual/<trial>/{left,right}/`。每台相机的 `_00_` 和 `_10_` INSV 保留在同一目录。 [采集与存放说明](data_collection.doc)
- **新处理结果**由脚本写到同一采集布置的 `processed/<mode>/<trial>/`。树中的 `recollect_20261116_1720/` 和 `single_hand_201117/` 是已有的处理批次。

## 处理并回放一条 trial

确认 `datasets` 指向已挂载且可写的外置盘。

回到项目根目录运行，替换尖括号占位符。脚本通过 `uv run --no-sync` 使用项目的虚拟环境。

```bash
./scripts/process-trial.sh '<setup>' '<single|dual>' '<trial_YYMMDD_NNN>' --anchor-ids '<ID,ID,...>' --anchor-size '<anchor_size_m>'
```

- `<setup>` 填采集布置目录名，例如 `pick_cup_01`。
- `--anchor-ids` 填场地固定 AprilTag 的 ID，按逗号分隔，第一项是世界参考锚点。`--anchor-size` 填标签内方形的实际边长，单位为米。
- 末尾加 `--replay` 可在重建后打开 Rerun。之后也可复制脚本打印的回放命令重开结果。需要训练数据时，在首次处理时加 `--state`。
- `--state` 检查到 Hold 结束位置超过前 10% 时，会显示百分比和原始 `video_time` 秒数，并询问是否同步裁剪整个重建结果。确认后生成同级的 `<trial>_hold_trim/` 副本及训练 state；原始结果保留。请在交互终端执行，回车默认取消。已有结果可按[处理流程第 6 步](process_and_reconstruct.md#6-生成训练-state)单独生成 state。
- 结果自动写到外置盘上的 `datasets/<setup>/processed/<mode>/<trial>/`。终端会输出结果位置或失败阶段和日志位置。

用 `./scripts/process-trial.sh --help` 查看参数。每个阶段做什么、生成哪些文件和怎么排查失败，见[处理流程说明](process_and_reconstruct.md)。

### 示例：处理 dual arm `pick_cup_01` 

```bash
./scripts/process-trial.sh pick_cup_01 dual trial_261116_001 --anchor-ids 0,1 --anchor-size 0.100
```

原始录像位于 `datasets/pick_cup_01/dual/trial_261116_001/`，处理结果会写到 `datasets/pick_cup_01/processed/dual/trial_261116_001/`。
