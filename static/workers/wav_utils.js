/**
 * WAV 音频处理工具函数（供 tts_fetch.worker.js 和 tts_preload.worker.js 共享）
 */

function hasWavHeader(arrayBuffer) {
  if (arrayBuffer.byteLength < 12) return false;
  const view = new DataView(arrayBuffer);
  return view.getUint32(0, false) === 0x52494646 && view.getUint32(8, false) === 0x57415645;
}

function getSampleRateFromWav(arrayBuffer) {
  if (arrayBuffer.byteLength < 28) return 32000;
  return new DataView(arrayBuffer).getUint32(24, true);
}

function writeString(view, offset, string) {
  for (let i = 0; i < string.length; i++) {
    view.setUint8(offset + i, string.charCodeAt(i));
  }
}

function pcmToWav(pcmArrayBuffer, sampleRate, numChannels, bitsPerSample) {
  const bytesPerSample = bitsPerSample / 8;
  const blockAlign = numChannels * bytesPerSample;
  const byteRate = sampleRate * blockAlign;
  const dataSize = pcmArrayBuffer.byteLength;
  const headerSize = 44;
  const totalSize = headerSize + dataSize;

  const buffer = new ArrayBuffer(totalSize);
  const view = new DataView(buffer);

  writeString(view, 0, 'RIFF');
  view.setUint32(4, totalSize - 8, true);
  writeString(view, 8, 'WAVE');

  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, numChannels, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true);
  view.setUint16(32, blockAlign, true);
  view.setUint16(34, bitsPerSample, true);

  writeString(view, 36, 'data');
  view.setUint32(40, dataSize, true);

  new Uint8Array(buffer, 44).set(new Uint8Array(pcmArrayBuffer));

  return new Blob([buffer], { type: 'audio/wav' });
}

/**
 * 解析 TTS 错误响应，返回格式化的错误文本
 */
async function parseTTSError(resp) {
  let errText = 'HTTP ' + resp.status;
  try {
    const ct = resp.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      const data = await resp.json();
      const detail = (data && (data.detail || data)) || {};
      const code = detail.error_code || detail.code || null;
      const msg = detail.message || detail.detail || '';
      if (code || msg) {
        errText = (code ? code + ':' : '') + (typeof msg === 'string' ? msg : JSON.stringify(msg));
      }
    } else {
      const textBody = await resp.text();
      if (textBody) errText = textBody;
    }
  } catch (e) {}
  return errText;
}
