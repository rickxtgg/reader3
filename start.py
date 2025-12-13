#!/usr/bin/env python3
"""
Reader3 一键启动脚本
- 处理 EPUB 文件
- 维护书籍索引
- 启动阅读服务器
"""

import os
import sys
import json
import shutil
import subprocess
from pathlib import Path

# 书籍索引文件
BOOKS_INDEX_FILE = "books_index.json"
SCRIPT_DIR = Path(__file__).parent.resolve()


def load_books_index() -> list:
    """加载书籍索引"""
    index_path = SCRIPT_DIR / BOOKS_INDEX_FILE
    if index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_books_index(books: list):
    """保存书籍索引"""
    index_path = SCRIPT_DIR / BOOKS_INDEX_FILE
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(books, f, ensure_ascii=False, indent=2)


def add_book_to_index(book_id: str, directory: str, title: str = ""):
    """添加书籍到索引"""
    books = load_books_index()
    
    # 检查是否已存在
    for book in books:
        if book.get("id") == book_id:
            # 更新现有记录
            book["directory"] = directory
            if title:
                book["title"] = title
            save_books_index(books)
            return
    
    # 添加新记录
    books.append({
        "id": book_id,
        "directory": directory,
        "title": title
    })
    save_books_index(books)


def clean_path(path: str) -> str:
    """清理路径（处理拖拽时可能带的各种格式）"""
    path = path.strip()
    
    # PowerShell 拖拽格式: & 'path' 或 & "path"
    if path.startswith("& '") and path.endswith("'"):
        path = path[3:-1]
    elif path.startswith('& "') and path.endswith('"'):
        path = path[3:-1]
    elif path.startswith("&'") and path.endswith("'"):
        path = path[2:-1]
    elif path.startswith('&"') and path.endswith('"'):
        path = path[2:-1]
    
    # 普通引号格式
    elif (path.startswith('"') and path.endswith('"')) or \
         (path.startswith("'") and path.endswith("'")):
        path = path[1:-1]
    
    return path.strip()


def process_epub(epub_path: str) -> bool:
    """处理 EPUB 文件"""
    epub_path = clean_path(epub_path)
    
    if not os.path.exists(epub_path):
        print(f"❌ 文件不存在: {epub_path}")
        return False
    
    if not epub_path.lower().endswith('.epub'):
        print(f"❌ 不是 EPUB 文件: {epub_path}")
        return False
    
    print(f"\n📖 正在处理: {epub_path}")
    
    # 获取输出目录名
    epub_name = os.path.splitext(os.path.basename(epub_path))[0]
    output_dir = SCRIPT_DIR / f"{epub_name}_data"
    
    # 运行 reader3.py
    try:
        # 使用系统默认编码（Windows 中文系统通常是 GBK）
        import locale
        system_encoding = locale.getpreferredencoding(False)
        
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "reader3.py"), epub_path],
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            encoding=system_encoding,
            errors='replace'  # 遇到无法解码的字符用替换符
        )
        
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        
        if result.returncode != 0:
            print(f"❌ 处理失败，返回码: {result.returncode}")
            return False
            
    except Exception as e:
        print(f"❌ 运行 reader3.py 时出错: {e}")
        return False
    
    # 检查输出目录是否在其他位置（epub 文件不在当前目录时）
    epub_dir = os.path.dirname(os.path.abspath(epub_path))
    original_output = os.path.join(epub_dir, f"{epub_name}_data")
    
    if original_output != str(output_dir) and os.path.exists(original_output):
        # 移动到当前工作目录
        if output_dir.exists():
            shutil.rmtree(output_dir)
        shutil.move(original_output, output_dir)
        print(f"📁 已移动书籍数据到: {output_dir}")
    
    # 读取书籍标题
    title = epub_name
    index_file = output_dir / "index.json"
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                title = data.get("title", epub_name)
        except Exception:
            pass
    
    # 更新书籍索引
    book_id = f"{epub_name}_data"
    add_book_to_index(book_id, str(SCRIPT_DIR), title)
    print(f"✅ 已添加到书籍索引: {title}")
    
    return True


