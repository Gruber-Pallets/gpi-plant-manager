document.addEventListener('click', async event => {
  const button = event.target.closest('[data-saturday-action="activate-from-schedule"]');
  if (!button || button.disabled) return;

  button.disabled = true;
  try {
    const response = await fetch('/api/staffing/saturday-recruiting/activate-from-schedule', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({day: button.dataset.day}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not start Saturday recruiting.');
    window.location.reload();
  } catch (error) {
    button.disabled = false;
    window.alert(error.message);
  }
});
