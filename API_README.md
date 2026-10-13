# IndexTTS2 API 使用文档

主要接口速览：
- `POST /tts`：文本转语音，返回音频文件或JSON
- `POST /tts_stream`：文本转语音，完成后以分块流式返回音频文件（便于边下边播）
- `POST /tts_realtime`：真正实时推送，片段生成后立即以NDJSON行返回base64音频
- `POST /regenerate_segment`：重新生成指定片段
- `GET /set_gpt_weights`：在线切换 GPT 权重
- `GET /set_s2mel_weights`：在线切换 S2Mel 权重
- `GET /set_bigvgan_weights`：在线切换 BigVGAN 权重或预置模型
- `GET /control`：控制接口，restart/exit
- `GET /project_info`：获取项目信息与片段状态
- `GET /estimate_memory`：估算内存占用
- `GET /health`：健康检查

## 启动服务

```bash
python api.py -a 0.0.0.0 -p 9881 -c checkpoints/config.yaml -m checkpoints
```

### 启动参数说明

- `-a, --bind_addr`: 绑定地址，默认 `127.0.0.1`
- `-p, --port`: 监听端口，默认 `9881`（避免与GPT-SoVITS的9880冲突）
- `-c, --config`: 配置文件路径，默认 `checkpoints/config.yaml`
- `-m, --model_dir`: 模型目录，默认 `checkpoints`
- `--multi-gpu`: 启用多GPU模式
- `--gpu-ids`: 指定GPU ID列表，逗号分隔，如 `0,1,2`
- `--no-fp16`: 禁用FP16（降低精度，节省显存）
- `--no-cuda-kernel`: 禁用CUDA自定义核
- `--use-accel`: 使用GPT2加速引擎
- `--torch-compile`: 启用torch.compile优化

### 示例

```bash
# 单GPU启动
python api.py -a 0.0.0.0 -p 9881 -c checkpoints/config.yaml -m checkpoints

# 多GPU启动
python api.py -a 0.0.0.0 -p 9881 -c checkpoints/config.yaml -m checkpoints --multi-gpu --gpu-ids 0,1,2

# 启用加速优化
python api.py -a 0.0.0.0 -p 9881 -c checkpoints/config.yaml -m checkpoints --use-accel --torch-compile
```

---

## API 接口列表

### 1. 健康检查

**GET** `/health`

检查API服务是否正常运行。

**响应示例：**
```json
{
  "status": "ok"
}
```

---

### 2. 文本转语音（TTS）

**POST** `/tts`

将文本转换为语音，支持完整的IndexTTS2功能。

---

### 2b. 文本转语音（实时推送）

**POST** `/tts_realtime`

逐片段实时推送，返回 NDJSON（每行一个 JSON 对象）。

#### 响应事件格式

- 片段完成：
```json
{"event":"segment","index":0,"duration":2.5,"text":"片段文本","audio_b64":"...base64..."}
```
- 全部完成：
```json
{"event":"done","final_audio":".../final_audio.wav","subtitle_srt":".../subtitles.srt","subtitle_vtt":".../subtitles.vtt"}
```
- 出错：
```json
{"event":"error","message":"错误信息"}
```

#### 请求示例

```bash
curl -N -X POST "http://127.0.0.1:9881/tts_realtime" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "你好，这是实时推送测试。",
    "spk_audio_prompt": "examples/voice_01.wav"
  }'
```

说明：使用 `-N` 关闭 curl 的缓冲，可边收边处理。客户端收到 `audio_b64` 后自行解码播放或存文件。

---

### 2c. 文本转语音（生成完成后分块传输）

**POST** `/tts_stream`

生成完成后，以流式（chunked）方式返回最终音频文件，便于边下边播。推理过程仍需先完成。

#### 请求示例

```bash
curl -X POST "http://127.0.0.1:9881/tts_stream" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "这是分块传输测试。",
    "spk_audio_prompt": "examples/voice_01.wav",
    "output_format": "wav"
  }' \
  --output output_stream.wav
```

