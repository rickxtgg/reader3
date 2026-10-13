importScripts('./wav_utils.js');

const controllers = new Map();
let runId = 0;

function abortAll() {
  try {
    for (const [, ctl] of controllers) {
      try { ctl.abort(); } catch {}
    }
  } catch {}
  controllers.clear();
}

async function fetchBlobForText(text, ttsParams, controller) {
  const resp = await fetch('/api/tts/speak?text=' + encodeURIComponent(text) + '&' + ttsParams, { signal: controller.signal });
  if (!resp.ok) {
    throw new Error(await parseTTSError(resp));
  }
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
    runId++;
    abortAll();
    return;
  }
  if (data.type !== 'start') return;

  runId++;
  const myRun = runId;
  abortAll();

  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const ttsParams = data.ttsParams || '';

  // 从 ttsParams 中解析并发数 (concurrency=N)
  let concurrency = 1;
  try {
    const match = ttsParams.match(/concurrency=(\d+)/);
    if (match) {
        concurrency = parseInt(match[1], 10);
        if (isNaN(concurrency) || concurrency < 1) concurrency = 1;
        if (concurrency > 6) concurrency = 6;
    }
  } catch {}

  const MAX_CONCURRENCY = concurrency;
  let activeCount = 0;
  const queue = [...tasks];

  const processNext = async () => {
    if (myRun !== runId || queue.length === 0) return;

    const t = queue.shift();
    const id = t.id;
    const text = t.text;
    if (!text) {
      await processNext();
      return;
    }

    if (myRun !== runId) return;
    activeCount++;
    const ctl = new AbortController();
    controllers.set(id, ctl);

    try {
      const blob = await fetchBlobForText(text, ttsParams, ctl);
      if (myRun === runId) {
        self.postMessage({ type: 'preload_blob', id, blob });
      }
    } catch (err) {
      const aborted = err && (err.name === 'AbortError');
      if (!aborted && myRun === runId) {
        self.postMessage({ type: 'error', id, error: err?.message || 'ERROR' });
      }
    } finally {
      controllers.delete(id);
      activeCount--;
      if (myRun === runId) await processNext();
    }
  };

  // 启动初始批次
  const initialBatch = Math.min(MAX_CONCURRENCY, queue.length);
  for (let i = 0; i < initialBatch; i++) {
    processNext();
  }
};
