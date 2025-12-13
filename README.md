# Reader3

![reader3](reader3.png)

一个轻量级的自托管 EPUB 阅读器，支持逐章阅读。方便将章节内容复制到 LLM 进行共读。

## 功能特点

- 📚 **多目录支持** - 书籍可存放在任意位置
- 🌿 **护眼模式** - 三种主题：亮色/护眼/深色
- 🔊 **朗读功能** - 支持微软TTS、GPT-SoVITS、Index-TTS
- ⌨️ **快捷键** - 方向键翻页，+/- 调整字号
- 💾 **阅读进度** - 自动保存滚动位置

## 快速开始

### 方式一：一键启动（推荐）

**Windows 用户**：直接将 EPUB 文件拖拽到 `start.bat` 上即可。

**命令行**：
```bash
# 交互模式
uv run start.py

# 直接处理文件并启动服务器
uv run start.py "C:\Books\your_book.epub"

# 只启动服务器
uv run start.py --server
```

### 方式二：分步操作

```bash
# 1. 处理 EPUB 文件
uv run reader3.py your_book.epub

# 2. 启动服务器
uv run server.py
```

访问 [http://localhost:8123](http://localhost:8123) 开始阅读。

## 阅读器功能

### 主题切换
- ☀️ **亮色模式** - 默认白底黑字
- 🌿 **护眼模式** - 暖色调米黄背景
- 🌙 **深色模式** - 暗色背景

### 朗读功能
支持三种 TTS 引擎：
- **微软 TTS** - 免费在线，音质好（需联网）
- **GPT-SoVITS** - 本地部署，支持音色克隆
- **Index-TTS** - 本地部署

### 快捷键
| 按键 | 功能 |
|------|------|
| `←` `→` | 上一章/下一章 |
| `+` `-` | 增大/减小字号 |
| `空格` | 播放/暂停朗读 |

## 配置文件

`config.json` 配置示例：
```json
{
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
            "ref_audio_path": "path/to/reference.wav",
            "prompt_text": "参考音频的文本内容",
            "prompt_lang": "zh",
            "speed_factor": 1.0
        },
        "indextts": {
            "api_url": "http://127.0.0.1:8080"
        }
    }
}
```

### TTS 配置详解

#### Microsoft Edge TTS
| 参数 | 说明 | 示例 |
|------|------|------|
| `voice` | 语音名称 | `zh-CN-XiaoxiaoNeural` |
| `rate` | 语速 | `+50%` 或 `-30%` |
| `volume` | 音量 | `+50%` 或 `-30%` |
| `pitch` | 音调 | `+50Hz` 或 `-30Hz` |

#### GPT-SoVITS
| 参数 | 说明 | 默认值 |
|------|------|--------|
| `api_url` | API 地址 | `http://127.0.0.1:9880` |
| `text_lang` | 文本语言 | `zh` |
| `ref_audio_path` | 参考音频路径 | 空 |
| `prompt_text` | 参考音频文本 | 空 |
| `prompt_lang` | 参考音频语言 | `zh` |
| `top_k` | Top-K 采样 | `5` |
| `top_p` | Top-P 采样 | `1` |
| `temperature` | 温度 | `1` |
| `text_split_method` | 分割方法 | `cut5` |
| `speed_factor` | 语速 | `1.0` |
| `streaming_mode` | 流式输出 | `true` |

#### Index-TTS
| 参数 | 说明 | 默认值 |
|------|------|--------|
| `api_url` | API 地址 | `http://127.0.0.1:8080` |
| `voice` | 语音名称 | 空 |

## 书籍管理

- **添加书籍**：在书库页面添加包含 `*_data` 文件夹的目录
- **删除书籍**：直接删除对应的 `*_data` 文件夹
- **书籍索引**：`books_index.json` 记录所有处理过的书籍位置

## 依赖

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/) 包管理器

## License

MIT
