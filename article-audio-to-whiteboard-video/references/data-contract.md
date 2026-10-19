# 内部素材与时间轴契约

素材阶段是内部准备，不作为默认终点。用同一份分镜数据生成 storyboard 和 handoff；字段名字保留 v1 兼容，不代表本技能依赖其他技能。

## 目录

在工作区新建任务目录。材料目录和正式工程是同级；不要在已非空的正式工程中先装依赖再导入。

```text
task/
  input/                          原文、原音频测量和转录缓存
  materials/
    article-source.txt            用户原文
    narration.txt                 核对后的实际讲解稿
    storyboard.csv
    manifest.json
    video-handoff.json
    style-lock.md                 画风、字体、配色、最终尺寸
    editing-notes.md               文案订正、状态组、必要动画与限制
    credits.txt
    editable/                     SVG、布局数据、导出脚本、实际对象图层
    prompts/                      绘制简报或生图记录
    exports/ready/                最终采用 PNG/JPEG/WebP
    sources/source-log.json
    qa/
  project/                        pipeline init 创建
    assets/ clips/ audio/ alignment/
    source-package/               完整 materials 快照
    renderer/                     可独立运行的工具和字体
    timeline.json subtitles.json subtitles.srt subtitles.vtt
    preview/ renders/ exports/ qa/
```

单次影片不写入技能目录。对交付文件的引用使用包内相对路径和正斜杠，不含父目录跳转、机器绝对路径或未保存的网络 URL。仅诊断日志可记原路径。

## 素材清单与来源

manifest 顶层：`schema_version: 1`、`project`、`assets`。每条资产：

| 字段 | 值 |
|---|---|
| id / shot_ids / label | `A001` 等稳定编号、镜头 ID 数组、中文名称 |
| kind | generated / sourced / user-provided |
| status / visual_review | 正式采用须为 checked / pass，必须实际看图后填写 |
| file / transparent_required | 最终展示文件相对路径、布尔值 |
| license_status | generated-no-clearance-claim / terms-checked；尚未核实时标记实际状态，不假造通过 |
| source_page / license_url | 实际来源与使用条件引用；自绘图可空 |
| prompt_file | generated 必填实际绘制简报或生成记录 |
| notes | 表达意义、creation_method、editable_files、状态组及限制 |

程序图解与生图都可以 kind=generated，在 notes 与 source-log 用 `creation_method=vector/code/imagegen` 区分。只有真正透明的对象才标记 transparent_required；白底主图不必透明。重要对象保留 SVG ID，不用“合成图”冒充独立图层。

`sources/source-log.json` 顶层必须为数组，用 asset_id 关联。自绘记录 media_type、creation_method、prompt_file、editable_files、字体等依赖；外部记录 creator、source_page、license_url、license_status、restrictions 和取得日期。使用用户图时，实际授权说明可记录在包内文件并作为条款引用，不要求虚构公开网址。未核实来源只可用于明确标记的草稿，不能直接改状态通过导入。

`credits.txt` 保存字体、图标和外部图的必要署名；自绘声明制作方式，不声称现场拍摄或已获全部权利。没有外部图片也保留字体许可。

## 分镜与 handoff

storyboard 基础列：

`shot_id,narration,duration_estimate_s,purpose,composition,camera_motion,entry_order,subtitle_space,asset_ids`

追加 `representation,scene_group_id,state_order,meaning_change,editable_files` 可表达连续状态。`S001` 等镜头 ID 顺序稳定；asset_ids 可用逗号分隔。narration 拼接后忽略空白须与 narration.txt 相同，不为了状态图重复讲解。

handoff 顶层：

```json
{
  "schema_version": 1,
  "package_id": "project-slug",
  "manifest": "manifest.json",
  "storyboard": "storyboard.csv",
  "narration_file": "narration.txt",
  "canvas": {"width": 1920, "height": 1080, "fps": 30, "safe": {"top": 60, "bottom": 180, "left": 80, "right": 80}},
  "theme": {"background": "#FFFFFF", "ink": "#0B214A", "accent": "#FF7A00", "blue": "#1597F5"},
  "presentation": {"show_source_credits": false, "show_image_labels": false, "image_enter": "none", "image_enter_duration": 0.4},
  "assets": [],
  "shots": []
}
```

这只是字段示意，空数组不能交付。assets 每项含 `id,file,sha256,width,height`；哈希与尺寸从真实最终文件测得，file 与 manifest 一致。width/height 为偶数画布，fps 正整数。画布安全区随规格缩放，字幕不压在主体图上。

shots 每项包含 `shot_id,narration,duration_estimate_s,title,asset_ids,layout,cues`，顺序和文案与 storyboard 完全一致。duration_estimate_s 是初始预算，优先取真实录音句段长度；导入后由实际对齐写最终时间。过程组还在 shot 上填 `scene_group_id`、递增 `state_order`。

- `single`：一张主体图；`compare`：两张有意义的比较图；`text`：无图，必须填写 visual_reason，避免大段文字代替解释。
- 首标题只写 shot.title；不要再在零秒加相同 title cue。
- cues 允许 `at,kind,text,sub,dur,data,anchor_text,anchor_occurrence`。at 是本镜局部秒数，动作与字幕不能越镜。
- kind 仅 `title/point/highlight/focus/reset/subtitle`。图像由导入器建立，不在 handoff 添加 image/move/draw 等未支持动作。
- subtitle 含 text 和 dur，按序无重叠覆盖 narration；省略时导入器自动断句，最终仍需音频对齐与查看。
- highlight/focus 的 data 含 `asset_id` 与归一化 `rect:[x,y,w,h]`，范围在 0..1 且宽高正；highlight.style 为 circle/rect/underline。rect 针对实际展示版本。
- reset 只指定 data.asset_id；title/point/subtitle 不放注释坐标。
- 需要跟随口播的动作填确实位于本镜 narration 内的 anchor_text；词语重复时优先用较长片段消歧，也可用正整数 anchor_occurrence 指定第几次出现。没有锚点的短文字或初始图可从镜头起点显示；非零时刻的强调不能依赖未经对齐的旧估算。

## 连续状态与最终时间

同组状态共享尺寸、对象位置、主体比例与标题/关键词区域。对齐脚本依据 scene_group_id 和连续同素材禁重复入场；不要只靠 editing-notes 写“不要闪白”却漏掉结构字段。相同组不是同一素材文件：每状态可用不同 A 编号。

需要对象移动或逐笔路径时，显式实现实际引擎/源图层并验证；可写动画计划帮助维护，但计划不自动变成动作。默认分步状态已可独立解释知识。

最终 `timeline.json` 为时间真值：fps、total_frames、duration_s、audio{file,sha256,duration_s,tail_pad_s}；各镜有 start_frame、frame_count、start_s、end_s、clip、narration、asset_ids。所有全局时间由整数帧推导。`alignment/` 保存原始估算和对齐差异，SRT/VTT 与 clips 的字幕从同一结果产生。
