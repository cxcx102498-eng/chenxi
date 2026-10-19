# chenxi

## article-audio-to-whiteboard-video

将科普文章与已有录音制作成白板讲解视频，交付 MP4、SRT/VTT 字幕和可编辑工程。

完整 skill 位于 [`article-audio-to-whiteboard-video/`](article-audio-to-whiteboard-video/)，入口说明为 [`SKILL.md`](article-audio-to-whiteboard-video/SKILL.md)。包含音频对齐、素材导入、渲染脚本、参考文档、测试和中文字体。

### 使用

将 `article-audio-to-whiteboard-video` 文件夹放入你的 skills 目录，提供文章和已有音频，并请求使用该 skill 制作视频。默认中文、白底、16:9、1920×1080、30 fps；保留原始录音。

基础 Python 依赖见 [`scripts/requirements.txt`](article-audio-to-whiteboard-video/scripts/requirements.txt)。运行环境、浏览器、FFmpeg 和可选转录依赖说明见 [`references/runtime.md`](article-audio-to-whiteboard-video/references/runtime.md)。

### 第三方许可

渲染引擎保留原始 [`LICENSE`](article-audio-to-whiteboard-video/scripts/engine/LICENSE)。字体沿用各自许可，详见 [`LICENSES.md`](article-audio-to-whiteboard-video/scripts/engine/lib/fonts/LICENSES.md) 和 [`OFL.txt`](article-audio-to-whiteboard-video/scripts/engine/lib/fonts/OFL.txt)。
