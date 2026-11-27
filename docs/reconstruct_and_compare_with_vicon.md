# 单机 X3 批量重建与 Vicon 验证

用同一条命令逐条重建 X3 轨迹并生成训练 state。要验证轨迹的位置误差，可指定一条标定试次，拟合相机与 Vicon 刚体之间的固定位置偏移（杆臂），再用这组杆臂验证其他试次。[批处理脚本](../scripts/reconstruct_and_calibrate.sh)

## 使用方法

先完成时间配对，使所选试次都在配对目录的 `summary.csv` 中有记录。配对状态决定试次能否参与 Vicon 评估，所有选中的试次都会尝试重建。单机原片放在任务目录的 `single/raw/x3/trial_YYMMDD_NNN/`。[时间对齐指南](temporal_pairing_with_vicon.md)

从项目根目录运行，替换尖括号中的任务名、试次号和现场锚点参数。

```bash
bash scripts/reconstruct_and_calibrate.sh \
  --dataset 'datasets/<任务名>' --trials '<NNN>' \
  --anchor-ids '<ID,ID,...>' --anchor-size '<anchor_size_m>'
```

要做跨试次验证，在同一条命令中选入标定和验证试次，并用 `--calibration` 指定其中一条。各试次应保持相同的相机与刚体安装关系。例如，以下命令选择三条试次，用第一条标定。

```bash
bash scripts/reconstruct_and_calibrate.sh \
  --dataset 'datasets/<任务名>' --trials 001 002 003 \
  --anchor-ids '<ID,ID,...>' --anchor-size '<anchor_size_m>' \
  --calibration 001
```

录制时使用双音标记的试次，在同一条命令中加上 `--state-boundary audio`。程序读取单机 `_00_` 原片，在第二声 1000 Hz 结束时开始，在 1600 Hz → 1200 Hz 的第一声响起时结束。支持 0.55 秒和 0.95 秒两个版本。每条试次需要一组顺序正确的开始音和结束音。缺失、重复或顺序错误时，该试次记为 `state_error`，候选时间可在日志中查看。

现场播放用的 0.95 秒 WAV 文件在 [开始音](../assets/audio/x3-start-1000hz-950ms.wav) 和 [结束音](../assets/audio/x3-end-1600-1200hz-950ms.wav)。只检查某段原片里的标记时，可在项目根目录运行：

```bash
.venv/bin/python scripts/detect_x3_markers.py '<原片目录>/VID_..._00_196.insv'
```

音频模式直接取标记窗口与各训练数据流的共同覆盖区间，使用原视频时间轴。对应的 state 保存在试次目录下的 `audio_state/state_s8_r100/`。切换模式可复用同一条轨迹，重跑时按所选模式复用 state。Vicon 标定和验证使用该模式实际写出的训练时间窗。

## 参数

| 参数 | 说明 |
|---|---|
| `--dataset 'datasets/<任务名>'` | 必填。传入包含 `single/` 的任务根目录。 |
| `--anchor-ids '<ID,ID,...>'` | 必填。填写互不重复的非负整数，用逗号分隔。首个 ID 是参考锚点。 |
| `--anchor-size '<anchor_size_m>'` | 必填。锚点标签内方形的实际边长，单位为米。 |
| `--trials '<NNN>'` | 可选。传入一个或多个三位试次号，也可使用 Bash 范围，如 `{001..010}`。省略时处理配对总表中的全部试次。 |
| `--state-boundary hold-tap\|audio` | 训练窗口的标记方式，默认 `hold-tap`。同一批次采用同一种方式。 |
| `--overwrite-state` | 复用匹配的轨迹，重新生成所选模式的 state。成功后替换旧文件。配合 `--calibration` 可用新时间窗重新评分，生成失败的试次记录失败状态。 |
| `--calibration '<NNN>'` | 可选。指定所选试次中的一条用于拟合杆臂。省略时只执行重建和 state 生成。 |
| `--pairing-dir '<配对目录>'` | 默认读取任务下的 `single/pairing/`。若配对时自定义了输出目录，在这里传入同一路径。 |
| `--output-dir '<输出目录>'` | 批次清单、日志和评分的输出目录，默认为 `single/reconstruction/`。各试次的重建轨迹和 state 存放在 `single/processed/`。 |

如果输出目录已有 `exclusions.csv`，其中 `status=excluded` 的试次仍处理 state，评分表记为 `excluded`。

## 程序框图

