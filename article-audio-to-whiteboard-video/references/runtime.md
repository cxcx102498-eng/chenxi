# 一条流水线：音频、工程、渲染

以下 `PYTHON`、`SKILL`、`AUDIO`、`TRANSCRIPT`、`MATERIALS`、`PROJECT` 均为实际路径的占位记号。Windows 用 `-X utf8` 并为含空格路径加正确引号。所有脚本使用参数调用程序，不拼接不可信 shell 命令。

## 环境与输入

桌面环境可用 load_workspace_dependencies 找到实际 Python。先检查 Pillow、NumPy、Playwright、imageio-ffmpeg、PyAV 和 Chromium 系浏览器；可复用 Chrome/Edge，通过 `AI_VIDEO_BROWSER` 指定。优先复用已有环境，缺包才安装到任务专用目录或虚拟环境，不能在待导入的 PROJECT 提前放依赖。

```text
PYTHON -m pip install -r SKILL/scripts/requirements.txt
```

基础依赖只负责导入、渲染和测量；需要本地新转录时才另装 `faster-whisper`。模型首次使用可能下载；先查看本地缓存，避免反复下载和重复转录。已有可信词级转录只在音频哈希和解码时长匹配时复用。

先把文章保存在 input/article-source.txt，测量原音频：

```text
PYTHON -X utf8 SKILL/scripts/align_audio.py probe --audio AUDIO --out input/audio-info.json
PYTHON -X utf8 SKILL/scripts/align_audio.py transcribe --audio AUDIO --out input/transcript.json --model small --language zh
```

可使用其他实际可用转录工具，把结果整理为相同 schema；不自动将用户音频发送外部服务。转录记录应含 file、sha256、duration_s、segments（start/end/text 与 words[{start,end,word}]）。测量采用真实解码样本而非 MP3 容器名义时长。ASR词内插值不是音素级对齐，句段时间也不包装成词级精度。

先核对录音与文章；只做必要格式整理与明确识别修正。大量错配、缺整句、非单调时间戳或错误录音须修正输入，不通过整体拉伸或放松阈值跳过。

## 材料与正式工程

按数据契约制作实际素材，不要求用户填写 JSON。制作代表镜时可直接用 `scripts/run.py render --spec <代表镜配置> --stills <有效时刻> --out <预览目录>` 查看最终版式，无需另建完整预检工程。初始化目录本身不会生成图片：

```text
PYTHON -X utf8 SKILL/scripts/materials.py init MATERIALS
PYTHON -X utf8 SKILL/scripts/materials.py audit MATERIALS
PYTHON -X utf8 SKILL/scripts/pipeline.py init --package MATERIALS --out PROJECT
```

`PROJECT` 必须为空或不存在且位于 MATERIALS 之外。init 一次校验 handoff、图像哈希、文案对应和坐标，复制完整素材源与自包含 renderer。失败先修具体原因，不靠 --draft 把未完成资产交付成正式片。

随后所有工作优先用工程自己的 renderer，以保证交付可重建：

```text
PYTHON -X utf8 PROJECT/renderer/align_audio.py align --project PROJECT --audio AUDIO --transcript input/transcript.json
PYTHON -X utf8 PROJECT/renderer/pipeline.py preview --project PROJECT --ids S001,S003
```

示例 ID 换成真正代表画面的镜头。align 保存原始估算快照、复制音频、输出 actual timeline、字幕和 anchor 时间。再次对齐以当前 timeline/clips 为准，保留已修正的标题、图框和字幕断句；初始快照仅作审计，不恢复覆盖新编辑。结构或文案变化记录在 alignment/project-edit-report.json。失败报告在 alignment/diagnostic.json；先读取报告，再决定校正识别、手工核对小段、或询问实质冲突。禁止反复放松错误阈值直到通过。

同组连续状态和连续同图禁重复入场；独立图可 0.4 秒淡入。检查 point 区域是否改变媒体高度。长片分批预览并可并行渲独立镜头，不把整段音轨混到每镜。

## 增量渲染与导出

```text
PYTHON -X utf8 PROJECT/renderer/pipeline.py render --project PROJECT --workers 2
PYTHON -X utf8 PROJECT/renderer/pipeline.py export --project PROJECT --name video.mp4
```

render 校验配置与画面框景，按实际依赖哈希复用合格片段。spec、主体图、引擎或字体变动会使对应缓存失效；不能只看 MP4 文件存在就跳过。镜头数和分辨率变化不依赖历史示例的固定参数。阶段日志保留耗时与失败原因，修复后从失败步骤继续。

export 按 timeline 顺序拼无声音画面，混入完整原音轨并编码 AAC。视频应为 ceil(真实音频秒数×fps) 帧，仅在尾部补不足一帧的静音；不使用 -shortest、改语速或静默截尾。字幕已经由片段绘制，输出 SRT/VTT 供修改和平台使用，不再次烧录。

导出会进行完整解码和最终抽帧。按 [quality.md](quality.md) 实际查看结果后，记录 qa/visual-review.md，再打包：

```text
PYTHON -X utf8 PROJECT/renderer/pipeline.py package --project PROJECT
```

ZIP 包括可重建资源，不含浏览器、虚拟环境、依赖缓存或可重新生成的大体积中间件。交付脚本不能替代人工语义与视觉判断。

## 深入调整单镜

必要时直接用 `renderer/run.py validate/render/qa --spec PROJECT/clips/S001.json`。render 可选 `--stills 0.5,2` 和 `--out`，时间应在镜头内。spec 顶层为 grammar/duration/fps/width/height/safe/theme/data/cues，默认 grammar=y4_media_board。

导入后的 image cue：image 为相对 spec 路径，asset_id 稳定，data.box 是媒体区归一坐标。highlight/focus.rect 是原图片归一坐标，两者含义不同。全片图片的 show_source_credits 与 show_image_labels 默认 false。中文字体随 engine/lib/fonts 复制，并保留 engine/LICENSE 及字体许可。

工程内渲染器是快照；技能更新不会自动更新已经导出的工程。修改旧工程必须检查实际运行副本，重新输出并看最终 MP4，不能只修改技能文档就宣称已修复影片。
