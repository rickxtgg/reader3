"""
IndexTTS2 Web API（FastAPI）

用法示例：
    python api.py -a 0.0.0.0 -p 9881 -c checkpoints/config.yaml -m checkpoints

主要接口：
    POST /tts  文本转语音，返回音频文件或JSON
    POST /regenerate_segment  重新生成指定片段
    GET  /project_info  获取项目信息
    GET  /estimate_memory  估算内存占用
    GET  /health 健康检查
"""

import argparse
import os
import sys
import time
from datetime import datetime
from typing import List, Optional, Union
import base64
import threading
import queue
import json
import logging
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import uvicorn
import shutil
from pathlib import Path

now_dir = os.getcwd()
sys.path.append(now_dir)
sys.path.append(os.path.join(now_dir, "indextts"))

from indextts.infer_v2_streaming import IndexTTS2Streaming, ProjectManager  # noqa: E402

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(now_dir, "api.log"), encoding='utf-8')
    ]
)
logger = logging.getLogger("IndexTTS2_API")

parser = argparse.ArgumentParser(description="IndexTTS2 API")
parser.add_argument("-a", "--bind_addr", type=str, default="127.0.0.1", help="绑定地址")
parser.add_argument("-p", "--port", type=int, default=9881, help="监听端口")
parser.add_argument("-c", "--config", type=str, default="checkpoints/config.yaml", help="配置文件路径")
parser.add_argument("-m", "--model_dir", type=str, default="checkpoints", help="模型目录")
parser.add_argument("--multi-gpu", action="store_true", help="启用模型并行(单模型跨多卡)")
parser.add_argument("--gpu-ids", type=str, default="", help="指定GPU ID列表, 逗号分隔")
parser.add_argument("--no-fp16", action="store_true", help="禁用FP16")
parser.add_argument("--no-cuda-kernel", action="store_true", help="禁用CUDA自定义核")
parser.add_argument("--use-accel", action="store_true", help="使用GPT2加速引擎")
parser.add_argument("--torch-compile", action="store_true", help="启用torch.compile")
parser.add_argument("--num-processes", type=int, default=1, help="工作进程数 (默认1，>1启用多进程并行)")
args = parser.parse_args()

# 全局变量，用于多进程模式下的worker
worker_engine = None

def init_worker(gpu_queue, engine_args):
    """
    工作进程初始化函数
    """
    global worker_engine
    
    # 获取分配的GPU ID
    try:
        device_id = gpu_queue.get(timeout=5)
    except queue.Empty:
        device_id = 0
        
    # 设置环境变量，确保每个进程只看到分配的GPU（如果不是多卡模型并行模式）
    # 注意：如果 multi_gpu=True，则 engine_args['multi_gpu'] 为 True，此时 worker 需要看到所有卡
    
    multi_gpu_mode = engine_args.get('multi_gpu', False)
    
    if not multi_gpu_mode:
        # 数据并行模式：每个 worker 独占一个 GPU (或者多个 worker 共享一个 GPU，通过 MPS)
        # 我们通过设置 device 参数来控制
        device = f"cuda:{device_id}"
        logger.info(f"Worker {os.getpid()} initializing on {device}...")
        
        # 覆写 multi_gpu 为 False，确保 IndexTTS2Streaming 不会尝试跨卡
        engine_args['multi_gpu'] = False
        engine_args['device'] = device
        engine_args['gpu_ids'] = [device_id]
    else:
        # 模型并行模式：每个 worker 都需要访问指定的 gpu_ids
        logger.info(f"Worker {os.getpid()} initializing in Model Parallel mode on GPUs {engine_args.get('gpu_ids')}...")
    
    try:
        # 初始化引擎
        worker_engine = IndexTTS2Streaming(**engine_args)
        logger.info(f"Worker {os.getpid()} initialization complete.")
    except Exception as e:
        logger.error(f"Worker {os.getpid()} initialization failed: {e}")
        raise e

def run_inference_task(method_name, ipc_queue=None, **kwargs):
    """
    在 worker 中执行推理任务
    """
    global worker_engine
    if worker_engine is None:
        raise RuntimeError("Worker engine not initialized")
    
    # 如果提供了 IPC 队列，说明需要跨进程回传进度（用于 tts_realtime）
    if ipc_queue is not None:
        def remote_progress_cb(current, total, seg_info):
            # 将进度信息放入跨进程队列
            # 注意：这里我们只传输基本数据类型，避免复杂的对象序列化问题
            if seg_info.get("status") != "completed":
                return
            
            audio_path = seg_info.get("audio_path")
            if not audio_path or not os.path.exists(audio_path):
                return
                
            try:
                # 读取音频数据
                with open(audio_path, "rb") as f:
                    audio_bytes = f.read()
                    
                payload = {
                    "event": "segment",
                    "index": seg_info.get("index"),
                    "duration": seg_info.get("duration"),
                    "text": seg_info.get("text"),
                    "audio_b64": base64.b64encode(audio_bytes).decode("utf-8"),
                }
                ipc_queue.put(payload)
            except Exception as e:
                ipc_queue.put({"event": "error", "message": f"Worker read failed: {e}"})
        
        # 将构造好的回调函数注入 kwargs
        kwargs['progress_callback'] = remote_progress_cb

    method = getattr(worker_engine, method_name)
    return method(**kwargs)

