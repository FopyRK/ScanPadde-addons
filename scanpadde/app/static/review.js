(() => {
  const main = document.querySelector('main');
  const id = main.dataset.source;
  const out = document.querySelector('#groups');
  const state = document.querySelector('#state');
  const esc = value => String(value ?? '—').replace(/[&<>]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[char]));
  const metadataFields = [['supplier', 'Lieferant'], ['invoice_number', 'Rechnungsnr.'], ['document_type', 'Dokumenttyp']];
  const statusText = {proposed: 'vorgeschlagen', review_required: 'Prüfung erforderlich', approved: 'freigegeben', rejected: 'verworfen'};
  const preview = document.createElement('dialog');
  preview.className = 'seitenvorschau';
  preview.innerHTML = '<button class="seitenvorschau-schliessen" type="button">Schließen</button><p class="seitenvorschau-titel"></p><img alt="Vergrößerte Dokumentseite">';
  document.body.append(preview);
  preview.querySelector('button').onclick = () => preview.close();
  preview.addEventListener('click', event => { if (event.target === preview) preview.close(); });

  function showPreview(page) {
    preview.querySelector('.seitenvorschau-titel').textContent = `Seite ${page.page_number}`;
    preview.querySelector('img').src = `../api/pages/${page.id}/thumbnail`;
    preview.showModal();
  }

  async function call(path, method, body) {
    const response = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!response.ok) throw new Error(await response.text());
    return response.json();
  }

  async function load() {
    let groups = await fetch(`../api/sources/${id}/groups`).then(response => response.json());
    if (!groups.length || groups.some(group => group.algorithm_version !== 'phase3-segmentation-v2' && group.algorithm_version !== 'ollama-review-apply-v1')) {
      await call(`../api/sources/${id}/analyze`, 'POST', {});
      groups = await fetch(`../api/sources/${id}/groups`).then(response => response.json());
    }
    const evidence = await fetch(`../api/groups/${groups[0]?.id}/evidence`).then(response => response.ok ? response.json() : []);
    state.textContent = `${groups.length} vorgeschlagene Gruppen`;
    out.innerHTML = '';
    for (const group of groups) {
      const detail = await fetch(`../api/groups/${group.id}`).then(response => response.json());
      const metadata = detail.metadata_json;
      const card = document.createElement('section');
      card.className = 'group';
      card.dataset.groupId = group.id;
      const pageCards = detail.pages.map(page => `<article class="page-card" data-page-id="${page.id}"><img class="thumbnail" src="../api/pages/${page.id}/thumbnail" alt="Vorschau Seite ${page.page_number}"><strong>Seite ${page.page_number}</strong><p class="ocr-snippet">OCR: ${esc((page.ocr_snippet || '').slice(0, 100))}</p></article>`).join('');
      const boundaries = detail.pages.slice(0, -1).map(page => {
        const item = evidence.find(entry => entry.after_page_id === page.id);
        return `<p class="boundary">Trennung nach Seite ${page.page_number}: ${esc(item?.evidence_json || 'keine Hinweise')}</p>`;
      }).join('');
      card.innerHTML = `<h2>Gruppe ${group.id} · <span class="group-status">${esc(statusText[group.status] || group.status)}</span></h2><p class="pages">${detail.pages.map(page => `Seite ${page.page_number}`).join(', ')}</p><div class="page-cards">${pageCards}</div><p class="metadata">Lieferant: <span data-field="supplier">${esc(metadata.supplier.effective_value?.value)}</span> · Rechnungsnr.: <span data-field="invoice_number">${esc(metadata.invoice_number.effective_value?.value)}</span> · Typ: <span data-field="document_type">${esc(metadata.document_type.effective_value?.value)}</span></p>${boundaries}<button data-a="split">Vor Seite trennen …</button> <button data-a="merge">Mit vorheriger Gruppe zusammenführen</button> <button data-a="move">Seite verschieben …</button> ${metadataFields.map(([field, label]) => `<button data-a="edit" data-field="${field}">${label} bearbeiten</button>`).join(' ')} <button data-a="approve">Gruppe freigeben</button> <button data-a="review">Prüfung erforderlich markieren</button>`;
      card.onclick = async event => {
        const pageCard = event.target.closest('.page-card');
        if (pageCard) {
          const page = detail.pages.find(item => item.id === Number(pageCard.dataset.pageId));
          if (page) showPreview(page);
          return;
        }
        const action = event.target.dataset.a;
        if (!action) return;
        try {
          if (action === 'approve' || action === 'review') {
            await call(`../api/sources/${id}/${action === 'approve' ? 'approve' : 'needs-review'}`, 'POST', {group_id: group.id});
          } else if (action === 'edit') {
            const field = event.target.dataset.field;
            const value = prompt(event.target.textContent, metadata[field].effective_value?.value || '');
            if (value !== null) await call(`../api/sources/${id}/metadata`, 'PATCH', {group_id: group.id, field, value});
          } else if (action === 'split') {
            const number = prompt('Vor welcher Seitennummer soll getrennt werden?');
            const page = detail.pages.find(item => item.page_number === Number(number));
            if (page) await call(`../api/sources/${id}/split`, 'POST', {group_id: group.id, before_page_id: page.id});
          } else if (action === 'merge') {
            const previous = groups[groups.indexOf(group) - 1];
            if (previous) await call(`../api/sources/${id}/merge`, 'POST', {group_ids: [previous.id, group.id]});
          } else if (action === 'move') {
            const number = prompt('Welche Seitennummer soll verschoben werden?');
            const target = prompt('Ziel-Gruppennummer eingeben:');
            const page = detail.pages.find(item => item.page_number === Number(number));
            if (page && target) await call(`../api/sources/${id}/move-page`, 'POST', {page_id: page.id, target_group_id: Number(target)});
          }
          await load();
        } catch (error) {
          alert(error.message);
        }
      };
      out.append(card);
    }
  }
  load().catch(error => state.textContent = `Prüfungsfehler: ${error.message}`);
})();
