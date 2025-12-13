(() => {
  function onEngineChange() {
    const engine = document.getElementById('tts-engine').value;
    localStorage.setItem(TTS_ENGINE_KEY, engine);
    updateVisibility();
    save();
  }

  function updateVisibility() {
    const engine = document.getElementById('tts-engine').value;
    document.getElementById('edge-settings').style.display = engine === 'edge' ? 'block' : 'none';
    document.getElementById('gptsovits-settings').style.display = engine === 'gptsovits' ? 'block' : 'none';
    document.getElementById('indextts-settings').style.display = engine === 'indextts' ? 'block' : 'none';
  }

  function save() {
    const autoAdvance = document.getElementById('tts-auto-advance').checked;
    ttsAutoAdvance = autoAdvance;
    ttsSkipToc = document.getElementById('tts-skip-toc').checked;
    ttsSkipCopyright = document.getElementById('tts-skip-copyright').checked;

    const settings = {
      autoAdvance: autoAdvance,
      skipToc: ttsSkipToc,
      skipCopyright: ttsSkipCopyright,
      preload: parseInt(document.getElementById('tts-preload-count').value || '-1', 10),
      engine: document.getElementById('tts-engine').value,
      edge: {
        voice: document.getElementById('edge-voice').value,
        rate: document.getElementById('edge-rate').value,
        volume: document.getElementById('edge-volume').value,
        pitch: document.getElementById('edge-pitch').value
      },
      gptsovits: {
        url: document.getElementById('gptsovits-url').value,
        version: document.getElementById('gptsovits-version').value,
        method: document.getElementById('gptsovits-method').value,
        streaming: document.getElementById('gptsovits-streaming').checked,
        ref: document.getElementById('gptsovits-ref').value,
        prompt: document.getElementById('gptsovits-prompt').value,
        textLang: document.getElementById('gptsovits-text-lang').value,
        promptLang: document.getElementById('gptsovits-prompt-lang').value,
        topK: document.getElementById('gptsovits-top-k').value,
        topP: document.getElementById('gptsovits-top-p').value,
        temperature: document.getElementById('gptsovits-temperature').value,
        textSplit: document.getElementById('gptsovits-text-split').value,
        speed: document.getElementById('gptsovits-speed').value,
        repetition: document.getElementById('gptsovits-repetition').value,
        seed: document.getElementById('gptsovits-seed').value,
        parallel: document.getElementById('gptsovits-parallel').checked,
        splitBucket: document.getElementById('gptsovits-split-bucket').checked,
        batchSize: document.getElementById('gptsovits-batch-size').value,
        sampleSteps: document.getElementById('gptsovits-sample-steps').value,
        gptPath: document.getElementById('gptsovits-gpt-path').value,
        sovitsPath: document.getElementById('gptsovits-sovits-path').value,
        modelDir: document.getElementById('gptsovits-model-dir').value,
        refList: Array.from(document.getElementById('gptsovits-ref-list').options).map(o => ({ value: o.value, text: o.text }))
      },
      indextts: {
        url: document.getElementById('indextts-url').value
      }
    };
    localStorage.setItem(TTS_SETTINGS_KEY, JSON.stringify(settings));
    localStorage.setItem(TTS_ENGINE_KEY, settings.engine);
    TTS_PRELOAD_COUNT = settings.preload;
    persistPreloadCount(TTS_PRELOAD_COUNT);

    document.getElementById('edge-rate-value').textContent = (settings.edge.rate >= 0 ? '+' : '') + settings.edge.rate + '%';
    document.getElementById('edge-volume-value').textContent = (settings.edge.volume >= 0 ? '+' : '') + settings.edge.volume + '%';
    document.getElementById('edge-pitch-value').textContent = (settings.edge.pitch >= 0 ? '+' : '') + settings.edge.pitch + 'Hz';
    document.getElementById('gptsovits-speed-value').textContent = settings.gptsovits.speed + 'x';

    updateVisibility();
  }

  function load() {
    try {
      const saved = localStorage.getItem(TTS_SETTINGS_KEY);
      if (saved) {
        const settings = JSON.parse(saved);
        const autoAdvance = settings.autoAdvance !== false;
        document.getElementById('tts-auto-advance').checked = autoAdvance;
        ttsAutoAdvance = autoAdvance;

        const skipToc = settings.skipToc !== false;
        const skipCopyright = settings.skipCopyright !== false;
        document.getElementById('tts-skip-toc').checked = skipToc;
        document.getElementById('tts-skip-copyright').checked = skipCopyright;
        ttsSkipToc = skipToc;
        ttsSkipCopyright = skipCopyright;

        const preload = Number.isFinite(settings.preload) ? settings.preload : SERVER_PRELOAD_DEFAULT;
        document.getElementById('tts-preload-count').value = preload ?? -1;
        TTS_PRELOAD_COUNT = preload ?? -1;

        document.getElementById('tts-engine').value = settings.engine || 'edge';

        if (settings.edge) {
          document.getElementById('edge-voice').value = settings.edge.voice || 'zh-CN-XiaoxiaoNeural';
          document.getElementById('edge-rate').value = settings.edge.rate || 0;
          document.getElementById('edge-volume').value = settings.edge.volume || 0;
          document.getElementById('edge-pitch').value = settings.edge.pitch || 0;
        }

        if (settings.gptsovits) {
          const g = settings.gptsovits;
          document.getElementById('gptsovits-url').value = g.url || 'http://127.0.0.1:9880';
          document.getElementById('gptsovits-version').value = g.version || 'v2';
          document.getElementById('gptsovits-method').value = g.method || 'POST';
          document.getElementById('gptsovits-streaming').checked = g.streaming !== false;
          document.getElementById('gptsovits-ref').value = g.ref || '';
          document.getElementById('gptsovits-prompt').value = g.prompt || '';
          if (typeof updateGPTSoVITSLangOptions === 'function') updateGPTSoVITSLangOptions();
          document.getElementById('gptsovits-text-lang').value = g.textLang || 'all_zh';
          document.getElementById('gptsovits-prompt-lang').value = g.promptLang || 'all_zh';
          document.getElementById('gptsovits-top-k').value = g.topK || 5;
          document.getElementById('gptsovits-top-p').value = g.topP || 1;
          document.getElementById('gptsovits-temperature').value = g.temperature || 1;
          document.getElementById('gptsovits-text-split').value = g.textSplit || 'cut5';
          document.getElementById('gptsovits-speed').value = g.speed || 1;
          document.getElementById('gptsovits-speed-value').textContent = (g.speed || 1) + 'x';
          document.getElementById('gptsovits-repetition').value = g.repetition || 1.35;
          document.getElementById('gptsovits-seed').value = g.seed || -1;
          document.getElementById('gptsovits-parallel').checked = g.parallel !== false;
          document.getElementById('gptsovits-split-bucket').checked = g.splitBucket !== false;
          document.getElementById('gptsovits-batch-size').value = g.batchSize || 1;
          document.getElementById('gptsovits-sample-steps').value = g.sampleSteps || 32;
          document.getElementById('gptsovits-gpt-path').value = g.gptPath || '';
          document.getElementById('gptsovits-sovits-path').value = g.sovitsPath || '';
          document.getElementById('gptsovits-model-dir').value = g.modelDir || '.';
          if (g.refList && g.refList.length > 1) {
            const select = document.getElementById('gptsovits-ref-list');
            select.innerHTML = '';
            g.refList.forEach(item => {
              const opt = document.createElement('option');
              opt.value = item.value;
              opt.textContent = item.text;
              select.appendChild(opt);
            });
            select.style.display = 'block';
          }
        } else {
          if (typeof updateGPTSoVITSLangOptions === 'function') updateGPTSoVITSLangOptions();
        }

        if (settings.indextts) {
          document.getElementById('indextts-url').value = settings.indextts.url || 'http://127.0.0.1:8080';
        }

        document.getElementById('edge-rate-value').textContent = (settings.edge?.rate >= 0 ? '+' : '') + (settings.edge?.rate || 0) + '%';
        document.getElementById('edge-volume-value').textContent = (settings.edge?.volume >= 0 ? '+' : '') + (settings.edge?.volume || 0) + '%';
        document.getElementById('edge-pitch-value').textContent = (settings.edge?.pitch >= 0 ? '+' : '') + (settings.edge?.pitch || 0) + 'Hz';
      }
    } catch (e) {}

    if (!document.getElementById('gptsovits-text-lang').options.length) {
      if (typeof updateGPTSoVITSLangOptions === 'function') updateGPTSoVITSLangOptions();
    }
    if (TTS_PRELOAD_COUNT === -1 && SERVER_PRELOAD_DEFAULT !== undefined) {
      document.getElementById('tts-preload-count').value = SERVER_PRELOAD_DEFAULT ?? -1;
      TTS_PRELOAD_COUNT = SERVER_PRELOAD_DEFAULT ?? -1;
    }

    updateVisibility();
  }

  function getParams() {
    const engine = document.getElementById('tts-engine').value;
    let params = 'engine=' + engine;
    if (engine === 'edge') {
      const rate = document.getElementById('edge-rate').value;
      const volume = document.getElementById('edge-volume').value;
      const pitch = document.getElementById('edge-pitch').value;
      const voice = document.getElementById('edge-voice').value;
      params += '&voice=' + encodeURIComponent(voice);
      params += '&rate=' + encodeURIComponent((rate >= 0 ? '+' : '') + rate + '%');
      params += '&volume=' + encodeURIComponent((volume >= 0 ? '+' : '') + volume + '%');
      params += '&pitch=' + encodeURIComponent((pitch >= 0 ? '+' : '') + pitch + 'Hz');
    } else if (engine === 'gptsovits') {
      let refPath = document.getElementById('gptsovits-ref').value.trim();
      refPath = refPath.replace(/^["']|["']$/g, '');
      const g = {
        url: document.getElementById('gptsovits-url').value,
        method: document.getElementById('gptsovits-method').value,
        streaming: document.getElementById('gptsovits-streaming').checked,
        ref: refPath,
        prompt: document.getElementById('gptsovits-prompt').value,
        textLang: document.getElementById('gptsovits-text-lang').value,
        promptLang: document.getElementById('gptsovits-prompt-lang').value,
        topK: document.getElementById('gptsovits-top-k').value,
        topP: document.getElementById('gptsovits-top-p').value,
        temperature: document.getElementById('gptsovits-temperature').value,
        textSplit: document.getElementById('gptsovits-text-split').value,
        speed: document.getElementById('gptsovits-speed').value,
        repetition: document.getElementById('gptsovits-repetition').value,
        seed: document.getElementById('gptsovits-seed').value,
        parallel: document.getElementById('gptsovits-parallel').checked,
        splitBucket: document.getElementById('gptsovits-split-bucket').checked,
        batchSize: document.getElementById('gptsovits-batch-size').value,
        sampleSteps: document.getElementById('gptsovits-sample-steps').value
      };
      params += '&method=' + g.method;
      params += '&streaming_mode=' + g.streaming;
      if (g.url) params += '&api_url=' + encodeURIComponent(g.url);
      if (g.ref) params += '&ref_audio_path=' + encodeURIComponent(g.ref);
      if (g.prompt) params += '&prompt_text=' + encodeURIComponent(g.prompt);
      params += '&text_lang=' + g.textLang;
      params += '&prompt_lang=' + g.promptLang;
      params += '&top_k=' + g.topK;
      params += '&top_p=' + g.topP;
      params += '&temperature=' + g.temperature;
      params += '&text_split_method=' + g.textSplit;
      params += '&speed_factor=' + g.speed;
      params += '&repetition_penalty=' + g.repetition;
      params += '&seed=' + g.seed;
      params += '&parallel_infer=' + g.parallel;
      params += '&split_bucket=' + g.splitBucket;
      params += '&batch_size=' + g.batchSize;
      params += '&sample_steps=' + g.sampleSteps;
    } else if (engine === 'indextts') {
      const url = document.getElementById('indextts-url').value;
      if (url) params += '&api_url=' + encodeURIComponent(url);
    }
    return params;
  }

  window.Settings = {
    onEngineChange,
    updateVisibility,
    save,
    load,
    getParams,
  };
})();