```mermaid
flowchart TD
    A["读取配对总表，选择试次"] --> B["逐条复用或重建 X3 轨迹"]
    B -->|重建成功| C{"已有可复用的训练 state？"}
    C -->|有| R["读取 state 的实际起止时间"]
    C -->|无| Mode{"state 边界模式"}
    Mode -->|audio| Audio["在原片音轨中查找唯一一组开始与结束双音"]
    Audio -->|有效| AW["用标记与各数据流的共同区间生成 state"]
    Audio -->|缺失或歧义| X
    AW -->|成功| R
    AW -->|生成失败| X
    Mode -->|hold-tap| Hold["选 Hold：在夹爪速度 ≤ 0.15 m/s 的采样点中<br/>找到距锚点最近的一点，取其所在的连续低速段（至少 0.4 秒）"]
    Hold --> Tap["选 Tap：合并间隔 < 0.08 秒的相邻候选峰<br/>将敲击按相邻间隔 ≤ 0.7 秒分组<br/>同机计入的敲击至少相隔 0.20 秒<br/>取最后一组至少三次敲击的首峰作为 Tap 时刻"]
    Tap --> W["Hold 结束晚于原片 20% 或 Tap 首峰早于 80% 时记录警告<br/>从 Hold 结束后 1 秒到首次敲击开始前 1 秒取各流共同区间"]
    Hold -.无有效低速段.-> X["记为 state_error，保留已重建的轨迹"]
    Tap -.无连续三击组.-> X
    W -->|成功| R
    W -->|生成失败| X
    R --> D["汇总各试次的重建状态、state 状态和可用训练时间窗"]
    X --> D
    B -->|重建失败| D
    D --> E{"已指定标定试次且其配对通过？"}
    E -->|否| F["发布重建清单"]
    E -->|是| G["在标定试次的训练时间窗内拟合杆臂"]
    G --> H{"取得有效标定？"}
    H -->|否| I["在评分表记录原因，跳过跨试次验证"]
    H -->|是| J["固定杆臂，验证其余配对通过且有轨迹和 state 的试次"]
    I --> K["发布重建清单和评分表"]
    J --> K
```

## 输出与重跑

先看输出目录下的 `reconstruction.csv`。每条试次一行，`built` 表示本次生成，`reused` 表示复用已有结果。`estimate` 和 `state_rrd` 给出实际文件路径，`state_start_s` 和 `state_end_s` 给出训练 state 在原视频时间轴上的起止秒数。

| 路径（相对于输出目录） | 用途 |
|---|---|
| `reconstruction.csv` | 累计重建清单。每次发布更新本次试次的整行，新试次按编号追加，已有试次保留行位置。遇到 `reconstruction_error` 或 `state_error` 时，查看该次运行的 `logs/<NNN>_reconstruction.log`。 |
| `summary.csv` | 累计评分表。重新评分会覆盖该试次整行，包括失败状态。只做重建的试次保留上次评分；从未评分的记为 `not_scored`，误差留空。可用 `calibration_trial` 核对各行采用的标定试次。 |
| `run_*/`、`latest/` | 新运行直接写在输出根目录，参数、日志、重建清单和可选评分表均只属于本批。`latest/` 指向最近一次已发布的运行。 |
| `run_*/calibration.json` | 成功标定时生成。`lever_m` 是用于验证的三维杆臂，单位为米。 |
| `run_*/scores/`、`run_*/paired_mocap/` | 保存该次评分产生的逐帧误差文件，以及供复核的已对时 Vicon 轨迹。 |

可自行将已完成的 `run_*/` 移入 `history/`，根目录两张总表仍可直接读取。如果移动的是 `latest` 指向的运行，在输出根目录执行 `ln -sfnT "history/$run_name" latest`（`run_name` 为移入的目录名）。

**读评分表。** 查看跨试次精度时，选取 `stage=holdout` 且 `status=pass` 的行。`candidate` 行给出标定试次自身的拟合残差。每条试次只统计其训练 state 时间窗内有 Vicon 覆盖的关键帧。`full_frames` 是有效帧数，`full_p50_mm`、`full_p90_mm` 和 `full_p99_mm` 分别是位置误差的第 50、90 和 99 百分位数，单位为毫米。`scale_rejected` 表示尺度检查未通过。

要查看误差曲线，在项目根目录将所选评分运行的路径传给绘图脚本。图像写入该运行目录的 `keyframe_errors.png`。

```bash
.venv/bin/python ../vicon/processing/plot_batch_errors.py '<输出目录>/<评分运行目录>'
```

**重跑或更换配置。** 使用相同锚点参数重跑时，脚本复用同会话的完整重建和可用 state。已有轨迹而缺 state 时，只补做 state。Hold/Tap 的时序警告可在本次试次日志中查看。若生成失败，轨迹仍可复用，原因见试次目录的 `reports/state.log`。更换锚点且主目录已有其他配置时，新轨迹写入 `single/processed/anchors_<配置>/<试次>/`。只更换 `--calibration` 时，脚本复用匹配的重建和 state，用新的标定试次重新拟合并验证。
