import json
import os
import re

from bs4 import BeautifulSoup
import pandas as pd
import charset_normalizer
import docx2txt
import numpy as np
import openpyxl
import pandas as pd
import pdfplumber
import requests
import torch
import xlrd
from PIL import Image, ImageOps, ImageSequence
from markdownify import markdownify as md
from ..config import current_dir_path
from .video_parameters import resolve_frame_count, resolve_video_dimensions

file_path = os.path.join(current_dir_path, "file")
programming_languages_extensions = [".py", ".js", ".java", ".c", ".cpp", ".html", ".css", ".sql", ".r", ".swift"]


def read_one(path):
    text = ""
    if path.endswith(".docx") or path.endswith(".DOCX"):
        text += docx2txt.process(path)
    elif path.endswith(".md") or path.endswith(".MD"):
        with open(path, "r", encoding="utf-8") as f:
            text += f.read()
    elif path.endswith(".pdf") or path.endswith(".PDF"):
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text += page.extract_text()
    elif path.endswith(".xlsx") or path.endswith(".XLSX"):
        workbook = openpyxl.load_workbook(path)
        for sheet in workbook.worksheets:
            # 检查工作表是否至少有一行数据
            if sheet.max_row > 1:  # 至少有一行数据（不包括表头）
                # 获取工作表名称
                text += f"## {sheet.title} 的内容\n"

                # 获取表头
                headers = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
                if headers:
                    text += "|" + " | ".join([" " if cell is None else str(cell) for cell in headers]) + "|\n"
                    text += "|" + " | ".join(["---"] * len(headers)) + "|\n"
                else:
                    text += "没有找到表头。\n"
                    continue

                # 获取数据行
                for row in sheet.iter_rows(min_row=2, values_only=True):
                    if any(cell is not None for cell in row):
                        text += "|" + " | ".join([" " if cell is None else str(cell) for cell in row]) + "|\n"
                    else:
                        # 如果整行都是空的，则停止读取当前工作表
                        break
    elif path.endswith(".xls") or path.endswith(".XLS"):
        workbook = xlrd.open_workbook(path)
        for sheet_index in range(workbook.nsheets):
            sheet = workbook.sheet_by_index(sheet_index)
            # 检查工作表是否为空
            if sheet.nrows > 0:
                text += f"## {sheet.name} 的内容\n"
                text += "| " + " | ".join(sheet.row_values(0)) + " |\n"  # 添加表头
                text += "| " + " | ".join(["---"] * sheet.ncols) + " |\n"  # 添加分隔符
                for row_num in range(1, sheet.nrows):
                    text += "| " + " | ".join([str(cell) for cell in sheet.row_values(row_num)]) + " |\n"
    elif path.endswith(".csv") or path.endswith(".CSV"):
        # 检测文件编码
        with open(path, "rb") as file:
            content = file.read()
        result = charset_normalizer.from_bytes(content)
        encoding = result.best().encoding if result else 'utf-8'
        # 读取 CSV 文件
        df = pd.read_csv(path, encoding=encoding)
        text += df.to_markdown(index=True)
    elif path.endswith(".txt") or path.endswith(".TXT"):
        with open(path, "r", encoding="utf-8") as f:
            text += f.read()
    elif path.endswith(".json") or path.endswith(".JSON"):
        with open(path, "r", encoding="utf-8") as f:
            text += json.dumps(json.load(f), ensure_ascii=False, indent=4)
    elif any(path.endswith(extension) for extension in programming_languages_extensions):
        try:
            with open(path, "r", encoding="utf-8") as file:
                text += file.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="latin-1") as file:
                text += file.read()
    out = [{"source": path,"paragraph_index":"full text" , "file_content": text}]
    return out


file_path = os.path.join(current_dir_path, "file")


