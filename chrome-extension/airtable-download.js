'use strict';

(function () {
  const API_BASE = 'https://lightspeed-extension-production.up.railway.app';

  function getBaseAndTableFromUrl() {
    const path = (window.location.pathname || '').trim();
    const match = path.match(/\/app([a-zA-Z0-9_-]+)\/(tbl[a-zA-Z0-9_-]+)/);
    if (match) return { baseId: 'app' + match[1], tableId: match[2] };
    return null;
  }

  function ensureButton() {
    if (!document.body) return;
    if (document.getElementById('ls-airtable-download-wrap')) return;
    const wrap = document.createElement('div');
    wrap.id = 'ls-airtable-download-wrap';
    wrap.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:2147483647;';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Download images to folder';
    btn.title = 'Download all images from the Image column into a folder on your computer';
    btn.style.cssText = 'padding:10px 16px;font-size:14px;font-weight:500;color:#fff;background:#06c;border:none;border-radius:6px;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,0.2);';
    btn.onmouseover = function () { btn.style.background = '#05a'; };
    btn.onmouseout = function () { btn.style.background = '#06c'; };
    btn.onclick = runDownload;
    wrap.appendChild(btn);
    document.body.appendChild(wrap);
  }

  function runDownload() {
    const ids = getBaseAndTableFromUrl();
    if (!ids) {
      alert('Open a table view (URL should contain /app.../tbl...) to download images.');
      return;
    }
    chrome.storage.sync.get(['connection_id'], function (data) {
      const connectionId = (data && data.connection_id) ? data.connection_id.trim() : '';
      if (!connectionId) {
        alert('No connection key. Set up the extension from Lightspeed and connect once, or add your connection key in extension options.');
        return;
      }
      const btn = document.querySelector('#ls-airtable-download-wrap button');
      if (btn) {
        btn.disabled = true;
        btn.textContent = 'Preparing…';
      }
      fetch(API_BASE + '/api/airtable/image-urls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          connection_id: connectionId,
          base_id: ids.baseId,
          table_id: ids.tableId
        })
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.success && res.urls && res.urls.length > 0) {
            chrome.runtime.sendMessage({
              action: 'downloadAirtableImages',
              urls: res.urls,
              folderName: 'Airtable_Images'
            }, function () {
              if (btn) {
                btn.disabled = false;
                btn.textContent = 'Download images to folder';
              }
              alert('Downloading ' + res.urls.length + ' image(s) into folder "Airtable_Images" in your Downloads.');
            });
          } else if (res.success && (!res.urls || res.urls.length === 0)) {
            if (btn) {
              btn.disabled = false;
              btn.textContent = 'Download images to folder';
            }
            alert('No images found in the Image column on this table.');
          } else {
            if (btn) {
              btn.disabled = false;
              btn.textContent = 'Download images to folder';
            }
            alert(res.error || 'Failed to get image list.');
          }
        })
        .catch(function (err) {
          if (btn) {
            btn.disabled = false;
            btn.textContent = 'Download images to folder';
          }
          alert('Request failed: ' + (err.message || err));
        });
    });
  }

  function init() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', ensureButton);
    } else {
      ensureButton();
    }
  }

  function startObserver() {
    if (!document.body) {
      setTimeout(startObserver, 50);
      return;
    }
    ensureButton();
    const observer = new MutationObserver(function () {
      if (document.getElementById('ls-airtable-download-wrap')) return;
      ensureButton();
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }
  init();
  startObserver();
})();
