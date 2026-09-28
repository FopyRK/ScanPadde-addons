import json, time
from app.features import extract, normalize_amount, normalize_invoice
from app.segmentation import boundary, reprocess_source, group_detail, override

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

def test_boundary_switch_and_continuation():
    a=extract('ACME GmbH\nRechnung Nr: A1\nSeite 1 von 2')
    b=extract('ACME GmbH\nRechnung Nr: A1\nSeite 2 von 2')
    c=extract('OTHER GmbH\nRechnung Nr: B2\nSeite 1 von 1')
    assert 'page_counter_continuation' in boundary(a,b)['strong_for_continue']
    assert boundary(b,c)['decision']=='split' and 'supplier_switch' in boundary(b,c)['strong_for_split']

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
