"""
EPUB 阅读器 Web 服务
"""

import os
import json
import asyncio
import tempfile
import re
import uuid
import shutil
import subprocess
import concurrent.futures
import hashlib
import time
import base64
import ctypes
from pathlib import Path
from typing import Optional, List, Dict, Any
from collections import OrderedDict
from contextlib import asynccontextmanager
from shutil import which
from urllib.parse import quote, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException, Query, UploadFile, File, Form, Body
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from book_index_utils import add_book_to_index as shared_add_book_to_index
from book_index_utils import clean_path
from book_index_utils import load_books_index as shared_load_books_index
from book_index_utils import norm_path as shared_norm_path
from book_index_utils import save_books_index as shared_save_books_index
from reader3 import Book, process_epub, save_to_json

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 提供静态资源服务
app.mount("/static", StaticFiles(directory="static"), name="static")

"""
模块结构:
1. 配置管理与全局常量
2. 书籍索引与目录管理
3. 导入任务与进度查询
4. 书籍缓存与封面索引
5. 书库视图与阅读路由
6. 备份区与删除/恢复操作
7. TTS 配置、参考音频与朗读进度
8. TTS 引擎代理与 HTTP 客户端
9. TTS 合成实现 (Edge / GPT-SoVITS / Index-TTS)
"""

# === 配置管理 ===

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "book_directories": ["."],
    "tts": {
        "default_engine": "edge",
        "edge": {
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "volume": "+0%",
        "pitch": "+0Hz"
        },
        "gptsovits": {
        "api_url": "http://127.0.0.1:9880",
        "text_lang": "zh",
        "ref_audio_path": "",
        "prompt_text": "",
        "prompt_lang": "zh",
        "top_k": 5,
        "top_p": 1,
        "temperature": 1,
        "text_split_method": "cut5",
        "batch_size": 1,
        "batch_threshold": 0.75,
        "split_bucket": True,
        "speed_factor": 1.0,
        "streaming_mode": True,
        "seed": -1,
        "parallel_infer": True,
        "repetition_penalty": 1.35
        },
        "indextts": {
            "api_url": "http://127.0.0.1:9881",
            "spk_audio_prompt": "",
            "emo_audio_prompt": "",
            "emo_alpha": 1.0,
            "emo_vector": None,
            "use_emo_text": False,
            "emo_text": "",
            "use_random": False,
            "output_format": "wav",
            "audio_bitdepth": "int16",
            "max_text_tokens_per_segment": 120,
            "interval_silence_ms": 200,
            "resume_project": True,
            "split_bucket": True,
            "bucket_threshold": 0.75,
            "do_sample": True,
            "top_p": 0.8,
            "top_k": 30,
            "temperature": 0.8,
            "length_penalty": 0.0,
            "num_beams": 3,
            "repetition_penalty": 10.0,
            "max_mel_tokens": 1500
        },
        "saved_refs": [],
        "preload_count": -1
    },
    "reading_progress": {},
    "reading_progress_save_interval": 3.0,
    "import_jobs_max": 500,
}


def _merge_config_defaults(config: dict) -> dict:
    """将默认值合并到已加载的配置中，确保所有必需字段存在"""
    for key, value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = value
    if "tts" not in config:
        config["tts"] = DEFAULT_CONFIG["tts"]
    else:
        tts = config["tts"]
        if "saved_refs" not in tts:
            tts["saved_refs"] = []
        if "preload_count" not in tts:
            tts["preload_count"] = -1
        if "indextts" not in tts:
            tts["indextts"] = DEFAULT_CONFIG["tts"]["indextts"]
        else:
            for k, v in DEFAULT_CONFIG["tts"]["indextts"].items():
                if k not in tts["indextts"]:
                    tts["indextts"][k] = v
    if "reading_progress" not in config:
        config["reading_progress"] = {}
    return config


