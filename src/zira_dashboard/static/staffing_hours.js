(() => {
  document.addEventListener("keydown", (e) => {
    if (e.key === 'Escape') {
      const detail = document.activeElement?.closest('details.hours-row-detail[open]');
      if (!detail) return;
      detail.open = false;
      const summary = detail.querySelector('summary');
      if (summary) summary.focus();
    }
  });
})();