class MultiProcessTTSEngine:
    """
    多进程 TTS 引擎包装器
    """
    def __init__(self, num_processes, gpu_ids, engine_args):
        self.num_processes = num_processes
        self.gpu_ids = gpu_ids
        self.engine_args = engine_args
        # 创建一个持久的 Manager 用于管理跨进程队列
        self.manager = mp.Manager()
        self.executor = None # 不再使用 ProcessPoolExecutor，改用 mp.Pool
        self.pool = None
        self.gpu_queue = self.manager.Queue()

        
        # 准备 GPU 分配队列
        # 如果 gpu_ids 为空，默认使用 0
        available_gpus = gpu_ids if gpu_ids else [0]
        
        # 循环分配 GPU 给 worker
        for i in range(num_processes):
            self.gpu_queue.put(available_gpus[i % len(available_gpus)])
            
    # 重新实现为使用 mp.Pool
    def start_pool(self, gpu_ids, engine_args):
        # 准备 GPU 分配队列
        # 使用 self.gpu_queue 而不是新的 manager queue
        
        # 启动进程池
        # 设置 start_method 为 spawn 以确保 CUDA 正确初始化
        try:
            mp.set_start_method('spawn', force=True)
        except RuntimeError:
            pass
            
        self.pool = mp.Pool(
            processes=self.num_processes,
            initializer=init_worker,
            initargs=(self.gpu_queue, engine_args)
        )
        logger.info(f"Started multiprocessing pool with {self.num_processes} workers.")

    def infer_with_project(self, ipc_queue=None, **kwargs):
        # 如果传入了 progress_callback，必须移除，因为函数无法被 pickle
        if 'progress_callback' in kwargs:
            del kwargs['progress_callback']
            
        return self.pool.apply(run_inference_task, args=("infer_with_project", ipc_queue), kwds=kwargs)

    def regenerate_segment(self, **kwargs):
        return self.pool.apply(run_inference_task, args=("regenerate_segment", None), kwds=kwargs)
        
    def get_ipc_queue(self):
        """获取一个新的跨进程通信队列"""
        return self.manager.Queue()

        
    def shutdown(self):
        self.pool.close()
        self.pool.join()

# 初始化引擎逻辑
gpu_ids = [int(x) for x in args.gpu_ids.split(",") if x.strip().isdigit()]

engine_args = {
    "cfg_path": args.config,
    "model_dir": args.model_dir,
    "use_fp16": not args.no_fp16,
    "use_cuda_kernel": not args.no_cuda_kernel,
    "use_accel": args.use_accel,
    "use_torch_compile": args.torch_compile,
    "multi_gpu": args.multi_gpu,
    "gpu_ids": gpu_ids,
}

tts_engine = None

if mp.current_process().name == 'MainProcess':
    if args.num_processes > 1:
        logger.info(f"Initializing Multi-Process Engine with {args.num_processes} processes...")
        tts_engine = MultiProcessTTSEngine(args.num_processes, gpu_ids, engine_args)
        tts_engine.start_pool(gpu_ids, engine_args)
    else:
        logger.info("Initializing Single-Process Engine...")
        tts_engine = IndexTTS2Streaming(**engine_args)

app = FastAPI(title="IndexTTS2 API", version="0.1.0")

# 全局推理锁：用于单进程模式下保护非线程安全的模型
# 如果是多进程模式，此锁不会被使用（或没必要）
inference_mutex = threading.Lock()

def safe_infer_with_project(**kwargs):
    """安全执行推理，单进程模式下加锁"""
    if args.num_processes > 1:
        return tts_engine.infer_with_project(**kwargs)
    
    with inference_mutex:
        return tts_engine.infer_with_project(**kwargs)

def safe_regenerate_segment(**kwargs):
    """安全执行重新生成，单进程模式下加锁"""
    if args.num_processes > 1:
        return tts_engine.regenerate_segment(**kwargs)
        
    with inference_mutex:
        return tts_engine.regenerate_segment(**kwargs)


# ... (后续代码保持不变，直到 _tts_handler)

DEFAULT_OUTPUT_ROOT = os.path.join(now_dir, "outputs", "api")
os.makedirs(DEFAULT_OUTPUT_ROOT, exist_ok=True)

UPLOAD_AUDIO_DIR = os.path.join(now_dir, "uploads", "audio")
os.makedirs(UPLOAD_AUDIO_DIR, exist_ok=True)

PRESETS_DIR = os.path.join(now_dir, "presets")
os.makedirs(PRESETS_DIR, exist_ok=True)


class TTSRequest(BaseModel):
    text: str
    spk_audio_prompt: str
    output_dir: Optional[str] = None
    emo_audio_prompt: Optional[str] = None
    emo_alpha: float = 1.0
    emo_vector: Optional[List[float]] = None
    use_emo_text: bool = False
    emo_text: Optional[str] = None
    use_random: bool = False
    max_text_tokens_per_segment: int = 120
    interval_silence_ms: int = 200
    output_format: str = "wav"
    audio_bitdepth: str = "int16"
    resume_project: bool = True
    split_bucket: bool = True
    bucket_threshold: float = 0.75
    do_sample: bool = True
    top_p: float = 0.8
    top_k: int = 30
    temperature: float = 0.8
    length_penalty: float = 0.0
    num_beams: int = 3
    repetition_penalty: float = 10.0
    max_mel_tokens: int = 1500
    return_json_only: bool = False
    verbose: bool = False


