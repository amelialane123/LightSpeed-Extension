'use strict';

(function () {
  const params = new URLSearchParams(window.location.search);
  const keyFromUrl = params.get('key');
  const keyFromDom = document.getElementById('key');
  const key = (keyFromUrl && keyFromUrl.trim()) || (keyFromDom && keyFromDom.textContent && keyFromDom.textContent.trim()) || '';
  if (!key) return;
  chrome.runtime.sendMessage({ action: 'saveConnectionKey', connection_id: key }, function () {
    const notice = document.createElement('p');
    notice.className = 'saved';
    notice.style.cssText = 'color:#080;font-weight:bold;margin-top:1rem;';
    notice.textContent = 'Key saved to extension. You can close this tab and use Export to Airtable on any Lightspeed item list page.';
    document.body.appendChild(notice);
  });
})();