class load_file:
    @classmethod
    def INPUT_TYPES(s):
        paths = [f for f in os.listdir(file_path)]
        return {
            "required": {
                "absolute_path": ("STRING", {"default": ""}),
                "relative_path": (paths, {"default": "test.txt"}),
                "is_enable": ("BOOLEAN", {"default": True}),
            },
            "optional": {},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("file_content",)

    FUNCTION = "file"

    # OUTPUT_NODE = False

    CATEGORY = "大模型派对（llm_party）/知识库（knowbase）"

    def file(self, relative_path, absolute_path="", is_enable=True):
        if is_enable == False:
            return (None,)
        if absolute_path != "":
            path = absolute_path
        else:
            path = os.path.join(file_path, relative_path)
        out = read_one(path)
        out = json.dumps(out, ensure_ascii=False, indent=4)
        return (out,)


class load_file_folder:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "folder_path": ("STRING", {"default": "C://Users/"}),
                "is_enable": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "related_characters": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("file_content",)

    FUNCTION = "file"

    # OUTPUT_NODE = False

    CATEGORY = "大模型派对（llm_party）/知识库（knowbase）"

    def file(self, folder_path, related_characters="", is_enable=True):
        if is_enable == False:
            return (None,)
        # 获取文件夹中的所有文件，并读取到字符串中
        out = []
        for root, dirs, files in os.walk(folder_path):
            if related_characters != "":
                for file in files:
                    # 如果文件名包含相关字符
                    if related_characters in file:
                        file_path = os.path.join(root, file)
                        out.extend(read_one(file_path))
            else:
                for file in files:
                    file_path = os.path.join(root, file)
                    out.extend(read_one(file_path))
        out = json.dumps(out, ensure_ascii=False, indent=4)
        return (out,)


class load_url:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "url": ("STRING", {"default": "https://example.com"}),
                "is_enable": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "with_jina": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("file_content",)

    FUNCTION = "file"

    # OUTPUT_NODE = False

    CATEGORY = "大模型派对（llm_party）/知识库（knowbase）"

    def file(self, url, with_jina=True, is_enable=True):
        if not is_enable:
            return (None,)
        try:
            jina = "https://r.jina.ai/"
            if with_jina:
                url = jina + url

            response = requests.get(url, timeout=10)
            response.raise_for_status()  # 确保请求成功

            # 使用 charset_normalizer 检测编码
            detected_encoding = charset_normalizer.detect(response.content)['encoding']
            response.encoding = detected_encoding if detected_encoding else 'utf-8'
        except requests.exceptions.RequestException as e:
            print(f"请求发生错误: {e}")
            return (None,)
        
        out = response.text
        print(out)  # Debugging: Check the HTML content
        
        if not with_jina:
            # 使用 BeautifulSoup 解析 HTML
            soup = BeautifulSoup(out, 'html.parser')
            # 提取主要内容
            main_content = soup.get_text()
            # 将 HTML 转换为 Markdown
            out = md(main_content, convert=['p', 'h1', 'h2', 'h3', 'a', 'img'], heading_style="ATX", bullets="*+-", strong_em_symbol="ASTERISK")
            # 去掉多余的换行符
            out = re.sub(r'\n+', '\n', out)
        out = [{"source": url,"paragraph_index":"full text" , "file_content": out}]
        out = json.dumps(out, ensure_ascii=False, indent=4)
        return (out,)


# 定义一个函数，将图片路径转换为与 save_images 函数兼容的格式
def convert_image_to_compatible_format(image_path):
    # 从给定路径加载图片
    img = Image.open(image_path).convert("RGB")

    # 将图片转换为numpy数组并归一化
    img_array = np.asarray(img) / 255.0

    # 将numpy数组转换为torch张量
    img_tensor = torch.tensor(img_array).permute(2, 0, 1).unsqueeze(0)  # 调整维度顺序并添加批次维度

    # 返回预期格式的张量
    return img_tensor


# MiniMax H3 等参考视频模型按 24 fps 对齐参考素材，参考视频最长 15 秒。
REFERENCE_VIDEO_FPS = 24
MAX_REFERENCE_VIDEO_SECONDS = 15.0
IMAGE_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif")


def resolve_media_path(path):
    """把 start_workflow 的路径控件值解析成真实文件路径。

    控件值可能是 API 调用方传入的绝对路径，也可能是通过 ComfyUI 上传控件
    传入的 input 目录文件名，两种都要能加载。
    """
    raw = str(path or "").strip().strip('"')
    if not raw:
        return ""
    if os.path.exists(raw):
        return raw
    try:
        import folder_paths

        candidate = folder_paths.get_annotated_filepath(raw)
    except Exception:
        candidate = ""
    if candidate and os.path.exists(candidate):
        return candidate
    return raw


def load_image_batch(path):
    """加载一张图片（或一个图片目录）为 IMAGE 批量张量。"""
    targets = []
    if os.path.isdir(path):
        targets = [
            os.path.join(path, filename)
            for filename in os.listdir(path)
            if filename.lower().endswith(IMAGE_FILE_EXTENSIONS)
        ]
    elif path:
        targets = [path]

    frames = []
    for target in targets:
        with Image.open(target) as image_file:
            for frame in ImageSequence.Iterator(image_file):
                frame = ImageOps.exif_transpose(frame)
                if frame.mode == "I":
                    frame = frame.point(lambda i: i * (1 / 256)).convert("L")
                array = np.array(frame.convert("RGB")).astype(np.float32) / 255.0
                frames.append(torch.from_numpy(array).unsqueeze(0))

    if not frames:
        if os.path.isdir(path):
            print(f"[start_workflow] 目录中没有可用的图片: {path}")
        return None
    if len(frames) == 1:
        return frames[0]
    return torch.cat(frames, dim=0)


def load_reference_video(path, max_frames=None):
    """解码参考视频，返回 (IMAGE 帧序列, AUDIO 音轨或 None)。

    帧序列按 24 fps 重采样（参考视频模型的时间轴），并截断到
    ``max_frames``，避免为一段长视频白白解码/占用显存。
    """
    from comfy_api.input_impl import VideoFromFile

    duration = 0.0
    if max_frames:
        duration = min(MAX_REFERENCE_VIDEO_SECONDS, float(max_frames) / REFERENCE_VIDEO_FPS)
    components = VideoFromFile(path, duration=duration).get_components()

    frames = components.images
    if not isinstance(frames, torch.Tensor) or frames.shape[0] == 0:
        raise ValueError(f"没有从参考视频中解码出画面: {path}")

    source_fps = _positive_float(components.frame_rate, REFERENCE_VIDEO_FPS)
    if abs(source_fps - REFERENCE_VIDEO_FPS) > 0.01:
        frames = frames[resample_video_frame_indices(frames.shape[0], source_fps)]
    if max_frames:
        frames = frames[: int(max_frames)]
    if frames.shape[0] == 0:
        raise ValueError(f"参考视频截断后没有剩余帧: {path}")

    soundtrack = components.audio
    audio = None
    if soundtrack is not None:
        waveform = soundtrack.get("waveform")
        if waveform is not None and waveform.numel() > 0:
            audio = {
                "waveform": waveform.float(),
                "sample_rate": int(soundtrack["sample_rate"]),
            }
    return frames.contiguous(), audio


def resample_video_frame_indices(frame_count, source_fps):
    """把源帧映射到 24 fps 参考时间轴上的最近帧下标。"""
    if frame_count <= 0:
        return []
    if source_fps <= 0:
        return list(range(frame_count))
    target_count = max(1, int(round(frame_count * REFERENCE_VIDEO_FPS / source_fps)))
    step = source_fps / REFERENCE_VIDEO_FPS
    return [min(frame_count - 1, int(round(index * step))) for index in range(target_count)]


def load_reference_audio(path):
    """把音频文件（也可以是带音轨的视频）解码成 ComfyUI AUDIO 字典。"""
    waveform, sample_rate = _decode_audio_file(path)
    if waveform is None or waveform.numel() == 0:
        raise ValueError(f"没有从参考音频中解码出声音: {path}")
    waveform = waveform.float()
    if waveform.dim() == 1:
        waveform = waveform.unsqueeze(0)
    return {"waveform": waveform.unsqueeze(0), "sample_rate": int(sample_rate)}


def _decode_audio_file(path):
    try:
        from comfy_extras.nodes_audio import load as load_comfy_audio
    except Exception:
        load_comfy_audio = None
    if load_comfy_audio is not None:
        return load_comfy_audio(path)
    return _decode_audio_with_av(path)


def _decode_audio_with_av(path):
    import av

    with av.open(path) as container:
        stream = next((item for item in container.streams if item.type == "audio"), None)
        if stream is None:
            raise ValueError(f"文件中没有音频流: {path}")
        sample_rate = int(stream.codec_context.sample_rate or stream.rate or 44100)
        channels = stream.codec_context.channels or 1

        chunks = []
        for frame in container.decode(streams=stream.index):
            chunk = torch.from_numpy(frame.to_ndarray())
            if chunk.shape[0] != channels:
                chunk = chunk.view(-1, channels).t()
            chunks.append(chunk)

        if not chunks:
            raise ValueError(f"没有解码出音频帧: {path}")
        waveform = torch.cat(chunks, dim=1)

    if waveform.dtype.is_floating_point:
        return waveform.float(), sample_rate
    if waveform.dtype == torch.int16:
        return waveform.float() / (2**15), sample_rate
    if waveform.dtype == torch.int32:
        return waveform.float() / (2**31), sample_rate
    raise ValueError(f"不支持的音频采样格式 {waveform.dtype}: {path}")


def _positive_float(value, default=None):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class start_workflow:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {},
            "optional": {
                "file_content": ("STRING", {"forceInput": True}),
                "image_input1": ("IMAGE", {}),
                "image_input2": ("IMAGE", {}),
                "file_path": ("STRING", {"default": ""}),
                "img_path1": ("STRING", {"default": "", "image_upload": True}),
                "img_path2": ("STRING", {"default": "", "image_upload": True}),
                "system_prompt": ("STRING", {"default": "你是一个强大的智能助手"}),
                "user_prompt": ("STRING", {"default": "你好"}),
                "positive_prompt": ("STRING", {"default": ""}),
                "negative_prompt": ("STRING", {"default": ""}),
                "model_name": ("STRING", {"default": ""}),
                "user_history": ("STRING", {"default": ""}),
                "duration": ("FLOAT", {"default": 5.0, "min": 0.0, "max": 120.0, "step": 0.1}),
                "fps": ("FLOAT", {"default": 16.0, "min": 1.0, "max": 120.0, "step": 0.1}),
                "resolution": ("STRING", {"default": ""}),
                "aspect_ratio": ("STRING", {"default": ""}),
                "size": ("STRING", {"default": ""}),
                "width": ("INT", {"default": 640, "min": 0, "max": 8192, "step": 8}),
                "height": ("INT", {"default": 640, "min": 0, "max": 8192, "step": 8}),
                # 多参考素材（MiniMax H3 ref2v 等）：最多 9 张参考图、3 段参考视频，
                # 每段视频可配 1 条音轨，另有 3 段独立参考音频。
                "img_path3": ("STRING", {"default": "", "image_upload": True}),
                "img_path4": ("STRING", {"default": "", "image_upload": True}),
                "img_path5": ("STRING", {"default": "", "image_upload": True}),
                "img_path6": ("STRING", {"default": "", "image_upload": True}),
                "img_path7": ("STRING", {"default": "", "image_upload": True}),
                "img_path8": ("STRING", {"default": "", "image_upload": True}),
                "img_path9": ("STRING", {"default": "", "image_upload": True}),
                "video_path1": ("STRING", {"default": "", "video_upload": True}),
                "video_path2": ("STRING", {"default": "", "video_upload": True}),
                "video_path3": ("STRING", {"default": "", "video_upload": True}),
                "video_audio_path1": ("STRING", {"default": "", "audio_upload": True}),
                "video_audio_path2": ("STRING", {"default": "", "audio_upload": True}),
                "video_audio_path3": ("STRING", {"default": "", "audio_upload": True}),
                "audio_path1": ("STRING", {"default": "", "audio_upload": True}),
                "audio_path2": ("STRING", {"default": "", "audio_upload": True}),
                "audio_path3": ("STRING", {"default": "", "audio_upload": True}),
            },
        }

    RETURN_TYPES = (
        "STRING",
        "IMAGE",
        "IMAGE",
        "STRING",
        "STRING",
        "STRING",
        "STRING",
        "STRING",
        "STRING",
        "FLOAT",
        "FLOAT",
        "STRING",
        "STRING",
        "STRING",
        "INT",
        "INT",
        "INT",
        # 参考图 image3..image9
        "IMAGE",
        "IMAGE",
        "IMAGE",
        "IMAGE",
        "IMAGE",
        "IMAGE",
        "IMAGE",
        # 参考视频 video1..video3
        "IMAGE",
        "IMAGE",
        "IMAGE",
        # 参考视频音轨 video_audio1..video_audio3
        "AUDIO",
        "AUDIO",
        "AUDIO",
        # 独立参考音频 audio1..audio3
        "AUDIO",
        "AUDIO",
        "AUDIO",
    )
    RETURN_NAMES = (
        "file_content",
        "image1",
        "image2",
        "system_prompt",
        "user_prompt",
        "positive_prompt",
        "negative_prompt",
        "model_name",
        "user_history",
        "duration",
        "fps",
        "resolution",
        "aspect_ratio",
        "size",
        "width",
        "height",
        "frame_count",
        "image3",
        "image4",
        "image5",
        "image6",
        "image7",
        "image8",
        "image9",
        "video1",
        "video2",
        "video3",
        "video_audio1",
        "video_audio2",
        "video_audio3",
        "audio1",
        "audio2",
        "audio3",
    )

    FUNCTION = "load_all"

    # OUTPUT_NODE = False

    CATEGORY = "大模型派对（llm_party）/工作流（workflow）"

    def load_all(
        self,
        file_content="",
        image_input1="",
        image_input2="",
        file_path="",
        img_path1="",
        img_path2="",
        system_prompt="你是一个强大的智能助手",
        user_prompt="你好",
        positive_prompt="",
        negative_prompt="",
        model_name="",
        user_history="",
        duration=5,
        fps=16,
        resolution="",
        aspect_ratio="",
        size="",
        width=640,
        height=640,
        img_path3="",
        img_path4="",
        img_path5="",
        img_path6="",
        img_path7="",
        img_path8="",
        img_path9="",
        video_path1="",
        video_path2="",
        video_path3="",
        video_audio_path1="",
        video_audio_path2="",
        video_audio_path3="",
        audio_path1="",
        audio_path2="",
        audio_path3="",
    ):
        file_out = []
        if file_content is not None and file_content != "":
            file_out += file_content
        if file_path is not None and file_path != "":
            if os.path.isdir(file_path):
                for path in os.listdir(file_path):
                    path = os.path.join(file_path, path)
                    file_out.extend(read_one(path))
            else:
                print(file_path)
                file_out = read_one(file_path)
                print(file_out)
        file_out = json.dumps(file_out, ensure_ascii=False, indent=4)
        img_out = None
        if isinstance(image_input1, torch.Tensor):
            img_out = image_input1
        img_out2 = None
        if isinstance(image_input2, torch.Tensor):
            img_out2 = image_input2
        if img_path1 is not None and img_path1 != "":
            img_out = load_image_batch(resolve_media_path(img_path1))

        if img_path2 is not None and img_path2 != "":
            img_out2 = load_image_batch(resolve_media_path(img_path2))

        system_out = system_prompt
        user_out = user_prompt
        positive_out = positive_prompt
        negative_out = negative_prompt
        model_name_out = model_name
        user_history_out = user_history
        try:
            duration_out = max(0.0, float(duration))
        except (TypeError, ValueError):
            duration_out = 5.0
        try:
            fps_out = max(1.0, float(fps))
        except (TypeError, ValueError):
            fps_out = 16.0
        try:
            width_out = int(width)
        except (TypeError, ValueError):
            width_out = 640
        try:
            height_out = int(height)
        except (TypeError, ValueError):
            height_out = 640
        width_out, height_out = resolve_video_dimensions(
            width_out,
            height_out,
            resolution,
            size,
            aspect_ratio,
        )
        frame_count_out = resolve_frame_count(duration_out, fps_out)

        # 参考图 3..9：@image / API 多图请求都会落到这里
        reference_image_outs = []
        for image_path in (img_path3, img_path4, img_path5, img_path6, img_path7, img_path8, img_path9):
            resolved_image = resolve_media_path(image_path)
            reference_image_outs.append(load_image_batch(resolved_image) if resolved_image else None)

        # 参考视频（参考视频模型的参考片段），音轨默认取视频自带的，
        # 也可以用 video_audio_pathN 覆盖
        reference_video_frames = []
        reference_video_audios = []
        for video_path, video_audio_path in (
            (video_path1, video_audio_path1),
            (video_path2, video_audio_path2),
            (video_path3, video_audio_path3),
        ):
            resolved_video = resolve_media_path(video_path)
            resolved_audio = resolve_media_path(video_audio_path)

            frames = None
            soundtrack = None
            if resolved_video:
                frames, soundtrack = load_reference_video(resolved_video, max_frames=frame_count_out)
                if soundtrack is None:
                    print(f"[start_workflow] 参考视频没有音轨，video_audio 输出为空: {resolved_video}")
            if resolved_audio:
                soundtrack = load_reference_audio(resolved_audio)
                if frames is None:
                    print(f"[start_workflow] 参考音轨缺少对应的参考视频，会被下游忽略: {resolved_audio}")
            reference_video_frames.append(frames)
            reference_video_audios.append(soundtrack)

        # 独立参考音频 1..3
        reference_audios = []
        for audio_path in (audio_path1, audio_path2, audio_path3):
            resolved_audio_path = resolve_media_path(audio_path)
            reference_audios.append(
                load_reference_audio(resolved_audio_path) if resolved_audio_path else None
            )

        return (
            file_out,
            img_out,
            img_out2,
            system_out,
            user_out,
            positive_out,
            negative_out,
            model_name_out,
            user_history_out,
            duration_out,
            fps_out,
            str(resolution or ""),
            str(aspect_ratio or ""),
            str(size or ""),
            width_out,
            height_out,
            frame_count_out,
            *reference_image_outs,
            *reference_video_frames,
            *reference_video_audios,
            *reference_audios,
        )


