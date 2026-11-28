(() => {
  const dialog = document.getElementById('ticket-qr-dialog');
  if (typeof dialog.showModal !== 'function') return;
  const image = document.getElementById('ticket-qr-image');
  const title = document.getElementById('ticket-qr-title');
  document.querySelectorAll('.show-ticket-qr').forEach((link) => {
    link.addEventListener('click', (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      title.textContent = 'Ticket #' + link.dataset.ticket;
      image.src = link.href;
      dialog.showModal();
    });
  });
})();
