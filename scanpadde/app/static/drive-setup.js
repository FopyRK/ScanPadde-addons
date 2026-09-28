(() => {
  const form = document.querySelector("#drive-config-form");
  if (!form) return;
  const input = document.querySelector("#drive-config-file");
  const status = document.querySelector("#drive-config-status");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = input.files && input.files[0];
    if (!file) return;
    status.textContent = "Speichere die Drive-Verbindung …";
    try {
      const response = await fetch("api/drive/config", {method: "POST", body: file});
      if (!response.ok) throw new Error("upload_failed");
      status.textContent = "Drive-Verbindung gespeichert. Der Import wird nun automatisch geprüft.";
      input.value = "";
    } catch (_) {
      status.textContent = "Die Drive-Verbindung konnte nicht gespeichert werden. Bitte Add-on-Einstellungen prüfen.";
    }
  });
})();
