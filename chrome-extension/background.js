'use strict';

// Set to your deployed backend URL (no trailing slash)
const API_BASE = 'https://lightspeed-extension-production.up.railway.app';

chrome.runtime.onInstalled.addListener(function (details) {
  if (details.reason === 'install') {
    chrome.tabs.create({ url: API_BASE + '/connect' });
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.action === 'saveConnectionKey') {
    const key = (msg.connection_id || '').trim();
    if (!key) {
      sendResponse({ ok: false });
      return true;
    }
    chrome.storage.sync.set({ connection_id: key }, function () {
      sendResponse({ ok: true });
    });
    return true;
  }
  if (msg.action === 'openGallery') {
    const categoryId = msg.categoryId || 'ALL';
    const listingFilters = msg.listingFilters || {};
    chrome.storage.sync.get(['connection_id'], function (data) {
      const connectionId = (data && data.connection_id) ? data.connection_id.trim() : '';
      if (!connectionId) {
        chrome.tabs.create({ url: API_BASE + '/connect' });
        sendResponse({ ok: false, error: 'Not connected. Add your connection key in the extension options.' });
        return;
      }
      let url = API_BASE + '/gallery?key=' + encodeURIComponent(connectionId) + '&category_id=' + encodeURIComponent(categoryId);
      if (Object.keys(listingFilters).length) url += '&listing_filters=' + encodeURIComponent(JSON.stringify(listingFilters));
      chrome.tabs.create({ url: url });
      sendResponse({ ok: true });
    });
    return true;
  }
  if (msg.action !== 'runExport') {
    sendResponse({ ok: false, error: 'Unknown action' });
    return true;
  }
  const categoryId = msg.categoryId || 'ALL';
  const listingFilters = msg.listingFilters || {};
  chrome.storage.sync.get(['connection_id'], function (data) {
    const connectionId = (data && data.connection_id) ? data.connection_id.trim() : '';
    const body = { category_id: categoryId };
    if (connectionId) body.connection_id = connectionId;
    if (Object.keys(listingFilters).length) body.listing_filters = listingFilters;
    fetch(API_BASE + '/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.success && data.airtable_url) {
          chrome.tabs.create({ url: data.airtable_url });
        } else if (data.error && (data.error.includes('Reconnect') || data.error.includes('Connection not found') || data.error.includes('Missing connection_id'))) {
          chrome.tabs.create({ url: API_BASE + '/connect' });
        }
        sendResponse({ ok: data.success, error: data.error, output: data.output });
      })
      .catch(function () {
        sendResponse({ ok: false, error: 'Could not reach export backend. Check that it is running and that API_BASE in the extension is correct.' });
      });
  });
  return true;
});