class RegenerateRequest(BaseModel):
    output_dir: str
    segment_index: int
    spk_audio_prompt: Optional[str] = None
    emo_audio_prompt: Optional[str] = None
    emo_alpha: float = 1.0
    emo_vector: Optional[List[float]] = None
    output_format: Optional[str] = None
    audio_bitdepth: Optional[str] = None
    do_sample: bool = True
    top_p: float = 0.8
    top_k: int = 30
    temperature: float = 0.8
    length_penalty: float = 0.0
    num_beams: int = 3
    repetition_penalty: float = 10.0
    max_mel_tokens: int = 1500
    return_json_only: bool = False
    verbose: bool = False


class RestoreRequest(BaseModel):
    project_name: str
    segment_index: int
    version: int


class PreviewRequest(BaseModel):
    text: str
    max_tokens: int = 120


class Preset(BaseModel):
    name: str
    emo_control_method: Union[int, str]
    emo_weight: float
    emo_vector: List[float]
    emo_text: str
    emo_random: bool
    max_tokens: int
    interval_silence: int
    do_sample: bool
    top_p: float
    top_k: int
    temperature: float
    length_penalty: float
    num_beams: int
    repetition_penalty: float
    max_mel_tokens: int
    created_at: Optional[str] = None



def _build_output_dir(custom_dir: Optional[str]) -> str:
    if custom_dir:
        return custom_dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(DEFAULT_OUTPUT_ROOT, f"job_{ts}_{int(time.time() * 1000)}")


def _guess_media_type(fmt: str) -> str:
    fmt = (fmt or "wav").lower()
    if fmt in {"wav", "mp3", "flac", "ogg"}:
        return f"audio/{fmt}"
    return "application/octet-stream"


def _safe_json_response(error_msg: str, status: int = 400):
    return JSONResponse(status_code=status, content={"message": error_msg})


def _file_chunk_generator(path: str, chunk_size: int = 65536):
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/upload_audio")
async def upload_audio(audio_file: UploadFile = File(...)):
    """
    上传参考音频文件到服务器
    
    返回上传后的文件路径，可用于后续的 spk_audio_prompt 或 emo_audio_prompt
    """
    try:
        # 读取文件内容
        file_content = await audio_file.read()
        
        # 检查文件是否为空
        if not file_content or len(file_content) == 0:
            return _safe_json_response("上传的文件为空", status=400)
        
        # 检查文件类型（允许更宽松的检查，因为某些客户端可能不发送正确的 content_type）
        if audio_file.content_type and not audio_file.content_type.startswith("audio/"):
            # 如果不是音频类型，检查文件扩展名
            filename = audio_file.filename or ""
            if not any(filename.lower().endswith(ext) for ext in [".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"]):
                return _safe_json_response(f"不支持的文件类型: {audio_file.content_type}", status=400)
        
        # 生成唯一文件名
        import hashlib
        file_hash = hashlib.md5(file_content).hexdigest()[:8]
        file_ext = os.path.splitext(audio_file.filename or "audio")[1] or ".wav"
        original_filename = audio_file.filename or "uploaded_audio"
        safe_filename = f"{file_hash}_{original_filename}"
        # 清理文件名，保留更多字符
        safe_filename = "".join(c for c in safe_filename if c.isalnum() or c in "._-")
        if not safe_filename.endswith(file_ext):
            safe_filename += file_ext
        
        save_path = os.path.join(UPLOAD_AUDIO_DIR, safe_filename)
        
        # 确保目录存在
        os.makedirs(UPLOAD_AUDIO_DIR, exist_ok=True)
        
        # 保存文件
        with open(save_path, "wb") as f:
            f.write(file_content)
        
        # 验证文件是否成功保存
        if not os.path.exists(save_path) or os.path.getsize(save_path) == 0:
            return _safe_json_response("文件保存失败", status=500)
        
        return {
            "message": "success",
            "file_path": save_path,
            "relative_path": os.path.relpath(save_path, now_dir),
            "filename": safe_filename,
            "file_size": len(file_content)
        }
    except Exception as e:
        import traceback
        error_msg = f"upload failed: {str(e)}\n{traceback.format_exc()}"
        return _safe_json_response(error_msg, status=500)


@app.get("/uploaded_audios")
async def get_uploaded_audios():
    """获取已上传的参考音频列表"""
    if not os.path.exists(UPLOAD_AUDIO_DIR):
        return []
    
    audios = []
    for f in os.listdir(UPLOAD_AUDIO_DIR):
        if f.lower().endswith(('.wav', '.mp3', '.flac', '.ogg', '.m4a', '.aac')):
            path = os.path.join(UPLOAD_AUDIO_DIR, f)
            audios.append({
                "filename": f,
                "path": path,
                "relative_path": os.path.relpath(path, now_dir),
                "size": os.path.getsize(path),
                "modified": os.path.getmtime(path)
            })
    # 按修改时间倒序
    audios.sort(key=lambda x: x["modified"], reverse=True)
    return audios


@app.get("/presets")
async def list_presets():
    """获取所有预设列表"""
    if not os.path.exists(PRESETS_DIR):
        return []
    presets = []
    for f in os.listdir(PRESETS_DIR):
        if f.endswith(".json"):
            try:
                with open(os.path.join(PRESETS_DIR, f), "r", encoding="utf-8") as file:
                    data = json.load(file)
                    # 确保 name 字段存在
                    if "name" not in data:
                        data["name"] = f[:-5]
                    presets.append(data)
            except Exception:
                pass
    # 按创建时间倒序
    presets.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return presets


