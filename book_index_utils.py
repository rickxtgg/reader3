import json
import os
import shutil
from pathlib import Path
from typing import List, Union


def norm_path(path: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))
    except Exception:
        return path


def clean_path(path: str) -> str:
    path = str(path or "").strip()
    if path.startswith("& '") and path.endswith("'"):
        path = path[3:-1]
    elif path.startswith('& "') and path.endswith('"'):
        path = path[3:-1]
    elif path.startswith("&'") and path.endswith("'"):
        path = path[2:-1]
    elif path.startswith('&"') and path.endswith('"'):
        path = path[2:-1]
    elif (path.startswith('"') and path.endswith('"')) or (path.startswith("'") and path.endswith("'")):
        path = path[1:-1]
    return path.strip()


def _resolve_index_path(index_file: Union[str, Path]) -> str:
    return str(index_file if isinstance(index_file, Path) else Path(index_file))


def load_books_index(index_file: Union[str, Path]) -> List[dict]:
    index_path = _resolve_index_path(index_file)
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"加载书籍索引失败: {index_path}, 错误: {e}")
            bak_path = index_path + ".bak"
            if os.path.exists(bak_path):
                try:
                    with open(bak_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data = data if isinstance(data, list) else []
                    print(f"已从备份恢复书籍索引: {bak_path}")
                    try:
                        with open(index_path, "w", encoding="utf-8") as wf:
                            json.dump(data, wf, ensure_ascii=False, indent=2)
                    except Exception as ew:
                        print(f"将备份索引写回 {index_path} 失败: {ew}")
                    return data
                except Exception as eb:
                    print(f"读取书籍索引备份失败: {bak_path}, 错误: {eb}")
    return []


def save_books_index(index_file: Union[str, Path], entries: List[dict]) -> None:
    index_path = _resolve_index_path(index_file)
    try:
        if os.path.exists(index_path):
            try:
                shutil.copy2(index_path, index_path + ".bak")
            except Exception as e:
                print(f"备份书籍索引失败: {index_path} -> {index_path}.bak, 错误: {e}")
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存书籍索引失败: {index_path}, 错误: {e}")


def add_book_to_index(
    index_file: Union[str, Path],
    book_id: str,
    directory: str,
    title: str = "",
    *,
    dedupe_by_directory: bool = False,
    prepend: bool = False,
) -> None:
    entries = load_books_index(index_file)
    if dedupe_by_directory:
        dirn = norm_path(directory)
        entries = [
            e for e in entries
            if not (e.get("id") == book_id and norm_path(e.get("directory", "")) == dirn)
        ]
        item = {"id": book_id, "directory": directory, "title": title}
        if prepend:
            entries.insert(0, item)
        else:
            entries.append(item)
        save_books_index(index_file, entries)
        return

    for entry in entries:
        if entry.get("id") == book_id:
            entry["directory"] = directory
            if title:
                entry["title"] = title
            save_books_index(index_file, entries)
            return

    entries.append({"id": book_id, "directory": directory, "title": title})
    save_books_index(index_file, entries)
