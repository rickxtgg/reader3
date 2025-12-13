(() => {
  let fetchWorker = null;
  let preloadWorker = null;
  const pending = new Map(); // index -> {resolve, reject}
  const preloadCache = new Map(); // id -> Blob
  const preloadTextMap = new Map();
  const preloadCacheByTextKey = new Map();
  let idbPromise = null;

  function getEngineName() {
    try { return document.getElementById('tts-engine')?.value || 'edge'; } catch { return 'edge'; }
  }

  async function sha256Hex(str) {
    try {
      const buf = new TextEncoder().encode(str || '');
      const hash = await crypto.subtle.digest('SHA-256', buf);
      const view = new Uint8Array(hash);
      let hex = '';
      for (let i = 0; i < view.length; i++) hex += ('0' + view[i].toString(16)).slice(-2);
      return hex;
    } catch {
      return djb2Hex(str || '');
    }
  }

  function djb2Hex(str) {
    let h = 5381;
    for (let i = 0; i < str.length; i++) h = ((h << 5) + h) + str.charCodeAt(i);
    let hex = (h >>> 0).toString(16);
    while (hex.length < 8) hex = '0' + hex;
    return hex;
  }

  async function computePreloadKey(text) {
    const book = window.BOOK_ID || '';
    const engine = getEngineName();
    const hash = await sha256Hex(text || '');
    return 'pre:' + book + ':' + engine + ':' + hash;
  }

  function openIDB() {
    if (idbPromise) return idbPromise;
    idbPromise = new Promise((resolve, reject) => {
      try {
        const req = indexedDB.open('ttsPreload', 1);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains('blobs')) db.createObjectStore('blobs', { keyPath: 'key' });
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
      } catch (e) { reject(e); }
    });
    return idbPromise;
  }

  async function idbSet(key, blob) {
    try {
      const db = await openIDB();
      return await new Promise((resolve, reject) => {
        const tx = db.transaction('blobs', 'readwrite');
        try {
          const parts = String(key).split(':');
          const book = parts[1] || '';
          const engine = parts[2] || '';
          tx.objectStore('blobs').put({ key, blob, updatedAt: Date.now(), book, engine });
        } catch {
          tx.objectStore('blobs').put({ key, blob, updatedAt: Date.now() });
        }
        tx.oncomplete = () => resolve(true);
        tx.onerror = () => reject(tx.error);
      });
    } catch { return false; }
  }

  async function idbGet(key) {
    try {
      const db = await openIDB();
      const rec = await new Promise((resolve) => {
        const tx = db.transaction('blobs', 'readonly');
        const req = tx.objectStore('blobs').get(key);
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => resolve(null);
      });
      if (!rec) return null;
      try {
        const tx = db.transaction('blobs', 'readwrite');
        tx.objectStore('blobs').put({ key, blob: rec.blob, updatedAt: Date.now(), book: rec.book, engine: rec.engine });
      } catch {}
      return rec.blob || null;
    } catch { return null; }
  }

  async function idbGetAll(filter = null) {
    try {
      const db = await openIDB();
      const rows = await new Promise((resolve) => {
        const tx = db.transaction('blobs', 'readonly');
        const req = tx.objectStore('blobs').getAll();
        req.onsuccess = () => resolve(Array.isArray(req.result) ? req.result : []);
        req.onerror = () => resolve([]);
      });
      if (filter && (filter.book || filter.engine)) {
        return rows.filter(r => (
          (!filter.book || r.book === filter.book) &&
          (!filter.engine || r.engine === filter.engine)
        ));
      }
      return rows;
    } catch { return []; }
  }

  async function idbDelete(key) {
    try {
      const db = await openIDB();
      return await new Promise((resolve) => {
        const tx = db.transaction('blobs', 'readwrite');
        const req = tx.objectStore('blobs').delete(key);
        req.onsuccess = () => resolve(true);
        req.onerror = () => resolve(false);
      });
    } catch { return false; }
  }

  async function idbClearAll(filter = null) {
    try {
      const db = await openIDB();
      if (!filter || (!filter.book && !filter.engine)) {
        return await new Promise((resolve) => {
          const tx = db.transaction('blobs', 'readwrite');
          const req = tx.objectStore('blobs').clear();
          req.onsuccess = () => resolve(true);
          req.onerror = () => resolve(false);
        });
      }
      const rows = await idbGetAll(filter);
      let ok = true;
      for (const r of rows) {
        const del = await new Promise((resolve) => {
          const tx = db.transaction('blobs', 'readwrite');
          const req = tx.objectStore('blobs').delete(r.key);
          req.onsuccess = () => resolve(true);
          req.onerror = () => resolve(false);
        });
        ok = ok && del;
      }
      return ok;
    } catch { return false; }
  }

  function ensureFetchWorker() {
    if (fetchWorker) return fetchWorker;
    fetchWorker = new Worker('/static/workers/tts_fetch.worker.js');
    fetchWorker.onmessage = (e) => {
      const msg = e.data || {};
      if (msg.type === 'f32Chunk') {
        if (msg.preload) return;
        try {
          const buffer = msg.buffer;
          const arr = new Float32Array(buffer);
          if (typeof window.enqueueFloatChunk === 'function') {
            window.enqueueFloatChunk(arr, msg.sampleRate, msg.index);
          }
        } catch {}
        return;
      }
      if (msg.type === 'blob') {
        const entry = pending.get(msg.index);
        if (entry) {
          try {
            msg.blob._streamPlayed = !!msg.streamed;
          } catch {}
          pending.delete(msg.index);
          entry.resolve(msg.blob);
        }
        return;
      }
      if (msg.type === 'error') {
        const entry = pending.get(msg.index);
        if (entry) {
          pending.delete(msg.index);
          entry.reject(new Error(msg.error || 'ERROR'));
        }
        return;
      }
    };
    fetchWorker.onerror = (e) => {
      // 全局错误：拒绝所有挂起请求
      for (const [idx, entry] of pending) {
        try { entry.reject(new Error('WORKER_ERROR')); } catch {}
      }
      pending.clear();
    };
    return fetchWorker;
  }

  function ensurePreloadWorker() {
    if (preloadWorker) return preloadWorker;
    preloadWorker = new Worker('/static/workers/tts_preload.worker.js');
    preloadWorker.onmessage = async (e) => {
      const msg = e.data || {};
      if (msg.type === 'preload_blob') {
        try { preloadCache.set(msg.id, msg.blob); } catch {}
        try {
          const t = preloadTextMap.get(msg.id);
          if (t) {
            const key = await computePreloadKey(t);
            try { preloadCacheByTextKey.set(key, msg.blob); } catch {}
            try { await idbSet(key, msg.blob); } catch {}
            try { await enforceLimits(); } catch {}
          }
        } catch {}
        try { if (typeof window.onTTSPreloadBlob === 'function') window.onTTSPreloadBlob(msg.id, msg.blob); } catch {}
        return;
      }
    };
    preloadWorker.onerror = () => {};
    return preloadWorker;
  }

  async function fetchAudioViaWorker(index, options = {}) {
    const para = (window.ttsParagraphs || [])[index];
    const text = options.text || (para ? para.text : null);
    if (!text) return null;

    const worker = ensureFetchWorker();
    const params = typeof window.getTTSParams === 'function' ? window.getTTSParams() : '';

    return new Promise((resolve, reject) => {
      pending.set(index, { resolve, reject });
      worker.postMessage({ type: 'fetch', index, text, ttsParams: params, preload: !!options.preload });
    });
  }

  function abortFetchWorker() {
    if (fetchWorker) {
      try { fetchWorker.postMessage({ type: 'stop' }); } catch {}
    }
  }

  function startPreloadQueue(texts) {
    const worker = ensurePreloadWorker();
    const params = typeof window.getTTSParams === 'function' ? window.getTTSParams() : '';
    const tasks = (texts || []).map((t, i) => ({ id: i, text: t }));
    try { preloadTextMap.clear(); } catch {}
    try { for (const t of tasks) { preloadTextMap.set(t.id, t.text); } } catch {}
    try { worker.postMessage({ type: 'start', tasks, ttsParams: params }); } catch {}
  }

  function stopPreloadQueue() {
    if (preloadWorker) {
      try { preloadWorker.postMessage({ type: 'stop' }); } catch {}
    }
  }

  function setPreloadBlob(id, blob) { try { preloadCache.set(id, blob); } catch {} }
  function getPreloadBlob(id) { try { return preloadCache.get(id) || null; } catch { return null; } }
  function clearPreloadCache() { try { preloadCache.clear(); } catch {} try { preloadTextMap.clear(); } catch {} try { preloadCacheByTextKey.clear(); } catch {} }

  async function setPreloadBlobByText(text, blob) {
    try {
      const key = await computePreloadKey(text);
      preloadCacheByTextKey.set(key, blob);
      await idbSet(key, blob);
      await enforceLimits();
    } catch {}
  }

  async function getPreloadBlobByText(text) {
    try {
      const key = await computePreloadKey(text);
      const mem = preloadCacheByTextKey.get(key);
      if (mem) return mem;
      return await idbGet(key);
    } catch { return null; }
  }

  let preloadLimits = { maxItems: 300, maxTotalMB: 512 };
  try {
    const saved = localStorage.getItem('TTS_PRELOAD_LIMITS');
    if (saved) {
      const obj = JSON.parse(saved);
      if (obj && Number.isFinite(obj.maxItems) && Number.isFinite(obj.maxTotalMB)) preloadLimits = obj;
    }
  } catch {}

  function setPreloadLimits(limits) {
    try {
      if (!limits) return;
      const maxItems = Number.isFinite(limits.maxItems) ? limits.maxItems : preloadLimits.maxItems;
      const maxTotalMB = Number.isFinite(limits.maxTotalMB) ? limits.maxTotalMB : preloadLimits.maxTotalMB;
      preloadLimits = { maxItems, maxTotalMB };
      localStorage.setItem('TTS_PRELOAD_LIMITS', JSON.stringify(preloadLimits));
    } catch {}
  }

  function getPreloadLimits() { return preloadLimits; }

  async function enforceLimits() {
    try {
      const all = await idbGetAll();
      let count = all.length;
      let totalBytes = 0;
      for (const r of all) totalBytes += (r.blob?.size || 0);
      const maxItems = preloadLimits.maxItems;
      const maxBytes = preloadLimits.maxTotalMB * 1024 * 1024;
      if (count <= maxItems && totalBytes <= maxBytes) return true;
      all.sort((a, b) => (a.updatedAt || 0) - (b.updatedAt || 0));
      while ((count > maxItems) || (totalBytes > maxBytes)) {
        const r = all.shift();
        if (!r) break;
        await idbDelete(r.key);
        count--;
        totalBytes -= (r.blob?.size || 0);
      }
      return true;
    } catch { return false; }
  }

  async function getPreloadStats(filter = null) {
    try {
      const all = await idbGetAll(filter);
      const count = all.length;
      let totalBytes = 0;
      for (const r of all) totalBytes += (r.blob?.size || 0);
      return { count, totalBytes, totalMB: +(totalBytes / 1024 / 1024).toFixed(2) };
    } catch { return { count: 0, totalBytes: 0, totalMB: 0 }; }
  }

  async function clearIDBPreload(filter = null) { try { return await idbClearAll(filter); } catch { return false; } }

  window.TTS = {
    fetchAudioViaWorker,
    abortFetchWorker,
    startPreloadQueue,
    stopPreloadQueue,
    setPreloadBlob,
    getPreloadBlob,
    clearPreloadCache,
    setPreloadBlobByText,
    getPreloadBlobByText,
    setPreloadLimits,
    getPreloadLimits,
    enforceLimits,
    getPreloadStats,
    clearIDBPreload,
  };
})();