@app.post("/presets")
async def save_preset(preset: Preset):
    """保存预设"""
    if not preset.name:
        return _safe_json_response("Preset name is required", status=400)
    
    # 清理名称
    safe_name = "".join(c for c in preset.name if c.isalnum() or c in "._- ")
    if not safe_name:
        return _safe_json_response("Invalid preset name", status=400)
        
    preset.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_path = os.path.join(PRESETS_DIR, f"{safe_name}.json")
    
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(preset.model_dump(), f, ensure_ascii=False, indent=2)
    except Exception as e:
        return _safe_json_response(f"Save preset failed: {e}", status=500)
        
    return {"message": "success", "name": safe_name}


@app.delete("/presets/{name}")
async def delete_preset(name: str):
    """删除预设"""
    safe_name = "".join(c for c in name if c.isalnum() or c in "._- ")
    file_path = os.path.join(PRESETS_DIR, f"{safe_name}.json")
    
    if not os.path.exists(file_path):
        return _safe_json_response("Preset not found", status=404)
        
    try:
        os.remove(file_path)
    except Exception as e:
        return _safe_json_response(f"Delete preset failed: {e}", status=500)
        
    return {"message": "success"}


@app.get("/control")
async def control(command: str = None):
    """
    控制接口：
    - restart: 重启进程
    - exit: 终止进程
    """
    if command is None:
        return _safe_json_response("command is required")
    if command == "restart":
        os.execl(sys.executable, sys.executable, *sys.argv)
    elif command == "exit":
        os.kill(os.getpid(), 15)
    else:
        return _safe_json_response("unsupported command")
    return {"message": "ok"}


async def _tts_handler(req_dict: dict):
    """统一的TTS处理逻辑"""
    if not req_dict.get("text") or not req_dict.get("spk_audio_prompt"):
        return _safe_json_response("text 和 spk_audio_prompt 为必填参数")

    # 检查参考音频文件是否存在
    spk_audio_prompt = req_dict["spk_audio_prompt"]
    if not os.path.exists(spk_audio_prompt):
        # 尝试相对路径
        rel_path = os.path.join(now_dir, spk_audio_prompt)
        if os.path.exists(rel_path):
            req_dict["spk_audio_prompt"] = rel_path
        else:
            return _safe_json_response(
                f"参考音频文件不存在: {spk_audio_prompt} (尝试的路径: {rel_path})", 
                status=400
            )

    output_dir = _build_output_dir(req_dict.get("output_dir"))
    os.makedirs(output_dir, exist_ok=True)

    try:
        # 使用 run_in_threadpool 在线程池中运行阻塞的推理函数
        # 注意：如果是多进程模式，tts_engine.infer_with_project 会在进程池中运行
        result = await run_in_threadpool(
            safe_infer_with_project,
            spk_audio_prompt=req_dict["spk_audio_prompt"],
            text=req_dict["text"],
            output_dir=output_dir,
            emo_audio_prompt=req_dict.get("emo_audio_prompt"),
            emo_alpha=req_dict.get("emo_alpha", 1.0),
            emo_vector=req_dict.get("emo_vector"),
            use_emo_text=req_dict.get("use_emo_text", False),
            emo_text=req_dict.get("emo_text"),
            use_random=req_dict.get("use_random", False),
            max_text_tokens_per_segment=req_dict.get("max_text_tokens_per_segment", 120),
            interval_silence_ms=req_dict.get("interval_silence_ms", 200),
            output_format=req_dict.get("output_format", "wav"),
            resume_project=req_dict.get("resume_project", True),
            split_bucket=req_dict.get("split_bucket", True),
            bucket_threshold=req_dict.get("bucket_threshold", 0.75),
            verbose=req_dict.get("verbose", False),
            do_sample=req_dict.get("do_sample", True),
            top_p=req_dict.get("top_p", 0.8),
            top_k=req_dict.get("top_k", 30),
            temperature=req_dict.get("temperature", 0.8),
            length_penalty=req_dict.get("length_penalty", 0.0),
            num_beams=req_dict.get("num_beams", 3),
            repetition_penalty=req_dict.get("repetition_penalty", 10.0),
            max_mel_tokens=req_dict.get("max_mel_tokens", 1500),
            audio_bitdepth=req_dict.get("audio_bitdepth", "int16"),
        )
        
        # 记录处理完成的文本内容
        logger.info(f"Processed text: {req_dict['text'][:100]}..." if len(req_dict['text']) > 100 else f"Processed text: {req_dict['text']}")
        
    except Exception as e:  # pragma: no cover - runtime guard
        import traceback
        error_detail = traceback.format_exc()
        # 在verbose模式下返回详细错误，否则只返回简要信息
        if req_dict.get("verbose", False):
            return _safe_json_response(f"infer failed: {str(e)}\n{error_detail}", status=500)
        else:
            return _safe_json_response(f"infer failed: {str(e)}", status=500)

    final_audio = result.get("final_audio")
    return_json_only = req_dict.get("return_json_only", False)
    if return_json_only:
        return result
    if not final_audio or not os.path.exists(final_audio):
        return JSONResponse(status_code=500, content={"message": "audio file not found", **result})

    media_type = _guess_media_type(req_dict.get("output_format", "wav"))
    return FileResponse(final_audio, media_type=media_type, filename=os.path.basename(final_audio))


