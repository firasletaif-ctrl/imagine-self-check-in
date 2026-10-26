(() => {
  const installButton = document.getElementById('install-app-button');
  const installDialog = document.getElementById('install-help-dialog');
  const closeButton = document.getElementById('close-install-help');
  if (!installButton) return;

  let installPrompt = null;
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const userAgent = navigator.userAgent || '';
  const isAppleMobile = /iPhone|iPad|iPod/i.test(userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isSafari = /Safari/i.test(userAgent) && !/(Chrome|Chromium|CriOS|Edg|FxiOS|OPR)/i.test(userAgent);
  const isMacSafari = /Macintosh/i.test(userAgent) && isSafari;

  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    installPrompt = event;
    if (!isStandalone) installButton.hidden = false;
  });

  window.addEventListener('appinstalled', () => {
    installPrompt = null;
    installButton.hidden = true;
  });

  if (!isStandalone && (isAppleMobile || isMacSafari)) installButton.hidden = false;

  installButton.addEventListener('click', async () => {
    if (installPrompt) {
      installPrompt.prompt();
      await installPrompt.userChoice;
      installPrompt = null;
      installButton.hidden = true;
      return;
    }
    if (installDialog && typeof installDialog.showModal === 'function') installDialog.showModal();
  });

  closeButton?.addEventListener('click', () => installDialog?.close());
  installDialog?.addEventListener('click', event => {
    if (event.target === installDialog) installDialog.close();
  });

  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost')) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(error => {
        console.warn('Imagine PWA service worker registration failed:', error);
      });
    }, { once: true });
  }
})();
