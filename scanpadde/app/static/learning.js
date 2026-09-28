(() => {
  const state = document.querySelector('#learning-state');
  const out = document.querySelector('#learning-rules');
  const reset = document.querySelector('#learning-reset');
  if (!state || !out || !reset) return;
  const escapeHtml = value => String(value ?? '').replace(/[&<>]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[char]));
  const labels = {supplier: 'Lieferant', invoice_label: 'Nummern-Bezeichnung'};

  async function request(path, options = {}) {
    const response = await fetch(path, options);
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async function load() {
    try {
      const rules = await request('api/learning-library');
      state.textContent = rules.length ? `${rules.length} lokale Lernregel${rules.length === 1 ? '' : 'n'}` : 'Noch keine freigegebenen Lernregeln.';
      out.innerHTML = rules.map(rule => `<tr><td>${escapeHtml(labels[rule.field] || rule.field)}</td><td>${escapeHtml(rule.value)}</td><td class="learning-status">${rule.source_is_approved ? 'aus freigegebener Gruppe' : 'lokal gespeichert'}</td><td><button type="button" data-rule-id="${rule.id}">Regel löschen</button></td></tr>`).join('');
      out.querySelectorAll('button[data-rule-id]').forEach(button => button.addEventListener('click', async () => {
        if (!confirm('Diese Lernregel wird lokal gelöscht. Bereits bearbeitete Dokumente bleiben unverändert. Fortfahren?')) return;
        await request(`api/learning-library/${button.dataset.ruleId}`, {method: 'DELETE'});
        await load();
      }));
    } catch (error) {
      state.textContent = `Lernbibliothek konnte nicht geladen werden: ${error.message}`;
    }
  }

  reset.addEventListener('click', async () => {
    if (!confirm('Alle lokalen Lernregeln werden gelöscht. Bereits bearbeitete Dokumente bleiben unverändert. Fortfahren?')) return;
    await request('api/learning-library/reset', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: true})});
    await load();
  });
  load();
})();
