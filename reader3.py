"""
将 EPUB 文件解析为结构化对象，用于通过 Web 界面提供阅读服务。
"""

import os
import json
import shutil
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional
from datetime import datetime
from urllib.parse import unquote

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Comment

# --- 数据结构 ---

@dataclass
class ChapterContent:
    """
    表示 EPUB 中的物理文件（Spine 项）。
    单个文件可能包含多个逻辑章节（目录条目）。
    """
    id: str           # 内部 ID（如 'item_1'）
    href: str         # 文件名（如 'part01.html'）
    title: str        # 从文件推测的标题
    content: str      # 清理后的 HTML（图片路径已重写）
    text: str         # 纯文本，用于搜索/LLM
    order: int        # 线性阅读顺序


@dataclass
class TOCEntry:
    """表示导航侧边栏中的逻辑条目。"""
    title: str
    href: str         # 原始 href（如 'part01.html#chapter1'）
    file_href: str    # 仅文件名（如 'part01.html'）
    anchor: str       # 仅锚点（如 'chapter1'），无则为空
    children: List['TOCEntry'] = field(default_factory=list)


@dataclass
class BookMetadata:
    """书籍元数据"""
    title: str
    language: str
    authors: List[str] = field(default_factory=list)
    description: Optional[str] = None
    publisher: Optional[str] = None
    date: Optional[str] = None
    identifiers: List[str] = field(default_factory=list)
    subjects: List[str] = field(default_factory=list)


