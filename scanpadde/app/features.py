"""Transparent, rule-based page feature extraction. No model or OCR calls occur here."""
import hashlib
import json
import re
from datetime import datetime

FEATURE_VERSION = "phase3-rules-v1"

MONTHS = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
          "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
          "november": 11, "dezember": 12, "january": 1, "february": 2, "march": 3,
          "april": 4, "may": 5, "june": 6, "july": 7, "august": 8, "september": 9,
          "october": 10, "november": 11, "december": 12}

def _norm(value): return re.sub(r"\s+", " ", value.strip()).upper()
def _candidate(value, kind, evidence, box=None, normalized=None):
    return {"value": value, "normalized": normalized or _norm(value), "source": kind,
            "evidence": evidence, "coordinates": box, "rule_version": FEATURE_VERSION}

def normalize_invoice(value):
    return re.sub(r"[\s-]+", "", value).upper()

def normalize_amount(value):
    raw = value.strip().replace("€", "").replace("EUR", "").strip()
    if "," in raw: raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1: raw = raw.replace(".", "")
    try: return f"{float(raw):.2f}"
    except ValueError: return raw

def normalize_iban(value): return re.sub(r"\s+", "", value).upper()

def _words(words_json):
    try: raw = json.loads(words_json or "[]")
    except (TypeError, json.JSONDecodeError): raw = []
    result = []
    for w in raw:
        bbox = w.get("bbox") or {k: w.get(k) for k in ("x", "y", "w", "h")}
        result.append((w.get("text", ""), bbox))
    return result

