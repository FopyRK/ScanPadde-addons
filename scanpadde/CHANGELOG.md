# Changelog

## 0.1.13 — 2026-09-28

- Keep the short connection timeout for local Ollama while allowing the
  separately bounded inference-response timeout.

## 0.1.12 — 2026-09-28

- Preserve explicitly configured optional local-analysis settings when the
  one-time hand-off repairs legacy default OCR fields.

## 0.1.11 — 2026-09-28

- Add an optional, private-LAN Ollama review pass. It only proposes contiguous
  page groups from existing OCR, stores no OCR text or model prose, and never
  approves, exports, renames, or changes existing groups automatically.

## 0.1.10 — 2026-09-28

- Add a remote-only, cache-aware “OCR nachholen” action for sources archived before OCR was configured.

## 0.1.9 — 2026-09-28

- Recognize common OCR variants of receipt-number labels and compare document identifiers across blank reverse sides.
- Refresh proposed groups when the grouping algorithm changes; human review overrides remain authoritative.

## 0.1.8 — 2026-09-28

- Treat labelled receipt/document numbers as document identity evidence during review grouping.
- Show a labelled receipt/document number in the compact review number field when no invoice number exists.

## 0.1.7 — 2026-09-28

- Fix review analysis for pages containing VAT/tax labels, which previously caused a server error.

## 0.1.6 — 2026-09-28

- Repair migration of a previously configured remote-OCR worker when Home Assistant has created only default release options.
- Never overwrite an explicit release configuration.

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
