import ffmpeg
import os
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import glob
from pathlib import Path
import tempfile


def get_video_duration(video_path):
    """
    获取视频时长（秒）
    """
    try:
        probe = ffmpeg.probe(video_path)
        duration = float(probe['streams'][0]['duration'])
        return duration
    except Exception as e:
        print(f"获取视频时长失败 {video_path}: {e}")
        return 0


def cut_single_segment_with_end(video_path, start_time, end_time, output_path, prepared_end_path, end_duration):
    """
    按给定起止时间切分单段视频并添加结尾视频，确保音视频同步
    """
    try:
        # 获取主视频信息
        main_probe = ffmpeg.probe(video_path)
        
        # 获取主视频的分辨率
        main_video_stream = next(s for s in main_probe['streams'] if s['codec_type'] == 'video')
        main_width = int(main_video_stream['width'])
        main_height = int(main_video_stream['height'])
        
        # 使用临时文件来避免concat的复杂性
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_main = os.path.join(temp_dir, "temp_main.mp4")
            
            # 切分主视频并重新编码以确保兼容性
            # 添加容错参数来处理损坏的视频数据
            input_stream = ffmpeg.input(video_path, ss=start_time, t=end_time-start_time, 
                                       **{'fflags': '+ignidx+igndts'})  # 忽略损坏的数据
            # 使用setsar filter来统一SAR参数
            video_stream = input_stream.video.filter('scale', main_width, main_height, flags='lanczos').filter('setsar', '1')
            audio_stream = input_stream.audio
            
            (
                ffmpeg
                .output(
                    video_stream,
                    audio_stream,
                    temp_main,
                    vcodec='libx264',
                    preset='fast',
                    **{'profile:v': 'main'},
                    r=30,  # 固定帧率为30fps
                    acodec='aac',
                    ar=44100,
                    ac=2,
                    **{'fflags': '+ignidx+igndts'}  # 输出时也忽略错误
                )
                .overwrite_output()
                .run(quiet=True)
            )
            
            # 使用filter_complex进行更可靠的合并
            main_input = ffmpeg.input(temp_main)
            end_input = ffmpeg.input(prepared_end_path)
            
            (
                ffmpeg
                .filter([main_input.video, main_input.audio, end_input.video, end_input.audio], 
                       'concat', n=2, v=1, a=1)
                .output(output_path, vcodec='libx264', acodec='aac')
                .overwrite_output()
                .run(quiet=True)
            )
        
        # 验证最终视频时长
        final_probe = ffmpeg.probe(output_path)
        final_duration = float(final_probe['streams'][0]['duration'])
        expected_duration = (end_time - start_time) + end_duration
        
        print(f"切分时长: {end_time - start_time:.2f}s, 结尾时长: {end_duration:.2f}s, 最终时长: {final_duration:.2f}s")
        
        if abs(final_duration - expected_duration) > 0.1:  # 允许0.1秒误差
            print(f"警告: 时长不匹配! 期望: {expected_duration:.2f}s, 实际: {final_duration:.2f}s")
        
        return True
    except Exception as e:
        print(f"处理视频失败 {video_path}: {e}")
        # 如果是ffmpeg错误，显示更详细的信息
        if hasattr(e, 'stderr') and e.stderr:
            try:
                error_msg = e.stderr.decode('utf8')
                print(f"FFmpeg错误详情: {error_msg}")
            except:
                pass
        return False


