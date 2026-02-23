'use strict';

// Must match API_BASE in background.js; update both when deploying
const API_BASE = 'http://127.0.0.1:5050';

document.getElementById('save').onclick = function () {
  const key = (document.getElementById('key').value || '').trim();
  const status = document.getElementById('status');
  if (!key) {
    status.textContent = 'Enter a connection key.';
    status.className = 'muted';
    return;
  }
  chrome.storage.sync.set({ connection_id: key }, function () {
    status.textContent = 'Saved. You can use the Export to Airtable button on Lightspeed.';
    status.className = 'saved';
    updateConfigureLink(key);
  });
};

function updateConfigureLink(connectionId) {
  const el = document.getElementById('configure-link');
  if (connectionId) {
    const url = API_BASE + '/settings?key=' + encodeURIComponent(connectionId);
    el.innerHTML = '<a href="' + url + '" target="_blank" rel="noopener">Configure which fields to export</a>';
  } else {
    el.textContent = '';
  }
}

chrome.storage.sync.get(['connection_id'], function (data) {
  if (data.connection_id) {
    document.getElementById('key').value = data.connection_id;
  }
  updateConfigureLink(data.connection_id || '');
});
