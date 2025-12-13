"""
EPUB 阅读器 Web 服务
"""

import os
import json
import asyncio
import tempfile
from pathlib import Path
from functools import lru_cache
from typing import Optional, List, Dict, Any
from fastapi import Body

from fastapi import FastAPI, Request, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import re
import uuid
import shutil
import subprocess
import concurrent.futures

from reader3 import Book, process_epub, save_to_json

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# 提供静态资源服务
app.mount("/static", StaticFiles(directory="static"), name="static")

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
            "api_url": "http://127.0.0.1:8080",
            "voice": ""
        },
        # 用户自定义参考音频列表
        "saved_refs": [],
        # 朗读预加载默认值
        "preload_count": -1
    },
    # 朗读进度记录：book_id -> {chapter_index, paragraph_index}
    "reading_progress": {}
}


def load_config() -> dict:
    """加载配置文件；若不存在则创建默认配置文件"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                # 合并默认配置
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                # 确保 tts 下的新增字段存在
                if "tts" not in config:
                    config["tts"] = DEFAULT_CONFIG["tts"]
                else:
                    if "saved_refs" not in config["tts"]:
                        config["tts"]["saved_refs"] = []
                    if "preload_count" not in config["tts"]:
                        config["tts"]["preload_count"] = -1
                if "reading_progress" not in config:
                    config["reading_progress"] = {}
                return config
        except Exception as e:
            print(f"加载配置失败: {e}")
    # 不存在或加载失败：写入默认配置文件
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"创建默认配置失败: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    """保存配置文件"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)


CONFIG = load_config()

# 书籍索引文件（由 start.py 维护）
BOOKS_INDEX_FILE = "books_index.json"


