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

初次安装环境或锁定的依赖更新后，如果同级目录有 `specialist_description` 仓库，运行以下命令。脚本按本项目锁文件指定的版本从该本地仓库安装夹爪模型。有 GitHub SSH 访问权限时，也可以直接执行 `uv sync --locked --python 3.14`。

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

每个任务数据集按采集模式分为 `single/` 和 `dual/`。两种模式使用相同的四个子目录。下面以 11 月 25 日的任务为例。

```text
pick_and_place_261125/
├── single/
│   ├── raw/
│   │   ├── x3/trial_261125_001/       # 同一台 X3 的两份 INSV
│   │   └── vicon/                  # 原始 Vicon CSV
│   ├── processed/
│   │   └── trial_261125_001/        # 每条试次一个目录
│   │       ├── base/、estimate/、viz/           # 完整重建层
│   │       ├── assets/、blueprints/             # 回放资源
│   │       ├── reports/                         # 重建及 state 运行日志
│   │       ├── state_s8_r100/                   # 成功生成的训练 state
│   │       └── state_trim_reports/hold_trim.json # 旧版试次可能留有此清单
│   ├── pairing/                    # 最新 summary.csv、motive_csv、paired_mocap
│   └── reconstruction/
│       ├── run_*/                  # 新运行直接写在这里，含本批明细
│       ├── history/                # 手动归档的运行
│       ├── latest                  # 指向最近发布的运行目录
│       ├── reconstruction.csv      # 各试次最新的重建清单
│       └── summary.csv             # 各试次的累计评分表
└── dual/
    ├── raw/                        # 有双机数据时用 trial_YYMMDD_NNN/{right,left}/
    ├── processed/
    ├── pairing/
    └── reconstruction/
```

- **采集归档**按模式和日期编号。每台 X3 的 `_00_` 和 `_10_` 文件放在同一目录。当前 Vicon 配对和批量标定脚本处理单机数据，`--dataset` 传任务根目录，如 `datasets/pick_and_place_261125`。[采集说明](data_collection.doc)
- **单条产物**在 `<模式>/processed/<trial>/`，训练 state 在该试次的 `state_s8_r100/<会话>.rrd`。更换锚点后的备用结果在 `single/processed/anchors_<配置>/<trial>/`。
- **存储复用**：配对从 `processed/` 读取完整的 `base`，角速度匹配失败时查找已有 `estimate`。批量重建复用相同配置的轨迹和 state，标定读取轨迹和 state 时间窗。备用锚点的完整 base 和标定运行的 Vicon 输入优先使用硬链接，同一文件在多个目录中可见但只占一份磁盘空间。裁剪回放时从完整重建临时生成视频及轨迹，沿用原始视频时间，退出查看器后清理。[回放命令](process_and_reconstruct.md#7-回放)重跑配对时，字节相同的 CSV 和 RRD 继续使用现有文件。
- **历史记录**在本批的 `single/pairing/history/`，原先的四个配对尝试目录按原名保留。迁移前的日志记录的是当时的路径；查看跨批次重建与评分总表用 `single/reconstruction/{reconstruction,summary}.csv`，单次结果看对应的 `run_*/`。
- **旧数据集**如 `pass_pen_01` 仍可按原目录处理。新任务的单机原片进入 `single/raw/x3/`，早期放在 `single/raw/trial_…/` 的数据也可读取。若该目录不存在且试次直接位于模式目录下，则使用旧布局。

创建新任务时，在 Bash 中把任务名换成实际名称：

```bash
task="$PWD/datasets/<新任务名>"
mkdir -p -- "$task"/{single,dual}/{raw,processed,pairing,reconstruction}
mkdir -p -- "$task/single/raw"/{x3,vicon}
```

## 处理并回放一条 trial

确认 `datasets` 指向已挂载且可写的外置盘。

回到项目根目录运行，替换尖括号占位符。脚本通过 `uv run --no-sync` 使用项目的虚拟环境。

```bash
./scripts/process_one_trial.sh '<setup>' '<single|dual>' '<trial_YYMMDD_NNN>' --anchor-ids '<ID,ID,...>' --anchor-size '<anchor_size_m>'
```

- `<setup>` 填采集布置目录名，例如 `pass_pen_01`。
- `--anchor-ids` 填场地固定 AprilTag 的 ID，按逗号分隔，第一项是世界参考锚点。`--anchor-size` 填标签内方形的实际边长，单位为米。
- 末尾加 `--replay` 可在重建后打开 Rerun。之后也可复制脚本打印的回放命令重开结果。需要训练数据时，在首次处理时加 `--state`。
- `--state` 在原片上检测一次 Hold 和 Tap。Hold 结束晚于原片 20% 或 Tap 首峰早于 80% 时输出警告并继续。已有轨迹可按[处理流程第 6 步](process_and_reconstruct.md#6-生成训练-state)生成 state。
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
- 需要评估位置误差时，在第二条命令末尾加 `--calibration NNN`，用所选的一条试次拟合相机到 Vicon 刚体的杆臂。标定成功后，用同一杆臂验证其余配对通过且有可用轨迹和 state 的试次。

配对状态见 `single/pairing/summary.csv`，匹配判据和重跑方法见[时间配对说明](temporal_pairing_with_vicon.md)。

跨批次重建清单见 `single/reconstruction/reconstruction.csv`，累计评分见同目录的 `summary.csv`。新试次追加，重跑按试次号更新。轨迹复用规则和误差指标见[批量重建与标定说明](reconstruct_and_compare_with_vicon.md)。

### 示例：用试次 048 标定，验证 049 和 050

本例处理 `pick_and_place_261125` 的三条单机试次。场地锚点为 0、1、2，参考锚点为 0，标签内方形边长为 0.100 米。

```bash
ds="$PWD/datasets/pick_and_place_261125"
bash scripts/pair_x3_vicon.sh --dataset "$ds" --trials {049..100} \
  --anchor-ids 0,1,2 --anchor-size 0.100

bash scripts/reconstruct_and_calibrate.sh --dataset "$ds" --trials {049..100} \
  --anchor-ids 0,1,2 --anchor-size 0.100 --calibration 048
```
