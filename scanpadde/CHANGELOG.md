# Changelog

## 0.1.5 — 2026-09-27

- Fix review-page asset and API paths under Home Assistant Ingress.

## 0.1.4 — 2026-09-27

- Add a local, one-time Home Assistant release hand-off for the database, OCR cache and remote-OCR configuration.
- The hand-off uses SQLite's consistent backup API and does not send content outside the local installation.

## 0.1.3 — 2026-09-27

- Makes the document-group analysis and review entry visible from the source overview.
- Preserves the existing review-first workflow: no automatic export, rename, booking, or approval.
- Adds a release path for the separate Home Assistant Add-on repository.

## 0.1.0 — 2026-09-26

Phase-1A-Grundgerüst: SQLite-Schema 1, persistente Inbox/Queue, SHA-256-Originalarchiv,
PDF-/Bild-Seitenstruktur, Ingress-Dashboard und Lese-API, synthetische Tests.
Container-/HA-Validierung noch ausstehend. Keine produktive Installation.
## 0.1.2

- Adds Phase-3A page features, transparent boundary evidence, persistent document groups and human review overrides.
- Adds the review view and read/write grouping APIs; no OCR, export, booking, Ollama, or real-document processing is added.