def load_books_index() -> List[dict]:
    """加载书籍索引文件"""
    if os.path.exists(BOOKS_INDEX_FILE):
        try:
            with open(BOOKS_INDEX_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_books_index(entries: List[dict]) -> None:
    """保存书籍索引文件"""
    try:
        with open(BOOKS_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存书籍索引失败: {e}")

def _normp(p: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    except Exception:
        return p

def add_to_books_index(book_id: str, directory: str, title: str = ""):
    entries = load_books_index()
    dirn = _normp(directory)
    entries = [e for e in entries if not (e.get("id") == book_id and _normp(e.get("directory", "")) == dirn)]
    entries.insert(0, {"id": book_id, "directory": directory, "title": title})
    save_books_index(entries)

SUPPORTED_EXTS = [".epub", ".mobi", ".azw3", ".fb2", ".txt", ".pdf"]
EXECUTOR = concurrent.futures.ProcessPoolExecutor(max_workers=3)
IMPORT_JOBS: Dict[str, Dict[str, Any]] = {}

def _find_ebook_convert() -> Optional[str]:
    from shutil import which
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
        try:
            subprocess.run([conv, input_path, dest_epub], check=True)
            epub_path = dest_epub
        except Exception as e:
            return {"status": "error", "error": str(e), "file": input_path}
    try:
        os.makedirs(base_dir, exist_ok=True)
    except Exception:
        pass
    out_dir = os.path.join(base_dir, out_dir_base + "_data")
    if os.path.isdir(out_dir) and os.path.exists(os.path.join(out_dir, "book.json")):
        return {"status": "skipped", "file": input_path, "out_dir": out_dir}
    from reader3 import process_epub, save_to_json
    try:
        book_obj = process_epub(epub_path, out_dir)
        save_to_json(book_obj, out_dir)
        return {
            "status": "done",
            "file": input_path,
            "out_dir": out_dir,
            "book_id": os.path.basename(out_dir),
            "base_dir": os.path.dirname(out_dir),
            "title": book_obj.metadata.title
        }
    except Exception as e:
        return {"status": "error", "error": str(e), "file": input_path}

def _schedule_import(path: str, base_dir: Optional[str] = None, sanitize: bool = False, temp_input: bool = False) -> str:
    job_id = str(uuid.uuid4())
    p = os.path.abspath(path)
    bd = os.path.abspath(base_dir) if base_dir else os.path.dirname(p)
    IMPORT_JOBS[job_id] = {"id": job_id, "file": p, "status": "queued", "temp_input": bool(temp_input)}
    fut = EXECUTOR.submit(_job_worker, p, bd, sanitize)
    IMPORT_JOBS[job_id]["future"] = fut
    def _cb(f):
        try:
            res = f.result()
            IMPORT_JOBS[job_id].update(res)
            IMPORT_JOBS[job_id]["status"] = res.get("status", "done")
            if res.get("status") == "done" and res.get("book_id"):
                add_to_books_index(res["book_id"], res["base_dir"], res.get("title", ""))
            if IMPORT_JOBS[job_id].get("temp_input"):
                try:
                    orig = IMPORT_JOBS[job_id].get("file")
                    if orig and os.path.isfile(orig):
                        os.remove(orig)
                    conv = os.path.splitext(IMPORT_JOBS[job_id].get("file") or "")[0] + ".converted.epub"
                    if conv and os.path.isfile(conv):
                        os.remove(conv)
                except Exception:
                    pass
        except Exception as e:
            IMPORT_JOBS[job_id]["status"] = "error"
            IMPORT_JOBS[job_id]["error"] = str(e)
    fut.add_done_callback(_cb)
    return job_id


def get_book_directories() -> List[str]:
    """获取所有书籍目录（包括从索引文件中提取的），进行路径规范化去重"""
    base_dirs = list(CONFIG.get("book_directories", ["."]))
    def normp(p: str) -> str:
        try:
            return os.path.normcase(os.path.normpath(os.path.abspath(p)))
        except Exception:
            return p
    seen = set(normp(d) for d in base_dirs)
    dirs = list(base_dirs)
    for book in load_books_index():
        book_dir = book.get("directory", "")
        if book_dir:
            nd = normp(book_dir)
            if nd not in seen:
                dirs.append(book_dir)
                seen.add(nd)
    return dirs


# === 书籍缓存（支持多目录） ===

# 缓存：book_id -> (目录路径, Book对象)
_book_cache: Dict[str, tuple] = {}


def find_book_path(book_id: str) -> Optional[str]:
    """在所有配置目录中查找书籍"""
    safe_book_id = os.path.basename(book_id)
    
    for base_dir in get_book_directories():
        book_path = os.path.join(base_dir, safe_book_id)
        json_path = os.path.join(book_path, "book.json")
        if os.path.exists(json_path):
            return base_dir
    return None


def load_book_cached(book_id: str) -> Optional[Book]:
    """从 JSON 文件加载书籍（支持多目录）"""
    # 检查缓存
    if book_id in _book_cache:
        return _book_cache[book_id][1]
    
    base_dir = find_book_path(book_id)
    if not base_dir:
        return None
    
    safe_book_id = os.path.basename(book_id)
    json_path = os.path.join(base_dir, safe_book_id, "book.json")
    
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        book = Book.from_dict(data)
        # 缓存（限制大小）
        if len(_book_cache) > 10:
            # 移除最早的缓存
            oldest = next(iter(_book_cache))
            del _book_cache[oldest]
        _book_cache[book_id] = (base_dir, book)
        return book
    except Exception as e:
        print(f"加载书籍 {book_id} 时出错: {e}")
        return None


def get_book_base_dir(book_id: str) -> Optional[str]:
    """获取书籍所在的基础目录"""
    if book_id in _book_cache:
        return _book_cache[book_id][0]
    return find_book_path(book_id)


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

def scan_all_books() -> List[dict]:
    """扫描所有目录中的书籍"""
    books = []
    seen_ids = set()
    invalid_dirs = []
    
    # 首先从书籍索引文件加载
    for book_entry in load_books_index():
        book_id = book_entry.get("id", "")
        book_dir = book_entry.get("directory", ".")
        
        if book_id and book_id not in seen_ids:
            full_path = os.path.join(book_dir, book_id)
            if os.path.isdir(full_path):
                index = load_book_index(book_dir, book_id)
                book_json = os.path.join(full_path, "book.json")
                if index and os.path.exists(book_json):
                    cover = find_cover_image(book_dir, book_id)
                    prog = CONFIG.get("reading_progress", {}).get(book_id)
                    pct = None
                    detail = None
                    try:
                        if prog and isinstance(index.get('chapters'), int) and index['chapters'] > 0:
                            pct = int(round(((prog.get('chapter_index', 0) + 1) / index['chapters']) * 100))
                            bobj = load_book_cached(book_id)
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
                        pct = pct if pct is not None else None
                    seen_ids.add(book_id)
                    books.append({
                        "id": book_id,
                        "title": index['title'],
                        "author": ", ".join(index.get('authors', [])),
                        "chapters": index['chapters'],
                        "location": book_dir,
                        "cover": cover or "",
                        "progress": prog or {},
                        "progress_percent": pct,
                        "progress_detail": detail
                    })
    
    # 然后扫描配置的目录
    for base_dir in CONFIG.get("book_directories", ["."]):
        # 检查路径是否存在且是目录
        if not os.path.exists(base_dir):
            invalid_dirs.append(base_dir)
            continue
        if not os.path.isdir(base_dir):
            invalid_dirs.append(base_dir)
            continue
        
        try:
            for item in os.listdir(base_dir):
                if item.endswith("_data") and item not in seen_ids:
                    full_path = os.path.join(base_dir, item)
                    if os.path.isdir(full_path):
                        index = load_book_index(base_dir, item)
                        book_json = os.path.join(full_path, "book.json")
                        if index and os.path.exists(book_json):
                            cover = find_cover_image(base_dir, item)
                            prog = CONFIG.get("reading_progress", {}).get(item)
                            pct = None
                            detail = None
                            try:
                                if prog and isinstance(index.get('chapters'), int) and index['chapters'] > 0:
                                    pct = int(round(((prog.get('chapter_index', 0) + 1) / index['chapters']) * 100))
                                    bobj = load_book_cached(item)
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
                                pct = pct if pct is not None else None
                            seen_ids.add(item)
                            books.append({
                                "id": item,
                                "title": index['title'],
                                "author": ", ".join(index.get('authors', [])),
                                "chapters": index['chapters'],
                                "location": base_dir,
                                "cover": cover or "",
                                "progress": prog or {},
                                "progress_percent": pct,
                                "progress_detail": detail
                            })
        except Exception as e:
            print(f"扫描目录 {base_dir} 时出错: {e}")
            invalid_dirs.append(base_dir)
    
    # 自动清理无效目录
    if invalid_dirs:
        for invalid_dir in invalid_dirs:
            if invalid_dir in CONFIG["book_directories"] and invalid_dir != ".":
                print(f"自动移除无效目录: {invalid_dir}")
                CONFIG["book_directories"].remove(invalid_dir)
        save_config(CONFIG)
    
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

import hashlib

AUDIO_CACHE_DIR = Path("tts_cache")

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
    import base64
    
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
    import shutil
    
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
async def serve_image(book_id: str, image_name: str):
    """为书籍提供图片服务"""
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)
    
    base_dir = get_book_base_dir(book_id)
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
async def redirect_to_first_chapter(book_id: str):
    """重定向到第一章"""
    return RedirectResponse(url=f"/read/{book_id}/0", status_code=302)


@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: str, chapter_index: int):
    """阅读器主界面"""
    book = load_book_cached(book_id)
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
                    return f"/read/{book_id}/images/{os.path.basename(u)}"
                return f"/read/{book_id}/images/{os.path.basename(u)}"

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


def clean_path(path: str) -> str:
    """清理路径（移除首尾引号和空白）"""
    path = path.strip()
    # 移除首尾的引号
    if (path.startswith('"') and path.endswith('"')) or \
       (path.startswith("'") and path.endswith("'")):
        path = path[1:-1]
    return path.strip()

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

# === 朗读进度存储 ===

@app.get("/api/progress")
async def get_progress(book_id: str = Query(..., description="书籍 ID")):
    """获取朗读进度"""
    progress = CONFIG.get("reading_progress", {}).get(book_id)
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
    CONFIG.setdefault("reading_progress", {})[book_id] = {
        "chapter_index": chapter_index,
        "paragraph_index": paragraph_index
    }
    save_config(CONFIG)
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
    def normp(p: str) -> str:
        try:
            return os.path.normcase(os.path.normpath(os.path.abspath(p)))
        except Exception:
            return p
    target = normp(raw) if raw else ""
    # 配置目录移除（规范化匹配）
    cur_dirs = CONFIG.get("book_directories", [])
    new_dirs = [d for d in cur_dirs if normp(d) != target]
    if len(new_dirs) != len(cur_dirs):
        CONFIG["book_directories"] = new_dirs
        save_config(CONFIG)
    # 索引目录移除（规范化匹配），默认执行；当 keep_index=true 时跳过
    if not keep_index:
        index_entries = load_books_index()
        if index_entries:
            filtered = [b for b in index_entries if normp(b.get("directory", "")) != target]
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
        book_obj = process_epub(path, out_dir)
        save_to_json(book_obj, out_dir)
        book_id = os.path.basename(out_dir)
        base_dir = os.path.dirname(out_dir)
        add_to_books_index(book_id, base_dir, book_obj.metadata.title)
        return {"success": True, "id": book_id, "directory": base_dir}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导入失败: {e}")

@app.post("/api/import/directory")
async def import_directory(request: Request):
    # 目录内所有支持的电子书提交为导入任务，输出目录统一在工作目录，行为与上传导入一致
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
    jobs = []
    for root, _, files in os.walk(path):
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXTS:
                jobs.append(_schedule_import(os.path.join(root, name), base_dir, sanitize_flag))
    return {"success": True, "jobs": jobs, "count": len(jobs)}

@app.post("/api/import/upload-books")
async def import_upload_books(files: List[UploadFile] = File(...), sanitize: Optional[str] = Form(None)):
    base_dir = CONFIG.get("book_directories", ["."])[0] if CONFIG.get("book_directories") else "."
    base_dir = os.path.abspath(base_dir)
    os.makedirs(base_dir, exist_ok=True)
    jobs = []
    sanitize_flag = False
    try:
        sanitize_flag = bool(sanitize) and str(sanitize).lower() not in ("", "0", "false")
    except Exception:
        sanitize_flag = False
    # 将上传的原始文件写入系统临时文件，使用原始文件名与后缀，处理完成后清理
    for f in files:
        try:
            orig_name = os.path.basename(f.filename)
            ext = os.path.splitext(orig_name)[1].lower()
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            try:
                content = await f.read()
                with open(tmp.name, "wb") as out:
                    out.write(content)
            finally:
                try:
                    tmp.close()
                except Exception:
                    pass
            if ext in SUPPORTED_EXTS:
                jobs.append(_schedule_import(tmp.name, base_dir, sanitize_flag, temp_input=True))
        except Exception:
            pass
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

@app.post("/api/books/delete")
async def delete_books(ids: List[str] = Body(..., embed=True, description="书籍ID列表")):
    TRASH_DIR.mkdir(exist_ok=True)
    trash = load_trash_index()
    entries = load_books_index()
    removed = []
    for bid in ids:
        try:
            base_dir = get_book_base_dir(bid)
            if not base_dir:
                for e in entries:
                    if e.get("id") == bid:
                        base_dir = e.get("directory")
                        break
            if not base_dir:
                continue
            src = Path(base_dir) / bid
            if src.is_dir():
                dst = TRASH_DIR / bid
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.move(str(src), str(dst))
                entries = [e for e in entries if e.get("id") != bid]
                save_books_index(entries)
                trash[bid] = {
                    "original_dir": base_dir,
                    "title": bid,
                }
                removed.append(bid)
        except Exception as e:
            print(f"删除 {bid} 失败: {e}")
    save_trash_index(trash)
    for bid in removed:
        _book_cache.pop(bid, None)
    return {"success": True, "removed": removed}

@app.get("/api/books/trash")
async def list_trash():
    trash = load_trash_index()
    items = []
    for bid, info in trash.items():
        items.append({"id": bid, **info})
    return {"items": items}

@app.post("/api/books/trash/restore")
async def restore_trash(id: str = Body(..., embed=True)):
    trash = load_trash_index()
    info = trash.get(id)
    if not info:
        raise HTTPException(status_code=404, detail="未找到备份项")
    src = TRASH_DIR / id
    if not src.is_dir():
        raise HTTPException(status_code=404, detail="备份目录不存在")
    base_dir = info.get("original_dir", ".")
    dst = Path(base_dir) / id
    if dst.exists():
        dst = Path(base_dir) / f"{id}_restored"
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
    import os
    
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


# 全局 HTTP 客户端（复用连接）
# 限制最大连接数，防止耗尽资源
import httpx
HTTP_CLIENT = httpx.AsyncClient(
    timeout=300.0,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
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
            except:
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
            except:
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
        await HTTP_CLIENT.get(f"{api_url}/control", params={"command": command}, timeout=10.0)
        return {"success": True}
    except:
        # 服务可能已停止或正在重启
        return {"success": True}


# === TTS API ===

@app.get("/api/tts/speak")
async def tts_speak(
    text: str = Query(..., description="要朗读的文本"),
    engine: str = Query("edge", description="TTS 引擎: edge/gptsovits/indextts"),
    # Edge TTS 参数
    voice: Optional[str] = Query(None, description="Edge TTS 语音"),
    rate: Optional[str] = Query(None, description="Edge TTS 语速"),
    volume: Optional[str] = Query(None, description="Edge TTS 音量"),
    pitch: Optional[str] = Query(None, description="Edge TTS 音调"),
    # GPT-SoVITS 基本参数
    api_url: Optional[str] = Query(None, description="GPT-SoVITS/Index-TTS API 地址"),
    method: Optional[str] = Query("POST", description="请求方式: GET/POST"),
    ref_audio_path: Optional[str] = Query(None, description="参考音频路径"),
    prompt_text: Optional[str] = Query(None, description="参考音频文本"),
    prompt_lang: Optional[str] = Query(None, description="参考音频语言"),
    text_lang: Optional[str] = Query(None, description="文本语言"),
    # GPT-SoVITS 采样参数
    top_k: Optional[int] = Query(None, description="Top K 采样"),
    top_p: Optional[float] = Query(None, description="Top P 采样"),
    temperature: Optional[float] = Query(None, description="温度"),
    # GPT-SoVITS 文本处理参数
    text_split_method: Optional[str] = Query(None, description="文本分割方法"),
    batch_size: Optional[int] = Query(None, description="批次大小"),
    split_bucket: Optional[bool] = Query(None, description="分桶处理"),
    # GPT-SoVITS 输出参数
    speed_factor: Optional[float] = Query(None, description="语速因子"),
    streaming_mode: Optional[bool] = Query(None, description="流式模式"),
    media_type: Optional[str] = Query(None, description="输出格式: wav/raw/ogg/aac"),
    # GPT-SoVITS 其他参数
    seed: Optional[int] = Query(None, description="随机种子"),
    parallel_infer: Optional[bool] = Query(None, description="并行推理"),
    repetition_penalty: Optional[float] = Query(None, description="重复惩罚"),
    sample_steps: Optional[int] = Query(None, description="采样步数 (V3)"),
):
    """文本转语音 API - 支持 Edge TTS / GPT-SoVITS / Index-TTS"""
    tts_config = CONFIG.get("tts", {})
    
    if engine == "edge":
        edge_config = dict(tts_config.get("edge", {}))
        if voice:
            edge_config["voice"] = voice
        if rate:
            edge_config["rate"] = rate
        if volume:
            edge_config["volume"] = volume
        if pitch:
            edge_config["pitch"] = pitch
        return await tts_edge(text, edge_config)
    
    elif engine == "gptsovits":
        gptsovits_config = dict(tts_config.get("gptsovits", {}))
        # 基本参数
        if api_url:
            gptsovits_config["api_url"] = api_url
        if method:
            gptsovits_config["method"] = method
        if ref_audio_path:
            gptsovits_config["ref_audio_path"] = ref_audio_path
        if prompt_text:
            gptsovits_config["prompt_text"] = prompt_text
        if prompt_lang:
            gptsovits_config["prompt_lang"] = prompt_lang
        if text_lang:
            gptsovits_config["text_lang"] = text_lang
        # 采样参数
        if top_k is not None:
            gptsovits_config["top_k"] = top_k
        if top_p is not None:
            gptsovits_config["top_p"] = top_p
        if temperature is not None:
            gptsovits_config["temperature"] = temperature
        # 文本处理参数
        if text_split_method:
            gptsovits_config["text_split_method"] = text_split_method
        if batch_size is not None:
            gptsovits_config["batch_size"] = batch_size
        if split_bucket is not None:
            gptsovits_config["split_bucket"] = split_bucket
        # 输出参数
        if speed_factor is not None:
            gptsovits_config["speed_factor"] = speed_factor
        if streaming_mode is not None:
            gptsovits_config["streaming_mode"] = streaming_mode
        if media_type:
            gptsovits_config["media_type"] = media_type
        # 其他参数
        if seed is not None:
            gptsovits_config["seed"] = seed
        if parallel_infer is not None:
            gptsovits_config["parallel_infer"] = parallel_infer
        if repetition_penalty is not None:
            gptsovits_config["repetition_penalty"] = repetition_penalty
        if sample_steps is not None:
            gptsovits_config["sample_steps"] = sample_steps
            
        return await tts_gptsovits(text, gptsovits_config)
    
    elif engine == "indextts":
        indextts_config = dict(tts_config.get("indextts", {}))
        if api_url:
            indextts_config["api_url"] = api_url
        return await tts_indextts(text, indextts_config)
    
    else:
        raise HTTPException(status_code=400, detail=f"不支持的 TTS 引擎: {engine}")


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
        raise HTTPException(status_code=500, detail="请安装 edge-tts: pip install edge-tts")
    
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
    
    # 创建临时文件
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        tmp_path = tmp.name
    
    await communicate.save(tmp_path)
    
    def iterfile():
        with open(tmp_path, "rb") as f:
            yield from f
        os.unlink(tmp_path)
    
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
    import httpx
    
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
        raise HTTPException(status_code=504, detail="GPT-SoVITS 请求超时，请尝试缩短文本长度")
    except httpx.HTTPStatusError as e:
        error_detail = e.response.text if e.response else str(e)
        print(f"GPT-SoVITS 请求失败: {error_detail}")
        raise HTTPException(status_code=500, detail=f"GPT-SoVITS 请求失败: {error_detail}")
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="无法连接到 GPT-SoVITS 服务，请确认服务已启动")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GPT-SoVITS 请求失败: {e}")


async def tts_indextts(text: str, config: dict):
    """Index-TTS
    
    参数说明:
    - api_url: API 地址，默认 http://127.0.0.1:8080
    - voice: 语音名称（如果 API 支持）
    """
    import httpx
    
    api_url = config.get("api_url", "http://127.0.0.1:8080")
    
    payload = {"text": text}
    
    # 添加可选的语音参数
    voice = config.get("voice", "")
    if voice:
        payload["voice"] = voice
    
    try:
        response = await HTTP_CLIENT.post(
            f"{api_url}/tts",
            json=payload
        )
        response.raise_for_status()
        
        content_type = response.headers.get("content-type", "audio/wav")
        return StreamingResponse(
            iter([response.content]),
            media_type=content_type
        )
    except httpx.HTTPStatusError as e:
        error_detail = e.response.text if e.response else str(e)
        raise HTTPException(status_code=500, detail=f"Index-TTS 请求失败: {error_detail}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Index-TTS 请求失败: {e}")


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
    import uvicorn
    print("正在启动服务器: http://127.0.0.1:8123")
    print(f"书籍目录: {get_book_directories()}")
    
    # 尝试禁用 Windows 控制台 QuickEdit 模式，防止点击控制台导致服务器暂停
    if os.name == 'nt':
        try:
            import ctypes
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
