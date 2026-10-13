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
      uploadToServer: !!document.getElementById('tts-upload-to-server')?.checked,
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
        url: document.getElementById('indextts-url').value,
        spk: document.getElementById('indextts-spk').value,
        emo: document.getElementById('indextts-emo').value,
        emoAlpha: document.getElementById('indextts-emo-alpha').value,
        useEmoText: document.getElementById('indextts-use-emo-text').checked,
        emoText: document.getElementById('indextts-emo-text').value,
        outputFormat: document.getElementById('indextts-output-format').value,
        audioBitdepth: document.getElementById('indextts-audio-bitdepth').value,
        maxTextTokens: document.getElementById('indextts-max-text-tokens').value,
        intervalSilenceMs: document.getElementById('indextts-interval-silence').value,
        doSample: document.getElementById('indextts-do-sample').checked,
        useRandom: document.getElementById('indextts-use-random').checked,
        resumeProject: document.getElementById('indextts-resume-project').checked,
        splitBucket: document.getElementById('indextts-split-bucket').checked,
        bucketThreshold: document.getElementById('indextts-bucket-threshold').value,
        topP: document.getElementById('indextts-top-p').value,
        topK: document.getElementById('indextts-top-k').value,
        temperature: document.getElementById('indextts-temperature').value,
        lengthPenalty: document.getElementById('indextts-length-penalty').value,
        numBeams: document.getElementById('indextts-num-beams').value,
        repetitionPenalty: document.getElementById('indextts-repetition-penalty').value,
        maxMelTokens: document.getElementById('indextts-max-mel-tokens').value,
        concurrency: document.getElementById('indextts-concurrency').value
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
          const i = settings.indextts;
          document.getElementById('indextts-url').value = i.url || 'http://127.0.0.1:9881';
          document.getElementById('indextts-spk').value = i.spk || '';
          document.getElementById('indextts-emo').value = i.emo || '';
          document.getElementById('indextts-emo-alpha').value = i.emoAlpha || 1;
          document.getElementById('indextts-emo-alpha-value').textContent = i.emoAlpha || 1;
          document.getElementById('indextts-use-emo-text').checked = !!i.useEmoText;
          document.getElementById('indextts-emo-text').value = i.emoText || '';
          document.getElementById('indextts-output-format').value = i.outputFormat || 'wav';
          document.getElementById('indextts-audio-bitdepth').value = i.audioBitdepth || 'int16';
          document.getElementById('indextts-max-text-tokens').value = i.maxTextTokens || 120;
          document.getElementById('indextts-interval-silence').value = i.intervalSilenceMs || 200;
          document.getElementById('indextts-do-sample').checked = i.doSample !== false;
          document.getElementById('indextts-use-random').checked = i.useRandom !== false;
          document.getElementById('indextts-resume-project').checked = i.resumeProject !== false;
          document.getElementById('indextts-split-bucket').checked = i.splitBucket !== false;
          document.getElementById('indextts-bucket-threshold').value = i.bucketThreshold || 0.75;
          document.getElementById('indextts-top-p').value = i.topP || 0.8;
          document.getElementById('indextts-top-k').value = i.topK || 30;
          document.getElementById('indextts-temperature').value = i.temperature || 0.8;
          document.getElementById('indextts-length-penalty').value = (i.lengthPenalty ?? 0.0);
          document.getElementById('indextts-num-beams').value = i.numBeams || 3;
          document.getElementById('indextts-repetition-penalty').value = i.repetitionPenalty || 10.0;
          document.getElementById('indextts-max-mel-tokens').value = i.maxMelTokens || 1500;
          document.getElementById('indextts-concurrency').value = i.concurrency || 1;
        }

        if (Object.prototype.hasOwnProperty.call(settings, 'uploadToServer')) {
          const el = document.getElementById('tts-upload-to-server');
          if (el) el.checked = !!settings.uploadToServer;
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
      let spk = document.getElementById('indextts-spk').value.trim();
      let emo = document.getElementById('indextts-emo').value.trim();
      spk = spk.replace(/^["']|["']$/g, '');
      emo = emo.replace(/^["']|["']$/g, '');
      const emoAlpha = document.getElementById('indextts-emo-alpha').value;
      const useEmoText = document.getElementById('indextts-use-emo-text').checked;
      const emoText = document.getElementById('indextts-emo-text').value;
      const outputFormat = document.getElementById('indextts-output-format').value;
      const audioBitdepth = document.getElementById('indextts-audio-bitdepth').value;
      const maxTextTokens = document.getElementById('indextts-max-text-tokens').value;
      const intervalSilenceMs = document.getElementById('indextts-interval-silence').value;
      const doSample = document.getElementById('indextts-do-sample').checked;
      const useRandom = document.getElementById('indextts-use-random').checked;
      const resumeProject = document.getElementById('indextts-resume-project').checked;
      const splitBucket = document.getElementById('indextts-split-bucket').checked;
      const bucketThreshold = document.getElementById('indextts-bucket-threshold').value;
      const topP = document.getElementById('indextts-top-p').value;
      const topK = document.getElementById('indextts-top-k').value;
      const temperature = document.getElementById('indextts-temperature').value;
      const lengthPenalty = document.getElementById('indextts-length-penalty').value;
      const numBeams = document.getElementById('indextts-num-beams').value;
      const repetitionPenalty = document.getElementById('indextts-repetition-penalty').value;
      const maxMelTokens = document.getElementById('indextts-max-mel-tokens').value;
      if (url) params += '&api_url=' + encodeURIComponent(url);
      if (spk) params += '&spk_audio_prompt=' + encodeURIComponent(spk);
      if (emo) params += '&emo_audio_prompt=' + encodeURIComponent(emo);
      if (emoAlpha) params += '&emo_alpha=' + encodeURIComponent(emoAlpha);
      params += '&use_emo_text=' + useEmoText;
      if (emoText) params += '&emo_text=' + encodeURIComponent(emoText);
      if (outputFormat) params += '&output_format=' + encodeURIComponent(outputFormat);
      if (audioBitdepth) params += '&audio_bitdepth=' + encodeURIComponent(audioBitdepth);
      if (maxTextTokens) params += '&max_text_tokens_per_segment=' + encodeURIComponent(maxTextTokens);
      if (intervalSilenceMs) params += '&interval_silence_ms=' + encodeURIComponent(intervalSilenceMs);
      params += '&do_sample=' + doSample;
      params += '&use_random=' + useRandom;
      params += '&resume_project=' + resumeProject;
      params += '&split_bucket=' + splitBucket;
      if (bucketThreshold) params += '&bucket_threshold=' + encodeURIComponent(bucketThreshold);
      if (topP) params += '&top_p=' + encodeURIComponent(topP);
      if (topK) params += '&top_k=' + encodeURIComponent(topK);
      if (temperature) params += '&temperature=' + encodeURIComponent(temperature);
      if (lengthPenalty) params += '&length_penalty=' + encodeURIComponent(lengthPenalty);
      if (numBeams) params += '&num_beams=' + encodeURIComponent(numBeams);
      if (repetitionPenalty) params += '&repetition_penalty=' + encodeURIComponent(repetitionPenalty);
      if (maxMelTokens) params += '&max_mel_tokens=' + encodeURIComponent(maxMelTokens);
      
      // 获取并发数
      const concurrency = document.getElementById('indextts-concurrency')?.value || 1;
      params += '&concurrency=' + encodeURIComponent(concurrency);
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
