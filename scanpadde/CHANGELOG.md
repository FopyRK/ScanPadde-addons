# Changelog

## 0.1.38 — 2026-09-28

- Add Phase 4A: approved-review PDF materialisation with persistent export
  records, atomic validation and SHA-256, deterministic safe names, Ingress
  download, idempotency, and restart recovery. Original archives and OCR are
  never modified; a later group edit forks a review revision and supersedes
  the older export.

## 0.1.37 — 2026-09-28

- Make the ScanPadde Ingress entry available to normal Home Assistant users.
  This changes only the app-panel visibility; app configuration, Home
  Assistant administration, and configured secrets remain restricted.

## 0.1.36 — 2026-09-28

- Fix delivery of the Drive-connection setup script so the private `rclone.conf` upload form works in the ScanPadde interface.

## 0.1.35 — 2026-09-28

- Add a Drive-connection upload in the ScanPadde interface. It stores only a validated `rclone.conf` in the add-on's private configuration mount, never in the shared Home Assistant configuration folder, Git, SQLite, or the status API.

## 0.1.34 — 2026-09-28

- Allow a locally confirmed deletion of unreviewed test sources, including their local original, rendered-page, and OCR records. Sources containing approved documents remain protected.

## 0.1.33 — 2026-09-28

- Detect exact duplicate document groups from their ordered local page-image hashes, flag them for review, and allow a duplicate to be hidden or restored without deleting originals or OCR.

## 0.1.32 — 2026-09-28

- Add a local, manageable learning library for confirmed supplier aliases and invoice-label patterns. Rules are promoted on approval and can be removed individually or reset without changing documents, OCR, or exports.

## 0.1.31 — 2026-09-28

- Keep the reviewer at the current position after edits, move approved groups below pending work, and add a persistent “Zur Übersicht” link at the top of the review page.

## 0.1.30 — 2026-09-28

- Add a selectable, copyable local OCR-text panel for each review page, so a reviewer can copy a number or date directly into the metadata form without transcribing it from the image.

## 0.1.29 — 2026-09-28

- Reconcile interleaved proposed groups when supplier and document number match and their distinct page counters share one total; the combined result is always marked `review_required` and keeps its physical page order.
- Do not let a coincidental page-counter sequence override an explicit document-number change.

## 0.1.28 — 2026-09-28

- Add an opt-in Google Drive intake through a local rclone profile: files are copied to the local inbox first and moved in Drive only after local hash archival and complete OCR.
- Keep Drive OAuth credentials only in the Supervisor-provided add-on configuration directory; never store them in Git, SQLite, or the status API.

## 0.1.27 — 2026-09-28

- Move fully processed inbox files into a local processed archive only after hash archival and complete OCR; incomplete and failed inputs remain in the inbox.

## 0.1.26 — 2026-09-28

- Show each source's document names and review progress in the overview; generate readable names from document type, supplier, and invoice number, with an optional manual name override.

## 0.1.25 — 2026-09-28

- Allow reviewers to reorder pages within a group and to hide a blank page from that group without deleting the original or OCR record.

## 0.1.24 — 2026-09-28

- Learn locally confirmed supplier aliases and labelled invoice-number formats from manual review entries; no OCR text, documents, or rules leave ScanPadde.

## 0.1.23 — 2026-09-28

- Detect supplier legal forms in inline OCR, accept hyphenated invoice labels,
  and show the proposed invoice or document date in the review.

## 0.1.22 — 2026-09-28

- Open each review-page preview in a readable, closable full-page overlay
  without reloading the review or changing any document data.

## 0.1.21 — 2026-09-28

- Translate all review controls, statuses, prompts, and page action labels to
  German.

## 0.1.20 — 2026-09-28

- Show supplier, document number, and other OCR metadata suggestions from any
  page in a review group, including documents that begin with a blank reverse.

## 0.1.19 — 2026-09-28

- Add an explicit, confirmed “Vorschlag übernehmen” action for local Ollama
  grouping hints. It creates only `review_required` groups and preserves
  originals, OCR, exports, and the local audit trail.

## 0.1.18 — 2026-09-28

- Use local page positions inside each bounded Ollama request, then map the
  review-only suggestion safely back to the source pages.

## 0.1.17 — 2026-09-28

- Split larger local Ollama grouping requests into bounded page chunks before
  joining their ordered, review-only suggestions.

## 0.1.16 — 2026-09-28

- Use a compact local-model start-page proposal built from extracted OCR
  features, avoiding long OCR excerpts and verbose group JSON.

## 0.1.15 — 2026-09-28

- Further compact the local Ollama page evidence and cap its structured response
  so grouping remains usable on a small private-LAN model.

## 0.1.14 — 2026-09-28

- Bound the local Ollama prompt to compact page evidence and its response to a
  small structured grouping result, so large review stacks remain practical.

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