def cut_video_with_end(video_path, cut_duration, end_video_path, video_output_dir):
    """
    将视频按 cut_duration 切分为多段，并为每段添加结尾视频，输出到 video_output_dir
    """
    video_name = Path(video_path).stem
    tid = threading.get_ident()
    video_duration = get_video_duration(video_path)

    if video_duration < cut_duration:
        print(f"[TID {tid}] 跳过视频 {video_name}: 时长 {video_duration:.2f}s 小于切分时长 {cut_duration}s")
        return

    # 计算可以切分的段数，确保不超过视频时长
    num_segments = int(video_duration // cut_duration)

    # 确保最后一段不会超出视频时长
    if num_segments * cut_duration >= video_duration:
        num_segments = max(1, num_segments - 1)

    print(f"[TID {tid}] 处理视频 {video_name}: 总时长 {video_duration:.2f}s, 将切分为 {num_segments} 段")

    # 预处理结尾视频（循环外一次），按主视频分辨率/参数
    try:
        main_probe = ffmpeg.probe(video_path)
        main_video_stream = next(s for s in main_probe['streams'] if s['codec_type'] == 'video')
        main_width = int(main_video_stream['width'])
        main_height = int(main_video_stream['height'])
        end_probe = ffmpeg.probe(end_video_path)
        end_duration = float(end_probe['streams'][0]['duration'])
    except Exception as e:
        print(f"[TID {tid}] 准备结尾视频失败: {e}")
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        prepared_end_path = os.path.join(temp_dir, "prepared_end.mp4")

        try:
            end_input_stream = ffmpeg.input(end_video_path)
            end_video_stream = end_input_stream.video.filter('scale', main_width, main_height, flags='lanczos').filter('setsar', '1')
            end_audio_stream = end_input_stream.audio

            (
                ffmpeg
                .output(
                    end_video_stream,
                    end_audio_stream,
                    prepared_end_path,
                    vcodec='libx264',
                    preset='fast',
                    **{'profile:v': 'main'},
                    r=30,
                    acodec='aac',
                    ar=44100,
                    ac=2
                )
                .overwrite_output()
                .run(quiet=True)
            )
        except Exception as e:
            print(f"[TID {tid}] 结尾视频转码失败: {e}")
            return

        # 切分视频并添加结尾
        for i in range(num_segments):
            start_time = i * cut_duration
            end_time = (i + 1) * cut_duration

            # 确保切分时间不超过视频时长
            if end_time > video_duration:
                end_time = video_duration

            # 如果切分时长太短，跳过
            if end_time - start_time < cut_duration * 0.5:  # 如果切分时长小于一半，跳过
                print(f"[TID {tid}] 跳过: segment_{i+1:03d}.mp4 (切分时长太短: {end_time - start_time:.2f}s)")
                continue

            output_filename = f"segment_{i+1:03d}.mp4"
            output_path = os.path.join(video_output_dir, output_filename)

            success = cut_single_segment_with_end(video_path, start_time, end_time, output_path, prepared_end_path, end_duration)
            if success:
                print(f"[TID {tid}] 完成: {output_filename}")
            else:
                print(f"[TID {tid}] 失败: {output_filename}")


def process_video(video_path, cut_duration, end_video_path, output_dir):
    """
    处理单个视频文件
    """
    video_name = Path(video_path).stem
    tid = threading.get_ident()
    
    # 创建视频输出目录
    video_output_dir = os.path.join(output_dir, video_name)
    os.makedirs(video_output_dir, exist_ok=True)
    
    # 将循环放入 cut_video_with_end 内部
    print(f"[TID {tid}] 开始处理: {video_name}")
    cut_video_with_end(video_path, cut_duration, end_video_path, video_output_dir)
    print(f"[TID {tid}] 处理完成: {video_name}")


def process_videos_folder(input_folder, cut_duration, end_video_path, output_dir, max_workers=4):
    """
    处理文件夹中的所有视频
    """
    # 支持的视频格式
    video_extensions = ['*.mp4', '*.avi', '*.mov', '*.mkv', '*.flv', '*.wmv', '*.m4v']
    
    # 获取所有视频文件
    video_files = []
    for ext in video_extensions:
        video_files.extend(glob.glob(os.path.join(input_folder, ext)))
        video_files.extend(glob.glob(os.path.join(input_folder, ext.upper())))
    
    if not video_files:
        print(f"在文件夹 {input_folder} 中未找到视频文件")
        return
    
    print(f"找到 {len(video_files)} 个视频文件")
    
    # 创建输出目录
    os.makedirs(output_dir, exist_ok=True)
    
    # 使用线程池处理视频
    tid_main = threading.get_ident()
    print(f"[TID {tid_main}] 准备启动线程池，workers={max_workers}")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_video = {}
        for video_file in video_files:
            print(f"[TID {tid_main}] 提交任务: {Path(video_file).name}")
            future = executor.submit(process_video, video_file, cut_duration, end_video_path, output_dir)
            future_to_video[future] = video_file
        
        # 等待所有任务完成
        for future in as_completed(future_to_video):
            video_file = future_to_video[future]
            try:
                future.result()
            except Exception as e:
                print(f"[TID {tid_main}] 处理视频时发生错误: {Path(video_file).name}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='批量切分视频并添加结尾')
    parser.add_argument('--input_folder', help='输入视频文件夹路径')
    parser.add_argument('--cut_duration', type=float, help='每个切分视频的时长（秒）')
    parser.add_argument('--end_video_path', help='结尾视频文件路径')
    parser.add_argument('--output_dir', default='./output', help='输出目录（默认: ./output）')
    parser.add_argument('--workers', type=int, default=4, help='线程数（默认: 4）')
    
    args = parser.parse_args()
    
    # 检查输入文件夹是否存在
    if not os.path.exists(args.input_folder):
        print(f"错误: 输入文件夹 {args.input_folder} 不存在")
        exit(1)
    
    # 检查结尾视频是否存在
    if not os.path.exists(args.end_video_path):
        print(f"错误: 结尾视频文件 {args.end_video_path} 不存在")
        exit(1)
    
    print(f"开始处理视频...")
    print(f"输入文件夹: {args.input_folder}")
    print(f"切分时长: {args.cut_duration} 秒")
    print(f"结尾视频: {args.end_video_path}")
    print(f"输出目录: {args.output_dir}")
    print(f"线程数: {args.workers}")
    print("-" * 50)
    
    process_videos_folder(args.input_folder, args.cut_duration, args.end_video_path, args.output_dir, args.workers)
    
    print("-" * 50)
    print("所有视频处理完成！")