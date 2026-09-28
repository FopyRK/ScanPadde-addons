"""Deterministic grouping with explicit evidence and auditable human overrides."""
import json, time
from .db import transaction
from .features import extract, FEATURE_VERSION

ALGORITHM_VERSION = "phase3-segmentation-v2"
def _values(f, name): return {x["normalized"] for x in f.get(name, []) if x.get("normalized")}
def _first(f, name): return (f.get(name) or [None])[0]

def _document_ids(features):
    """Document numbers and invoice numbers are equally valid identity evidence.

    Retail receipts often use a ``Belegnummer`` rather than a
    ``Rechnungsnummer``.  Treating only the latter as an identity caused the
    real-review pipeline to join distinct receipts from the same supplier.
    """
    return _values(features, "invoice_number_candidates") | _values(features, "document_number_candidates")

def _first_document_id(features):
    return _first_available(features, "invoice_number_candidates") or _first_available(features, "document_number_candidates")

def _first_available(features, name):
    """Use the first traceable candidate across a complete document group."""
    for feature in features:
        candidate = _first(feature, name)
        if candidate:
            return candidate
    return None

def boundary(left, right):
    e = {"strong_for_split": [], "medium_for_split": [], "weak_for_split": [], "strong_for_continue": [], "medium_for_continue": [], "conflicts": []}
    ls, rs = _values(left, "supplier_candidates"), _values(right, "supplier_candidates")
    li, ri = _document_ids(left), _document_ids(right)
    if ls and rs and ls.isdisjoint(rs): e["strong_for_split"].append("supplier_switch")
    if li and ri and li.isdisjoint(ri): e["strong_for_split"].append("document_number_switch")
    if left["blankness"] != "content" or right["blankness"] != "content": e["weak_for_split"].append("blank_separator")
    if left["layout_fingerprint"] != right["layout_fingerprint"]: e["medium_for_split"].append("layout_change")
    if left["footer_header_repetition_signals"].get("header") == right["footer_header_repetition_signals"].get("header") and left["footer_header_repetition_signals"].get("header"): e["medium_for_continue"].append("same_header")
    if ls and ls == rs: e["strong_for_continue"].append("same_supplier")
    if li and li == ri: e["strong_for_continue"].append("same_document_number")
    lp, rp = _first(left, "page_number_candidates"), _first(right, "page_number_candidates")
    if rp and rp.get("value") == "1": e["strong_for_split"].append("new_page_1")
    if lp and rp and lp.get("count") == rp.get("count"):
        try:
            if int(rp["value"]) == int(lp["value"]) + 1: e["strong_for_continue"].append("page_counter_continuation")
        except ValueError: pass
    # A shared supplier is compatible with a new invoice. Only a page-counter
    # continuation can contradict a strong new-document signal.
    if e["strong_for_split"] and "page_counter_continuation" in e["strong_for_continue"]:
        e["conflicts"].append("split_and_continue_evidence")
    decision = "review" if e["conflicts"] else "split" if e["strong_for_split"] else "continue"
    confidence = "high" if (decision == "split" and len(e["strong_for_split"]) >= 1 and not e["conflicts"]) or (decision == "continue" and len(e["strong_for_continue"]) >= 1) else "medium" if decision != "review" else "low"
    return {**e, "decision": decision, "decision_reason": (e["conflicts"] or e["strong_for_split"] or e["strong_for_continue"] or ["insufficient_evidence"])[0], "confidence": confidence}

def _metadata(features):
    fields = {"supplier": "supplier_candidates", "recipient": "recipient_candidates", "invoice_number": "invoice_number_candidates", "document_type": "probable_document_type", "invoice_date": "date_candidates", "gross": "amount_candidates", "IBAN": "iban_bic_candidates"}
    metadata = {key: {"auto_value": _first_available(features, source), "human_value": None,
                      "effective_value": _first_available(features, source), "evidence_pages": []}
                for key, source in fields.items()}
    # The review UI intentionally has one compact number field.  Populate it
    # with a receipt/document number when the document has no invoice number.
    if metadata["invoice_number"]["auto_value"] is None:
        document_id = _first_document_id(features)
        metadata["invoice_number"] = {"auto_value": document_id, "human_value": None,
                                      "effective_value": document_id, "evidence_pages": []}
    return metadata

