import ffmpeg
import os
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import glob
from pathlib import Path
import tempfile
import json


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


def find_json_file_for_video(video_path):
    """
    为视频文件查找对应的JSON配置文件
    返回: JSON文件路径 或 None 如果不存在
    """
    video_path_obj = Path(video_path)
    video_dir = video_path_obj.parent
    video_stem = video_path_obj.stem  # 不包含扩展名的文件名
    
    # 尝试不同的JSON文件名
    json_candidates = [
        video_dir / f"{video_stem}.json",
        video_dir / f"{video_stem}.JSON",
    ]
    
    for json_path in json_candidates:
        if json_path.exists():
            return str(json_path)
    
    return None


def load_json_config(json_file_path):
    """
    加载JSON配置文件，提取merged的segments信息
    返回: merged_segments列表 或 None 如果不存在或无效
    """
    try:
        if not os.path.exists(json_file_path):
            print(f"JSON文件不存在: {json_file_path}")
            return None
            
        with open(json_file_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # 检查是否有merged配置
        if 'merged' in config and isinstance(config['merged'], dict):
            merged = config['merged']
            segments = merged.get('segments', [])
            
            if segments and isinstance(segments, list):
                # 验证segments格式
                valid_segments = []
                for segment in segments:
                    if (isinstance(segment, dict) and 
                        'start' in segment and 'end' in segment and
                        isinstance(segment['start'], (int, float)) and 
                        isinstance(segment['end'], (int, float)) and
                        segment['start'] >= 0 and segment['end'] > segment['start']):
                        valid_segments.append({
                            'start': float(segment['start']),
                            'end': float(segment['end']),
                            'score': segment.get('score', 0)
                        })
                
                if valid_segments:
                    print(f"JSON配置加载成功: 找到 {len(valid_segments)} 个有效segments")
                    return valid_segments
                else:
                    print("JSON配置中merged.segments格式无效")
                    return None
            else:
                print("JSON配置中merged.segments为空或格式错误")
                return None
        else:
            print("JSON配置中未找到merged配置")
            return None
            
    except Exception as e:
        print(f"加载JSON配置文件失败: {e}")
        return None


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


def remove_and_merge_video_segments(video_path, merged_segments, temp_dir):
    """
    根据merged segments删除视频片段，然后将剩余部分合并成一个新视频
    返回: 合并后的视频文件路径
    """
    tid = threading.get_ident()
    
    try:
        # 获取主视频信息
        main_probe = ffmpeg.probe(video_path)
        main_video_stream = next(s for s in main_probe['streams'] if s['codec_type'] == 'video')
        main_width = int(main_video_stream['width'])
        main_height = int(main_video_stream['height'])
        video_duration = float(main_probe['streams'][0]['duration'])
        
        # 创建剩余片段列表
        remaining_segments = []
        
        # 按时间顺序排序segments
        sorted_segments = sorted(merged_segments, key=lambda x: x['start'])
        
        current_time = 0.0
        
        for i, segment in enumerate(sorted_segments):
            start_time = segment['start']
            end_time = segment['end']
            
            # 如果当前时间小于segment开始时间，说明中间有剩余片段
            if current_time < start_time:
                remaining_duration = start_time - current_time
                if remaining_duration >= 0.5:  # 只保留时长大于0.5秒的片段
                    remaining_segments.append({
                        'start': current_time,
                        'end': start_time,
                        'duration': remaining_duration
                    })
                    print(f"[TID {tid}] 保留片段: {current_time:.2f}s-{start_time:.2f}s, 时长: {remaining_duration:.2f}s")
            
            # 更新当前时间到segment结束时间
            current_time = max(current_time, end_time)
        
        # 检查最后一段
        if current_time < video_duration:
            remaining_duration = video_duration - current_time
            if remaining_duration >= 0.5:
                remaining_segments.append({
                    'start': current_time,
                    'end': video_duration,
                    'duration': remaining_duration
                })
                print(f"[TID {tid}] 保留最后片段: {current_time:.2f}s-{video_duration:.2f}s, 时长: {remaining_duration:.2f}s")
        
        if not remaining_segments:
            print(f"[TID {tid}] 没有有效的剩余片段")
            return None
        
        # 提取剩余片段
        extracted_segments = []
        for i, segment in enumerate(remaining_segments):
            temp_segment_path = os.path.join(temp_dir, f"remaining_{i+1:03d}.mp4")
            
            # 提取剩余片段
            input_stream = ffmpeg.input(video_path, ss=segment['start'], t=segment['duration'],
                                     **{'fflags': '+ignidx+igndts'})
            
            video_stream = input_stream.video.filter('scale', main_width, main_height, flags='lanczos').filter('setsar', '1')
            audio_stream = input_stream.audio
            
            (
                ffmpeg
                .output(
                    video_stream,
                    audio_stream,
                    temp_segment_path,
                    vcodec='libx264',
                    preset='fast',
                    **{'profile:v': 'main'},
                    r=30,
                    acodec='aac',
                    ar=44100,
                    ac=2,
                    **{'fflags': '+ignidx+igndts'}
                )
                .overwrite_output()
                .run(quiet=True)
            )
            
            # 验证提取的片段
            segment_probe = ffmpeg.probe(temp_segment_path)
            actual_duration = float(segment_probe['streams'][0]['duration'])
            
            print(f"[TID {tid}] 提取剩余片段 {i+1}: {segment['start']:.2f}s-{segment['end']:.2f}s, 实际时长: {actual_duration:.2f}s")
            extracted_segments.append(temp_segment_path)
        
        # 合并所有剩余片段
        merged_video_path = os.path.join(temp_dir, "merged_video.mp4")
        merge_video_segments(extracted_segments, merged_video_path, tid)
        
        return merged_video_path
            
    except Exception as e:
        print(f"[TID {tid}] 删除和合并视频片段失败: {e}")
        return None


def merge_video_segments(segment_paths, output_path, tid):
    """
    合并多个视频片段为一个视频，优先使用concat demuxer方法
    """
    try:
        if len(segment_paths) == 1:
            # 如果只有一个片段，直接复制
            import shutil
            shutil.copy2(segment_paths[0], output_path)
            print(f"[TID {tid}] 单个片段，直接复制到: {output_path}")
            return True
        
        # 直接使用concat demuxer方法（最可靠）
        print(f"[TID {tid}] 使用concat demuxer方法合并 {len(segment_paths)} 个片段...")
        return merge_video_segments_concat_demuxer(segment_paths, output_path, tid)
        
    except Exception as e:
        print(f"[TID {tid}] concat demuxer合并失败: {e}")
        # 如果concat demuxer失败，尝试逐个合并的方法
        try:
            print(f"[TID {tid}] 尝试使用逐个合并方法...")
            return merge_video_segments_sequential(segment_paths, output_path, tid)
        except Exception as e2:
            print(f"[TID {tid}] 所有合并方法都失败: {e2}")
            return False


def merge_video_segments_concat_demuxer(segment_paths, output_path, tid):
    """
    使用concat demuxer方法合并视频片段
    """
    try:
        # 创建concat文件列表
        concat_file_path = output_path.replace('.mp4', '_concat.txt')
        
        with open(concat_file_path, 'w') as f:
            for segment_path in segment_paths:
                f.write(f"file '{segment_path}'\n")
        
        # 使用concat demuxer
        (
            ffmpeg
            .input(concat_file_path, format='concat', safe=0)
            .output(output_path, vcodec='copy', acodec='copy')
            .overwrite_output()
            .run(quiet=True)
        )
        
        # 清理临时文件
        os.remove(concat_file_path)
        
        # 验证合并后的视频
        merged_probe = ffmpeg.probe(output_path)
        merged_duration = float(merged_probe['streams'][0]['duration'])
        print(f"[TID {tid}] concat demuxer合并完成: {len(segment_paths)} 个片段，总时长: {merged_duration:.2f}s")
        
        return True
        
    except Exception as e:
        print(f"[TID {tid}] concat demuxer合并失败: {e}")
        return False


def merge_video_segments_sequential(segment_paths, output_path, tid):
    """
    使用逐个合并的方法合并视频片段
    """
    try:
        if len(segment_paths) == 1:
            import shutil
            shutil.copy2(segment_paths[0], output_path)
            print(f"[TID {tid}] 单个片段，直接复制到: {output_path}")
            return True
        
        # 逐个合并片段
        current_output = segment_paths[0]
        
        for i in range(1, len(segment_paths)):
            next_segment = segment_paths[i]
            temp_output = output_path.replace('.mp4', f'_temp_{i}.mp4')
            
            # 合并当前输出和下一个片段
            (
                ffmpeg
                .input(current_output)
                .input(next_segment)
                .filter_complex('[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]')
                .output(temp_output, vcodec='copy', acodec='copy')
                .overwrite_output()
                .run(quiet=True)
            )
            
            # 如果不是第一个临时文件，删除之前的临时文件
            if i > 1:
                prev_temp = output_path.replace('.mp4', f'_temp_{i-1}.mp4')
                if os.path.exists(prev_temp):
                    os.remove(prev_temp)
            
            current_output = temp_output
        
        # 将最终结果移动到目标位置
        if current_output != output_path:
            import shutil
            shutil.move(current_output, output_path)
        
        # 验证合并后的视频
        merged_probe = ffmpeg.probe(output_path)
        merged_duration = float(merged_probe['streams'][0]['duration'])
        print(f"[TID {tid}] 逐个合并完成: {len(segment_paths)} 个片段，总时长: {merged_duration:.2f}s")
        
        return True
        
    except Exception as e:
        print(f"[TID {tid}] 逐个合并失败: {e}")
        return False


def cut_video_with_end(video_path, cut_duration, end_video_path, video_output_dir, merged_segments=None):
    """
    将视频按 cut_duration 切分为多段，并为每段添加结尾视频，输出到 video_output_dir
    如果提供了merged_segments，先按segments提取视频片段，再对每个片段进行切分
    """
    video_name = Path(video_path).stem
    tid = threading.get_ident()
    video_duration = get_video_duration(video_path)

    # 如果有merged_segments，先提取片段并合并
    if merged_segments:
        print(f"[TID {tid}] 使用JSON配置处理视频 {video_name}: 总时长 {video_duration:.2f}s, 将删除 {len(merged_segments)} 个segments并合并剩余部分")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # 删除视频片段并合并剩余部分
            merged_video_path = remove_and_merge_video_segments(video_path, merged_segments, temp_dir)
            
            if not merged_video_path:
                print(f"[TID {tid}] 视频 {video_name} 没有有效的剩余片段可合并")
                return
            
            # 获取合并后视频的时长
            merged_duration = get_video_duration(merged_video_path)
            
            if merged_duration < cut_duration:
                print(f"[TID {tid}] 跳过合并视频: 时长 {merged_duration:.2f}s 小于切分时长 {cut_duration}s")
                return
            
            # 计算可以切分的段数
            num_segments = int(merged_duration // cut_duration)
            if num_segments * cut_duration >= merged_duration:
                num_segments = max(1, num_segments - 1)
            
            print(f"[TID {tid}] 处理合并视频: 时长 {merged_duration:.2f}s, 将切分为 {num_segments} 段")
            
            # 对合并后的视频进行切分
            cut_segment_with_end(merged_video_path, cut_duration, end_video_path, video_output_dir, num_segments, tid)
    else:
        # 原有的处理逻辑
        if video_duration < cut_duration:
            print(f"[TID {tid}] 跳过视频 {video_name}: 时长 {video_duration:.2f}s 小于切分时长 {cut_duration}s")
            return

        # 计算可以切分的段数，确保不超过视频时长
        num_segments = int(video_duration // cut_duration)

        # 确保最后一段不会超出视频时长
        if num_segments * cut_duration >= video_duration:
            num_segments = max(1, num_segments - 1)

        print(f"[TID {tid}] 处理视频 {video_name}: 总时长 {video_duration:.2f}s, 将切分为 {num_segments} 段")
        
        # 切分视频
        cut_segment_with_end(video_path, cut_duration, end_video_path, video_output_dir, num_segments, tid)


def cut_segment_with_end(video_path, cut_duration, end_video_path, output_dir, num_segments, tid):
    """
    对单个视频片段进行切分并添加结尾视频
    """
    video_duration = get_video_duration(video_path)
    
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
            output_path = os.path.join(output_dir, output_filename)

            success = cut_single_segment_with_end(video_path, start_time, end_time, output_path, prepared_end_path, end_duration)
            if success:
                print(f"[TID {tid}] 完成: {output_filename}")
            else:
                print(f"[TID {tid}] 失败: {output_filename}")


def process_video(video_path, cut_duration, end_video_path, output_dir):
    """
    处理单个视频文件，自动检测对应的JSON配置文件
    """
    video_name = Path(video_path).stem
    tid = threading.get_ident()
    
    # 创建视频输出目录
    video_output_dir = os.path.join(output_dir, video_name)
    os.makedirs(video_output_dir, exist_ok=True)
    
    # 自动检测JSON配置文件
    json_file_path = find_json_file_for_video(video_path)
    merged_segments = None
    
    if json_file_path:
        print(f"[TID {tid}] 找到JSON配置文件: {json_file_path}")
        merged_segments = load_json_config(json_file_path)
        if merged_segments is None:
            print(f"[TID {tid}] JSON配置加载失败，将使用常规处理模式")
    else:
        print(f"[TID {tid}] 未找到JSON配置文件，使用常规处理模式")
    
    # 将循环放入 cut_video_with_end 内部
    print(f"[TID {tid}] 开始处理: {video_name}")
    cut_video_with_end(video_path, cut_duration, end_video_path, video_output_dir, merged_segments)
    print(f"[TID {tid}] 处理完成: {video_name}")


def process_videos_folder(input_folder, cut_duration, end_video_path, output_dir, max_workers=4):
    """
    处理文件夹中的所有视频，每个视频自动检测对应的JSON配置文件
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
    print("JSON配置: 自动检测（每个视频查找同名JSON文件）")
    print("-" * 50)
    
    process_videos_folder(args.input_folder, args.cut_duration, args.end_video_path, args.output_dir, args.workers)
    
    print("-" * 50)
    print("所有视频处理完成！")