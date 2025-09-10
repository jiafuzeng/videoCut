# 视频批量切分工具

这个工具可以批量处理文件夹中的视频，按照指定时长切分每个视频，并在每个切分视频的结尾添加指定的结尾视频。

## 功能特点

- 支持多种视频格式（mp4, avi, mov, mkv, flv, wmv, m4v）
- 多线程并行处理，提高处理速度
- 自动跳过时长不满足条件的视频
- 为每个原视频创建独立的输出文件夹
- 自动添加结尾视频到每个切分片段

## 安装依赖

```bash
pip install -r requirements.txt
```

## 使用方法

```bash
python cut_videos.py \
  --input_folder <输入文件夹路径> \
  --cut_duration <切分时长(秒，支持小数)> \
  --end_video_path <结尾视频路径> \
  [--output_dir ./output] \
  [--workers 4]
```

### 参数说明

- `--input_folder`: 输入视频文件夹路径
- `--cut_duration`: 每个切分视频的时长（秒，支持小数，如 9.5）
- `--end_video_path`: 要添加到每个切分视频结尾的视频文件路径

### 可选参数

- `--output_dir`: 输出目录（默认: ./output）
- `--workers`: 线程数（默认: 4）

### 使用示例

```bash
# 基本用法（每段 30 秒）
python cut_videos.py \
  --input_folder /path/to/videos \
  --cut_duration 30 \
  --end_video_path /path/to/end_video.mp4

# 指定输出目录和线程数
python cut_videos.py \
  --input_folder /path/to/videos \
  --cut_duration 30 \
  --end_video_path /path/to/end_video.mp4 \
  --output_dir /path/to/output \
  --workers 8

# 处理当前项目示例目录，每段 60 秒
python cut_videos.py \
  --input_folder ./videos \
  --cut_duration 60 \
  --end_video_path ./endvideo/end.mp4 \
  --output_dir ./output

# 支持小数时长示例（每段 9.5 秒，10 线程）
python cut_videos.py \
  --input_folder ./videos \
  --cut_duration 9.5 \
  --end_video_path ./endvideo/end.mp4 \
  --output_dir ./output \
  --workers 10
```

## 输出结构

```
output/
├── movie1/
│   ├── segment_001.mp4
│   ├── segment_002.mp4
│   └── ...
├── documentary/
│   ├── segment_001.mp4
│   └── ...
└── ...
```

每个源视频会生成多个切分片段，每个片段都添加了结尾视频。输出目录名使用源视频的文件名（不包含扩展名）。

## 注意事项

1. 确保系统已安装 FFmpeg
2. 时长不满足切分条件的视频会被自动跳过
3. 处理过程中会显示进度信息
4. 支持多线程处理，可根据系统性能调整线程数