@dataclass
class Book:
    """主对象，用于序列化存储。"""
    metadata: BookMetadata
    spine: List[ChapterContent]  # 实际内容（线性文件列表）
    toc: List[TOCEntry]          # 导航树
    images: Dict[str, str]       # 映射：原始路径 -> 本地路径

    # 元信息
    source_file: str
    processed_at: str
    version: str = "3.0"

    def to_dict(self) -> dict:
        """转换为可 JSON 序列化的字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'Book':
        """从字典重建 Book 对象。"""
        return cls(
            metadata=BookMetadata(**data.get('metadata', {})),
            spine=[ChapterContent(**ch) for ch in data.get('spine', [])],
            toc=[cls._toc_from_dict(t) for t in data.get('toc', [])],
            images=data.get('images', {}),
            source_file=data.get('source_file', ''),
            processed_at=data.get('processed_at', ''),
            version=data.get('version', '3.0')
        )

    @staticmethod
    def _toc_from_dict(data: dict) -> TOCEntry:
        """递归重建 TOCEntry。"""
        return TOCEntry(
            title=data['title'],
            href=data['href'],
            file_href=data['file_href'],
            anchor=data['anchor'],
            children=[Book._toc_from_dict(c) for c in data.get('children', [])]
        )


# --- 工具函数 ---

def clean_html_content(soup: BeautifulSoup) -> BeautifulSoup:
    """清理 HTML 内容，移除危险和无用的标签。"""
    # 移除危险/无用标签
    for tag in soup(['script', 'style', 'iframe', 'video', 'nav', 'form', 'button']):
        tag.decompose()

    # 移除 HTML 注释
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # 移除 input 标签
    for tag in soup.find_all('input'):
        tag.decompose()

    return soup


def extract_plain_text(soup: BeautifulSoup) -> str:
    """提取纯文本，用于 LLM/搜索。"""
    text = soup.get_text(separator=' ')
    # 合并空白字符
    return ' '.join(text.split())


def parse_toc_recursive(toc_list, depth=0) -> List[TOCEntry]:
    """递归解析 ebooklib 的目录结构。"""
    result = []

    for item in toc_list:
        # ebooklib 目录项可能是 Link 对象或元组 (Section, [Children])
        if isinstance(item, tuple):
            section, children = item
            entry = TOCEntry(
                title=section.title,
                href=section.href,
                file_href=section.href.split('#')[0],
                anchor=section.href.split('#')[1] if '#' in section.href else "",
                children=parse_toc_recursive(children, depth + 1)
            )
            result.append(entry)
        elif isinstance(item, epub.Link):
            entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split('#')[0],
                anchor=item.href.split('#')[1] if '#' in item.href else ""
            )
            result.append(entry)
        # 注意：ebooklib 有时会返回不带子项的 Section 对象
        elif isinstance(item, epub.Section):
             entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split('#')[0],
                anchor=item.href.split('#')[1] if '#' in item.href else ""
            )
             result.append(entry)

    return result


def get_fallback_toc(book_obj) -> List[TOCEntry]:
    """如果目录缺失，从 Spine 构建简单目录。"""
    toc = []
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            # 尝试从文件名推测标题
            title = item.get_name().replace('.html', '').replace('.xhtml', '').replace('_', ' ').title()
            toc.append(TOCEntry(title=title, href=name, file_href=name, anchor=""))
    return toc


def extract_metadata_robust(book_obj) -> BookMetadata:
    """健壮地提取元数据，同时处理单值和列表值。"""
    def get_list(key):
        data = book_obj.get_metadata('DC', key)
        return [x[0] for x in data] if data else []

    def get_one(key):
        data = book_obj.get_metadata('DC', key)
        return data[0][0] if data else None

    return BookMetadata(
        title=get_one('title') or "未命名",
        language=get_one('language') or "en",
        authors=get_list('creator'),
        description=get_one('description'),
        publisher=get_one('publisher'),
        date=get_one('date'),
        identifiers=get_list('identifier'),
        subjects=get_list('subject')
    )


# --- 主转换逻辑 ---

def process_epub(epub_path: str, output_dir: str) -> Book:
    """处理 EPUB 文件，返回解析后的 Book 对象。"""
    # 1. 加载书籍
    print(f"正在加载 {epub_path}...")
    book = epub.read_epub(epub_path)

    # 2. 提取元数据
    metadata = extract_metadata_robust(book)

    # 3. 准备输出目录
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    images_dir = os.path.join(output_dir, 'images')
    os.makedirs(images_dir, exist_ok=True)

    # 4. 提取图片并构建映射
    print("正在提取图片...")
    image_map = {}  # 键：内部路径，值：本地相对路径

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            # 规范化文件名
            original_fname = os.path.basename(item.get_name())
            # 清理文件名以适应操作系统
            safe_fname = "".join([c for c in original_fname if c.isalpha() or c.isdigit() or c in '._-']).strip()

            # 保存到磁盘
            local_path = os.path.join(images_dir, safe_fname)
            with open(local_path, 'wb') as f:
                f.write(item.get_content())

            # 映射键：同时使用完整内部路径和文件名
            # 以应对 HTML src 属性格式不一致的情况
            rel_path = f"images/{safe_fname}"
            image_map[item.get_name()] = rel_path
            image_map[original_fname] = rel_path

    # 5. 处理目录
    print("正在解析目录...")
    toc_structure = parse_toc_recursive(book.toc)
    if not toc_structure:
        print("警告：目录为空，正在从 Spine 构建备用目录...")
        toc_structure = get_fallback_toc(book)

    # 6. 处理内容（基于 Spine 以保持 HTML 有效性）
    print("正在处理章节...")
    spine_chapters = []

    # 按 spine（线性阅读顺序）遍历
    for i, spine_item in enumerate(book.spine):
        item_id, linear = spine_item
        item = book.get_item_with_id(item_id)

        if not item:
            continue

        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            # 原始内容
            raw_content = item.get_content().decode('utf-8', errors='ignore')
            soup = BeautifulSoup(raw_content, 'html.parser')

            # A. 修复图片路径
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if not src: continue

                # 解码 URL（part01/image%201.jpg -> part01/image 1.jpg）
                src_decoded = unquote(src)
                filename = os.path.basename(src_decoded)

                # 尝试在映射中查找
                if src_decoded in image_map:
                    img['src'] = image_map[src_decoded]
                elif filename in image_map:
                    img['src'] = image_map[filename]

            # B. 清理 HTML
            soup = clean_html_content(soup)

            # C. 仅提取 body 内容
            body = soup.find('body')
            if body:
                # 提取 body 的内部 HTML
                final_html = "".join([str(x) for x in body.contents])
            else:
                final_html = str(soup)

            # D. 创建章节对象
            chapter = ChapterContent(
                id=item_id,
                href=item.get_name(),  # 重要：用于关联目录和内容
                title=f"第 {i+1} 节",  # 备用标题，真实标题来自目录
                content=final_html,
                text=extract_plain_text(soup),
                order=i
            )
            spine_chapters.append(chapter)

    # 7. 最终组装
    final_book = Book(
        metadata=metadata,
        spine=spine_chapters,
        toc=toc_structure,
        images=image_map,
        source_file=os.path.basename(epub_path),
        processed_at=datetime.now().isoformat()
    )

    return final_book


def save_to_json(book: Book, output_dir: str):
    """将书籍数据保存为 JSON（比 pickle 更安全）。"""
    json_path = os.path.join(output_dir, 'book.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(book.to_dict(), f, ensure_ascii=False)
    print(f"已保存结构化数据到 {json_path}")

    # 同时保存轻量级元数据索引，用于快速列出书库
    index_path = os.path.join(output_dir, 'index.json')
    index_data = {
        'title': book.metadata.title,
        'authors': book.metadata.authors,
        'chapters': len(book.spine),
        'processed_at': book.processed_at
    }
    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, ensure_ascii=False)
    print(f"已保存索引到 {index_path}")


# --- 命令行入口 ---

if __name__ == "__main__":

    import sys
    if len(sys.argv) < 2:
        print("用法: python reader3.py <文件.epub>")
        sys.exit(1)

    epub_file = sys.argv[1]
    assert os.path.exists(epub_file), "文件不存在。"
    out_dir = os.path.splitext(epub_file)[0] + "_data"

    book_obj = process_epub(epub_file, out_dir)
    save_to_json(book_obj, out_dir)
    print("\n--- 处理摘要 ---")
    print(f"书名: {book_obj.metadata.title}")
    print(f"作者: {', '.join(book_obj.metadata.authors)}")
    print(f"物理文件数 (Spine): {len(book_obj.spine)}")
    print(f"目录根条目数: {len(book_obj.toc)}")
    print(f"已提取图片数: {len(book_obj.images)}")