class load_img_path:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "img_path": ("STRING", {"default": "", "image_upload": True}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)

    FUNCTION = "load_all"

    # OUTPUT_NODE = False

    CATEGORY = "大模型派对（llm_party）/图片（image）"

    def load_all(self, img_path):
        img_out = []
        if img_path is not None and img_path != "":
            # 检查img_path是否是一个目录
            if os.path.isdir(img_path):
                # 遍历目录中的所有文件
                for filename in os.listdir(img_path):
                    # 检查文件是否是图片
                    if filename.lower().endswith((".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif")):
                        img_full_path = os.path.join(img_path, filename)
                        img = Image.open(img_full_path)
                        img = ImageOps.exif_transpose(img)
                        if img.mode == "I":
                            img = img.point(lambda i: i * (1 / 256)).convert("L")
                        image = img.convert("RGB")
                        image = np.array(image).astype(np.float32) / 255.0
                        image = torch.from_numpy(image).unsqueeze(0)
                        img_out.append(image)
            else:
                img = Image.open(img_path)
                for i in ImageSequence.Iterator(img):
                    i = ImageOps.exif_transpose(i)
                    if i.mode == "I":
                        i = i.point(lambda i: i * (1 / 256)).convert("L")
                    image = i.convert("RGB")
                    image = np.array(image).astype(np.float32) / 255.0
                    image = torch.from_numpy(image).unsqueeze(0)
                    img_out.append(image)

        if len(img_out) > 1:
            img_out = torch.cat(img_out, dim=0)
        elif img_out:
            img_out = img_out[0]
        if img_out == []:
            img_out = None
        return (img_out,)
