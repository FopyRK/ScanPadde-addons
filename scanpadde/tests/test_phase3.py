import json, time
from app.features import extract, normalize_amount, normalize_invoice
from app.segmentation import boundary, reprocess_source, group_detail, override, apply_ollama_suggestion, duplicate_candidates

def page(db, source, number, text, words=None):
    now=time.time(); pid=db.execute("INSERT INTO pages(source_file_id,page_number,width,height,status,created_at) VALUES(?,?,?,?,?,?)",(source,number,1000,1400,'ocr_completed',now)).lastrowid
    db.execute("INSERT INTO ocr_results(page_id,engine,engine_version,language,preprocessing_version,rotation,source_type,text,words_json,confidence,image_sha256,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(pid,'remote','1','deu+eng','none',0,'ocr',text,json.dumps(words or []),90,'x'+str(number),'completed',now)); return pid
def source(db, count=2):
    now=time.time(); return db.execute("INSERT INTO source_files(sha256,original_filename,archived_path,mime_type,size_bytes,page_count,status,first_seen_at,last_seen_at,archived_at) VALUES(?,?,?,?,?,?,?,?,?,?)",('s'+str(now),'test.pdf','originals/x','application/pdf',1,count,'ready',now,now,now)).lastrowid

def test_feature_extraction_roles_values_and_boxes():
    words=[{"text":"ACME","x":10,"y":10,"w":30,"h":12},{"text":"GmbH","x":45,"y":10,"w":30,"h":12}]
    f=extract("ACME GmbH\nRechnung Nr: AB- 123\nDatum 01.02.2026\nGesamt 1.234,50 EUR\nIBAN DE89 3704 0044 0532 0130 00\nSeite 1 von 2",json.dumps(words),1000,1400)
    assert f['supplier_candidates'] and f['invoice_number_candidates'][0]['normalized']=='AB123'
    assert normalize_amount('1.234,50')=='1234.50' and f['page_count_candidates'][0]['count']==2
    assert f['address_blocks']['header'] == ['ACME','GmbH']
    assert f['date_candidates'] and f['amount_candidates'][0]['normalized']=='1234.50' and f['iban_bic_candidates']

def test_recipient_role_type_and_blankness():
    f=extract("Supplier GmbH\nRechnungsempfänger: Buyer GmbH\nRECHNUNG")
    assert f['supplier_candidates'][0]['value']=='Supplier GmbH'
    assert f['recipient_candidates'][0]['value']=='Buyer GmbH'
    assert f['probable_document_type'][0]['normalized']=='invoice'
    assert extract('   ')['blankness']=='blank'

def test_feature_extraction_handles_tax_keyword_without_capture_group():
    features = extract('USt 19 Prozent')
    assert features['vat_tax_candidates'][0]['value'].lower().startswith('ust')

def test_feature_extraction_finds_inline_supplier_and_hyphenated_invoice_label():
    features = extract('ALPHA Handel GmbH & Co. KG Rechnung-Nr.: R-123 Datum 01.02.2026')
    assert features['supplier_candidates'][0]['normalized'].endswith('GMBH & CO. KG')
    assert features['invoice_number_candidates'][0]['normalized'] == 'R123'
    assert features['date_candidates'][0]['normalized'] == '01.02.2026'

def test_boundary_switch_and_continuation():
    a=extract('ACME GmbH\nRechnung Nr: A1\nSeite 1 von 2')
    b=extract('ACME GmbH\nRechnung Nr: A1\nSeite 2 von 2')
    c=extract('OTHER GmbH\nRechnung Nr: B2\nSeite 1 von 1')
    assert 'page_counter_continuation' in boundary(a,b)['strong_for_continue']
    assert boundary(b,c)['decision']=='split' and 'supplier_switch' in boundary(b,c)['strong_for_split']

def test_boundary_uses_receipt_number_as_document_identity():
    first = extract('MARKT GmbH\nBelegnummer: R-100\nSeite 1 von 2')
    continuation = extract('MARKT GmbH\nBelegnummer: R-100\nSeite 2 von 2')
    next_receipt = extract('MARKT GmbH\nBelegnummer: R-101\nSeite 1 von 1')
    assert 'same_document_number' in boundary(first, continuation)['strong_for_continue']
    evidence = boundary(continuation, next_receipt)
    assert evidence['decision'] == 'split'
    assert 'document_number_switch' in evidence['strong_for_split']

def test_exact_image_duplicates_are_review_hints_and_can_be_restored(env):
    _, db = env
    sid = source(db, 2)
    page(db, sid, 1, 'ACME GmbH\nRechnung Nr: A-1')
    page(db, sid, 2, 'BETA GmbH\nRechnung Nr: B-2')
    db.execute("UPDATE ocr_results SET image_sha256='same-page-image'")
    first, second = reprocess_source(db, sid)
    assert duplicate_candidates(db, sid) == [{"group_id": second, "duplicate_of": first,
                                              "page_count": 1, "method": "identical_page_images"}]
    override(db, sid, 'hide_duplicate', {'group_id': second})
    assert db.execute("SELECT status FROM document_groups WHERE id=?", (second,)).fetchone()['status'] == 'rejected'
    assert duplicate_candidates(db, sid) == []
    override(db, sid, 'restore_duplicate', {'group_id': second})
    assert db.execute("SELECT status FROM document_groups WHERE id=?", (second,)).fetchone()['status'] == 'review_required'

def test_reconciliation_merges_interleaved_counter_backed_documents_for_review(env):
    _, db = env
    sid = source(db, 4)
    page(db, sid, 1, 'ACME GmbH\\nRechnung Nr: A-1\\nSeite 1 von 2')
    page(db, sid, 2, 'BETA GmbH\\nRechnung Nr: B-1\\nSeite 1 von 2')
    page(db, sid, 3, 'ACME GmbH\\nRechnung Nr: A-1\\nSeite 2 von 2')
    page(db, sid, 4, 'BETA GmbH\\nRechnung Nr: B-1\\nSeite 2 von 2')
    groups = reprocess_source(db, sid)
    assert len(groups) == 2
    first = group_detail(db, groups[0])
    assert [item['page_number'] for item in first['pages']] == [1, 3]
    assert first['status'] == 'review_required'
    assert 'Automatisch zusammengeführt' in first['metadata_json']['grouping_evidence']['effective_value']['value']

def test_feature_extraction_accepts_ocr_variants_of_receipt_label():
    features = extract('Belegnummeı: Belegdatum 2612810610077726')
    assert features['document_number_candidates'][0]['normalized'] == '2612810610077726'

def test_grouping_overrides_and_restart_persistence(env):
    _,db=env; sid=source(db,3); p1=page(db,sid,1,'ACME GmbH\nRechnung Nr: A1\nSeite 1 von 2'); p2=page(db,sid,2,'ACME GmbH\nRechnung Nr: A1\nSeite 2 von 2'); p3=page(db,sid,3,'BETA GmbH\nRechnung Nr: B2')
    ids=reprocess_source(db,sid); assert len(ids)==2
    first=group_detail(db,ids[0]); assert len(first['pages'])==2
    override(db,sid,'metadata',{'group_id':ids[0],'field':'supplier','value':'Human Supplier'})
    assert group_detail(db,ids[0])['metadata_json']['supplier']['effective_value']['value']=='Human Supplier'
    override(db,sid,'split',{'group_id':ids[0],'before_page_id':p2})
    assert db.execute("SELECT count(*) FROM group_overrides").fetchone()[0]==2
    # The SQLite rows are the restart-persistent contract; no OCR job has been created.
    assert db.execute("SELECT count(*) FROM ocr_results").fetchone()[0]==3

def test_merge_move_and_invalid_review_actions_persist(env):
    _, db=env; sid=source(db,5)
    pages=[page(db,sid,n,f'ACME GmbH\nRechnung Nr: A-{n}') for n in range(1,6)]
    ids=reprocess_source(db,sid); assert len(ids)==5
    override(db,sid,'merge',{'group_ids':[ids[0],ids[1]]})
    merged=db.execute("SELECT id FROM document_groups WHERE source_file_id=? AND status='review_required' ORDER BY id DESC",(sid,)).fetchone()[0]
    assert [p['page_number'] for p in group_detail(db,merged)['pages']]==[1,2]
    # Make the requested A=1,2,3 / B=4,5 arrangement, then move page 3.
    override(db,sid,'merge',{'group_ids':[merged,ids[2]]})
    group_a=db.execute("SELECT id FROM document_groups WHERE source_file_id=? AND status='review_required' ORDER BY id DESC",(sid,)).fetchone()[0]
    override(db,sid,'merge',{'group_ids':[ids[3],ids[4]]})
    group_b=db.execute("SELECT id FROM document_groups WHERE source_file_id=? AND status='review_required' ORDER BY id DESC",(sid,)).fetchone()[0]
    override(db,sid,'move_page',{'page_id':pages[2],'target_group_id':group_b})
    assert [p['page_number'] for p in group_detail(db,group_a)['pages']]==[1,2]
    assert [p['page_number'] for p in group_detail(db,group_b)['pages']]==[3,4,5]
    assert db.execute("SELECT count(*) FROM group_overrides WHERE source_file_id=?",(sid,)).fetchone()[0]==4
    # Reprocessing retains human-controlled groups and creates no OCR rows.
    assert reprocess_source(db,sid) and db.execute("SELECT count(*) FROM ocr_results").fetchone()[0]==5
    import pytest
    with pytest.raises(ValueError): override(db,sid,'move_page',{'page_id':99999,'target_group_id':group_b})
    with pytest.raises(ValueError): override(db,sid,'split',{'group_id':group_b,'before_page_id':99999})
    other=source(db,1); page(db,other,1,'OTHER GmbH\nRechnung Nr: O-1'); other_id=reprocess_source(db,other)[0]
    with pytest.raises(ValueError): override(db,sid,'merge',{'group_ids':[group_a,other_id]})

def test_confirmed_ollama_hint_creates_review_groups_without_touching_ocr(env):
    _, db = env; sid = source(db, 3)
    page(db, sid, 1, 'ACME GmbH\\nRechnung Nr: A-1')
    page(db, sid, 2, 'ACME GmbH\\nRechnung Nr: A-1')
    page(db, sid, 3, 'BETA GmbH\\nRechnung Nr: B-2')
    initial = reprocess_source(db, sid)
    rows = db.execute("SELECT p.id,p.page_number,o.text,p.width,p.height,o.words_json FROM pages p JOIN ocr_results o ON o.page_id=p.id WHERE p.source_file_id=? ORDER BY p.page_number", (sid,)).fetchall()
    pages = [{"page_id": row["id"], "page_number": row["page_number"], "features": extract(row["text"], row["words_json"], row["width"], row["height"])} for row in rows]
    groups = [{"pages": [1, 2], "confidence": "low", "reason": "uncertain"}, {"pages": [3], "confidence": "low", "reason": "uncertain"}]
    applied = apply_ollama_suggestion(db, sid, groups, pages, 'local-model', 'digest-a')
    assert len(applied) == 2
    assert all(group_detail(db, group_id)['status'] == 'review_required' for group_id in applied)
    assert [page['page_number'] for page in group_detail(db, applied[0])['pages']] == [1, 2]
    assert db.execute("SELECT count(*) FROM ocr_results").fetchone()[0] == 3
    assert db.execute("SELECT count(*) FROM document_groups WHERE id IN (?,?) AND status='superseded'", initial).fetchone()[0] == 2
    import pytest
    with pytest.raises(ValueError, match='already_applied'):
        apply_ollama_suggestion(db, sid, groups, pages, 'local-model', 'digest-a')

def test_group_detail_suggests_metadata_found_on_a_later_page(env):
    _, db = env; sid = source(db, 2)
    page(db, sid, 1, '')
    page(db, sid, 2, 'ACME GmbH\nRechnung Nr: AB-123')
    group_id = reprocess_source(db, sid)[0]
    detail = group_detail(db, group_id)
    assert detail['metadata_json']['supplier']['effective_value']['value'] == 'ACME GmbH'
    assert detail['metadata_json']['invoice_number']['effective_value']['normalized'] == 'AB123'

def test_approved_metadata_creates_local_reusable_rules(env):
    _, db = env
    first = source(db, 1)
    page(db, first, 1, 'My Supplier\nVorgangsnummer: ZX-99')
    group = reprocess_source(db, first)[0]
    override(db, first, 'metadata', {'group_id': group, 'field': 'supplier', 'value': 'My Supplier'})
    override(db, first, 'metadata', {'group_id': group, 'field': 'invoice_number', 'value': 'ZX-99'})
    # Editing a field alone is intentionally not learning.  The reviewer must
    # release the group before its local pattern can influence later scans.
    assert db.execute("SELECT count(*) FROM learned_metadata_rules").fetchone()[0] == 0
    override(db, first, 'approve', {'group_id': group})
    learned = db.execute("SELECT field,value FROM learned_metadata_rules ORDER BY field").fetchall()
    assert [(row['field'], row['value']) for row in learned] == [('invoice_label', 'Vorgangsnummer'), ('supplier', 'My Supplier')]
    later = source(db, 1)
    page(db, later, 1, 'My Supplier\nVorgangsnummer: ZX-100')
    detail = group_detail(db, reprocess_source(db, later)[0])
    assert detail['metadata_json']['supplier']['effective_value']['value'] == 'My Supplier'
    assert detail['metadata_json']['invoice_number']['effective_value']['normalized'] == 'ZX100'

def test_review_can_reorder_and_exclude_pages_without_deleting_ocr(env):
    _, db = env
    sid = source(db, 3)
    first, second, blank = [page(db, sid, number, text) for number, text in [
        (1, 'ACME GmbH Rechnung Nr: A-1'), (2, 'ACME GmbH Rechnung Nr: A-1'), (3, '')
    ]]
    group = reprocess_source(db, sid)[0]
    override(db, sid, 'reorder_page', {'group_id': group, 'page_id': second, 'direction': 'earlier'})
    assert [item['id'] for item in group_detail(db, group)['pages']][:2] == [second, first]
    override(db, sid, 'exclude_page', {'group_id': group, 'page_id': blank})
    assert [item['id'] for item in group_detail(db, group)['pages']] == [second, first]
    assert db.execute('SELECT count(*) FROM ocr_results WHERE page_id=?', (blank,)).fetchone()[0] == 1

def test_group_name_is_generated_and_can_be_overridden(env):
    _, db = env
    sid = source(db, 1)
    page(db, sid, 1, 'ACME GmbH\nRechnung Nr: A-1')
    group = reprocess_source(db, sid)[0]
    assert group_detail(db, group)['metadata_json']['display_name']['effective_value']['value'] == 'Rechnung · ACME GmbH · A-1'
    override(db, sid, 'metadata', {'group_id': group, 'field': 'display_name', 'value': 'April-Rechnung'})
    assert group_detail(db, group)['metadata_json']['display_name']['effective_value']['value'] == 'April-Rechnung'
