"use strict";

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".source-delete");
  if (!button) return;
  const name = button.dataset.sourceName || "diese Quelle";
  if (!window.confirm(`„${name}“ einschließlich lokaler Original-, Seiten- und OCR-Daten endgültig löschen? Freigegebene Dokumente können hier nicht gelöscht werden.`)) return;
  button.disabled = true;
  try {
    const response = await fetch(`api/sources/${button.dataset.sourceId}/delete`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({confirm: true})
    });
    if (!response.ok) throw new Error((await response.json()).detail || "Löschen nicht möglich");
    window.location.reload();
  } catch (error) {
    button.disabled = false;
    window.alert(`Quelle wurde nicht gelöscht: ${error.message}`);
  }
});