#### 请求参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `text` | string | ✅ | - | 要合成的文本 |
| `spk_audio_prompt` | string | ✅ | - | 音色参考音频路径 |
| `output_dir` | string | ❌ | 自动生成 | 输出目录（用于断点续推） |
| `emo_audio_prompt` | string | ❌ | null | 情感参考音频路径 |
| `emo_alpha` | float | ❌ | 1.0 | 情感强度（0.0-1.0） |
| `emo_vector` | array[float] | ❌ | null | 情感向量 [喜,怒,哀,惧,厌恶,低落,惊喜,平静] |
| `use_emo_text` | bool | ❌ | false | 是否使用情感描述文本（实验功能） |
| `emo_text` | string | ❌ | null | 情感描述文本 |
| `use_random` | bool | ❌ | false | 情感随机采样 |
| `max_text_tokens_per_segment` | int | ❌ | 120 | 每段最大token数 |
| `interval_silence_ms` | int | ❌ | 200 | 分段间静音毫秒数 |
| `output_format` | string | ❌ | "wav" | 输出格式：wav, mp3, flac, ogg |
| `audio_bitdepth` | string | ❌ | "int16" | 采样深度：int16, int24, int32, float32 |
| `resume_project` | bool | ❌ | true | 是否续推已有项目 |
| `split_bucket` | bool | ❌ | true | 是否启用数据分桶优化 |
| `bucket_threshold` | float | ❌ | 0.75 | 分桶阈值 |
| `do_sample` | bool | ❌ | true | 是否采样 |
| `top_p` | float | ❌ | 0.8 | Top-p采样参数 |
| `top_k` | int | ❌ | 30 | Top-k采样参数 |
| `temperature` | float | ❌ | 0.8 | 温度参数 |
| `length_penalty` | float | ❌ | 0.0 | 长度惩罚 |
| `num_beams` | int | ❌ | 3 | Beam搜索数量 |
| `repetition_penalty` | float | ❌ | 10.0 | 重复惩罚 |
| `max_mel_tokens` | int | ❌ | 1500 | 最大mel token数 |
| `return_json_only` | bool | ❌ | false | 仅返回JSON元数据，不返回音频文件 |
| `verbose` | bool | ❌ | false | 详细日志 |

#### 请求示例

**基础用法：**
```bash
curl -X POST "http://127.0.0.1:9881/tts" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "你好，这是IndexTTS2的测试。",
    "spk_audio_prompt": "examples/voice_01.wav"
  }'
```

**完整参数：**
```bash
curl -X POST "http://127.0.0.1:9881/tts" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "这是一段较长的文本，用于测试IndexTTS2的完整功能。",
    "spk_audio_prompt": "examples/voice_01.wav",
    "emo_audio_prompt": "examples/emo_sad.wav",
    "emo_alpha": 0.8,
    "output_format": "wav",
    "audio_bitdepth": "int16",
    "max_text_tokens_per_segment": 120,
    "interval_silence_ms": 200,
    "do_sample": true,
    "top_p": 0.8,
    "top_k": 30,
    "temperature": 0.8,
    "repetition_penalty": 10.0,
    "return_json_only": false
  }'
```

**使用情感向量：**
```bash
curl -X POST "http://127.0.0.1:9881/tts" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "我很高兴！",
    "spk_audio_prompt": "examples/voice_01.wav",
    "emo_vector": [0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.0],
    "emo_alpha": 0.9
  }'
```

**仅获取元数据（不下载音频）：**
```bash
curl -X POST "http://127.0.0.1:9881/tts" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "测试文本",
    "spk_audio_prompt": "examples/voice_01.wav",
    "return_json_only": true
  }'
```

#### 响应说明

**成功（return_json_only=false）：**
- 直接返回音频文件（Content-Type: audio/wav 等）
- HTTP状态码：200

**成功（return_json_only=true）：**
```json
{
  "output_dir": "outputs/api/job_20250101_120000_1234567890",
  "final_audio": "outputs/api/job_20250101_120000_1234567890/final_audio.wav",
  "subtitle_srt": "outputs/api/job_20250101_120000_1234567890/subtitles.srt",
  "subtitle_vtt": "outputs/api/job_20250101_120000_1234567890/subtitles.vtt",
  "index_file": "outputs/api/job_20250101_120000_1234567890/index.json",
  "total_segments": 5,
  "completed_segments": 5,
  "failed_segments": 0,
  "total_duration": 12.5,
  "inference_time": 3.2
}
```

**失败：**
```json
{
  "message": "错误信息"
}
```
HTTP状态码：400 或 500

---

### 3. 重新生成片段

**POST** `/regenerate_segment`

重新生成指定片段，支持修改情感、生成参数等。

