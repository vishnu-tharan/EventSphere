(() => {
  const form = document.getElementById('checkin-form');
  const eventField = document.getElementById('scan-event');
  const start = document.getElementById('start-camera');
  const stop = document.getElementById('stop-camera');
  const manual = document.getElementById('manual-checkin');
  const help = document.getElementById('scanner-help');
  const banner = document.getElementById('scan-banner');
  const verdict = document.getElementById('scan-verdict');
  const reason = document.getElementById('scan-reason');
  let camera;
  let running = false;
  let checking = false;
  let previousCode = '';
  let previousSeen = 0;
  let nextScanAt = 0;

  function showResult(label, message, style) {
    banner.className = 'alert alert-' + style;
    verdict.textContent = label;
    reason.textContent = message;
    banner.hidden = false;
  }

  async function checkCode(code) {
    if (!running || !eventField.value) return;
    const now = Date.now();
    const sameTicketInView = code === previousCode && now - previousSeen < 900;
    if (code === previousCode) previousSeen = now;
    if (checking || sameTicketInView || now < nextScanAt) return;

    previousCode = code;
    previousSeen = now;
    checking = true;
    eventField.disabled = true;
    manual.disabled = true;
    showResult('Checking ticket', '', 'secondary');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const body = new URLSearchParams({
        event_id: eventField.value,
        csrf: form.elements.csrf.value,
        code,
        camera: 'true',
      });
      const response = await fetch(form.action, {
        method: 'POST', body, signal: controller.signal,
      });
      if (!response.ok) throw new Error('Check-in request failed');
      const result = await response.json();
      showResult(result.admitted ? 'ADMIT' : 'REJECT', result.reason, result.admitted ? 'success' : 'danger');
    } catch {
      showResult('Result unavailable', 'Remove the ticket from view and try again, or enter its code manually.', 'warning');
    } finally {
      clearTimeout(timeout);
      nextScanAt = Date.now() + 1200;
      checking = false;
      eventField.disabled = false;
      manual.disabled = false;
    }
  }

  form.addEventListener('submit', (event) => {
    if (checking) event.preventDefault();
  });

  eventField.addEventListener('change', () => {
    previousCode = '';
    nextScanAt = 0;
    banner.hidden = true;
    if (running) help.textContent = eventField.value ? 'Ready to scan.' : 'Choose an event to continue.';
  });

  start.addEventListener('click', async () => {
    if (!eventField.reportValidity()) return;
    if (typeof Html5Qrcode === 'undefined') {
      help.textContent = 'Scanner unavailable. Enter the ticket code manually.';
      return;
    }
    start.disabled = true;
    try {
      camera = new Html5Qrcode('reader', {
        formatsToSupport: [Html5QrcodeSupportedFormats.QR_CODE],
      });
      await camera.start({facingMode: 'environment'}, {fps: 8}, checkCode, () => {});
      running = true;
      start.hidden = true;
      stop.hidden = false;
      help.textContent = 'Ready to scan. Move each ticket away after its result.';
    } catch {
      start.disabled = false;
      help.textContent = 'Cannot start camera. Check camera permission or enter the code manually.';
    }
  });

  stop.addEventListener('click', async () => {
    stop.disabled = true;
    running = false;
    try {
      await camera.stop();
      camera.clear();
      previousCode = '';
      nextScanAt = 0;
      start.hidden = false;
      start.disabled = false;
      stop.hidden = true;
      help.textContent = 'Camera stopped.';
    } catch {
      running = true;
      help.textContent = 'Camera could not stop. Try again.';
    } finally {
      stop.disabled = false;
    }
  });
})();