def _enrich_metadata(metadata, features):
    """Fill only empty automatic fields in a review response, never overrides.

    A scan may begin with a blank reverse side or place a document number on a
    later page.  Returning these group-wide OCR candidates makes them visible
    as suggestions while leaving the persisted review decision untouched.
    """
    inferred = _metadata(features)
    for field, value in metadata.items():
        if value.get("human_value") is None and value.get("effective_value") is None:
            metadata[field] = inferred[field]
    return metadata

def reprocess_source(db, source_id):
    # A human intervention (including approval) is authoritative. Retain the
    # auditable grouping until a reviewer deliberately changes it again.
    active = db.execute("SELECT id FROM document_groups WHERE source_file_id=? AND status NOT IN ('superseded','rejected') ORDER BY id", (source_id,)).fetchall()
    if active and db.execute("SELECT 1 FROM group_overrides WHERE source_file_id=? LIMIT 1", (source_id,)).fetchone():
        return [g["id"] for g in active]
    rows = db.execute("""SELECT p.*, o.text, o.words_json FROM pages p LEFT JOIN ocr_results o ON o.id=(SELECT id FROM ocr_results WHERE page_id=p.id AND status='completed' ORDER BY created_at DESC LIMIT 1) WHERE p.source_file_id=? ORDER BY p.page_number""", (source_id,)).fetchall()
    if not rows: return []
    features = []
    with transaction(db):
        for row in rows:
            f = extract(row["text"] or "", row["words_json"], row["width"], row["height"])
            db.execute("INSERT INTO page_features(page_id,feature_version,data_json,created_at) VALUES(?,?,?,?) ON CONFLICT(page_id) DO UPDATE SET feature_version=excluded.feature_version,data_json=excluded.data_json,created_at=excluded.created_at", (row["id"], FEATURE_VERSION, json.dumps(f), time.time()))
            features.append(f)
        for g in db.execute("SELECT id FROM document_groups WHERE source_file_id=? AND status IN ('proposed','review_required')", (source_id,)): db.execute("UPDATE document_groups SET status='superseded' WHERE id=?", (g["id"],))
        revision = (db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM document_groups WHERE source_file_id=?", (source_id,)).fetchone()[0])
        starts, evidences = [0], []
        for i in range(len(rows)-1):
            # Scanner duplex output frequently puts a blank reverse side
            # between two content pages.  Compare the next content page to
            # the preceding content page as well, so a new receipt number is
            # not hidden by that blank separator.  The split still happens at
            # the new content page, preserving the reverse side with the
            # preceding document for review.
            left_index = i
            if features[i]["blankness"] != "content" and features[i + 1]["blankness"] == "content":
                for previous in range(i - 1, -1, -1):
                    if features[previous]["blankness"] == "content":
                        left_index = previous
                        break
            ev = boundary(features[left_index], features[i+1]); evidences.append(ev)
            db.execute("INSERT OR REPLACE INTO boundary_evidence VALUES(?,?,?,?,?)", (source_id, rows[i]["id"], ALGORITHM_VERSION, json.dumps(ev), time.time()))
            if ev["decision"] == "split": starts.append(i+1)
        starts.append(len(rows))
        groups=[]
        for a,b in zip(starts, starts[1:]):
            fs = features[a:b]; status = "review_required" if any(ev["decision"] == "review" for ev in evidences[a:b]) or len(_values(fs[0], "supplier_candidates")) == 0 and not _document_ids(fs[0]) else "proposed"
            gid = db.execute("INSERT INTO document_groups(source_file_id,revision,status,algorithm_version,metadata_json,created_at) VALUES(?,?,?,?,?,?)", (source_id,revision,status,ALGORITHM_VERSION,json.dumps(_metadata(fs)),time.time())).lastrowid
            for seq, row in enumerate(rows[a:b], 1): db.execute("INSERT INTO group_pages(group_id,page_id,sequence) VALUES(?,?,?)", (gid,row["id"],seq))
            groups.append(gid)
    return groups

def group_detail(db, group_id):
    group = db.execute("SELECT * FROM document_groups WHERE id=?", (group_id,)).fetchone()
    if not group: return None
    result=dict(group); result["metadata_json"]=json.loads(result["metadata_json"])
    result["pages"]=[dict(r) for r in db.execute("""SELECT p.*,
        (SELECT text FROM ocr_results WHERE page_id=p.id AND status='completed' ORDER BY created_at DESC LIMIT 1) AS ocr_snippet,
        (SELECT words_json FROM ocr_results WHERE page_id=p.id AND status='completed' ORDER BY created_at DESC LIMIT 1) AS ocr_words_json
        FROM group_pages gp JOIN pages p ON p.id=gp.page_id WHERE gp.group_id=? ORDER BY gp.sequence""", (group_id,))]
    features = [extract(page["ocr_snippet"] or "", page["ocr_words_json"], page["width"], page["height"])
                for page in result["pages"]]
    result["metadata_json"] = _enrich_metadata(result["metadata_json"], features)
    return result

def apply_ollama_suggestion(db, source_id, groups, pages, model, input_digest):
    """Turn a confirmed, local-only hint into review groups.

    This deliberately does not approve, export, rename, or alter originals/OCR.
    The full ordered partition is validated again at the mutation boundary.
    """
    page_numbers = [page["page_number"] for page in pages]
    proposed = [number for group in groups for number in group.get("pages", [])]
    if not groups or proposed != page_numbers:
        raise ValueError("invalid_ollama_suggestion")
    by_number = {page["page_number"]: page for page in pages}
    if len(by_number) != len(pages):
        raise ValueError("invalid_ollama_suggestion")
    for group in groups:
        numbers = group.get("pages")
        if (not isinstance(numbers, list) or not numbers or numbers != sorted(numbers)
                or numbers != list(range(numbers[0], numbers[-1] + 1))):
            raise ValueError("invalid_ollama_suggestion")
    with transaction(db):
        previous = db.execute("SELECT payload_json FROM group_overrides WHERE source_file_id=? AND action='apply_ollama_suggestion'", (source_id,)).fetchall()
        if any(json.loads(row["payload_json"]).get("input_digest") == input_digest for row in previous):
            raise ValueError("ollama_suggestion_already_applied")
        db.execute("INSERT INTO group_overrides(source_file_id,action,payload_json,created_at) VALUES(?,?,?,?)",
                   (source_id, "apply_ollama_suggestion", json.dumps({"model": model, "input_digest": input_digest,
                       "groups": [{"pages": group["pages"], "confidence": group.get("confidence", "low"),
                                   "reason": group.get("reason", "uncertain")} for group in groups]}), time.time()))
        db.execute("UPDATE document_groups SET status='superseded' WHERE source_file_id=? AND status NOT IN ('superseded','rejected')", (source_id,))
        revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM document_groups WHERE source_file_id=?", (source_id,)).fetchone()[0]
        group_ids = []
        for group in groups:
            group_pages = [by_number[number] for number in group["pages"]]
            group_id = db.execute("INSERT INTO document_groups(source_file_id,revision,status,algorithm_version,metadata_json,created_at) VALUES(?,?,?,?,?,?)",
                                  (source_id, revision, "review_required", "ollama-review-apply-v1",
                                   json.dumps(_metadata([page["features"] for page in group_pages])), time.time())).lastrowid
            for sequence, page in enumerate(group_pages, 1):
                db.execute("INSERT INTO group_pages(group_id,page_id,sequence) VALUES(?,?,?)", (group_id, page["page_id"], sequence))
            group_ids.append(group_id)
    return group_ids

def override(db, source_id, action, payload):
    with transaction(db):
        db.execute("INSERT INTO group_overrides(source_file_id,action,payload_json,created_at) VALUES(?,?,?,?)", (source_id,action,json.dumps(payload),time.time()))
        if action == "metadata":
            g=db.execute("SELECT * FROM document_groups WHERE id=?", (payload["group_id"],)).fetchone()
            if not g or g["source_file_id"] != source_id: raise ValueError("group_not_found")
            m=json.loads(g["metadata_json"]); field=payload["field"]
            if field not in m: raise ValueError("metadata_field_invalid")
            m[field]["human_value"]={"value":payload["value"],"source":"human_override"}; m[field]["effective_value"]=m[field]["human_value"]
            db.execute("UPDATE document_groups SET metadata_json=?,status='review_required' WHERE id=?", (json.dumps(m),g["id"]))
        elif action == "approve": db.execute("UPDATE document_groups SET status='approved' WHERE id=? AND source_file_id=?", (payload["group_id"],source_id))
        elif action == "needs_review": db.execute("UPDATE document_groups SET status='review_required' WHERE id=? AND source_file_id=?", (payload["group_id"],source_id))
        elif action == "split":
            g = db.execute("SELECT * FROM document_groups WHERE id=?", (payload["group_id"],)).fetchone()
            pages = db.execute("SELECT page_id FROM group_pages WHERE group_id=? ORDER BY sequence", (payload["group_id"],)).fetchall()
            split_at = next((i for i, p in enumerate(pages) if p["page_id"] == payload["before_page_id"]), None)
            if not g or g["source_file_id"] != source_id or split_at in (None, 0): raise ValueError("invalid_split")
            db.execute("UPDATE document_groups SET status='superseded' WHERE id=?", (g["id"],))
            for part in (pages[:split_at], pages[split_at:]):
                gid = db.execute("INSERT INTO document_groups(source_file_id,revision,status,algorithm_version,metadata_json,parent_group_id,created_at) VALUES(?,?,?,?,?,?,?)", (source_id,g["revision"] + 1,"review_required",ALGORITHM_VERSION,g["metadata_json"],g["id"],time.time())).lastrowid
                for seq, page in enumerate(part, 1): db.execute("INSERT INTO group_pages VALUES(?,?,?)", (gid,page["page_id"],seq))
        elif action == "merge":
            ids = payload.get("group_ids", [])
            groups = db.execute("SELECT * FROM document_groups WHERE id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall() if ids else []
            if len(groups) != 2 or any(g["source_file_id"] != source_id for g in groups): raise ValueError("invalid_merge")
            all_pages = db.execute("SELECT gp.page_id,p.page_number FROM group_pages gp JOIN pages p ON p.id=gp.page_id WHERE gp.group_id IN (?,?) ORDER BY p.page_number", ids).fetchall()
            for g in groups: db.execute("UPDATE document_groups SET status='superseded' WHERE id=?", (g["id"],))
            gid=db.execute("INSERT INTO document_groups(source_file_id,revision,status,algorithm_version,metadata_json,parent_group_id,created_at) VALUES(?,?,?,?,?,?,?)", (source_id,max(g["revision"] for g in groups)+1,"review_required",ALGORITHM_VERSION,groups[0]["metadata_json"],groups[0]["id"],time.time())).lastrowid
            for seq,p in enumerate(all_pages,1): db.execute("INSERT INTO group_pages VALUES(?,?,?)",(gid,p["page_id"],seq))
        elif action == "move_page":
            page_id, target = payload.get("page_id"), payload.get("target_group_id")
            g=db.execute("SELECT source_file_id FROM document_groups WHERE id=?",(target,)).fetchone()
            source=db.execute("SELECT gp.group_id FROM group_pages gp JOIN document_groups dg ON dg.id=gp.group_id WHERE gp.page_id=? AND dg.source_file_id=? AND dg.status NOT IN ('superseded','rejected')",(page_id,source_id)).fetchone()
            if not g or g["source_file_id"] != source_id or not source: raise ValueError("invalid_move")
            if source["group_id"] == target: raise ValueError("invalid_move")
            db.execute("DELETE FROM group_pages WHERE group_id=? AND page_id=?",(source["group_id"],page_id))
            db.execute("INSERT INTO group_pages VALUES(?,?,10000)",(target,page_id))
            for group_id in (source["group_id"], target):
                ordered=db.execute("SELECT gp.page_id FROM group_pages gp JOIN pages p ON p.id=gp.page_id WHERE gp.group_id=? ORDER BY p.page_number",(group_id,)).fetchall()
                # move out of the UNIQUE(group_id,sequence) range before compacting it
                db.execute("UPDATE group_pages SET sequence=sequence+10000 WHERE group_id=?",(group_id,))
                for seq,row in enumerate(ordered,1): db.execute("UPDATE group_pages SET sequence=? WHERE group_id=? AND page_id=?",(seq,group_id,row["page_id"]))