def convert_to_epub(src_path: str) -> str:
    """
    将 azw3/mobi/pdf/txt 等格式转换为 epub，依赖 Calibre 的 ebook-convert。
    返回生成的 epub 路径，失败则返回空字符串。
    """
    src_path = clean_path(src_path)
    src = Path(src_path)
    if not src.exists():
        print(f"❌ 文件不存在: {src_path}")
        return ""

    target = src.with_suffix(".converted.epub")
    cmd = ["ebook-convert", str(src), str(target)]
    print(f"🔄 正在转换为 EPUB: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        if result.returncode != 0:
            print(f"❌ 转换失败，返回码: {result.returncode}")
            return ""
        if not target.exists():
            print("❌ 转换后未找到 EPUB 文件")
            return ""
        print(f"✅ 转换完成: {target}")
        return str(target)
    except FileNotFoundError:
        print("❌ 未找到 ebook-convert，请安装 Calibre 或将其加入环境变量。")
    except Exception as e:
        print(f"❌ 转换时出错: {e}")
    return ""


def process_book_file(file_path: str) -> bool:
    """
    统一入口：支持 epub、azw3、mobi、pdf、txt。
    非 epub 将尝试通过 ebook-convert 转 epub，再走现有流程。
    """
    file_path = clean_path(file_path)
    if not os.path.exists(file_path):
        print(f"❌ 文件不存在: {file_path}")
        return False

    lower = file_path.lower()
    if lower.endswith(".epub"):
        return process_epub(file_path)

    convertible_exts = (".azw3", ".mobi", ".pdf", ".txt")
    if lower.endswith(convertible_exts):
        converted = convert_to_epub(file_path)
        if not converted:
            return False
        return process_epub(converted)

    print(f"❌ 不支持的格式: {file_path}")
    print("目前支持：epub/azw3/mobi/pdf/txt（非 epub 需依赖 Calibre ebook-convert）")
    return False


def list_books():
    """列出所有已处理的书籍"""
    books = load_books_index()
    
    if not books:
        print("\n📚 书库为空，请先处理一些 EPUB 文件")
        return
    
    print("\n📚 已处理的书籍:")
    print("-" * 50)
    for i, book in enumerate(books, 1):
        title = book.get("title", book.get("id", "未知"))
        directory = book.get("directory", ".")
        print(f"  {i}. {title}")
        print(f"     📁 {directory}/{book.get('id', '')}")
    print("-" * 50)


def start_server():
    """启动服务器"""
    print("\n🚀 正在启动阅读服务器...")
    print("=" * 50)
    
    # 使用 subprocess 启动服务器
    os.chdir(SCRIPT_DIR)
    subprocess.run([sys.executable, str(SCRIPT_DIR / "server.py")])


def main():
    print("=" * 50)
    print("  📚 Reader3 - EPUB 阅读器")
    print("=" * 50)
    
    # 检查命令行参数
    if len(sys.argv) > 1:
        # 处理命令行传入的文件
        for arg in sys.argv[1:]:
            if arg == "--server":
                start_server()
                return
            elif arg == "--list":
                list_books()
                return
            else:
                process_book_file(arg)
        
        # 处理完成后询问是否启动服务器
        print("\n")
        start = input("是否启动服务器？[Y/n]: ").strip().lower()
        if start != 'n':
            start_server()
        return
    
    # 交互模式
    while True:
        print("\n请选择操作:")
        print("  1. 处理新的书籍文件（epub/azw3/mobi/pdf/txt）")
        print("  2. 查看已处理的书籍")
        print("  3. 启动阅读服务器")
        print("  4. 退出")
        print()
        
        choice = input("请输入选项 (1-4): ").strip()
        
        if choice == "1":
            print("\n请输入书籍文件路径（可直接拖拽，支持 epub/azw3/mobi/pdf/txt）:")
            book_path = input("> ").strip()
            if book_path:
                process_book_file(book_path)
        
        elif choice == "2":
            list_books()
        
        elif choice == "3":
            start_server()
            break
        
        elif choice == "4":
            print("\n👋 再见!")
            break
        
        else:
            print("❌ 无效选项，请重新选择")


if __name__ == "__main__":
    main()

