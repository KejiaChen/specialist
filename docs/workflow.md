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
./scripts/process_one_trial.sh '<setup>' '<single|dual>' '<trial_YYMMDD_NNN>' --anchor-ids '<ID,ID,...>' --anchor-size '<anchor_size_m>'
```

- `<setup>` 填采集布置目录名，例如 `pass_pen_01`。
- `--anchor-ids` 填场地固定 AprilTag 的 ID，按逗号分隔，第一项是世界参考锚点。`--anchor-size` 填标签内方形的实际边长，单位为米。
- 末尾加 `--replay` 可在重建后打开 Rerun。之后也可复制脚本打印的回放命令重开结果。需要训练数据时，在首次处理时加 `--state`。
- `--state` 检查到 Hold 结束位置超过前 10% 或最强 Tap 早于最后 20% 时，会先寻找可行的裁剪窗口。找到后显示百分比和原始 `video_time` 秒数，并询问是否同步裁剪各层的开头和尾段。确认后将裁剪清单和训练 state 写入当前试次。回放当前试次时默认打开全段，加 `--trim` 可查看裁剪段。请在交互终端执行，回车默认取消。已有结果可按[处理流程第 6 步](process_and_reconstruct.md#6-生成训练-state)单独生成 state。
- 结果自动写到外置盘上的 `datasets/<setup>/<mode>/processed/<trial>/`。终端会输出结果位置或失败阶段和日志位置。

用 `./scripts/process_one_trial.sh --help` 查看参数。每个阶段做什么、生成哪些文件和怎么排查失败，见[处理流程说明](process_and_reconstruct.md)。

### 示例：处理双机试次 `pass_pen_01`

```bash
./scripts/process_one_trial.sh pass_pen_01 dual trial_261120_002 --anchor-ids 0,1 --anchor-size 0.100
```

此例使用尚未迁移的旧数据集。原始录像位于 `datasets/pass_pen_01/dual/trial_261120_002/`，处理结果会写到 `datasets/pass_pen_01/processed/dual/trial_261120_002/`。

## 单机 X3 与 Vicon 批处理

将每条试次的两份 X3 原片放在 `single/raw/x3/trial_YYMMDD_NNN/`，将同日期、同试次号的 CSV 放在 `single/raw/vicon/`。从项目根目录用 Bash 依次运行以下命令，替换尖括号占位符。

```bash
# 1. 对齐 X3 和 Vicon 的时间
bash scripts/pair_x3_vicon.sh --dataset 'datasets/<任务名>' --trials '<NNN>' \
  --anchor-ids '<ID,ID,...>' --anchor-size '<anchor_size_m>'

# 2. 逐条重建 X3 轨迹并生成训练 state
bash scripts/reconstruct_and_calibrate.sh --dataset 'datasets/<任务名>' --trials '<NNN>' \
  --anchor-ids '<ID,ID,...>' --anchor-size '<anchor_size_m>'
```

- `--dataset` 填含有 `single/` 的任务根目录。`--trials` 填三位试次号，可用空格列出多个，如 `048 052 073`，也可用 Bash 范围 `{048..100}`。重建时省略该参数会处理配对总表中的全部试次。
- `--anchor-ids` 填逗号分隔的锚点 ID，首个为参考锚点。`--anchor-size` 填标签内方形边长，单位为米。重建时两项必填。配对时两项可选，仅在角速度匹配未通过、改用线速度时筛选已有轨迹。`--min-overlap-fraction` 控制配对所需的最低重叠比例，默认 `0.5`。
- 重建会自动确认可行的 state 裁剪。需要评估位置误差时，在第二条命令末尾加 `--calibration NNN`，用所选的一条试次拟合相机到 Vicon 刚体的杆臂。标定成功后，用同一杆臂验证其余配对通过且有可用轨迹和 state 的试次。

配对状态见 `single/pairing/summary.csv`，匹配判据和重跑方法见[时间配对说明](temporal_pairing_with_vicon.md)。

最近完成批次的重建清单见 `single/reconstruction/reconstruction.csv`，最近一次评分见同目录的 `summary.csv`。轨迹复用规则和误差指标见[批量重建与标定说明](reconstruct_and_compare_with_vicon.md)。
