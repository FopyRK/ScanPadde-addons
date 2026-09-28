(() => {
  const source = document.querySelector('main').dataset.source;
  const button = document.querySelector('#ollama-analyze');
  const state = document.querySelector('#ollama-state');
  const escape = value => String(value ?? '—').replace(/[&<>]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[char]));
  const render = result => {
    if (!result.available) { state.textContent = 'Noch kein KI-Vorschlag vorhanden.'; return; }
    const groups = result.groups.map(group => `Seiten ${group.pages[0]}–${group.pages.at(-1)} · ${escape(group.confidence)} · ${escape(group.reason)}`).join(' | ');
    state.innerHTML = `<span class="ollama-suggestion">KI-Vorschlag (${escape(result.model)}): ${groups}. Bitte gegen die Originale prüfen.</span>`;
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
  load();
})();