#### 请求参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `output_dir` | string | ✅ | - | 项目输出目录 |
| `segment_index` | int | ✅ | - | 要重新生成的片段索引 |
| `spk_audio_prompt` | string | ❌ | 项目原设置 | 音色参考音频路径 |
| `emo_audio_prompt` | string | ❌ | 项目原设置 | 情感参考音频路径 |
| `emo_alpha` | float | ❌ | 1.0 | 情感强度 |
| `emo_vector` | array[float] | ❌ | null | 情感向量 |
| `output_format` | string | ❌ | 项目原设置 | 输出格式 |
| `audio_bitdepth` | string | ❌ | 项目原设置 | 采样深度 |
| `do_sample` | bool | ❌ | true | 是否采样 |
| `top_p` | float | ❌ | 0.8 | Top-p采样参数 |
| `top_k` | int | ❌ | 30 | Top-k采样参数 |
| `temperature` | float | ❌ | 0.8 | 温度参数 |
| `length_penalty` | float | ❌ | 0.0 | 长度惩罚 |
| `num_beams` | int | ❌ | 3 | Beam搜索数量 |
| `repetition_penalty` | float | ❌ | 10.0 | 重复惩罚 |
| `max_mel_tokens` | int | ❌ | 1500 | 最大mel token数 |
| `return_json_only` | bool | ❌ | false | 仅返回JSON元数据 |
| `verbose` | bool | ❌ | false | 详细日志 |

#### 请求示例

```bash
curl -X POST "http://127.0.0.1:9881/regenerate_segment" \
  -H "Content-Type: application/json" \
  -d '{
    "output_dir": "outputs/api/job_20250101_120000_1234567890",
    "segment_index": 0,
    "emo_vector": [0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.0],
    "temperature": 0.9
  }'
```

#### 响应说明

**成功（return_json_only=false）：**
- 返回重新生成的音频文件
- HTTP状态码：200

**成功（return_json_only=true）：**
```json
{
  "success": true,
  "segment_index": 0,
  "audio_file": "outputs/api/job_xxx/segments/segment_000000.wav",
  "duration": 2.5,
  "text": "片段文本内容"
}
```

**失败：**
```json
{
  "success": false,
  "segment_index": 0,
  "error": "错误信息"
}
```

---

### 4. 在线切换权重

**GET** `/set_gpt_weights`  
**GET** `/set_s2mel_weights`  
**GET** `/set_bigvgan_weights`

权重路径可使用绝对路径，或相对 `checkpoints/`。BigVGAN 支持传入预置模型名或本地权重文件。

#### 请求示例

```bash
curl "http://127.0.0.1:9881/set_gpt_weights?weights_path=checkpoints/gpt_new.ckpt"
curl "http://127.0.0.1:9881/set_s2mel_weights?weights_path=checkpoints/s2mel_new.ckpt"
curl "http://127.0.0.1:9881/set_bigvgan_weights?weights_path=bigvgan_24khz_100band"
```

#### 响应示例
```json
{"message": "gpt weights loaded", "weights_path": "checkpoints/gpt_new.ckpt"}
```

---

### 4. 获取项目信息

**GET** `/project_info`

查询项目的详细信息，包括所有片段状态。

#### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `output_dir` | string | ✅ | 项目输出目录 |

#### 请求示例

```bash
curl "http://127.0.0.1:9881/project_info?output_dir=outputs/api/job_20250101_120000_1234567890"
```

#### 响应说明

```json
{
  "output_dir": "outputs/api/job_20250101_120000_1234567890",
  "created_at": "2025-01-01T12:00:00",
  "updated_at": "2025-01-01T12:05:00",
  "total_segments": 5,
  "completed_segments": 5,
  "failed_segments": 0,
  "pending_segments": 0,
  "total_duration": 12.5,
  "final_audio": "outputs/api/job_xxx/final_audio.wav",
  "subtitle_srt": "outputs/api/job_xxx/subtitles.srt",
  "segments": [
    {
      "index": 0,
      "text": "第一段文本",
      "duration": 2.5,
      "status": "completed",
      "audio_file": "segments/segment_000000.wav",
      "start_time": 0.0,
      "end_time": 2.5
    },
    {
      "index": 1,
      "text": "第二段文本",
      "duration": 3.0,
      "status": "completed",
      "audio_file": "segments/segment_000001.wav",
      "start_time": 2.7,
      "end_time": 5.7
    }
  ]
}
```

---

### 5. 估算内存占用

**GET** `/estimate_memory`

根据文本长度估算内存占用和推荐处理方式。

#### 请求参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `text_length` | int | ✅ | - | 文本长度（字符数） |
| `max_tokens_per_segment` | int | ❌ | 120 | 每段最大token数 |

#### 请求示例

```bash
curl "http://127.0.0.1:9881/estimate_memory?text_length=10000&max_tokens_per_segment=120"
```

#### 响应说明

```json
{
  "text_length": 10000,
  "estimated_segments": 167,
  "estimated_duration_minutes": 11.1,
  "non_streaming_cpu_mb": 14.5,
  "streaming_cpu_mb": 0.09,
  "model_gpu_mb": 8000,
  "per_segment_gpu_mb": 2000,
  "recommendation": "streaming"
}
```

---

## 功能特性

### 1. 完整的情感控制