@app.get("/tts")
async def tts_get(
    text: str = None,
    spk_audio_prompt: str = None,
    output_dir: Optional[str] = None,
    emo_audio_prompt: Optional[str] = None,
    emo_alpha: float = 1.0,
    emo_vector: Optional[str] = None,  # JSON字符串格式
    use_emo_text: bool = False,
    emo_text: Optional[str] = None,
    use_random: bool = False,
    max_text_tokens_per_segment: int = 120,
    interval_silence_ms: int = 200,
    output_format: str = "wav",
    audio_bitdepth: str = "int16",
    resume_project: bool = True,
    split_bucket: bool = True,
    bucket_threshold: float = 0.75,
    do_sample: bool = True,
    top_p: float = 0.8,
    top_k: int = 30,
    temperature: float = 0.8,
    length_penalty: float = 0.0,
    num_beams: int = 3,
    repetition_penalty: float = 10.0,
    max_mel_tokens: int = 1500,
    return_json_only: bool = False,
    verbose: bool = False,
):
    """GET方法支持，从query参数读取"""
    req_dict = {
        "text": text,
        "spk_audio_prompt": spk_audio_prompt,
        "output_dir": output_dir,
        "emo_audio_prompt": emo_audio_prompt,
        "emo_alpha": emo_alpha,
        "emo_vector": json.loads(emo_vector) if emo_vector else None,
        "use_emo_text": use_emo_text,
        "emo_text": emo_text,
        "use_random": use_random,
        "max_text_tokens_per_segment": max_text_tokens_per_segment,
        "interval_silence_ms": interval_silence_ms,
        "output_format": output_format,
        "audio_bitdepth": audio_bitdepth,
        "resume_project": resume_project,
        "split_bucket": split_bucket,
        "bucket_threshold": bucket_threshold,
        "do_sample": do_sample,
        "top_p": top_p,
        "top_k": top_k,
        "temperature": temperature,
        "length_penalty": length_penalty,
        "num_beams": num_beams,
        "repetition_penalty": repetition_penalty,
        "max_mel_tokens": max_mel_tokens,
        "return_json_only": return_json_only,
        "verbose": verbose,
    }
    return await _tts_handler(req_dict)


@app.post("/tts")
async def tts(req: TTSRequest):
    """POST方法支持，从JSON body读取"""
    # 兼容 Pydantic V1 和 V2
    try:
        req_dict = req.model_dump()  # Pydantic V2
    except AttributeError:
        req_dict = req.dict()  # Pydantic V1 兼容
    return await _tts_handler(req_dict)


@app.get("/api/tts/speak")
async def tts_speak_compat(
    text: str = None,
    spk_audio_prompt: str = None,
    output_dir: Optional[str] = None,
    emo_audio_prompt: Optional[str] = None,
    emo_alpha: float = 1.0,
    use_emo_text: bool = False,
    output_format: str = "wav",
    audio_bitdepth: str = "int16",
    max_text_tokens_per_segment: int = 120,
    interval_silence_ms: int = 200,
    do_sample: bool = True,
    top_p: float = 0.8,
    top_k: int = 30,
    temperature: float = 0.8,
    length_penalty: float = 0.0,
    num_beams: int = 3,
    repetition_penalty: float = 10.0,
    max_mel_tokens: int = 1500,
):
    """
    兼容性接口：/api/tts/speak
    重定向到 /tts，支持GET方法
    """
    req_dict = {
        "text": text,
        "spk_audio_prompt": spk_audio_prompt,
        "output_dir": output_dir,
        "emo_audio_prompt": emo_audio_prompt,
        "emo_alpha": emo_alpha,
        "use_emo_text": use_emo_text,
        "output_format": output_format,
        "audio_bitdepth": audio_bitdepth,
        "max_text_tokens_per_segment": max_text_tokens_per_segment,
        "interval_silence_ms": interval_silence_ms,
        "do_sample": do_sample,
        "top_p": top_p,
        "top_k": top_k,
        "temperature": temperature,
        "length_penalty": length_penalty,
        "num_beams": num_beams,
        "repetition_penalty": repetition_penalty,
        "max_mel_tokens": max_mel_tokens,
        "return_json_only": False,
        "verbose": False,
    }
    return await _tts_handler(req_dict)


