importScripts('./wav_utils.js');

const controllers = new Map();

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
      throw new Error(await parseTTSError(resp));
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
