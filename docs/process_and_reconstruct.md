# Specialist 处理流程

本文按 `scripts/process_one_trial.sh` 的执行顺序说明每一步，并给出可单独运行的命令。手动处理时先设置第 1 步的变量，再跳到所需阶段。完整流程的入口和环境配置见[使用指南](workflow.md)。

已有重建结果，可直接跳到[第 7 步回放](#7-回放)。

## 1. 解析参数并定位数据

脚本读取 setup、模式、试次和锚点参数，检查外置盘与输出路径，锁定当前试次，再为每台相机查找 `_00_` 和 `_10_` 配对。通过后在外置盘创建暂存目录。 [参数解析](../scripts/process_one_trial.sh#L35) · [路径检查](../scripts/process_one_trial.sh#L68) · [配对](../scripts/process_one_trial.sh#L97) · [暂存](../scripts/process_one_trial.sh#L155)

单独执行后续命令时，在同一个 Bash 终端设置以下变量。使用已有产物时，把 `dataset` 改成实际绝对路径。写入前确认外置盘已挂载且可写。

```bash
cd ~/specialist-reconstruction/specialist
root="$PWD"
setup='<setup>'
mode='<single|dual>'
trial='<trial_YYMMDD_NNN>'
raw="$root/datasets/$setup/$mode/raw"
if [[ $mode == single ]]; then raw="$raw/x3"; fi
input="$raw/$trial"
dataset="$root/datasets/$setup/$mode/processed/$trial"
export PATH="$root/tools/ffmpeg/bin:$HOME/.local/bin:$PATH"
set -o pipefail
```

只在检查 INSV 或转换时设置 `masters`。下面是单机写法。双机将数组改为两项，依次填入 `"$input/right/<right_00_filename>"` 和 `"$input/left/<left_00_filename>"`。

```bash
masters=("$input/<master_00_filename>")
```

## 2. 检查输入

脚本计算 SHA-256，检查方形 H.264 视频并完整解码。双机还检查两份主文件的音频轨。下方命令将这些信息打印到终端，供单独检查时核对。 [输入检查代码](../scripts/process_one_trial.sh#L172)

```bash
(
  set -e
  for master in "${masters[@]}"; do
    name="${master##*/}"
    rear="${master%/*}/${name/_00_/_10_}"
    for lens in "$master" "$rear"; do
      sha256sum -- "$lens"
      ffprobe -v error -show_entries stream=codec_type,codec_name,width,height \
        -of csv=p=0 "$lens"
      ffmpeg -nostdin -v error -xerror -i "$lens" -map 0:v:0 -f null -
    done
  done
)
```

## 3. 转换与同步

单机试次的输出目录如果只有 `base/`，脚本会校验并复用它，然后计算轨迹。更换锚点并写入备用目录时，也可复用主目录的 base。单独重跑已有完整输出的试次会停止；批量复用已有重建见[重建与标定](reconstruct_and_compare_with_vicon.md)。手动转换时使用第 1 步设置的 `masters`。双机用第一份主文件作为时钟参考，按音频同步后保留共同区间。生成 `base/<session_id>.rrd`，脚本从文件名取得会话 ID。同步出错时看 `convert.log`。 [转换命令](../scripts/process_one_trial.sh#L210)

```bash
mkdir -p -- "$dataset/reports" &&
  TMPDIR="$dataset" uv run --no-sync python scripts/convert.py \
    --input-insvs "${masters[@]}" --output-dir "$dataset" \
    2>&1 | tee -a "$dataset/reports/convert.log"
```

## 4. 重建轨迹与夹爪

把 `base/<session_id>.rrd` 的文件名去掉 `.rrd` 后填入 `session`。按实际场地填写锚点 ID 和内方形边长，单位为米。第一个 ID 是世界参考锚点。生成 `estimate/`、`viz/`、`blueprints/` 和 `assets/`。 [重建命令](../scripts/process_one_trial.sh#L230)

```bash
session='<session_id>'
anchor_ids=('<reference_anchor_id>' '<additional_anchor_id>')
anchor_size='<anchor_size_m>'
mkdir -p -- "$dataset/reports" &&
  TMPDIR="$dataset" uv run --no-sync python scripts/estimate.py \
    --base "$dataset/base/$session.rrd" --anchor.tag-ids "${anchor_ids[@]}" \
    --anchor.tag-size "$anchor_size" --output-dir "$dataset" \
    2>&1 | tee -a "$dataset/reports/estimate.log"
```

只有一个锚点时，删掉数组里的第二项。重建失败时先看 `estimate.log`。

## 5. 发布重建结果

完整脚本先把验证通过的重建产物和日志发布到 `<mode>/processed/<trial>/`，再处理可选的训练 state。这样取消裁剪或 state 生成失败时，完整重建仍可回放。手动命令已直接写入 `dataset`，可跳过这一步。 [发布代码](../scripts/process_one_trial.sh#L240)

## 6. 生成训练 state

完整脚本在传入 `--state` 时执行本步；不加该参数只做重建，不检查 Hold/Tap。单独运行时使用同一会话的 `base` 和 `estimate`。成功后生成 `state_s8_r100/<session_id>.rrd`。检查失败时看 `reports/state.log`。 [state 命令](../scripts/process_one_trial.sh#L270)

[state_with_trim.py](../scripts/state_with_trim.py) 从整段 IMU 合加速度中检测 Tap。相邻候选峰间隔 ≤ 0.5 秒就归成一组。单机选含最高峰的一组，一个峰也可入选。双机只比较两台相机都检出的组，以两台相机各自最高峰得分中的较低值评比。选中组的**首峰**作为 Tap 时间。

每台相机的 Hold 结束点须在区间前 10%，Tap 首峰须在后 20%。若原片不满足比例，脚本寻找符合条件的最长共同窗口，同时保留每台相机至少 0.4 秒连续 Hold 和整组 Tap。找到后，单独运行默认提示 `[y/N]`，输入 `y`、`yes` 或 `是` 确认。批处理 `reconstruct_and_calibrate.sh` 自动确认。找不到窗口时可在 `reports/state.log` 查看原因。

确认后临时裁剪 `base` 和 `estimate` 供 state 计算，沿用原视频时间戳。为支持解码，视频可能比轨迹多保留一小段前导帧。[state.py](../scripts/state.py) 会重新检测手势，在 Hold 结束后和 Tap 首峰前各留 1 秒，并取各数据流共同覆盖的时段生成训练 state。检查通过后，脚本把训练 state 写入本试次的 `state_s8_r100/<会话>.rrd`（默认配置），把裁剪清单写入 `state_trim_reports/hold_trim.json`。原片已满足比例时只生成 state。清单记录完整重建的位置、裁剪区间和手势；运行日志保存在完整重建的 `reports/state.log`。单条脚本已保存重建但 state 失败时，退出码为 3。

如果 `process_one_trial.sh` 处理后再单独生成训练 state，直接使用它的产物目录作为 `dataset`。`tee -a` 会把本次输出追加到 `reports/state.log`，首次运行时会创建该文件。在交互终端执行，方便确认裁剪。

```bash
cd ~/specialist-reconstruction/specialist
# dataset="$PWD/datasets/pick_and_place_261125/single/processed/trial_261125_087"
# session='20261125_143633_IAQEB260436VUM'
set -o pipefail

TMPDIR="$dataset" uv run --no-sync python scripts/state_with_trim.py \
  --base "$dataset/base/$session.rrd" --estimate "$dataset/estimate/$session.rrd" \
  --output-dir "$dataset" \
  2>&1 | tee -a "$dataset/reports/state.log"
```

例如，仍使用旧目录的 `pass_pen_01` 这条结果可以直接运行，无需重新转换或重建：

```bash
dataset="$PWD/datasets/pass_pen_01/processed/dual/trial_261120_002"
session='20261120_173242_IAQEB260436VUM'
uv run --no-sync python scripts/state_with_trim.py \
  --base "$dataset/base/$session.rrd" \
  --estimate "$dataset/estimate/$session.rrd" \
  --output-dir "$dataset"
```

## 7. 回放

设置 `SPECIALIST_DATASET` 后运行同一个回放脚本。指向试次目录，默认回放全段。加 `--trim` 可按该试次的裁剪清单临时生成 `base`、`estimate` 和 `viz`，共用完整重建的模型和蓝图。例如回放 087 的裁剪段：

```bash
cd ~/specialist-reconstruction/specialist
dataset="$PWD/datasets/pick_and_place_261125/single/processed/trial_261125_087"
SPECIALIST_DATASET="$dataset" bash scripts/view-reconstruction.sh --trim
```

关闭 Rerun 后，临时目录自动清理。默认使用系统临时目录，也可用 `TMPDIR` 指定有可用空间的位置。查看器沿用原视频时间；若要同时显示训练 state，在命令末尾加 `"$dataset"/state_s8_r100/*.rrd`。单条处理的 `--replay` 也走同一入口。[回放脚本](../scripts/view-reconstruction.sh)
