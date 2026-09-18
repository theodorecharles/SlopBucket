import json
import sys
import time

import pytest

from slop import rpc


def fake_codex(tmp_path, monkeypatch, body):
    script = tmp_path / "codex"
    script.write_text(f"#!{sys.executable}\n" + body)
    script.chmod(0o700)
    monkeypatch.setattr(rpc.shutil, "which", lambda _: str(script))


def test_sequential_handshake_and_account_requests(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, '''import json, sys
for line in sys.stdin:
    r=json.loads(line)
    if 'id' not in r: continue
    if r['method']=='initialize': result={'userAgent':'test'}
    elif r['method']=='account/read': result={'account':{'type':'chatgpt'}}
    else: result={'rateLimits':{}}
    print(json.dumps({'id':r['id'], 'result':result}), flush=True)
''')
    with rpc.CodexRPC(tmp_path, timeout=1) as client:
        assert client.call("account/read", {"refreshToken": True})["account"]["type"] == "chatgpt"
        assert client.call("account/rateLimits/read") == {"rateLimits": {}}
    assert client.proc.poll() is not None


def test_partial_line_cannot_hang_timeout(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, "import time, sys\nsys.stdout.write('{'); sys.stdout.flush(); time.sleep(30)\n")
    start = time.monotonic()
    with pytest.raises(rpc.RpcError, match="timed out"):
        with rpc.CodexRPC(tmp_path, timeout=0.1): pass
    assert time.monotonic() - start < 3


def test_sensitive_error_is_classified_but_not_logged(tmp_path, monkeypatch):
    fake_codex(tmp_path, monkeypatch, '''import json, sys
r=json.loads(sys.stdin.readline())
print(json.dumps({'id':r['id'],'error':{'message':'refresh_token_reused SECRET_TOKEN'}}),flush=True)
''')
    with pytest.raises(rpc.RpcError) as caught:
        with rpc.CodexRPC(tmp_path, timeout=1): pass
    assert caught.value.reauth
    assert "SECRET_TOKEN" not in str(caught.value)


@pytest.mark.parametrize("diagnostic,reauth", [("refresh_token_expired SECRET", True), ("connection timeout SECRET", False)])
def test_null_account_classifies_diagnostics_without_exposing_them(tmp_path, monkeypatch, diagnostic, reauth):
    fake_codex(tmp_path, monkeypatch, '''import json, sys
for line in sys.stdin:
    r=json.loads(line)
    if 'id' not in r: continue
    if r['method']=='initialize': result={}
    else:
        print(DIAGNOSTIC, file=sys.stderr, flush=True)
        result={'account':None}
    print(json.dumps({'id':r['id'],'result':result}),flush=True)
'''.replace('DIAGNOSTIC', repr(diagnostic)))
    with rpc.CodexRPC(tmp_path, timeout=1) as client:
        client.call("account/read", {"refreshToken": True})
        error = client.renewal_error()
        assert error.reauth is reauth
        assert "SECRET" not in str(error)