def extract(text, words_json=None, width=None, height=None, known_entities=None):
    """Return candidates with traceable regex/layout evidence; never probabilities."""
    known_entities = known_entities or {}
    words = _words(words_json)
    lowered = text.lower()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    def matches(pattern, source, normalizer=None):
        result = []
        for match in re.finditer(pattern, text, re.I):
            # Most extraction rules expose their value as capture group 1.
            # Keyword-presence rules such as VAT deliberately do not, so their
            # full match is the only safe, traceable candidate value.
            value = next((group for group in match.groups() if group is not None), match.group(0))
            result.append(_candidate(value, source, match.group(0),
                                     normalized=(normalizer(value) if normalizer else None)))
        return result
    invoices = matches(r"(?:rechnung(?:s)?\s*(?:nummer|nr\.?|no\.?)|invoice(?:\s*(?:no\.?|number))?)\s*[:#]?\s*([A-Z0-9][A-Z0-9 /_-]{2,})", "invoice_label", normalize_invoice)
    # Retail OCR commonly misreads the final letters of ``Belegnummer`` and
    # may put ``Belegdatum`` between its label and the numeric identifier.
    # A long numeric token near that label remains clear, auditable evidence.
    docs = matches(r"(?:beleg(?:nummer|num\w*|nr\.?)|document(?:\s*(?:no\.?|number))?)(?:\s*[:#]?\s*(?!belegdatum\b)([A-Z0-9][A-Z0-9 /_-]*\d[A-Z0-9 /_-]*)|[\s:;,.\-A-Za-zÄÖÜäöüß]{0,50}?(\d{6,}[A-Z0-9/_-]*))", "document_label", normalize_invoice)
    dates = matches(r"\b(\d{1,2}[.]\d{1,2}[.]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})\b", "date")
    for m in re.finditer(r"\b(\d{1,2})\.?\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", lowered, re.I):
        try: normalized = datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).date().isoformat()
        except ValueError: continue
        dates.append(_candidate(m.group(0), "written_date", m.group(0), normalized=normalized))
    amounts = matches(r"\b([0-9]{1,3}(?:[. ][0-9]{3})*(?:,[0-9]{2})|[0-9]+(?:\.[0-9]{2}))\s*(?:€|EUR)\b", "amount", normalize_amount)
    ibans = matches(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]){11,30})\b", "iban", normalize_iban)
    ibans = [x for x in ibans if len(x["normalized"]) >= 15 and re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]+$", x["normalized"])]
    pages = matches(r"(?:seite|page)\s*(\d+)\s*(?:von|of)\s*(\d+)", "page_counter")
    page_counters = [{**x, "count": int(re.search(r"(?:von|of)\s*(\d+)", x["evidence"], re.I).group(1))} for x in pages]
    types = []
    keywords = [("gutschrift", "credit_note"), ("lieferschein", "delivery_note"), ("mahnung", "reminder"),
                ("angebot", "offer"), ("bestellung", "purchase_order"), ("kassenbon", "receipt"),
                ("kontoauszug", "payment_record"), ("rechnung", "invoice"), ("invoice", "invoice")]
    for token, value in keywords:
        if token in lowered: types.append(_candidate(value, "document_type_keyword", token, normalized=value))
    # Coordinates divide visible word boxes into semantic regions. They remain evidence, not inferred certainty.
    regions = {"header": [], "footer": [], "left_address": [], "right_address": []}
    for word, box in words:
        y, x = box.get("y"), box.get("x")
        if y is None or x is None or not height or not width: continue
        if y < height * .25: regions["header"].append(word)
        if y > height * .78: regions["footer"].append(word)
        if y < height * .45 and x < width * .5: regions["left_address"].append(word)
        if y < height * .45 and x >= width * .5: regions["right_address"].append(word)
    suppliers, recipients = [], []
    org_lines = [l for l in lines if re.search(r"\b(GMBH|AG|KG|LTD|LLC|UG|E\.K\.)\b", l, re.I)]
    for index, line in enumerate(lines):
        if line not in org_lines: continue
        candidate = _candidate(line, "organization_line", line)
        context = " ".join(lines[max(0, index - 1):index + 1]).lower()
        if re.search(r"(?:rechnungsempfänger|bill to|kunde|customer|\ban)\s*:?", context):
            candidate["value"] = re.sub(r"^.*?(?:rechnungsempfänger|bill to|kunde|customer|\ban)\s*:?\s*", "", line, flags=re.I)
            candidate["normalized"] = _norm(candidate["value"])
            recipients.append(candidate)
        else: suppliers.append(candidate)
    for alias in known_entities.get("recipient_aliases", []):
        if alias.lower() in lowered: recipients.append(_candidate(alias, "known_recipient", alias))
    for alias in known_entities.get("supplier_aliases", []):
        if alias.lower() in lowered: suppliers.append(_candidate(alias, "known_supplier", alias))
    word_count = len(re.findall(r"\w+", text)); blankness = "blank" if word_count == 0 else "near_blank" if word_count < 6 else "content"
    return {"page_feature_version": FEATURE_VERSION, "probable_document_type": types[:1], "supplier_candidates": suppliers,
            "recipient_candidates": recipients, "invoice_number_candidates": invoices, "document_number_candidates": docs,
            "date_candidates": dates, "amount_candidates": amounts, "vat_tax_candidates": matches(r"(?:mwst|ust|vat)[^\n]{0,30}", "tax"),
            "page_number_candidates": page_counters, "page_count_candidates": page_counters, "iban_bic_candidates": ibans,
            "purchase_order_reference_candidates": matches(r"(?:bestell(?:nummer|nr\.?)|order(?:\s*(?:no\.?|number))?)\s*[:#]?\s*([A-Z0-9/_-]+)", "reference", normalize_invoice),
            "strong_heading_tokens": [l for l in lines[:8] if len(l) < 80 and (l.isupper() or l.lower() in [k[0] for k in keywords])],
            "address_blocks": regions, "footer_header_repetition_signals": {k: " ".join(v)[:180] for k, v in regions.items() if v},
            "blankness": blankness, "low_content_score": word_count, "layout_fingerprint": hashlib.sha256(json.dumps({k: len(v) for k,v in regions.items()}, sort_keys=True).encode()).hexdigest()[:16],
            "text_fingerprint": hashlib.sha256(re.sub(r"\d+", "#", _norm(text)).encode()).hexdigest()[:16]}