def load_config() -> dict:
    """加载配置文件；若不存在则创建默认配置文件"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            return _merge_config_defaults(config)
        except Exception as e:
            print(f"加载配置失败: {CONFIG_FILE}, 错误: {e}")
            bak_path = CONFIG_FILE + ".bak"
            if os.path.exists(bak_path):
                try:
                    with open(bak_path, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    config = _merge_config_defaults(config)
                    print(f"已从备份恢复配置: {bak_path}")
                    try:
                        with open(CONFIG_FILE, "w", encoding="utf-8") as wf:
                            json.dump(config, wf, ensure_ascii=False, indent=4)
                    except Exception as ew:
                        print(f"将备份配置写回 {CONFIG_FILE} 失败: {ew}")
                    return config
                except Exception as eb:
                    print(f"读取配置备份失败: {bak_path}, 错误: {eb}")
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)
        try:
            shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
        except Exception as e:
            print(f"创建配置备份失败: {CONFIG_FILE}.bak, 错误: {e}")
    except Exception as e:
        print(f"创建默认配置失败: {CONFIG_FILE}, 错误: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    """保存配置文件"""
    try:
        if os.path.exists(CONFIG_FILE):
            try:
                shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
            except Exception as e:
                print(f"备份配置文件失败: {CONFIG_FILE} -> {CONFIG_FILE}.bak, 错误: {e}")
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"保存配置失败: {CONFIG_FILE}, 错误: {e}")


CONFIG = load_config()

BOOK_CACHE_MAX = 10
try:
    cache_cfg = CONFIG.get("cache", {})
    BOOK_CACHE_MAX = int(cache_cfg.get("max_books", BOOK_CACHE_MAX))
except Exception:
    BOOK_CACHE_MAX = 10

READING_PROGRESS_SAVE_INTERVAL = 3.0
IMPORT_JOBS_MAX = 500
try:
    READING_PROGRESS_SAVE_INTERVAL = float(CONFIG.get("reading_progress_save_interval", READING_PROGRESS_SAVE_INTERVAL))
    IMPORT_JOBS_MAX = int(CONFIG.get("import_jobs_max", IMPORT_JOBS_MAX))
except Exception:
    READING_PROGRESS_SAVE_INTERVAL = 3.0
    IMPORT_JOBS_MAX = 500

_last_reading_progress_save = 0.0

# 书籍索引文件（由 start.py 维护）
BOOKS_INDEX_FILE = "books_index.json"


def load_books_index() -> List[dict]:
    return shared_load_books_index(BOOKS_INDEX_FILE)

def save_books_index(entries: List[dict]) -> None:
    shared_save_books_index(BOOKS_INDEX_FILE, entries)


def _cache_key(book_id: str, base_dir: str) -> str:
    return _normp(base_dir) + "|" + os.path.basename(book_id or "")


def _progress_storage_key(book_id: str, directory: Optional[str] = None) -> str:
    safe_id = os.path.basename(book_id or "")
    if directory:
        return _normp(directory) + "|" + safe_id
    return safe_id


def _read_progress_record(progress: object) -> Optional[Dict[str, int]]:
    if not isinstance(progress, dict):
        return None
    try:
        return {
            "chapter_index": int(progress.get("chapter_index", 0)),
            "paragraph_index": int(progress.get("paragraph_index", 0)),
        }
    except Exception:
        return None


def get_reading_progress(book_id: str, directory: Optional[str] = None) -> Optional[Dict[str, int]]:
    store = CONFIG.get("reading_progress", {})
    if directory:
        found = _read_progress_record(store.get(_progress_storage_key(book_id, directory)))
        if found:
            return found
    return _read_progress_record(store.get(os.path.basename(book_id or ""))) or _read_progress_record(store.get(book_id))


def set_reading_progress(book_id: str, chapter_index: int, paragraph_index: int, directory: Optional[str] = None) -> None:
    global _last_reading_progress_save
    if not book_id:
        return
    CONFIG.setdefault("reading_progress", {})[_progress_storage_key(book_id, directory)] = {
        "chapter_index": int(chapter_index),
        "paragraph_index": int(paragraph_index),
    }
    now = time.time()
    interval = READING_PROGRESS_SAVE_INTERVAL
    if interval <= 0:
        save_config(CONFIG)
        _last_reading_progress_save = now
        return
    if _last_reading_progress_save <= 0 or now - _last_reading_progress_save >= interval:
        save_config(CONFIG)
        _last_reading_progress_save = now

def _normp(p: str) -> str:
    return shared_norm_path(p)

def add_to_books_index(book_id: str, directory: str, title: str = ""):
    shared_add_book_to_index(
        BOOKS_INDEX_FILE,
        book_id,
        directory,
        title,
        dedupe_by_directory=True,
        prepend=True,
    )

SUPPORTED_EXTS = [".epub", ".mobi", ".azw3", ".fb2", ".txt", ".pdf"]
IMPORT_MAX_WORKERS = 3
EXECUTOR = concurrent.futures.ProcessPoolExecutor(max_workers=IMPORT_MAX_WORKERS)
IMPORT_JOBS: Dict[str, Dict[str, Any]] = {}

def _find_ebook_convert() -> Optional[str]:
    cands = [
        which("ebook-convert"),
        r"C:\Program Files\Calibre2\ebook-convert.exe",
        r"C:\Program Files\Calibre\ebook-convert.exe",
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None

def _sanitize_dir_name(name: str) -> str:
    try:
        # 保留中文、英文、数字，移除其他符号
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "", name)
    except Exception:
        return name


def _process_epub_to_out_dir(epub_path: str, out_dir: str) -> Dict[str, Any]:
    try:
        book_obj = process_epub(epub_path, out_dir)
        save_to_json(book_obj, out_dir)
    except Exception:
        staging = out_dir + ".__new"
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "book": book_obj,
        "out_dir": out_dir,
        "book_id": os.path.basename(out_dir),
        "base_dir": os.path.dirname(out_dir),
        "title": book_obj.metadata.title,
    }


def _job_worker(input_path: str, base_dir: str, sanitize: bool = False) -> Dict[str, Any]:
    ext = os.path.splitext(input_path)[1].lower()
    epub_path = input_path
    base_name = os.path.basename(os.path.splitext(input_path)[0])
    out_dir_base = _sanitize_dir_name(base_name) if sanitize else base_name
    if ext != ".epub":
        conv = _find_ebook_convert()
        if not conv:
            return {"status": "error", "error": "缺少ebook-convert，无法转换", "file": input_path}
        dest_epub = os.path.splitext(input_path)[0] + ".converted.epub"
        last_conv_error: Optional[Exception] = None
        for i in range(2):
            try:
                subprocess.run([conv, input_path, dest_epub], check=True)
                epub_path = dest_epub
                last_conv_error = None
                break
            except Exception as e:
                last_conv_error = e
                if i < 1:
                    try:
                        time.sleep(1.0 * (i + 1))
                    except Exception:
                        pass
        if last_conv_error is not None:
            return {"status": "error", "error": str(last_conv_error), "file": input_path}
    try:
        os.makedirs(base_dir, exist_ok=True)
    except Exception:
        pass
    out_dir = os.path.join(base_dir, out_dir_base + "_data")
    if os.path.isdir(out_dir) and os.path.exists(os.path.join(out_dir, "book.json")):
        return {"status": "skipped", "file": input_path, "out_dir": out_dir}
    last_proc_error: Optional[Exception] = None
    for i in range(2):
        try:
            result = _process_epub_to_out_dir(epub_path, out_dir)
            try:
                book_obj = result.get("book")
                title = ""
                if book_obj is not None:
                    title = getattr(getattr(book_obj, "metadata", None), "title", "") or ""
                title = title or base_name
                safe_title = _sanitize_dir_name(title) if title else out_dir_base
                if not safe_title:
                    safe_title = out_dir_base or "book"
                final_dir_name = safe_title + "_data"
                final_out_dir = os.path.join(base_dir, final_dir_name)
                if final_out_dir != out_dir and not os.path.exists(final_out_dir):
                    try:
                        os.rename(out_dir, final_out_dir)
                        result["out_dir"] = final_out_dir
                        result["book_id"] = final_dir_name
                        result["base_dir"] = base_dir
                        result["title"] = title
                    except Exception:
                        pass
            except Exception:
                pass
            return {
                "status": "done",
                "file": input_path,
                "out_dir": result["out_dir"],
                "book_id": result["book_id"],
                "base_dir": result["base_dir"],
                "title": result["title"],
            }
        except Exception as e:
            last_proc_error = e
            if i < 1:
                try:
                    time.sleep(1.0 * (i + 1))
                except Exception:
                    pass
    return {"status": "error", "error": str(last_proc_error) if last_proc_error else "未知错误", "file": input_path}

def _make_import_callback(job_id: str):
    """创建导入任务完成后的回调函数，处理结果更新、索引登记和临时文件清理"""
    def _cb(f):
        job = IMPORT_JOBS.get(job_id)
        if not job:
            return
        try:
            res = f.result()
            job.update(res)
            job["status"] = res.get("status", "done")
            if res.get("status") == "done" and res.get("book_id"):
                add_to_books_index(res["book_id"], res["base_dir"], res.get("title", ""))
            if res.get("status") in ("done", "skipped"):
                _cleanup_temp_input(job)
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
    return _cb


def _schedule_import(path: str, base_dir: Optional[str] = None, sanitize: bool = False, temp_input: bool = False, display_name: Optional[str] = None) -> str:
    job_id = str(uuid.uuid4())
    p = os.path.abspath(path)
    bd = os.path.abspath(base_dir) if base_dir else os.path.dirname(p)
    IMPORT_JOBS[job_id] = {
        "id": job_id,
        "file": p,
        "display_name": display_name or os.path.basename(p),
        "status": "queued",
        "temp_input": bool(temp_input),
        "created_at": time.time(),
        "base_dir": bd,
        "sanitize": bool(sanitize),
    }
    fut = EXECUTOR.submit(_job_worker, p, bd, sanitize)
    IMPORT_JOBS[job_id]["future"] = fut
    fut.add_done_callback(_make_import_callback(job_id))
    _compact_import_jobs()
    return job_id


def _cleanup_temp_input(job: dict) -> None:
    """成功或已存在时删除上传的临时原文件，失败时保留以便重试。"""
    if not job.get("temp_input"):
        return
    try:
        orig = job.get("file")
        if orig and os.path.isfile(orig):
            os.remove(orig)
        conv = os.path.splitext(orig or "")[0] + ".converted.epub"
        if conv and os.path.isfile(conv):
            os.remove(conv)
    except Exception:
        pass


def _retry_import_job(job_id: str) -> None:
    job = IMPORT_JOBS.get(job_id)
    if not job:
        raise KeyError("job not found")
    p = job.get("file")
    if not p or not os.path.isfile(p):
        raise FileNotFoundError("源文件已不存在，无法重试")
    bd = job.get("base_dir") or (CONFIG.get("book_directories", ["."])[0] if CONFIG.get("book_directories") else ".")
    bd = os.path.abspath(bd)
    sanitize = bool(job.get("sanitize"))
    job["status"] = "queued"
    job.pop("error", None)
    job.pop("out_dir", None)
    job.pop("book_id", None)
    job.pop("title", None)
    job["created_at"] = time.time()
    fut = EXECUTOR.submit(_job_worker, p, bd, sanitize)
    job["future"] = fut
    fut.add_done_callback(_make_import_callback(job_id))


def _compact_import_jobs() -> None:
    if IMPORT_JOBS_MAX <= 0:
        return
    if len(IMPORT_JOBS) <= IMPORT_JOBS_MAX:
        return
    items = sorted(IMPORT_JOBS.values(), key=lambda j: j.get("created_at", 0))
    excess = len(items) - IMPORT_JOBS_MAX
    for job in items:
        if excess <= 0:
            break
        status = str(job.get("status") or "")
        if status in ("done", "error", "skipped"):
            jid = job.get("id")
            if jid and jid in IMPORT_JOBS:
                IMPORT_JOBS.pop(jid, None)
                excess -= 1


def get_book_directories() -> List[str]:
    """获取所有书籍目录（包括从索引文件中提取的），进行路径规范化去重"""
    base_dirs = list(CONFIG.get("book_directories", ["."]))
    seen = set(_normp(d) for d in base_dirs)
    dirs = list(base_dirs)
    for book in load_books_index():
        book_dir = book.get("directory", "")
        if book_dir:
            nd = _normp(book_dir)
            if nd not in seen:
                dirs.append(book_dir)
                seen.add(nd)
    return dirs


# === 书籍缓存（支持多目录） ===

# 缓存：book_id -> (目录路径, Book对象)
_book_cache: OrderedDict[str, tuple] = OrderedDict()


def _resolve_preferred_dir(preferred_dir: Optional[str]) -> Optional[str]:
    if not preferred_dir:
        return None
    cleaned = clean_path(str(preferred_dir))
    if not cleaned:
        return None
    return os.path.abspath(cleaned)


def find_book_path(book_id: str, preferred_dir: Optional[str] = None, fallback: bool = True) -> Optional[str]:
    """在指定目录或所有配置目录中查找书籍"""
    safe_book_id = os.path.basename(book_id)
    preferred = _resolve_preferred_dir(preferred_dir)
    if preferred:
        json_path = os.path.join(preferred, safe_book_id, "book.json")
        if os.path.exists(json_path):
            return preferred
        if not fallback:
            return None

    for base_dir in get_book_directories():
        book_path = os.path.join(base_dir, safe_book_id)
        json_path = os.path.join(book_path, "book.json")
        if os.path.exists(json_path):
            return base_dir
    return None


def load_book_cached(book_id: str, preferred_dir: Optional[str] = None, fallback: bool = True) -> Optional[Book]:
    """从 JSON 文件加载书籍（支持多目录）"""
    safe_book_id = os.path.basename(book_id)
    preferred = _resolve_preferred_dir(preferred_dir)
    if preferred:
        key = _cache_key(safe_book_id, preferred)
        cached = _book_cache.get(key)
        if cached and os.path.exists(os.path.join(cached[0], safe_book_id, "book.json")):
            _book_cache.move_to_end(key)
            return cached[1]

    base_dir = find_book_path(safe_book_id, preferred, fallback=fallback)
    if not base_dir:
        return None

    key = _cache_key(safe_book_id, base_dir)
    cached = _book_cache.get(key)
    if cached:
        _book_cache.move_to_end(key)
        return cached[1]

    json_path = os.path.join(base_dir, safe_book_id, "book.json")

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        book = Book.from_dict(data)
        if BOOK_CACHE_MAX > 0:
            if len(_book_cache) >= BOOK_CACHE_MAX:
                _book_cache.popitem(last=False)
            _book_cache[key] = (base_dir, book)
        return book
    except Exception as e:
        print(f"加载书籍 {book_id} 时出错: {e}")
        return None


def get_book_base_dir(book_id: str, preferred_dir: Optional[str] = None, fallback: bool = True) -> Optional[str]:
    """获取书籍所在的基础目录"""
    safe_book_id = os.path.basename(book_id)
    preferred = _resolve_preferred_dir(preferred_dir)
    if preferred:
        key = _cache_key(safe_book_id, preferred)
        cached = _book_cache.get(key)
        if cached:
            _book_cache.move_to_end(key)
            return cached[0]
    return find_book_path(safe_book_id, preferred, fallback=fallback)


def _drop_book_cache(book_id: str, base_dir: Optional[str] = None) -> None:
    safe_id = os.path.basename(book_id or "")
    if base_dir:
        _book_cache.pop(_cache_key(safe_id, base_dir), None)
    _book_cache.pop(safe_id, None)
    for key in list(_book_cache):
        if key == safe_id or key.endswith("|" + safe_id):
            if base_dir is None or key == _cache_key(safe_id, base_dir):
                _book_cache.pop(key, None)


def load_book_index(base_dir: str, folder_name: str) -> Optional[dict]:
    """加载轻量级索引用于书库列表"""
    index_path = os.path.join(base_dir, folder_name, "index.json")
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def find_cover_image(base_dir: str, folder_name: str) -> Optional[str]:
    root = os.path.join(base_dir, folder_name)
    images_dir = os.path.join(root, "images")
    book_path = os.path.join(root, "book.json")
    candidates = []
    try:
        if os.path.exists(book_path):
            with open(book_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for ch in (data.get("spine", [])[:5] or []):
                c = ch.get("content") or ""
                m = re.search(r"<img[^>]*?src=['\"]([^'\"]+)['\"]", c)
                if m:
                    candidates.append(os.path.basename(m.group(1)))
                m2 = re.search(r"<image[^>]*?(?:xlink:href|href)=['\"]([^'\"]+)['\"]", c)
                if m2:
                    candidates.append(os.path.basename(m2.group(1)))
    except Exception:
        pass
    for name in candidates:
        p1 = os.path.join(images_dir, name)
        p2 = os.path.join(root, name)
        if os.path.exists(p1):
            return name
        if os.path.exists(p2):
            return name
    try:
        if os.path.isdir(images_dir):
            files = os.listdir(images_dir)
            for x in files:
                if re.search(r"(cover|封面|front)", x, re.I):
                    return x
            for x in files:
                if x.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".svg")):
                    return x
    except Exception:
        pass
    return None

def _build_book_info(book_id: str, base_dir: str, index: dict) -> dict:
    """根据索引数据构建书库列表所需的书籍信息字典"""
    cover = find_cover_image(base_dir, book_id)
    prog = get_reading_progress(book_id, base_dir)
    pct = None
    detail = None
    try:
        if prog and isinstance(index.get('chapters'), int) and index['chapters'] > 0:
            pct = int(round(((prog.get('chapter_index', 0) + 1) / index['chapters']) * 100))
            bobj = load_book_cached(book_id, base_dir)
            if bobj and 0 <= prog.get('chapter_index', 0) < len(bobj.spine):
                ci = prog.get('chapter_index', 0)
                total = sum(len((ch.text or '')) for ch in bobj.spine)
                read = sum(len((ch.text or '')) for ch in bobj.spine[:ci])
                curr = len((bobj.spine[ci].text or ''))
                detail = {
                    "chapter_title": bobj.spine[ci].title,
                    "read_words": read,
                    "total_words": total,
                    "current_chapter_words": curr
                }
    except Exception:
        pass
    return {
        "id": book_id,
        "title": index['title'],
        "author": ", ".join(index.get('authors', [])),
        "chapters": index['chapters'],
        "location": base_dir,
        "cover": cover or "",
        "progress": prog or {},
        "progress_percent": pct,
        "progress_detail": detail
    }


def _book_seen_key(book_id: str, directory: str) -> str:
    return _normp(directory) + "|" + os.path.basename(book_id or "")


def scan_all_books() -> List[dict]:
    """扫描所有目录中的书籍"""
    books = []
    seen_ids = set()

    # 首先从书籍索引文件加载
    for book_entry in load_books_index():
        book_id = book_entry.get("id", "")
        book_dir = book_entry.get("directory", ".")
        seen_key = _book_seen_key(book_id, book_dir)

        if book_id and seen_key not in seen_ids:
            full_path = os.path.join(book_dir, book_id)
            if os.path.isdir(full_path):
                index = load_book_index(book_dir, book_id)
                book_json = os.path.join(full_path, "book.json")
                if index and os.path.exists(book_json):
                    seen_ids.add(seen_key)
                    books.append(_build_book_info(book_id, book_dir, index))

    # 然后扫描配置的目录。目录暂时不可用时只跳过，不改配置。
    for base_dir in list(CONFIG.get("book_directories", ["."])):
        if not os.path.exists(base_dir) or not os.path.isdir(base_dir):
            print(f"跳过不可用的书籍目录: {base_dir}")
            continue

        try:
            for item in os.listdir(base_dir):
                seen_key = _book_seen_key(item, base_dir)
                if item.endswith("_data") and seen_key not in seen_ids:
                    full_path = os.path.join(base_dir, item)
                    if os.path.isdir(full_path):
                        index = load_book_index(base_dir, item)
                        book_json = os.path.join(full_path, "book.json")
                        if index and os.path.exists(book_json):
                            seen_ids.add(seen_key)
                            books.append(_build_book_info(item, base_dir, index))
        except Exception as e:
            print(f"扫描目录 {base_dir} 时出错: {e}")

    return books


# === Favicon 路由（避免404错误） ===

@app.get("/favicon.ico")
async def favicon():
    """返回空的favicon，避免浏览器404错误"""
    # 如果存在 reader3.png，可以作为图标返回
    icon_path = Path(__file__).parent / "reader3.png"
    if icon_path.exists():
        return FileResponse(str(icon_path), media_type="image/png")
    # 否则返回空响应
    return Response(content=b"", media_type="image/x-icon")


# === 音频缓存 API（保存到工作目录） ===

AUDIO_CACHE_DIR = Path("tts_cache")
TTS_REF_UPLOAD_DIR = Path("tts_refs")
    
def get_audio_cache_path(cache_key: str) -> Path:
    """根据缓存键生成文件路径"""
    # 使用 MD5 哈希作为文件名，避免特殊字符问题
    hash_name = hashlib.md5(cache_key.encode('utf-8')).hexdigest()
    return AUDIO_CACHE_DIR / f"{hash_name}.wav"

@app.post("/api/tts/cache")
async def save_audio_cache(
    cache_key: str = Body(..., embed=True, description="缓存键（文本+引擎）"),
    audio_data: str = Body(..., embed=True, description="Base64编码的音频数据")
):
    """保存音频到本地缓存目录"""
    try:
        # 确保缓存目录存在
        AUDIO_CACHE_DIR.mkdir(exist_ok=True)
        
        # 解码音频数据
        audio_bytes = base64.b64decode(audio_data)
        
        # 保存到文件
        cache_path = get_audio_cache_path(cache_key)
        cache_path.write_bytes(audio_bytes)
        
        return {"success": True, "path": str(cache_path), "size": len(audio_bytes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存缓存失败: {e}")

@app.get("/api/tts/cache")
async def get_audio_cache(cache_key: str = Query(..., description="缓存键")):
    """从本地缓存获取音频"""
    cache_path = get_audio_cache_path(cache_key)
    
    if cache_path.exists():
        return FileResponse(str(cache_path), media_type="audio/wav")
    else:
        # 返回 204 No Content 而不是 404，避免日志中出现大量"错误"
        return Response(status_code=204)

@app.delete("/api/tts/cache")
async def clear_audio_cache():
    """清空所有音频缓存"""
    try:
        if AUDIO_CACHE_DIR.exists():
            shutil.rmtree(AUDIO_CACHE_DIR)
            AUDIO_CACHE_DIR.mkdir(exist_ok=True)
        return {"success": True, "message": "缓存已清空"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清空缓存失败: {e}")

@app.get("/api/tts/cache/stats")
async def get_cache_stats():
    """获取缓存统计信息"""
    if not AUDIO_CACHE_DIR.exists():
        return {"count": 0, "total_size": 0, "path": str(AUDIO_CACHE_DIR)}
    
    files = list(AUDIO_CACHE_DIR.glob("*.wav"))
    total_size = sum(f.stat().st_size for f in files)
    
    return {
        "count": len(files),
        "total_size": total_size,
        "total_size_mb": round(total_size / 1024 / 1024, 2),
        "path": str(AUDIO_CACHE_DIR.absolute())
    }


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_manifest():
    return Response(content="{}", media_type="application/json")


# === API 路由 ===

@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """列出所有已处理的书籍"""
    books = scan_all_books()
    return templates.TemplateResponse("library.html", {
        "request": request,
        "books": books,
        # 目录标签仅显示配置中的目录
        "directories_config": CONFIG.get("book_directories", ["."])
    })


@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: str, image_name: str, book_dir: Optional[str] = Query(None, alias="dir")):
    """为书籍提供图片服务"""
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)
    
    base_dir = get_book_base_dir(book_id, book_dir)
    if not base_dir:
        raise HTTPException(status_code=404, detail="书籍未找到")
    
    images_dir = os.path.abspath(os.path.join(base_dir, safe_book_id, "images"))
    root_dir = os.path.abspath(os.path.join(base_dir, safe_book_id))
    candidates = [
        os.path.abspath(os.path.join(images_dir, safe_image_name)),
        os.path.abspath(os.path.join(root_dir, safe_image_name)),
    ]

    for img_path in candidates:
        if img_path.startswith(images_dir + os.sep) or img_path.startswith(root_dir + os.sep):
            if os.path.exists(img_path):
                return FileResponse(img_path)
    raise HTTPException(status_code=404, detail="图片未找到")

 

@app.get("/read/{book_id}")
async def redirect_to_first_chapter(book_id: str, request: Request):
    """重定向到第一章"""
    url = f"/read/{book_id}/0"
    if request.url.query:
        url += "?" + request.url.query
    return RedirectResponse(url=url, status_code=302)


@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(
    request: Request,
    book_id: str,
    chapter_index: int,
    book_dir: Optional[str] = Query(None, alias="dir"),
):
    """阅读器主界面"""
    book = load_book_cached(book_id, book_dir)
    resolved_dir = get_book_base_dir(book_id, book_dir) or ""
    dir_q = ("?dir=" + quote(resolved_dir, safe="")) if resolved_dir else ""
    if not book:
        raise HTTPException(status_code=404, detail="书籍未找到")

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="章节未找到")

    current_chapter = book.spine[chapter_index]
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None

    # 获取 TTS 配置
    tts_config = CONFIG.get("tts", {})

    # 重写图片地址为 /read/{book_id}/images/<name>
    def rewrite_image_src(book_id: str, html: str) -> str:
        try:
            def map_url(u: str) -> str:
                if u.startswith(('http://', 'https://', 'data:')):
                    return u
                if u.startswith(f"/read/{book_id}_data/"):
                    return f"/read/{book_id}/images/{os.path.basename(u)}{dir_q}"
                return f"/read/{book_id}/images/{os.path.basename(u)}{dir_q}"

            def repl_src(m):
                quote = m.group(2)
                src = m.group(3)
                new = map_url(src)
                return f"<img{m.group(1)}src={quote}{new}{quote}"

            def repl_srcset(m):
                attr = m.group(1)
                quote = m.group(2)
                val = m.group(3)
                parts = [p.strip() for p in val.split(',') if p.strip()]
                new_parts = []
                for p in parts:
                    segs = p.split()
                    if not segs:
                        continue
                    url = segs[0]
                    rest = ' '.join(segs[1:])
                    new_url = map_url(url)
                    new_parts.append((new_url + (' ' + rest if rest else '')).strip())
                new_val = ', '.join(new_parts) if new_parts else val
                return f"{attr}={quote}{new_val}{quote}"

            html = re.sub(r"<img([^>]*?)src=([\"'])([^\"']+)\2", repl_src, html)
            html = re.sub(r"(srcset)=([\"'])([^\"']+)\2", repl_srcset, html)
            # 兼容 SVG: <image xlink:href="..."> 和 <image href="...">
            html = re.sub(r"<image([^>]*?)xlink:href=([\"'])([^\"']+)\2", 
                          lambda m: f"<image{m.group(1)}xlink:href={m.group(2)}{map_url(m.group(3))}{m.group(2)}", html)
            html = re.sub(r"<image([^>]*?)href=([\"'])([^\"']+)\2", 
                          lambda m: f"<image{m.group(1)}href={m.group(2)}{map_url(m.group(3))}{m.group(2)}", html)
            # 重写内联样式中的 url(...) 引用
            def repl_style(m):
                attr = m.group(1)
                quote = m.group(2)
                val = m.group(3)
                def _rewrite_urls(s: str) -> str:
                    return re.sub(r"url\((['\"]?)([^'\")]+)\1\)", 
                                   lambda um: f"url('{map_url(um.group(2))}')", s)
                new_val = _rewrite_urls(val)
                return f"{attr}={quote}{new_val}{quote}"
            html = re.sub(r"(style)=([\"'])([^\"']+)\2", repl_style, html)
            return html
        except Exception:
            return html

    content_html = rewrite_image_src(book_id, current_chapter.content)

    return templates.TemplateResponse("reader.html", {
        "request": request,
        "book": book,
        "current_chapter": current_chapter,
        "chapter_index": chapter_index,
        "book_id": book_id,
        "book_dir": resolved_dir,
        "dir_q": dir_q,
        "prev_idx": prev_idx,
        "next_idx": next_idx,
        "tts_config": tts_config,
        "content_html": content_html
    })




# === 配置 API ===

@app.get("/api/config")
async def get_config():
    """获取当前配置"""
    return CONFIG


@app.post("/api/tts/upload-ref")
async def upload_tts_ref(file: UploadFile = File(...)):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="文件无效")
    try:
        TTS_REF_UPLOAD_DIR.mkdir(exist_ok=True)
        suffix = Path(file.filename).suffix or ".wav"
        original_name = Path(file.filename).stem
        raw = await file.read()
        name_seed = f"{file.filename}-{time.time()}"
        hash_name = hashlib.md5(name_seed.encode("utf-8")).hexdigest()
        save_path = TTS_REF_UPLOAD_DIR / f"{hash_name}{suffix}"
        with open(save_path, "wb") as f:
            f.write(raw)
        abs_path = str(save_path.absolute())
        refs = CONFIG.setdefault("tts", {}).setdefault("saved_refs", [])
        refs = [r for r in refs if not (r.get("path") == abs_path and r.get("prompt") == original_name)]
        refs.insert(0, {"path": abs_path, "prompt": original_name})
        CONFIG["tts"]["saved_refs"] = refs[:100]
        save_config(CONFIG)
        return {"success": True, "path": abs_path}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传参考音频失败: {e}")


@app.post("/api/tts/upload-indextts-ref")
async def upload_indextts_ref(file: UploadFile = File(...), api_url: str = Query(..., description="IndexTTS2 API 地址")):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="文件无效")
    api_url_clean = (api_url or "").strip()
    if not api_url_clean:
        raise HTTPException(status_code=400, detail="IndexTTS2 API 地址未配置")
    api_url_clean = api_url_clean.rstrip("/")
    try:
        raw = await file.read()
        files = {
            "audio_file": (file.filename, raw, file.content_type or "audio/wav"),
        }
        resp = await HTTP_CLIENT.post(f"{api_url_clean}/upload_audio", files=files, timeout=300.0)
        try:
            data = resp.json()
        except Exception:
            raise HTTPException(status_code=resp.status_code or 500, detail="IndexTTS2 上传参考音频失败")
        if resp.status_code != 200:
            detail = None
            if isinstance(data, dict):
                detail = data.get("message") or data.get("detail")
            raise HTTPException(status_code=resp.status_code, detail=detail or "IndexTTS2 上传参考音频失败")
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail="IndexTTS2 返回数据格式错误")
        file_path = data.get("file_path")
        if not file_path:
            raise HTTPException(status_code=500, detail="IndexTTS2 返回数据缺少文件路径")
        original_name = Path(file.filename).stem
        refs = CONFIG.setdefault("tts", {}).setdefault("saved_refs", [])
        refs = [r for r in refs if not (r.get("path") == file_path and r.get("prompt") == original_name)]
        refs.insert(0, {"path": file_path, "prompt": original_name})
        CONFIG["tts"]["saved_refs"] = refs[:100]
        save_config(CONFIG)
        return {"success": True, "path": file_path}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传参考音频失败: {e}")


# === 参考音频列表配置 ===
    
@app.get("/api/tts/refs")
async def get_saved_refs():
    """获取已保存的参考音频列表"""
    refs = CONFIG.get("tts", {}).get("saved_refs", [])
    return {"refs": refs}


@app.post("/api/tts/refs")
async def add_saved_ref(
    path: str = Body(..., embed=True, description="参考音频路径"),
    prompt: str = Body("", embed=True, description="参考文本")
):
    """添加参考音频到配置"""
    path_clean = clean_path(path)
    refs = CONFIG.setdefault("tts", {}).setdefault("saved_refs", [])
    # 去重：同路径同文本仅保留一份
    refs = [r for r in refs if not (r.get("path") == path_clean and r.get("prompt") == prompt)]
    refs.insert(0, {"path": path_clean, "prompt": prompt})
    # 最多保留100条
    CONFIG["tts"]["saved_refs"] = refs[:100]
    save_config(CONFIG)
    return {"success": True, "refs": CONFIG["tts"]["saved_refs"]}


@app.delete("/api/tts/refs")
async def delete_saved_ref(
    path: str = Query(..., description="要删除的参考音频路径")
):
    """删除指定参考音频"""
    path_clean = clean_path(path)
    refs = CONFIG.setdefault("tts", {}).setdefault("saved_refs", [])
    CONFIG["tts"]["saved_refs"] = [r for r in refs if r.get("path") != path_clean]
    save_config(CONFIG)
    return {"success": True, "refs": CONFIG["tts"]["saved_refs"]}

@app.post("/api/tts/ref-dir")
async def save_ref_audio_dir(
    dir_path: str = Body(..., embed=True, description="参考音频目录路径")
):
    """保存参考音频目录到配置"""
    dir_clean = clean_path(dir_path)
    CONFIG.setdefault("tts", {})["ref_audio_dir"] = dir_clean
    save_config(CONFIG)
    return {"success": True, "dir_path": dir_clean}


@app.get("/api/tts/ref-dir")
async def get_ref_audio_dir():
    """获取已保存的参考音频目录"""
    dir_path = CONFIG.get("tts", {}).get("ref_audio_dir", "")
    if not dir_path:
        return {"dir_path": "", "files": []}
    
    # 扫描目录中的音频文件
    audio_files = []
    dir_p = Path(dir_path)
    if dir_p.is_dir():
        for ext in ['*.mp3', '*.wav', '*.flac', '*.ogg', '*.m4a']:
            for f in dir_p.glob(ext):
                audio_files.append({
                    "name": f.name,
                    "path": str(f),
                    "prompt": f.stem  # 文件名（不含扩展名）作为提示文本
                })
    
    return {"dir_path": dir_path, "files": audio_files}


@app.post("/api/tts/preload")
async def set_preload_count(count: int = Body(..., embed=True, description="预加载段数，-1表示不限制")):
    """设置预加载段数并保存配置"""
    CONFIG.setdefault("tts", {})["preload_count"] = count
    save_config(CONFIG)
    return {"success": True, "preload_count": count}


@app.get("/api/config/advanced")
async def get_advanced_config():
    cfg = CONFIG
    cache_cfg = cfg.get("cache", {})
    return {
        "reading_progress_save_interval": cfg.get("reading_progress_save_interval", READING_PROGRESS_SAVE_INTERVAL),
        "import_jobs_max": cfg.get("import_jobs_max", IMPORT_JOBS_MAX),
        "cache_max_books": cache_cfg.get("max_books", BOOK_CACHE_MAX),
    }


@app.post("/api/config/advanced")
async def update_advanced_config(payload: Dict[str, Any]):
    global READING_PROGRESS_SAVE_INTERVAL, IMPORT_JOBS_MAX, BOOK_CACHE_MAX
    cfg = CONFIG
    changed = False
    try:
        if "reading_progress_save_interval" in payload:
            v = float(payload["reading_progress_save_interval"])
            if v < 0:
                v = 0.0
            cfg["reading_progress_save_interval"] = v
            READING_PROGRESS_SAVE_INTERVAL = v
            changed = True
        if "import_jobs_max" in payload:
            v = int(payload["import_jobs_max"])
            if v < 0:
                v = 0
            cfg["import_jobs_max"] = v
            IMPORT_JOBS_MAX = v
            changed = True
        if "cache_max_books" in payload:
            v = int(payload["cache_max_books"])
            if v < 0:
                v = 0
            cache_cfg = cfg.setdefault("cache", {})
            cache_cfg["max_books"] = v
            BOOK_CACHE_MAX = v
            changed = True
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"参数解析错误: {e}")
    if changed:
        save_config(cfg)
    return {
        "success": True,
        "config": {
            "reading_progress_save_interval": READING_PROGRESS_SAVE_INTERVAL,
            "import_jobs_max": IMPORT_JOBS_MAX,
            "cache_max_books": BOOK_CACHE_MAX,
        },
    }

# === 朗读进度存储 ===

@app.get("/api/progress")
async def get_progress(
    book_id: str = Query(..., description="书籍 ID"),
    directory: Optional[str] = Query(None, description="书籍所在目录"),
):
    """获取朗读进度"""
    progress = get_reading_progress(book_id, directory)
    return progress or {}


@app.post("/api/progress")
async def save_progress(request: Request):
    """保存朗读进度：兼容任意 JSON/表单格式，避免 422 校验错误"""
    data = {}
    try:
        # 优先解析 JSON
        data = await request.json()
    except Exception:
        # 回退解析表单
        try:
            form = await request.form()
            data = dict(form)
        except Exception:
            data = {}
    book_id = str(data.get("book_id", ""))
    try:
        chapter_index = int(data.get("chapter_index", 0))
    except Exception:
        chapter_index = 0
    try:
        paragraph_index = int(data.get("paragraph_index", 0))
    except Exception:
        paragraph_index = 0
    if not book_id:
        raise HTTPException(status_code=400, detail="book_id 不能为空")
    directory = str(data.get("directory") or "").strip() or None
    set_reading_progress(book_id, chapter_index, paragraph_index, directory)
    return {"success": True}


@app.post("/api/config/add-directory")
async def add_directory(request: Request):
    """添加书籍目录"""
    data = await request.json()
    path = data.get("path", "")
    
    # 清理路径（移除引号等）
    path = clean_path(path)
    
    if not path:
        raise HTTPException(status_code=400, detail="路径不能为空")
    
    # 规范化路径
    path = os.path.abspath(path)
    
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="路径不存在")
    
    # 必须是目录，不能是文件
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="请输入目录路径，不是文件路径")
    
    if path not in CONFIG["book_directories"]:
        CONFIG["book_directories"].append(path)
        save_config(CONFIG)
    
    return {"success": True, "directories": CONFIG["book_directories"]}


@app.post("/api/config/remove-directory")
async def remove_directory(request: Request):
    data = await request.json()
    raw = data.get("path", "")
    keep_index = bool(data.get("keep_index", False))
    raw = clean_path(raw)
    target = _normp(raw) if raw else ""
    # 配置目录移除（规范化匹配）
    cur_dirs = CONFIG.get("book_directories", [])
    new_dirs = [d for d in cur_dirs if _normp(d) != target]
    if len(new_dirs) != len(cur_dirs):
        CONFIG["book_directories"] = new_dirs
        save_config(CONFIG)
    # 索引目录移除（规范化匹配），默认执行；当 keep_index=true 时跳过
    if not keep_index:
        index_entries = load_books_index()
        if index_entries:
            filtered = [b for b in index_entries if _normp(b.get("directory", "")) != target]
            if len(filtered) != len(index_entries):
                save_books_index(filtered)
    return {
        "success": True,
        # 返回目录标签的数据（仅配置目录）
        "directories": CONFIG.get("book_directories", [])
    }

@app.post("/api/import/book")
async def import_book(request: Request):
    data = await request.json()
    path = clean_path(data.get("path", ""))
    if not path:
        raise HTTPException(status_code=400, detail="路径不能为空")
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="路径不存在")
    if not os.path.isfile(path) or not path.lower().endswith(".epub"):
        raise HTTPException(status_code=400, detail="请输入 .epub 文件路径")
    out_dir = os.path.splitext(path)[0] + "_data"
    try:
        result = _process_epub_to_out_dir(path, out_dir)
        add_to_books_index(result["book_id"], result["base_dir"], result["title"])
        return {"success": True, "id": result["book_id"], "directory": result["base_dir"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导入失败: {e}")

def _create_import_jobs_for_directory(path: str, base_dir: str, sanitize_flag: bool) -> List[str]:
    jobs: List[str] = []
    for root, _, files in os.walk(path):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXTS:
                jobs.append(_schedule_import(os.path.join(root, name), base_dir, sanitize_flag))
    return jobs


@app.post("/api/import/directory")
async def import_directory(request: Request):
    data = {}
    try:
        data = await request.json()
    except Exception:
        data = {}
    raw_path = clean_path(data.get("path", ""))
    sanitize_flag = bool(data.get("sanitize", False))
    if not raw_path:
        raise HTTPException(status_code=400, detail="路径不能为空")
    path = os.path.abspath(raw_path)
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail="路径不存在")
    if not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="请输入目录路径")
    base_dir = CONFIG.get("book_directories", ["."])[0] if CONFIG.get("book_directories") else "."
    base_dir = os.path.abspath(base_dir)
    os.makedirs(base_dir, exist_ok=True)
    jobs = _create_import_jobs_for_directory(path, base_dir, sanitize_flag)
    return {"success": True, "jobs": jobs, "count": len(jobs)}

async def _handle_upload_files(files: List[UploadFile], base_dir: str, sanitize_flag: bool) -> List[str]:
    jobs: List[str] = []
    chunk_size = 1024 * 1024
    for f in files:
        try:
            orig_name = os.path.basename(f.filename)
            stem, ext = os.path.splitext(orig_name)
            ext = ext.lower()
            if ext in SUPPORTED_EXTS:
                name_base = _sanitize_dir_name(stem) if sanitize_flag else stem
                if not name_base:
                    name_base = stem or "book"
                candidate = name_base + ext
                target = os.path.join(base_dir, candidate)
                idx = 1
                while os.path.exists(target):
                    candidate = f"{name_base}_{idx}{ext}"
                    target = os.path.join(base_dir, candidate)
                    idx += 1
                with open(target, "wb") as out:
                    while True:
                        chunk = await f.read(chunk_size)
                        if not chunk:
                            break
                        out.write(chunk)
                jobs.append(_schedule_import(target, base_dir, sanitize_flag, temp_input=True, display_name=orig_name))
        except Exception:
            continue
    return jobs


@app.post("/api/import/upload-books")
async def import_upload_books(files: List[UploadFile] = File(...), sanitize: Optional[str] = Form(None)):
    base_dir = CONFIG.get("book_directories", ["."])[0] if CONFIG.get("book_directories") else "."
    base_dir = os.path.abspath(base_dir)
    os.makedirs(base_dir, exist_ok=True)
    sanitize_flag = False
    try:
        sanitize_flag = bool(sanitize) and str(sanitize).lower() not in ("", "0", "false")
    except Exception:
        sanitize_flag = False
    jobs = await _handle_upload_files(files, base_dir, sanitize_flag)
    return {"success": True, "jobs": jobs}

@app.post("/api/import/scan-config")
async def import_scan_config(request: Request):
    try:
        await request.json()
    except Exception:
        pass
    dirs = CONFIG.get("book_directories", []) or ["."]
    added = []
    for d in dirs:
        try:
            base_dir = os.path.abspath(d)
            if not os.path.isdir(base_dir):
                continue
            for item in os.listdir(base_dir):
                full_path = os.path.join(base_dir, item)
                if item.endswith("_data") and os.path.isdir(full_path):
                    book_json = os.path.join(full_path, "book.json")
                    if os.path.exists(book_json):
                        idx = load_book_index(base_dir, item) or {}
                        add_to_books_index(item, base_dir, idx.get("title", ""))
                        added.append({"id": item, "directory": base_dir})
        except Exception:
            continue
    return {"success": True, "added": added}


@app.post("/api/import/retry")
async def import_retry(job_id: str = Query(..., description="要重试的任务ID")):
    job = IMPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    status = str(job.get("status") or "")
    if status not in ("error", "skipped"):
        raise HTTPException(status_code=400, detail="仅错误或已跳过的任务可以重试")
    try:
        _retry_import_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重试任务失败: {e}")
    return {"success": True, "job_id": job_id}

@app.get("/api/import/jobs")
async def list_import_jobs():
    items = []
    for jid, j in list(IMPORT_JOBS.items()):
        info = {k: v for k, v in j.items() if k != "future"}
        fut = j.get("future")
        try:
            info["running"] = bool(fut) and fut.running() and not fut.done()
        except Exception:
            info["running"] = False
        items.append(info)
    return {"jobs": items}

# === 备份（回收站） ===
TRASH_DIR = Path("trash_books")
TRASH_INDEX = Path("trash_index.json")

def load_trash_index() -> Dict[str, Any]:
    if TRASH_INDEX.exists():
        try:
            with open(TRASH_INDEX, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_trash_index(data: Dict[str, Any]):
    try:
        with open(TRASH_INDEX, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存回收站索引失败: {e}")

def _trash_record_key(base_dir: str, book_id: str) -> str:
    return _normp(base_dir) + "|" + os.path.basename(book_id or "")


def _delete_targets_from_payload(data: dict) -> List[dict]:
    books = data.get("books")
    if isinstance(books, list):
        items = []
        for item in books:
            if isinstance(item, str):
                items.append({"id": item, "directory": ""})
            elif isinstance(item, dict) and item.get("id"):
                items.append({"id": str(item.get("id")), "directory": str(item.get("directory") or "")})
        return items
    return [{"id": str(i), "directory": ""} for i in (data.get("ids") or []) if isinstance(i, str)]


@app.post("/api/books/delete")
async def delete_books(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    TRASH_DIR.mkdir(exist_ok=True)
    trash = load_trash_index()
    entries = load_books_index()
    removed = []
    for item in _delete_targets_from_payload(data):
        bid = item.get("id") or ""
        preferred = item.get("directory") or ""
        safe_id = os.path.basename(bid)
        if not safe_id:
            continue
        try:
            base_dir = get_book_base_dir(safe_id, preferred or None, fallback=not bool(preferred))
            if not base_dir:
                continue
            src = Path(base_dir) / safe_id
            if src.is_dir():
                dst_name = safe_id
                dst = TRASH_DIR / dst_name
                if dst.exists():
                    dst_name = f"{safe_id}__{uuid.uuid4().hex[:8]}"
                    dst = TRASH_DIR / dst_name
                shutil.move(str(src), str(dst))
                target_dir = _normp(base_dir)
                entries = [
                    e for e in entries
                    if not (e.get("id") == safe_id and _normp(e.get("directory", "")) == target_dir)
                ]
                save_books_index(entries)
                trash[_trash_record_key(base_dir, safe_id)] = {
                    "id": safe_id,
                    "original_dir": base_dir,
                    "title": safe_id,
                    "folder": dst_name,
                }
                removed.append(safe_id)
                _drop_book_cache(safe_id, base_dir)
        except Exception as e:
            print(f"删除 {bid} 失败: {e}")
    save_trash_index(trash)
    return {"success": True, "removed": removed}

@app.get("/api/books/trash")
async def list_trash():
    trash = load_trash_index()
    items = []
    for key, info in trash.items():
        if not isinstance(info, dict):
            info = {}
        items.append({
            "key": key,
            "id": info.get("id") or key,
            "title": info.get("title") or "",
            "original_dir": info.get("original_dir") or "",
            "folder": info.get("folder") or key,
        })
    return {"items": items}

@app.post("/api/books/trash/restore")
async def restore_trash(id: str = Body(..., embed=True)):
    trash = load_trash_index()
    info = trash.get(id)
    if not isinstance(info, dict):
        raise HTTPException(status_code=404, detail="未找到备份项")
    folder = info.get("folder") or id
    src = TRASH_DIR / folder
    if not src.is_dir():
        raise HTTPException(status_code=404, detail="备份目录不存在")
    base_dir = info.get("original_dir", ".")
    book_name = info.get("id") or os.path.basename(folder)
    dst = Path(base_dir) / book_name
    if dst.exists():
        dst = Path(base_dir) / f"{book_name}_restored"
    shutil.move(str(src), str(dst))
    add_to_books_index(dst.name, str(Path(base_dir).absolute()), info.get("title", ""))
    trash.pop(id, None)
    save_trash_index(trash)
    return {"success": True}

@app.post("/api/books/trash/clear")
async def clear_trash():
    if TRASH_DIR.exists():
        for item in TRASH_DIR.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except Exception:
                    pass
    save_trash_index({})
    return {"success": True}


# === GPT-SoVITS 代理 API (避免跨域问题) ===

@app.get("/api/gptsovits/models")
async def get_gptsovits_models(model_dir: str = Query(".", description="模型扫描根目录")):
    """获取 GPT-SoVITS 训练模型列表"""
    gpt_models = []
    sovits_models = []
    
    # 清理路径
    base_dir = clean_path(model_dir) if model_dir else "."
    
    # GPT 模型目录
    gpt_dirs = [
        "GPT_weights", "GPT_weights_v2", "GPT_weights_v2Pro", 
        "GPT_weights_v2ProPlus", "GPT_weights_v3", "GPT_weights_v4"
    ]
    
    # SoVITS 模型目录
    sovits_dirs = [
        "SoVITS_weights", "SoVITS_weights_v2", "SoVITS_weights_v2Pro",
        "SoVITS_weights_v2ProPlus", "SoVITS_weights_v3", "SoVITS_weights_v4"
    ]
    
    try:
        # 扫描 GPT 模型
        for gpt_dir in gpt_dirs:
            full_dir = os.path.join(base_dir, gpt_dir)
            if os.path.isdir(full_dir):
                for f in os.listdir(full_dir):
                    if f.endswith('.ckpt'):
                        rel_path = f"{gpt_dir}/{f}"
                        gpt_models.append({
                            "name": f"[{gpt_dir}] {f}",
                            "path": rel_path
                        })
        
        # 扫描 SoVITS 模型
        for sovits_dir in sovits_dirs:
            full_dir = os.path.join(base_dir, sovits_dir)
            if os.path.isdir(full_dir):
                for f in os.listdir(full_dir):
                    if f.endswith('.pth'):
                        rel_path = f"{sovits_dir}/{f}"
                        sovits_models.append({
                            "name": f"[{sovits_dir}] {f}",
                            "path": rel_path
                        })
                        
        print(f"从 {base_dir} 扫描到 {len(gpt_models)} 个 GPT 模型, {len(sovits_models)} 个 SoVITS 模型")
                        
    except Exception as e:
        print(f"扫描模型目录失败: {e}")
    
    return {"gpt_models": gpt_models, "sovits_models": sovits_models}


# 全局 HTTP 客户端（复用连接，限制最大连接数防止耗尽资源）
HTTP_MAX_KEEPALIVE_CONNECTIONS = 20
HTTP_MAX_CONNECTIONS = 50
HTTP_CLIENT = httpx.AsyncClient(
    timeout=300.0,
    limits=httpx.Limits(
        max_keepalive_connections=HTTP_MAX_KEEPALIVE_CONNECTIONS,
        max_connections=HTTP_MAX_CONNECTIONS,
    ),
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    try:
        await HTTP_CLIENT.aclose()
        print("✅ 已关闭全局 HTTP 客户端连接池")
    except Exception as e:
        print(f"⚠️ 关闭 HTTP 客户端失败: {e}")

app.router.lifespan_context = lifespan

@app.get("/@vite/client")
async def vite_client_stub():
    return Response(content="", media_type="application/javascript")


@app.get("/api/gptsovits/set_gpt_weights")
async def set_gpt_weights(
    api_url: str = Query("http://127.0.0.1:9880"),
    weights_path: str = Query(..., description="GPT 模型路径")
):
    """切换 GPT 模型 (代理请求避免跨域)"""
    try:
        resp = await HTTP_CLIENT.get(
            f"{api_url}/set_gpt_weights",
            params={"weights_path": weights_path}
        )
        if resp.status_code == 200:
            return {"success": True, "message": "GPT 模型切换成功"}
        else:
            try:
                data = resp.json()
                return {"success": False, "message": data.get("message", "切换失败")}
            except Exception:
                return {"success": False, "message": resp.text}
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="无法连接到 GPT-SoVITS 服务")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gptsovits/set_sovits_weights")
async def set_sovits_weights(
    api_url: str = Query("http://127.0.0.1:9880"),
    weights_path: str = Query(..., description="SoVITS 模型路径")
):
    """切换 SoVITS 模型 (代理请求避免跨域)"""
    try:
        resp = await HTTP_CLIENT.get(
            f"{api_url}/set_sovits_weights",
            params={"weights_path": weights_path}
        )
        if resp.status_code == 200:
            return {"success": True, "message": "SoVITS 模型切换成功"}
        else:
            try:
                data = resp.json()
                return {"success": False, "message": data.get("message", "切换失败")}
            except Exception:
                return {"success": False, "message": resp.text}
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="无法连接到 GPT-SoVITS 服务")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gptsovits/control")
async def gptsovits_control(
    api_url: str = Query("http://127.0.0.1:9880"),
    command: str = Query(..., description="命令: restart/exit")
):
    """GPT-SoVITS 服务控制 (代理请求避免跨域)"""
    try:
        resp = await HTTP_CLIENT.get(f"{api_url}/control", params={"command": command}, timeout=10.0)
        if 200 <= resp.status_code < 300:
            return {"success": True}
        try:
            data = resp.json()
            msg = data.get("message") or resp.text or "控制请求失败"
        except Exception:
            msg = resp.text or "控制请求失败"
        return {"success": False, "message": msg}
    except httpx.ConnectError:
        return {"success": False, "message": "无法连接到 GPT-SoVITS 服务"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# === Index-TTS 代理 API ===

@app.get("/api/indextts/uploaded_audios")
async def indextts_uploaded_audios(api_url: str = Query(..., description="IndexTTS2 API 地址")):
    """获取 IndexTTS 已上传的参考音频列表"""
    try:
        api_url = api_url.rstrip("/")
        resp = await HTTP_CLIENT.get(f"{api_url}/uploaded_audios", timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception:
        return []

@app.get("/api/indextts/presets")
async def indextts_list_presets(api_url: str = Query(..., description="IndexTTS2 API 地址")):
    """获取 IndexTTS 预设列表"""
    try:
        api_url = api_url.rstrip("/")
        resp = await HTTP_CLIENT.get(f"{api_url}/presets", timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception:
        return []

@app.post("/api/indextts/presets")
async def indextts_save_preset(
    request: Request,
    api_url: str = Query(..., description="IndexTTS2 API 地址")
):
    """保存 IndexTTS 预设"""
    try:
        api_url = api_url.rstrip("/")
        body = await request.json()
        resp = await HTTP_CLIENT.post(f"{api_url}/presets", json=body, timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/indextts/presets/{name}")
async def indextts_delete_preset(
    name: str,
    api_url: str = Query(..., description="IndexTTS2 API 地址")
):
    """删除 IndexTTS 预设"""
    try:
        api_url = api_url.rstrip("/")
        resp = await HTTP_CLIENT.delete(f"{api_url}/presets/{name}", timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/indextts/control")
async def indextts_control(
    api_url: str = Query(..., description="IndexTTS2 API 地址"),
    command: str = Query(..., description="命令: restart/exit")
):
    """IndexTTS 服务控制"""
    try:
        api_url = api_url.rstrip("/")
        resp = await HTTP_CLIENT.get(f"{api_url}/control", params={"command": command}, timeout=5.0)
        if 200 <= resp.status_code < 300:
            return {"success": True}
        try:
            data = resp.json()
            msg = data.get("message") or resp.text or "控制请求失败"
        except Exception:
            msg = resp.text or "控制请求失败"
        return {"success": False, "message": msg}
    except httpx.ConnectError:
        return {"success": False, "message": "无法连接到 IndexTTS 服务"}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/api/indextts/projects")
async def indextts_projects(api_url: str = Query(..., description="IndexTTS2 API 地址")):
    """获取 IndexTTS 项目列表"""
    try:
        api_url = api_url.rstrip("/")
        resp = await HTTP_CLIENT.get(f"{api_url}/projects", timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
        return []
    except Exception:
        return []


# === TTS API ===

def _merge_kwargs_to_config(base_config: dict, kwargs: dict, keys: list) -> dict:
    """将 kwargs 中非 None 的参数合并到配置字典中，减少重复的逐字段赋值"""
    config = dict(base_config)
    for key in keys:
        val = kwargs.get(key)
        if val is not None:
            config[key] = val
    return config


async def _tts_edge_handler(text: str, tts_config: dict, **kwargs: Any):
    edge_config = _merge_kwargs_to_config(
        tts_config.get("edge", {}), kwargs,
        ["voice", "rate", "volume", "pitch"]
    )
    return await tts_edge(text, edge_config)


async def _tts_gptsovits_handler(text: str, tts_config: dict, **kwargs: Any):
    gptsovits_config = _merge_kwargs_to_config(
        tts_config.get("gptsovits", {}), kwargs,
        ["api_url", "method", "ref_audio_path", "prompt_text", "prompt_lang",
         "text_lang", "top_k", "top_p", "temperature", "text_split_method",
         "batch_size", "split_bucket", "speed_factor", "streaming_mode",
         "media_type", "seed", "parallel_infer", "repetition_penalty", "sample_steps"]
    )
    return await tts_gptsovits(text, gptsovits_config)


async def _tts_indextts_handler(text: str, tts_config: dict, **kwargs: Any):
    indextts_config = _merge_kwargs_to_config(
        tts_config.get("indextts", {}), kwargs,
        ["api_url", "spk_audio_prompt", "emo_audio_prompt", "emo_alpha",
         "emo_vector", "use_emo_text", "emo_text", "use_random",
         "output_format", "audio_bitdepth", "max_text_tokens_per_segment",
         "interval_silence_ms", "resume_project", "split_bucket",
         "bucket_threshold", "do_sample", "top_p", "top_k", "temperature",
         "length_penalty", "num_beams", "repetition_penalty", "max_mel_tokens"]
    )
    return await tts_indextts(text, indextts_config)


TTS_HANDLERS: Dict[str, Any] = {
    "edge": _tts_edge_handler,
    "gptsovits": _tts_gptsovits_handler,
    "indextts": _tts_indextts_handler,
}


def _raise_tts_http_error(status_code: int, error_code: str, message: str) -> None:
    raise HTTPException(status_code=status_code, detail={"error_code": error_code, "message": message})


@app.get("/api/tts/speak")
async def tts_speak(
    text: str = Query(..., description="要朗读的文本"),
    engine: str = Query("edge", description="TTS 引擎: edge/gptsovits/indextts"),
    voice: Optional[str] = Query(None, description="Edge TTS 语音"),
    rate: Optional[str] = Query(None, description="Edge TTS 语速"),
    volume: Optional[str] = Query(None, description="Edge TTS 音量"),
    pitch: Optional[str] = Query(None, description="Edge TTS 音调"),
    api_url: Optional[str] = Query(None, description="GPT-SoVITS/Index-TTS API 地址"),
    method: Optional[str] = Query("POST", description="请求方式: GET/POST"),
    ref_audio_path: Optional[str] = Query(None, description="参考音频路径"),
    prompt_text: Optional[str] = Query(None, description="参考音频文本"),
    prompt_lang: Optional[str] = Query(None, description="参考音频语言"),
    text_lang: Optional[str] = Query(None, description="文本语言"),
    top_k: Optional[int] = Query(None, description="Top K 采样"),
    top_p: Optional[float] = Query(None, description="Top P 采样"),
    temperature: Optional[float] = Query(None, description="温度"),
    text_split_method: Optional[str] = Query(None, description="文本分割方法"),
    batch_size: Optional[int] = Query(None, description="批次大小"),
    split_bucket: Optional[bool] = Query(None, description="分桶处理"),
    speed_factor: Optional[float] = Query(None, description="语速因子"),
    streaming_mode: Optional[bool] = Query(None, description="流式模式"),
    media_type: Optional[str] = Query(None, description="输出格式: wav/raw/ogg/aac"),
    seed: Optional[int] = Query(None, description="随机种子"),
    parallel_infer: Optional[bool] = Query(None, description="并行推理"),
    repetition_penalty: Optional[float] = Query(None, description="重复惩罚"),
    sample_steps: Optional[int] = Query(None, description="采样步数 (V3)"),
    spk_audio_prompt: Optional[str] = Query(None, description="IndexTTS2 说话人参考音频路径"),
    emo_audio_prompt: Optional[str] = Query(None, description="IndexTTS2 情感参考音频路径"),
    emo_alpha: Optional[float] = Query(None, description="IndexTTS2 情感强度"),
    use_emo_text: Optional[bool] = Query(None, description="IndexTTS2 是否使用情感文本"),
    emo_text: Optional[str] = Query(None, description="IndexTTS2 情感文本"),
    output_format: Optional[str] = Query(None, description="IndexTTS2 输出格式"),
    audio_bitdepth: Optional[str] = Query(None, description="IndexTTS2 采样深度"),
    max_text_tokens_per_segment: Optional[int] = Query(None, description="IndexTTS2 每段最大文本token数"),
    interval_silence_ms: Optional[int] = Query(None, description="IndexTTS2 片间静音毫秒"),
    length_penalty: Optional[float] = Query(None, description="IndexTTS2 长度惩罚"),
    num_beams: Optional[int] = Query(None, description="IndexTTS2 Beam 数量"),
    use_random: Optional[bool] = Query(None, description="IndexTTS2 是否使用随机"),
    do_sample: Optional[bool] = Query(None, description="IndexTTS2 是否采样"),
    emo_vector: Optional[str] = Query(None, description="IndexTTS2 情感向量(JSON)"),
    resume_project: Optional[bool] = Query(None, description="IndexTTS2 恢复项目"),
    bucket_threshold: Optional[float] = Query(None, description="IndexTTS2 分桶阈值"),
    max_mel_tokens: Optional[int] = Query(None, description="IndexTTS2 最大 mel token 数"),
):
    tts_config = CONFIG.get("tts", {})
    handler = TTS_HANDLERS.get(engine)
    if handler is None:
        _raise_tts_http_error(400, "UNSUPPORTED_TTS_ENGINE", f"不支持的 TTS 引擎: {engine}")
    
    # 解析 emo_vector JSON 字符串
    parsed_emo_vector = None
    if emo_vector:
        try:
            parsed_emo_vector = json.loads(emo_vector)
        except (json.JSONDecodeError, TypeError):
            pass

    return await handler(
        text,
        tts_config,
        api_url=api_url,
        method=method,
        ref_audio_path=ref_audio_path,
        prompt_text=prompt_text,
        prompt_lang=prompt_lang,
        text_lang=text_lang,
        top_k=top_k,
        top_p=top_p,
        temperature=temperature,
        text_split_method=text_split_method,
        batch_size=batch_size,
        split_bucket=split_bucket,
        bucket_threshold=bucket_threshold,
        speed_factor=speed_factor,
        streaming_mode=streaming_mode,
        media_type=media_type,
        seed=seed,
        parallel_infer=parallel_infer,
        repetition_penalty=repetition_penalty,
        sample_steps=sample_steps,
        spk_audio_prompt=spk_audio_prompt,
        emo_audio_prompt=emo_audio_prompt,
        emo_alpha=emo_alpha,
        emo_vector=parsed_emo_vector,
        use_emo_text=use_emo_text,
        emo_text=emo_text,
        use_random=use_random,
        do_sample=do_sample,
        output_format=output_format,
        audio_bitdepth=audio_bitdepth,
        max_text_tokens_per_segment=max_text_tokens_per_segment,
        interval_silence_ms=interval_silence_ms,
        resume_project=resume_project,
        length_penalty=length_penalty,
        num_beams=num_beams,
        max_mel_tokens=max_mel_tokens,
        voice=voice,
        rate=rate,
        volume=volume,
        pitch=pitch,
    )


async def tts_edge(text: str, config: dict):
    """Microsoft Edge TTS
    
    参数说明:
    - voice: 语音名称，如 zh-CN-XiaoxiaoNeural
    - rate: 语速，如 +50% 或 -30%
    - volume: 音量，如 +50% 或 -30%
    - pitch: 音调，如 +50Hz 或 -30Hz
    """
    try:
        import edge_tts
    except ImportError:
        _raise_tts_http_error(500, "EDGE_NOT_INSTALLED", "请安装 edge-tts: pip install edge-tts")
    
    voice = config.get("voice", "zh-CN-XiaoxiaoNeural")
    rate = config.get("rate", "+0%")
    volume = config.get("volume", "+0%")
    pitch = config.get("pitch", "+0Hz")
    
    communicate = edge_tts.Communicate(
        text,
        voice,
        rate=rate,
        volume=volume,
        pitch=pitch
    )
    
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tmp_path = tmp.name
        await communicate.save(tmp_path)
    except Exception as e:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        _raise_tts_http_error(500, "EDGE_SYNTH_ERROR", f"Edge TTS 合成失败: {e}")
    
    def iterfile():
        try:
            with open(tmp_path, "rb") as f:
                yield from f
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    
    return StreamingResponse(iterfile(), media_type="audio/mpeg")


async def tts_gptsovits(text: str, config: dict):
    """GPT-SoVITS TTS
    
    完整参数说明 (参考官方 API 文档 api_v2.py):
    - api_url: API 地址，默认 http://127.0.0.1:9880
    - method: 请求方式 GET/POST
    - text_lang: 文本语言 (zh/en/ja/all_zh/all_ja)
    - ref_audio_path: 参考音频路径（必需）
    - prompt_text: 参考音频的文本内容
    - prompt_lang: 参考音频的语言
    - top_k: Top-K 采样参数 (默认 5)
    - top_p: Top-P 采样参数 (默认 1)
    - temperature: 温度参数 (默认 1)
    - text_split_method: 文本分割方法 (cut0-cut5)
    - batch_size: 批次大小 (默认 1)
    - batch_threshold: 批次分割阈值 (默认 0.75)
    - split_bucket: 是否分桶 (默认 True)
    - speed_factor: 语速因子 (默认 1.0)
    - fragment_interval: 音频片段间隔 (默认 0.3)
    - seed: 随机种子 (默认 -1)
    - media_type: 输出格式 wav/raw/ogg/aac (默认 wav)
    - streaming_mode: 流式模式 (默认 False)
    - parallel_infer: 并行推理 (默认 True)
    - repetition_penalty: 重复惩罚 (默认 1.35)
    - sample_steps: 采样步数 V3 (默认 32)
    - super_sampling: 超采样 V3 (默认 False)
    """
    api_url = config.get("api_url", "http://127.0.0.1:9880")
    method = config.get("method", "POST").upper()
    streaming_mode = config.get("streaming_mode", False)
    
    # 流式模式时强制使用 raw 格式，因为流式 WAV 的头部长度不正确
    # 非流式模式使用 wav 格式，浏览器可以直接播放
    media_type = "raw" if streaming_mode else config.get("media_type", "wav")
    
    # 构建请求参数
    params = {
        "text": text,
        "text_lang": config.get("text_lang", "zh"),
        "prompt_lang": config.get("prompt_lang", "zh"),
        "streaming_mode": streaming_mode,
        "media_type": media_type,
    }
    
    # 必需参数：参考音频
    ref_audio = config.get("ref_audio_path", "")
    if ref_audio:
        params["ref_audio_path"] = ref_audio
    
    # 可选参数：参考文本
    prompt_text = config.get("prompt_text", "")
    if prompt_text:
        params["prompt_text"] = prompt_text
    
    # 采样参数
    params["top_k"] = config.get("top_k", 5)
    params["top_p"] = config.get("top_p", 1)
    params["temperature"] = config.get("temperature", 1)
    
    # 文本处理参数
    params["text_split_method"] = config.get("text_split_method", "cut5")
    params["batch_size"] = config.get("batch_size", 1)
    params["batch_threshold"] = config.get("batch_threshold", 0.75)
    params["split_bucket"] = config.get("split_bucket", True)
    
    # 输出参数
    params["speed_factor"] = config.get("speed_factor", 1.0)
    params["fragment_interval"] = config.get("fragment_interval", 0.3)
    
    # 其他参数
    params["seed"] = config.get("seed", -1)
    params["parallel_infer"] = config.get("parallel_infer", True)
    params["repetition_penalty"] = config.get("repetition_penalty", 1.35)
    params["sample_steps"] = config.get("sample_steps", 32)
    params["super_sampling"] = config.get("super_sampling", False)
    
    try:
        # 流式模式：直接返回流式响应，不预先请求
        if streaming_mode:
            async def stream_audio():
                try:
                    # 使用较小的 chunk_size 确保数据能尽快返回前端
                    # GPT-SoVITS 返回的是 PCM 数据，4KB 约 0.0625 秒音频 (32kHz, 16bit, mono)
                    chunk_size = 8192  # 8KB per chunk
                    if method == "GET":
                        async with HTTP_CLIENT.stream("GET", f"{api_url}/tts", params=params) as r:
                            r.raise_for_status()
                            async for chunk in r.aiter_bytes(chunk_size=chunk_size):
                                yield chunk
                    else:
                        async with HTTP_CLIENT.stream("POST", f"{api_url}/tts", json=params) as r:
                            r.raise_for_status()
                            async for chunk in r.aiter_bytes(chunk_size=chunk_size):
                                yield chunk
                except httpx.HTTPStatusError as e:
                    # 流式请求中的错误无法正常返回，记录日志
                    print(f"GPT-SoVITS 流式请求错误: {e}")
                    raise
        
            # 流式模式使用 raw 格式，前端会转换为 WAV
            return StreamingResponse(stream_audio(), media_type="audio/raw")
        
        # 非流式模式：等待完整响应
        if method == "GET":
            response = await HTTP_CLIENT.get(f"{api_url}/tts", params=params)
        else:
            # 官方 api_v2.py POST 期望 JSON，且会校验 body 模型
            response = await HTTP_CLIENT.post(f"{api_url}/tts", json=params)
        
        response.raise_for_status()
        content_type = response.headers.get("content-type", "audio/wav")
        return Response(content=response.content, media_type=content_type)
    except httpx.TimeoutException:
        _raise_tts_http_error(504, "GPTSOVITS_TIMEOUT", "GPT-SoVITS 请求超时，请尝试缩短文本长度")
    except httpx.HTTPStatusError as e:
        error_detail = e.response.text if e.response else str(e)
        print(f"GPT-SoVITS 请求失败: {error_detail}")
        _raise_tts_http_error(500, "GPTSOVITS_HTTP_ERROR", f"GPT-SoVITS 请求失败: {error_detail}")
    except httpx.ConnectError:
        _raise_tts_http_error(503, "GPTSOVITS_CONNECT_ERROR", "无法连接到 GPT-SoVITS 服务，请确认服务已启动")
    except Exception as e:
        _raise_tts_http_error(500, "GPTSOVITS_UNKNOWN_ERROR", f"GPT-SoVITS 请求失败: {e}")


async def tts_indextts(text: str, config: dict):
    """IndexTTS2

    参数说明:
    - api_url: API 地址，默认 http://127.0.0.1:9881
    - spk_audio_prompt: 说话人参考音频路径（从 config 中读取）
    """
    api_url = (config.get("api_url") or "http://127.0.0.1:9881").rstrip("/")

    spk_audio_prompt = config.get("spk_audio_prompt") or ""
    if not spk_audio_prompt:
        _raise_tts_http_error(
            400,
            "INDEXTTS_MISSING_SPK_PROMPT",
            "IndexTTS2 需要配置 spk_audio_prompt（说话人参考音频路径）",
        )

    check_local = False
    is_windows_local_path = False
    try:
        parsed = urlparse(api_url)
        host = (parsed.hostname or "").lower()
        if host in ("127.0.0.1", "localhost"):
            check_local = True
    except Exception:
        check_local = True

    try:
        if os.name == "nt":
            if len(spk_audio_prompt) >= 3 and spk_audio_prompt[1] == ":" and spk_audio_prompt[2] in ("\\", "/"):
                is_windows_local_path = True
    except Exception:
        is_windows_local_path = False

    if (not check_local) and is_windows_local_path:
        _raise_tts_http_error(
            400,
            "INDEXTTS_REMOTE_WINDOWS_PATH",
            "检测到使用 Windows 本机路径作为远程 IndexTTS2 参考音频路径，请勾选“上传参考音频到服务器”并重新选择文件",
        )

    # 移除本地文件检查，直接由 IndexTTS 服务端校验
    # if check_local and not os.path.exists(spk_audio_prompt):
    #     _raise_tts_http_error(
    #         400,
    #         "INDEXTTS_SPK_NOT_FOUND",
    #         f"IndexTTS2 说话人参考音频不存在: {spk_audio_prompt}",
    #     )

    payload = {
        "text": text,
        "spk_audio_prompt": spk_audio_prompt,
    }

    emo_audio_prompt = config.get("emo_audio_prompt")
    if emo_audio_prompt:
        if (not check_local) and is_windows_local_path:
            _raise_tts_http_error(
                400,
                "INDEXTTS_REMOTE_WINDOWS_PATH",
                "检测到使用 Windows 本机路径作为远程 IndexTTS2 参考音频路径，请勾选“上传参考音频到服务器”并重新选择文件",
            )
        # 移除本地文件检查，直接由 IndexTTS 服务端校验
        # if check_local and not os.path.exists(emo_audio_prompt):
        #     _raise_tts_http_error(
        #         400,
        #         "INDEXTTS_EMO_NOT_FOUND",
        #         f"IndexTTS2 情感参考音频不存在: {emo_audio_prompt}",
        #     )
        payload["emo_audio_prompt"] = emo_audio_prompt

    # 批量填充可选参数
    _optional_keys = [
        "emo_alpha", "use_emo_text", "emo_text", "use_random", "emo_vector",
        "output_format", "audio_bitdepth", "resume_project", "split_bucket",
        "bucket_threshold", "max_text_tokens_per_segment", "interval_silence_ms",
        "do_sample", "top_p", "top_k", "temperature", "length_penalty",
        "num_beams", "repetition_penalty", "max_mel_tokens",
    ]
    for key in _optional_keys:
        val = config.get(key)
        if val is not None:
            payload[key] = val

    try:
        response = await HTTP_CLIENT.post(f"{api_url}/tts", json=payload)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "audio/wav")
        return StreamingResponse(iter([response.content]), media_type=content_type)
    except httpx.TimeoutException:
        _raise_tts_http_error(504, "INDEXTTS_TIMEOUT", "IndexTTS2 请求超时，请尝试缩短文本长度或稍后重试")
    except httpx.ConnectError as e:
        _raise_tts_http_error(503, "INDEXTTS_CONNECT_ERROR", f"无法连接到 IndexTTS2 服务: {e}")
    except httpx.HTTPStatusError as e:
        error_detail = e.response.text if e.response else str(e)
        print(f"IndexTTS2 请求失败: {error_detail}")
        _raise_tts_http_error(500, "INDEXTTS_HTTP_ERROR", f"IndexTTS2 请求失败: {error_detail}")
    except Exception as e:
        _raise_tts_http_error(500, "INDEXTTS_UNKNOWN_ERROR", f"IndexTTS2 请求失败: {e}")


@app.get("/api/tts/voices")
async def get_tts_voices():
    """获取可用的 Edge TTS 语音列表"""
    try:
        import edge_tts
        voices = await edge_tts.list_voices()
        # 只返回中文语音
        zh_voices = [v for v in voices if v["Locale"].startswith("zh-")]
        return {"voices": zh_voices}
    except ImportError:
        return {"voices": [], "error": "edge-tts 未安装"}
    except Exception as e:
        return {"voices": [], "error": str(e)}


if __name__ == "__main__":
    print("正在启动服务器: http://127.0.0.1:8123")
    print(f"书籍目录: {get_book_directories()}")
    
    # 尝试禁用 Windows 控制台 QuickEdit 模式，防止点击控制台导致服务器暂停
    if os.name == 'nt':
        try:
            kernel32 = ctypes.windll.kernel32
            # ENABLE_QUICK_EDIT_MODE = 0x0040
            # STD_INPUT_HANDLE = -10
            hInput = kernel32.GetStdHandle(-10)
            mode = ctypes.c_uint()
            if kernel32.GetConsoleMode(hInput, ctypes.byref(mode)):
                # 移除 QuickEdit 模式位
                mode.value &= ~0x0040
                # 移除 Insert 模式位 (0x0020)
                mode.value &= ~0x0020
                kernel32.SetConsoleMode(hInput, mode)
                print("✅ 已禁用 Windows 控制台快速编辑模式")
        except Exception as e:
            print(f"⚠️ 禁用快速编辑模式失败: {e}")
            
    uvicorn.run(app, host="127.0.0.1", port=8123)