- **情感参考音频**：使用 `emo_audio_prompt` 指定情感参考音频
- **情感向量控制**：使用 `emo_vector` 精确控制8种情绪（喜、怒、哀、惧、厌恶、低落、惊喜、平静）
- **情感描述文本**：使用 `use_emo_text` 和 `emo_text` 从文本自动检测情感（实验功能）
- **情感强度调节**：通过 `emo_alpha` 控制情感强度（0.0-1.0）

### 2. 大文本处理

- **自动分段**：根据 `max_text_tokens_per_segment` 自动分段处理
- **断点续推**：支持中断后继续生成（`resume_project=true`）
- **数据分桶优化**：通过 `split_bucket` 和 `bucket_threshold` 优化内存使用
- **流式处理**：边生成边保存，降低内存占用

### 3. 多格式输出

- **音频格式**：支持 wav、mp3、flac、ogg
- **采样深度**：支持 int16、int24、int32、float32
- **字幕生成**：自动生成 SRT 和 VTT 字幕文件

### 4. 项目管理

- **索引文件**：每个项目都有 `index.json` 记录所有信息
- **片段管理**：支持查看、重新生成单个片段
- **历史版本**：每个片段最多保留5个历史版本
- **状态跟踪**：pending、completed、failed、regenerating

### 5. 生成参数控制

- **采样策略**：`do_sample`、`top_p`、`top_k`、`temperature`
- **Beam搜索**：`num_beams`、`length_penalty`
- **重复控制**：`repetition_penalty`
- **Token限制**：`max_mel_tokens`、`max_text_tokens_per_segment`

---

## Python 客户端示例

```python
import requests
import json

API_BASE = "http://127.0.0.1:9881"

# 1. 健康检查
response = requests.get(f"{API_BASE}/health")
print(response.json())

# 2. 文本转语音
tts_data = {
    "text": "你好，这是IndexTTS2的测试。",
    "spk_audio_prompt": "examples/voice_01.wav",
    "output_format": "wav",
    "return_json_only": False
}
response = requests.post(f"{API_BASE}/tts", json=tts_data)
if response.status_code == 200:
    # 保存音频文件
    with open("output.wav", "wb") as f:
        f.write(response.content)
    print("音频已保存到 output.wav")
else:
    print(f"错误: {response.json()}")

# 3. 仅获取元数据
tts_data["return_json_only"] = True
response = requests.post(f"{API_BASE}/tts", json=tts_data)
result = response.json()
print(f"输出目录: {result['output_dir']}")
print(f"总片段数: {result['total_segments']}")

# 4. 重新生成片段
regenerate_data = {
    "output_dir": result["output_dir"],
    "segment_index": 0,
    "emo_vector": [0.8, 0.0, 0.0, 0.0, 0.0, 0.0, 0.2, 0.0],
    "return_json_only": True
}
response = requests.post(f"{API_BASE}/regenerate_segment", json=regenerate_data)
print(response.json())

# 5. 获取项目信息
response = requests.get(f"{API_BASE}/project_info", params={"output_dir": result["output_dir"]})
project_info = response.json()
print(f"已完成片段: {project_info['completed_segments']}/{project_info['total_segments']}")

# 6. 估算内存
response = requests.get(f"{API_BASE}/estimate_memory", params={"text_length": 10000})
memory_info = response.json()
print(f"推荐方式: {memory_info['recommendation']}")
```

---

## 注意事项

1. **端口冲突**：默认端口已改为 `9881`，避免与GPT-SoVITS的 `9880` 冲突
2. **文件路径**：音频路径可以是相对路径或绝对路径，相对路径相对于API启动目录
3. **大文本处理**：对于超长文本（>10000字符），建议使用流式处理（默认启用）
4. **断点续推**：如果项目已存在且文本相同，会自动续推未完成的片段
5. **内存管理**：多GPU模式下，模型会自动分配到不同GPU以优化内存使用
6. **输出目录**：如果不指定 `output_dir`，会自动生成带时间戳的目录

---

## 错误处理

所有接口在出错时都会返回JSON格式的错误信息：

```json
{
  "message": "错误描述"
}
```

常见错误：
- `400`：参数错误或验证失败
- `404`：项目或文件不存在
- `500`：服务器内部错误（模型加载失败、推理错误等）

---

## 性能优化建议

1. **多GPU模式**：使用 `--multi-gpu` 和 `--gpu-ids` 启用多GPU加速
2. **FP16精度**：默认启用FP16，可降低显存占用（如遇问题可用 `--no-fp16` 禁用）
3. **加速引擎**：使用 `--use-accel` 启用GPT2加速引擎
4. **数据分桶**：启用 `split_bucket` 优化内存使用
5. **torch.compile**：使用 `--torch-compile` 启用编译优化（需要PyTorch 2.0+）

