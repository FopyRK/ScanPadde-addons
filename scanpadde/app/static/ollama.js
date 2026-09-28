(() => {
  const source = document.querySelector('main').dataset.source;
  const button = document.querySelector('#ollama-analyze');
  const apply = document.querySelector('#ollama-apply');
  const state = document.querySelector('#ollama-state');
  const escape = value => String(value ?? '—').replace(/[&<>]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[char]));
  const render = result => {
    if (!result.available) { apply.disabled = true; state.textContent = 'Noch kein KI-Vorschlag vorhanden.'; return; }
    const groups = result.groups.map(group => `Seiten ${group.pages[0]}–${group.pages.at(-1)} · ${escape(group.confidence)} · ${escape(group.reason)}`).join(' | ');
    state.innerHTML = `<span class="ollama-suggestion">KI-Vorschlag (${escape(result.model)}): ${groups}. Bitte gegen die Originale prüfen.</span>`;
    apply.disabled = false;
  };
  const load = async () => {
    try { render(await fetch(`../api/sources/${source}/ollama-suggestion`).then(response => response.json())); }
    catch { state.textContent = 'KI-Vorschlag konnte nicht geladen werden.'; }
  };
  button.addEventListener('click', async () => {
    button.disabled = true;
    state.textContent = 'Lokale KI-Prüfung läuft …';
    try {
      const response = await fetch(`../api/sources/${source}/ollama-analyze`, {method:'POST'});
      if (!response.ok) throw new Error(await response.text());
      await load();
    } catch (error) {
      state.textContent = `KI-Prüfung nicht möglich: ${error.message}`;
    } finally { button.disabled = false; }
  });
  apply.addEventListener('click', async () => {
    if (!window.confirm('Diesen lokalen KI-Vorschlag als Review-Gruppen übernehmen? Originale, OCR und Exporte bleiben unverändert.')) return;
    apply.disabled = true;
    state.textContent = 'Vorschlag wird als überprüfbare Gruppen übernommen …';
    try {
      const response = await fetch(`../api/sources/${source}/apply-ollama-suggestion`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: true})
      });
      if (!response.ok) throw new Error(await response.text());
      window.location.reload();
    } catch (error) {
      state.textContent = `Übernahme nicht möglich: ${error.message}`;
      await load();
    }
  });
  load();
})();
