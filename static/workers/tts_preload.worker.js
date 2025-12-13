const controllers = new Map();

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
  for (let i = 0; i < string.length; i++) view.setUint8(offset + i, string.charCodeAt(i));
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

async function fetchBlobForText(text, ttsParams, controller) {
  const resp = await fetch('/api/tts/speak?text=' + encodeURIComponent(text) + '&' + ttsParams, { signal: controller.signal });
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  const ct = resp.headers.get('content-type') || '';
  const isRaw = ct.includes('raw') || ct.includes('pcm') || ct.includes('octet-stream');
  if (!isRaw) return await resp.blob();
  const reader = resp.body.getReader();
  let isFirst = true;
  let sampleRate = 32000;
  const chunks = [];
  let total = 0;
  let hasHeader = false;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.length;
    if (isFirst) {
      const buf = value.buffer.slice(value.byteOffset || 0, (value.byteOffset || 0) + (value.byteLength || value.length));
      if (hasWavHeader(buf)) {
        hasHeader = true;
        sampleRate = getSampleRateFromWav(buf);
      }
      isFirst = false;
    }
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) { merged.set(c, offset); offset += c.length; }
  return hasHeader ? new Blob([merged], { type: 'audio/wav' }) : pcmToWav(merged.buffer, sampleRate, 1, 16);
}

self.onmessage = async (e) => {
  const data = e.data || {};
  if (data.type === 'stop') {
    try { for (const [, ctl] of controllers) { try { ctl.abort(); } catch {} } } catch {}
    controllers.clear();
    return;
  }
  if (data.type !== 'start') return;
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const ttsParams = data.ttsParams || '';
  for (const t of tasks) {
    const id = t.id;
    const text = t.text;
    if (!text) continue;
    const ctl = new AbortController();
    controllers.set(id, ctl);
    try {
      const blob = await fetchBlobForText(text, ttsParams, ctl);
      self.postMessage({ type: 'preload_blob', id, blob });
    } catch (err) {
      const aborted = err && (err.name === 'AbortError');
      self.postMessage({ type: 'error', id, error: aborted ? 'ABORTED' : (err?.message || 'ERROR') });
    } finally {
      controllers.delete(id);
    }
  }
};

