(() => {
  function initImageRewrite(bookId) {
    try {
      const container = document.getElementById('book-content');
      if (!container) return;
      const dir = window.BOOK_DIR || '';
      const q = dir ? ('?dir=' + encodeURIComponent(dir)) : '';
      const imgs = container.querySelectorAll('img');
      imgs.forEach(img => {
        const src = img.getAttribute('src') || '';
        if (!src) return;
        const lower = src.toLowerCase();
        const isAbsolute = lower.startsWith('http://') || lower.startsWith('https://') || lower.startsWith('data:') || lower.startsWith('/');
        if (isAbsolute) return;
        const name = src.split('/').pop();
        img.setAttribute('src', `/read/${bookId}/images/${encodeURIComponent(name)}${q}`);
      });
    } catch {}
  }

  function initParagraphSelection(selectorsCsv) {
    const contentArea = document.querySelector('.content-area') || document.querySelector('.book-content') || document.querySelector('.content') || document.body;
    if (!contentArea) return;
    const selectors = (selectorsCsv || '').split(',').map(s => s.trim()).filter(Boolean);
    const matchesParagraph = (el) => selectors.some(sel => el.matches && el.matches(sel));
    contentArea.addEventListener('click', function(e) {
      let target = e.target;
      while (target && target !== contentArea) {
        if (matchesParagraph(target)) {
          let list = window.ttsParagraphs || [];
          if (!list.length && typeof window.initTTSParagraphs === 'function') {
            try { window.initTTSParagraphs(false); } catch {}
            list = window.ttsParagraphs || [];
          }
          const idx = list.findIndex(p => p.node === target);
          if (idx >= 0) {
            window.selectedParagraphIndex = idx;
            selectVisuals(idx, 'auto');
            showToolbar();
          }
          return;
        }
        target = target.parentElement;
      }
    });
  }

  function initBlankCloseListener(selectorsCsv) {
    document.addEventListener('click', function(e) {
      const toolbar = document.getElementById('paragraph-toolbar');
      const fab = document.getElementById('paragraph-fab');
      if (!toolbar || toolbar.style.display === 'none') return;
      if (toolbar.contains(e.target) || (fab && fab.contains(e.target))) return;
      const selectors = (selectorsCsv || '').split(',').map(s => s.trim()).filter(Boolean);
      const matchesParagraph = (el) => selectors.some(sel => el.matches && el.matches(sel));
      let target = e.target;
      while (target && target !== document.body) {
        if (matchesParagraph(target)) return;
        target = target.parentElement;
      }
      hideToolbar();
    });
  }
  function highlight(idx, behavior = 'smooth') {
    try {
      document.querySelectorAll('.tts-highlight').forEach(el => el.classList.remove('tts-highlight'));
      const list = window.ttsParagraphs || [];
      const para = list[idx];
      if (!para) return;
      para.node.classList.add('tts-highlight');
      para.node.scrollIntoView({ behavior: behavior === 'auto' ? 'auto' : 'smooth', block: 'center' });
    } catch {}
  }

  function selectVisuals(idx, behavior = 'smooth') {
    try {
      document.querySelectorAll('.paragraph-selected').forEach(el => el.classList.remove('paragraph-selected'));
      const list = window.ttsParagraphs || [];
      const para = list[idx];
      if (!para) return;
      para.node.classList.add('paragraph-selected');
      window.selectedParagraphIndex = idx;
      para.node.scrollIntoView({ behavior: behavior === 'auto' ? 'auto' : 'smooth', block: 'center' });
    } catch {}
  }

  function showToolbar() {
    try {
      const toolbar = document.getElementById('paragraph-toolbar');
      if (toolbar) {
        toolbar.style.display = 'flex';
      }
      const infoEl = document.getElementById('para-info');
      const list = window.ttsParagraphs || [];
      const idx = (window.selectedParagraphIndex ?? -1);
      if (infoEl) {
        if (idx >= 0 && list[idx]) {
          infoEl.textContent = `第 ${idx + 1}/${list.length} 段`;
        } else {
          infoEl.textContent = '未选择段落';
        }
      }
    } catch {}
  }

  function hideToolbar() {
    try {
      const toolbar = document.getElementById('paragraph-toolbar');
      if (toolbar) toolbar.style.display = 'none';
      document.querySelectorAll('.paragraph-selected').forEach(el => el.classList.remove('paragraph-selected'));
    } catch {}
  }

  function selectPrevParagraph() {
    const list = window.ttsParagraphs || [];
    if (!list.length) return;
    const idx = (window.selectedParagraphIndex ?? -1);
    if (idx > 0) {
      const next = idx - 1;
      window.selectedParagraphIndex = next;
      selectVisuals(next, 'smooth');
      showToolbar();
    }
  }

  function selectNextParagraph() {
    const list = window.ttsParagraphs || [];
    if (!list.length) return;
    const idx = (window.selectedParagraphIndex ?? -1);
    if (idx < list.length - 1) {
      const next = idx + 1;
      window.selectedParagraphIndex = next;
      selectVisuals(next, 'smooth');
      showToolbar();
    }
  }

  function togglePanel() {
    try {
      const panel = document.getElementById('tts-panel');
      if (!panel) return;
      const visible = panel.classList.contains('visible');
      if (visible) panel.classList.remove('visible'); else panel.classList.add('visible');
    } catch {}
  }

  async function startHere() {
    const idx = (window.selectedParagraphIndex ?? -1);
    if (idx < 0) return;
    try {
      const panel = document.getElementById('tts-panel');
      if (panel && !panel.classList.contains('visible')) panel.classList.add('visible');
    } catch {}
    try { if (typeof window.hideParagraphToolbar === 'function') window.hideParagraphToolbar(); } catch {}
    try { if (typeof window.ttsStop === 'function') await window.ttsStop(); } catch {}
    try { if (typeof window.ttsPlay === 'function') await window.ttsPlay(true, idx); } catch {}
  }

  function saveProgressHere() {
    const idx = (window.selectedParagraphIndex ?? -1);
    if (idx < 0) return;
    try { if (typeof window.saveProgressToServer === 'function') window.saveProgressToServer(window.CHAPTER_INDEX, idx); } catch {}
    try { window.ttsCurrentIndex = idx; } catch {}
    try { selectVisuals(idx, 'smooth'); } catch {}
    try { if (typeof window.updateTTSStatus === 'function') window.updateTTSStatus('进度已保存到第 ' + (idx + 1) + ' 段'); } catch {}
    try { alert('✅ 进度已保存到第 ' + (idx + 1) + ' 段'); } catch {}
  }

  function copySelectedText() {
    const idx = (window.selectedParagraphIndex ?? -1);
    if (idx < 0) return;
    const list = window.ttsParagraphs || [];
    const para = list[idx];
    if (!para) return;
    const text = para.text || '';
    try { navigator.clipboard.writeText(text).then(() => { try { alert('✅ 已复制到剪贴板'); } catch {} }); return; } catch {}
    const textarea = document.createElement('textarea');
    textarea.value = text;
    document.body.appendChild(textarea);
    textarea.select();
    try { document.execCommand('copy'); } catch {}
    document.body.removeChild(textarea);
    try { alert('✅ 已复制到剪贴板'); } catch {}
  }

  window.UI = {
    togglePanel,
    highlight,
    selectVisuals,
    showToolbar,
    hideToolbar,
    selectPrevParagraph,
    selectNextParagraph,
    initImageRewrite,
    initParagraphSelection,
    initBlankCloseListener,
    startHere,
    saveProgressHere,
    copySelectedText,
  };
  try {
    if (window.__deferredImageRewrite) {
      initImageRewrite(window.__deferredImageRewrite);
      window.__deferredImageRewrite = null;
    }
  } catch {}
})();