@app.post("/tts_stream")
async def tts_stream(req: TTSRequest):
    """
    流式（分块传输）返回生成的音频文件。
    说明：推理过程仍需先完成；完成后以chunked方式回传文件，便于客户端边下边播。
    """
    if not req.text or not req.spk_audio_prompt:
        return _safe_json_response("text 和 spk_audio_prompt 为必填参数")

    output_dir = _build_output_dir(req.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    try:
        result = await run_in_threadpool(
            safe_infer_with_project,
            spk_audio_prompt=req.spk_audio_prompt,
            text=req.text,
            output_dir=output_dir,
            emo_audio_prompt=req.emo_audio_prompt,
            emo_alpha=req.emo_alpha,
            emo_vector=req.emo_vector,
            use_emo_text=req.use_emo_text,
            emo_text=req.emo_text,
            use_random=req.use_random,
            max_text_tokens_per_segment=req.max_text_tokens_per_segment,
            interval_silence_ms=req.interval_silence_ms,
            output_format=req.output_format,
            resume_project=req.resume_project,
            split_bucket=req.split_bucket,
            bucket_threshold=req.bucket_threshold,
            verbose=req.verbose,
            do_sample=req.do_sample,
            top_p=req.top_p,
            top_k=req.top_k,
            temperature=req.temperature,
            length_penalty=req.length_penalty,
            num_beams=req.num_beams,
            repetition_penalty=req.repetition_penalty,
            max_mel_tokens=req.max_mel_tokens,
            audio_bitdepth=req.audio_bitdepth,
        )
    except Exception as e:  # pragma: no cover
        return _safe_json_response(f"infer failed: {e}", status=500)

    final_audio = result.get("final_audio")
    if req.return_json_only:
        return result
    if not final_audio or not os.path.exists(final_audio):
        return JSONResponse(status_code=500, content={"message": "audio file not found", **result})

    media_type = _guess_media_type(req.output_format)
    return StreamingResponse(_file_chunk_generator(final_audio), media_type=media_type)


@app.post("/tts_realtime")
async def tts_realtime(req: TTSRequest):
    """
    真正实时推送：在生成每个片段后，立即以NDJSON行流式返回，携带base64音频。
    事件格式：
      {"event":"segment","index":int,"duration":float,"audio_b64":str,"text":str}
      {"event":"done","final_audio":str,"subtitle_srt":str,"subtitle_vtt":str}
      {"event":"error","message":str}
    """
    if not req.text or not req.spk_audio_prompt:
        return _safe_json_response("text 和 spk_audio_prompt 为必填参数")

    output_dir = _build_output_dir(req.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    q: "queue.Queue[dict]" = queue.Queue()

    def progress_cb(current: int, total: int, seg_info: dict):
        # 只处理完成状态
        if seg_info.get("status") != "completed":
            return
        audio_path = seg_info.get("audio_path")
        if not audio_path or not os.path.exists(audio_path):
            return
        try:
            with open(audio_path, "rb") as f:
                audio_bytes = f.read()
            payload = {
                "event": "segment",
                "index": seg_info.get("index"),
                "duration": seg_info.get("duration"),
                "text": seg_info.get("text"),
                "audio_b64": base64.b64encode(audio_bytes).decode("utf-8"),
            }
            q.put(payload)
        except Exception as e:  # pragma: no cover
            q.put({"event": "error", "message": f"read segment failed: {e}"})

    def worker():
        ipc_queue = None
        stop_event = threading.Event()
        
        try:
            # 实时推理也需要加锁保护
            if args.num_processes > 1:
                # 获取一个跨进程队列
                ipc_queue = tts_engine.get_ipc_queue()
                
                # 启动一个后台线程来搬运 IPC 队列的数据到本地队列 q
                def pump_ipc():
                    while not stop_event.is_set():
                        try:
                            # 设定超时，以便能够响应停止信号
                            item = ipc_queue.get(timeout=0.1)
                            q.put(item)
                        except queue.Empty:
                            continue
                        except Exception:
                            break
                            
                pump_thread = threading.Thread(target=pump_ipc, daemon=True)
                pump_thread.start()
                
                result = tts_engine.infer_with_project(
                    ipc_queue=ipc_queue,  # 传入 IPC 队列
                    spk_audio_prompt=req.spk_audio_prompt,
                    text=req.text,
                    output_dir=output_dir,
                    emo_audio_prompt=req.emo_audio_prompt,
                    emo_alpha=req.emo_alpha,
                    emo_vector=req.emo_vector,
                    use_emo_text=req.use_emo_text,
                    emo_text=req.emo_text,
                    use_random=req.use_random,
                    max_text_tokens_per_segment=req.max_text_tokens_per_segment,
                    interval_silence_ms=req.interval_silence_ms,
                    output_format=req.output_format,
                    resume_project=req.resume_project,
                    split_bucket=req.split_bucket,
                    bucket_threshold=req.bucket_threshold,
                    verbose=req.verbose,
                    do_sample=req.do_sample,
                    top_p=req.top_p,
                    top_k=req.top_k,
                    temperature=req.temperature,
                    length_penalty=req.length_penalty,
                    num_beams=req.num_beams,
                    repetition_penalty=req.repetition_penalty,
                    max_mel_tokens=req.max_mel_tokens,
                    audio_bitdepth=req.audio_bitdepth,
                    # progress_callback 会在 MultiProcessTTSEngine 内部被处理
                )
                
                # 任务完成，停止搬运线程
                stop_event.set()
                pump_thread.join(timeout=1.0)
                
            else:
                with inference_mutex:
                    result = tts_engine.infer_with_project(
                        spk_audio_prompt=req.spk_audio_prompt,
                        text=req.text,
                        output_dir=output_dir,
                        emo_audio_prompt=req.emo_audio_prompt,
                        emo_alpha=req.emo_alpha,
                        emo_vector=req.emo_vector,
                        use_emo_text=req.use_emo_text,
                        emo_text=req.emo_text,
                        use_random=req.use_random,
                        max_text_tokens_per_segment=req.max_text_tokens_per_segment,
                        interval_silence_ms=req.interval_silence_ms,
                        output_format=req.output_format,
                        resume_project=req.resume_project,
                        split_bucket=req.split_bucket,
                        bucket_threshold=req.bucket_threshold,
                        verbose=req.verbose,
                        do_sample=req.do_sample,
                        top_p=req.top_p,
                        top_k=req.top_k,
                        temperature=req.temperature,
                        length_penalty=req.length_penalty,
                        num_beams=req.num_beams,
                        repetition_penalty=req.repetition_penalty,
                        max_mel_tokens=req.max_mel_tokens,
                        audio_bitdepth=req.audio_bitdepth,
                        progress_callback=progress_cb,
                    )
            q.put({"event": "done", **result})
        except Exception as e:  # pragma: no cover
            q.put({"event": "error", "message": f"infer failed: {e}"})
        finally:
            stop_event.set() # 确保线程退出
            q.put({"event": "__end__"})

    # 这里的worker已经是独立的线程，不会阻塞主线程，无需 run_in_threadpool
    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            # 如果是多进程模式，我们需要轮询 ipc_queue
            # 但这里我们简化逻辑：worker 线程会把 ipc_queue 的内容搬运到 q 中
            # 实际上，上面的 worker 函数是在线程中运行的，它负责等待结果
            
            # 对于多进程流式，我们需要一种机制来搬运数据
            # 由于 pool.apply 是阻塞的，worker 函数会一直等到推理完成
            # 所以我们需要另一个线程或者在 worker 函数中边等边搬运？
            # 不，pool.apply 是在 worker 线程中调用的。
            # 但是，pool.apply 会阻塞直到整个任务完成。
            # 这意味着在任务进行中，我们无法从 pool.apply 的返回值获取数据。
            # 数据必须通过 ipc_queue 实时传回来。
            
            # 所以，我们需要一个单独的线程来读取 ipc_queue 并放入 q
            # 但是为了简化，我们让 worker 线程只负责提交任务
            # 而读取 ipc_queue 的工作... 比较麻烦，因为我们不知道何时结束。
            # 改进方案：使用一个共享的 ipc_queue，并在 worker 线程中启动一个子线程来读取它？
            # 或者，直接让 stream 函数读取 q。
            # 问题是：谁负责从 ipc_queue 读取数据放入 q？
            
            # 让我们修改 worker 函数，使其支持多进程的流式搬运
            pass
            
            item = q.get()
            if item.get("event") == "__end__":
                break
            yield (json.dumps(item, ensure_ascii=False) + "\n")

    return StreamingResponse(stream(), media_type="application/jsonlines")


@app.post("/regenerate_segment")
async def regenerate_segment(req: RegenerateRequest):
    try:
        result = await run_in_threadpool(
            safe_regenerate_segment,
            output_dir=req.output_dir,
            segment_index=req.segment_index,
            spk_audio_prompt=req.spk_audio_prompt,
            emo_audio_prompt=req.emo_audio_prompt,
            emo_alpha=req.emo_alpha,
            emo_vector=req.emo_vector,
            output_format=req.output_format,
            audio_bitdepth=req.audio_bitdepth,
            verbose=req.verbose,
            do_sample=req.do_sample,
            top_p=req.top_p,
            top_k=req.top_k,
            temperature=req.temperature,
            length_penalty=req.length_penalty,
            num_beams=req.num_beams,
            repetition_penalty=req.repetition_penalty,
            max_mel_tokens=req.max_mel_tokens,
        )
    except Exception as e:  # pragma: no cover
        return _safe_json_response(f"regenerate failed: {e}", status=500)

    audio_path = result.get("audio_file")
    if req.return_json_only or not audio_path or not os.path.exists(audio_path):
        return result

    media_type = _guess_media_type(req.output_format or "wav")
    return FileResponse(audio_path, media_type=media_type, filename=os.path.basename(audio_path))


@app.get("/set_gpt_weights")
async def set_gpt_weights(weights_path: str = None):
    if not weights_path:
        return _safe_json_response("weights_path is required")
    try:
        return tts_engine.set_gpt_weights(weights_path)
    except Exception as e:  # pragma: no cover
        return _safe_json_response(f"set_gpt_weights failed: {e}", status=500)


@app.get("/set_s2mel_weights")
async def set_s2mel_weights(weights_path: str = None):
    if not weights_path:
        return _safe_json_response("weights_path is required")
    try:
        return tts_engine.set_s2mel_weights(weights_path)
    except Exception as e:  # pragma: no cover
        return _safe_json_response(f"set_s2mel_weights failed: {e}", status=500)


@app.get("/set_bigvgan_weights")
async def set_bigvgan_weights(weights_path: str = None, use_cuda_kernel: Optional[bool] = None):
    if not weights_path:
        return _safe_json_response("weights_path is required")
    try:
        return tts_engine.set_bigvgan_weights(weights_path, use_cuda_kernel)
    except Exception as e:  # pragma: no cover
        return _safe_json_response(f"set_bigvgan_weights failed: {e}", status=500)


@app.get("/project_info")
async def project_info(output_dir: str):
    try:
        info = tts_engine.get_project_info(output_dir)
    except Exception as e:  # pragma: no cover
        return _safe_json_response(f"get project info failed: {e}", status=500)
    if info is None:
        return _safe_json_response("项目不存在或索引文件缺失", status=404)
    return info


@app.get("/estimate_memory")
async def estimate_memory(text_length: int, max_tokens_per_segment: int = 120):
    try:
        from indextts.infer_v2_streaming import estimate_memory_usage
        return estimate_memory_usage(text_length, max_tokens_per_segment)
    except Exception as e:  # pragma: no cover
        return _safe_json_response(f"estimate failed: {e}", status=500)


@app.post("/preview_split")
async def preview_split(req: PreviewRequest):
    """预览文本分段结果"""
    if not req.text:
        return []

    def _split():
        # 使用 tts_engine 的分词器
        tokens = tts_engine.tokenizer.tokenize(req.text)
        segments = tts_engine.tokenizer.split_segments(tokens, req.max_tokens)

        # 清理文本
        def clean_text(t):
            import re
            t = t.replace('▁', '')
            t = re.sub(r'\s+', ' ', t)
            return t.strip()

        result = []
        for i, seg in enumerate(segments):
            text = clean_text(''.join(seg))
            result.append({
                "index": i,
                "text": text,
                "tokens": len(seg)
            })
        return result

    return await run_in_threadpool(_split)


@app.get("/projects")
async def list_projects():
    """列出所有项目"""
    projects = []
    if not os.path.exists(DEFAULT_OUTPUT_ROOT):
        return projects

    def _list():
        res = []
        for name in os.listdir(DEFAULT_OUTPUT_ROOT):
            project_path = os.path.join(DEFAULT_OUTPUT_ROOT, name)
            index_file = os.path.join(project_path, "index.json")

            if os.path.isdir(project_path) and os.path.exists(index_file):
                try:
                    with open(index_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    res.append({
                        "name": name,
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                        "total_segments": data.get("total_segments"),
                        "completed_segments": data.get("completed_segments"),
                        "total_duration": data.get("total_duration")
                    })
                except Exception:
                    pass
        # 按创建时间倒序
        res.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return res

    return await run_in_threadpool(_list)


@app.get("/files/{path:path}")
async def get_file(path: str):
    """下载输出目录下的文件（防止路径穿越）"""
    # 规范化路径
    abs_root = os.path.abspath(DEFAULT_OUTPUT_ROOT)
    abs_path = os.path.abspath(os.path.join(abs_root, path))
    
    # 安全检查：必须落在输出根目录内部，不能只共享前缀
    root_n = os.path.normcase(abs_root)
    path_n = os.path.normcase(abs_path)
    prefix = root_n if root_n.endswith(os.sep) else root_n + os.sep
    if path_n != root_n and not path_n.startswith(prefix):
        return _safe_json_response("Access denied", status=403)
    
    if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
        return _safe_json_response("File not found", status=404)

    media_type = _guess_media_type(os.path.splitext(abs_path)[1][1:])
    return FileResponse(abs_path, media_type=media_type)


@app.get("/project/{project_name}/history/{segment_index}")
async def get_history(project_name: str, segment_index: int):
    """获取片段历史记录"""
    project_path = os.path.join(DEFAULT_OUTPUT_ROOT, project_name)
    if not os.path.exists(project_path):
        return _safe_json_response("Project not found", status=404)

    def _get():
        project = ProjectManager(project_path)
        if not project.load_project():
            return []
        return project.get_segment_history(segment_index)

    return await run_in_threadpool(_get)


@app.post("/project/restore")
async def restore_version(req: RestoreRequest):
    """恢复片段到指定版本"""
    project_path = os.path.join(DEFAULT_OUTPUT_ROOT, req.project_name)
    if not os.path.exists(project_path):
        return _safe_json_response("Project not found", status=404)

    def _restore():
        project = ProjectManager(project_path)
        if not project.load_project():
            raise HTTPException(404, "Project load failed")

        if project.restore_segment_version(req.segment_index, req.version):
            # 重新合并音频和生成字幕
            try:
                project.merge_audio()
                project.generate_subtitles()
            except Exception as e:
                print(f"Merge/Subtitle failed after restore: {e}")
                
            return {
                "message": "success", 
                "new_version": project.segments[req.segment_index].current_version,
                "audio_file": project.segments[req.segment_index].audio_file
            }
        else:
            raise HTTPException(400, "Restore failed")

    try:
        return await run_in_threadpool(_restore)
    except HTTPException as e:
        raise e
    except Exception as e:
        return _safe_json_response(str(e), status=500)


@app.get("/project/{project_name}/detect_corrupted")
async def detect_corrupted(project_name: str, threshold: float = 0.99):
    """检测削波/损坏的音频片段"""
    project_path = os.path.join(DEFAULT_OUTPUT_ROOT, project_name)
    if not os.path.exists(project_path):
        return _safe_json_response("Project not found", status=404)

    def _detect():
        import soundfile as sf
        import numpy as np

        project = ProjectManager(project_path)
        if not project.load_project():
            return []

        corrupted = []
        for seg in project.segments:
            if seg.status != "completed" or not seg.audio_file:
                continue

            audio_path = os.path.join(project_path, seg.audio_file)
            if not os.path.exists(audio_path):
                continue

            try:
                audio, sr = sf.read(audio_path)

                if audio.dtype == np.int16:
                    max_val = 32767
                elif audio.dtype == np.int32:
                    max_val = 2147483647
                else:
                    max_val = 1.0

                # 计算削波比例
                clipped_samples = np.sum(np.abs(audio) >= max_val * threshold)
                clip_ratio = clipped_samples / len(audio)

                if clip_ratio > 0.01:  # 超过1%样本削波
                    corrupted.append({
                        "index": seg.index,
                        "text": seg.text,
                        "clip_ratio": float(clip_ratio),
                        "audio_file": seg.audio_file
                    })
            except Exception:
                pass
        return corrupted

    return await run_in_threadpool(_detect)


if __name__ == "__main__":
    uvicorn.run(app, host=args.bind_addr, port=args.port)

