const controllers = new Map();

function hasWavHeader(arrayBuffer) {
  if (arrayBuffer.byteLength < 12) return false;
  const view = new DataView(arrayBuffer);
  const riff = view.getUint32(0, false);
  const wave = view.getUint32(8, false);
  return riff === 0x52494646 && wave === 0x57415645;
}

function getSampleRateFromWav(arrayBuffer) {
  if (arrayBuffer.byteLength < 28) return 32000;
  const view = new DataView(arrayBuffer);
  return view.getUint32(24, true);
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

self.onmessage = async (e) => {
  const data = e.data || {};
  const type = data.type;

  if (type === 'stop') {
    try {
      for (const [, ctl] of controllers) {
        try { ctl.abort(); } catch {}
      }
    } catch {}
    controllers.clear();
    return;
  }

  if (type !== 'fetch') return;

  const { index, text, ttsParams, preload } = data;

  try {
    const controller = new AbortController();
    controllers.set(index, controller);
    const resp = await fetch('/api/tts/speak?text=' + encodeURIComponent(text) + '&' + ttsParams, {
      signal: controller.signal
    });

    if (!resp.ok) {
      throw new Error('HTTP ' + resp.status);
    }

    const contentType = resp.headers.get('content-type') || '';
    const isRaw = contentType.includes('raw') || contentType.includes('pcm') || contentType.includes('octet-stream');

    if (isRaw && resp.body) {
      const reader = resp.body.getReader();
      let isFirst = true;
      let sampleRate = 32000;
      let chunks = [];
      let total = 0;
      let hasHeader = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        total += value.length;

        let byteOffset = value.byteOffset || 0;
        let byteLength = value.byteLength || value.length || 0;

        if (isFirst) {
          const tempBuffer = value.buffer.slice(byteOffset, byteOffset + byteLength);
          if (hasWavHeader(tempBuffer)) {
            hasHeader = true;
            sampleRate = getSampleRateFromWav(tempBuffer);
            if (byteLength > 44) {
              byteOffset += 44;
              byteLength -= 44;
            } else {
              byteLength = 0;
            }
          }
          isFirst = false;
        }

        if (byteLength > 0) {
          const int16 = new Int16Array(value.buffer, byteOffset, byteLength / 2);
          const float32 = new Float32Array(int16.length);
          for (let i = 0; i < int16.length; i++) {
            float32[i] = int16[i] / 32768.0;
          }
          self.postMessage({
            type: 'f32Chunk',
            index,
            sampleRate,
            buffer: float32.buffer,
            preload: !!preload
          }, [float32.buffer]);
        }
      }

      const merged = new Uint8Array(total);
      let offset = 0;
      for (const c of chunks) {
        merged.set(c, offset);
        offset += c.length;
      }

      let blob;
      if (hasHeader) {
        blob = new Blob([merged], { type: 'audio/wav' });
      } else {
        blob = pcmToWav(merged.buffer, sampleRate, 1, 16);
      }

      self.postMessage({ type: 'blob', index, blob, streamed: true, preload: !!preload });
      return;
    }

    const blob = await resp.blob();
    self.postMessage({ type: 'blob', index, blob, streamed: false, preload: !!preload });

  } catch (err) {
    const aborted = err && (err.name === 'AbortError');
    self.postMessage({ type: 'error', index, error: aborted ? 'ABORTED' : (err?.message || 'ERROR') });
  } finally {
    controllers.delete(index);
  }
};
