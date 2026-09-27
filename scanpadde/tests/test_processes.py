import os
import subprocess
import sys
import time
from pathlib import Path
import pytest
from pypdf import PdfWriter
from app.db import connection
from app.ingestion import observe
from app.main import Runtime
from app.paths import Paths

HELPER = Path(__file__).with_name("process_helper.py")

@pytest.mark.parametrize("action,exitcode",[("crash_copy",75),("crash_published",75)])
def test_real_process_crash_and_recovery(env, action, exitcode):
    paths,db=env
    p=paths.guard("inbox/crash.pdf")
    w=PdfWriter();w.add_blank_page(100,200);w.write(p)
    before=p.read_bytes()
    observe(db,paths,time.time()-16);observe(db,paths,time.time()-1)
    root=paths.data.parent
    result=subprocess.run([sys.executable,str(HELPER),str(root),action],timeout=20,capture_output=True)
    assert result.returncode==exitcode, result.stderr.decode(errors="replace")
    assert db.execute("SELECT state FROM jobs").fetchone()[0]=="running"
    if action=="crash_copy":
        assert all(p.name.startswith(".archive-") for p in paths.guard("originals").iterdir())
    else:
        assert len([p for p in paths.guard("originals").iterdir() if not p.name.startswith(".")])==1
    result=subprocess.run([sys.executable,str(HELPER),str(root),"recover"],timeout=20,capture_output=True)
    assert result.returncode==0,result.stderr.decode(errors="replace")
    assert db.execute("SELECT count(*) FROM source_files").fetchone()[0]==1
    assert db.execute("SELECT count(*) FROM pages").fetchone()[0]==1
    # Ingestion/enumeration plus the Phase-2A render/OCR page checkpoints.
    assert db.execute("SELECT count(*) FROM jobs WHERE state='completed'").fetchone()[0]==4
    assert p.read_bytes()==before
    assert not list(paths.guard("originals").glob(".archive-*"))

def test_live_process_prevents_recovery(env):
    paths,db=env
    root=paths.data.parent
    proc=subprocess.Popen([sys.executable,str(HELPER),str(root),"wait"],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        assert proc.stdout.readline().strip()==b"READY"
        result=subprocess.run([sys.executable,str(HELPER),str(root),"recover"],timeout=10,capture_output=True)
        assert result.returncode!=0
    finally:
        proc.terminate();proc.wait(timeout=10)

def test_real_stability_window_and_clean_stop(tmp_path):
    paths=Paths(tmp_path/"share/scanpadde",tmp_path/"data")
    runtime=Runtime(paths)
    runtime.start()
    try:
        w=PdfWriter();w.add_blank_page(100,200);w.write(paths.guard("inbox/stable.pdf"))
        start=time.monotonic()
        completed=False
        while time.monotonic()-start<25:
            with connection(runtime.db_path) as db:
                completed=db.execute("SELECT count(*) FROM source_files WHERE status='ready'").fetchone()[0]==1
                if time.monotonic()-start<14:
                    assert db.execute("SELECT count(*) FROM source_files").fetchone()[0]==0
            if completed:
                break
            time.sleep(.2)
        assert completed
        assert time.monotonic()-start>=15
    finally:
        runtime.close()
    assert runtime.state=="stopped"
    assert not runtime.worker.is_alive()

@pytest.mark.skipif(os.name != "nt",reason="Windows junction regression")
def test_windows_junction_rejected(env,tmp_path):
    paths,_=env
    outside=tmp_path/"outside";outside.mkdir()
    junction=paths.root/"inbox/junction"
    # Fixed test paths only; cmd is used solely for mklink, never deletion/moving.
    result=subprocess.run(["cmd","/c","mklink","/J",str(junction),str(outside)],capture_output=True)
    assert result.returncode==0,result.stderr.decode(errors="replace")
    from app.paths import UnsafePath
    with pytest.raises(UnsafePath):
        paths.guard(junction/"test.pdf")
    junction.rmdir()
