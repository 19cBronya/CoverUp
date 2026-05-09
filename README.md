# CoverUp

CoverUp 是一个用于批量替换视频封面的工具，提供 GUI 与 CLI 两种使用方式，适合本地批处理场景。

## 功能特性

- 批量扫描目录中的视频文件（支持递归）
- 手动指定封面图并直接写入视频
- 支持两种封面处理模式
  - `metadata`：写入元数据封面
  - `first-frame`：替换首帧
- 按分钟窗口抽帧，自动生成候选封面

## 环境要求

- Python `>= 3.10`
- 平台：开发环境可跨平台；打包脚本面向 Windows 产物
- 依赖：`PySide6`、`ffmpeg`、`ffprobe`

## 快速开始（开发模式）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

启动 GUI：

```bash
coverup-gui
```

启动 CLI：

```bash
coverup-cli --help
```

## CLI 常用示例

扫描目录中的视频：

```bash
coverup-cli --scan-dir ./videos --recursive
```

探测单个视频元信息：

```bash
coverup-cli --probe ./videos/sample.mp4
```

抽取某一分钟的候选封面帧：

```bash
coverup-cli --sample-minute ./videos/sample.mp4 --minute-index 0 --count 12
```

直接替换封面：

```bash
coverup-cli --video ./videos/a.mp4 --cover ./covers/a.jpg --mode metadata
```

## Windows 打包

1. 将 `ffmpeg.exe` 和 `ffprobe.exe` 放到 `bin/windows/`。
2. 安装 PyInstaller：`pip install pyinstaller`
3. 运行：

```bash
python scripts/build_windows.py
```

打包输出目录：`dist/coverup/`。

## 隐私与安全说明

- 当前项目默认在本地处理视频与封面文件，不依赖远程服务。
- 仓库已排除构建产物、缓存文件与本机路径相关临时数据，避免把环境信息误提交到公开仓库。
- 使用前请确认你有相关视频素材的处理权限，并遵守当地法律法规与平台规则。
