"""A dedicated service viewer can read through HTTP without a human UI key."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from chemclaw_lab import auth, clients, db
from chemclaw_lab.api import create_api_server

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/create_external_reader.py'
spec = importlib.util.spec_from_file_location('external_reader_setup', SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def _counts(lab):
    return tuple(lab.scalar(f'SELECT COUNT(*) FROM {table}') for table in ('principals', 'memberships', 'auth_credentials'))


def test_issued_service_is_single_project_viewer_and_reads_over_http(lab, tmp_path, capsys):
    config_path = tmp_path / 'external-reader.json'
    server = create_api_server(lab.db_path, port=0, service_settings=lab.settings)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        assert setup.main(['--db', lab.db_path, '--project-id', 'P1', '--config', str(config_path), '--base-url', url]) == 0
        configuration = json.loads(config_path.read_text(encoding='utf-8'))
        output = capsys.readouterr()
        assert configuration['api_key'] not in output.out + output.err
        assert set(configuration) == {'base_url', 'project_id', 'principal_id', 'credential_id', 'api_key'}
        conn = db.connect(lab.db_path)
        try:
            context = auth.authenticate_principal(conn, configuration['api_key'])
            assert context.kind == 'service' and context.memberships == {'P1': 'viewer'}
            stored_hash = conn.execute('SELECT secret_hash FROM auth_credentials WHERE credential_id=?', (configuration['credential_id'],)).fetchone()[0]
            assert stored_hash != configuration['api_key']
            assert stored_hash == auth._hash_secret(configuration['api_key'])
        finally:
            conn.close()
        lab.execute("INSERT INTO samples(sample_id,project_id,label) VALUES('external-sample','P1','External sample')")
        client = clients.ReadOnlyQueryClient(configuration['base_url'], configuration['api_key'])
        assert [s['sample_id'] for s in client.list_samples(project_ids=['P1'])['samples']] == ['external-sample']
        with pytest.raises(clients.ClientError) as cross_project:
            client.list_samples(project_ids=['P2'])
        assert cross_project.value.status == 403 and cross_project.value.code == 'forbidden_scope'
        # Bypass the client's whitelist to exercise server-side viewer rights.
        with pytest.raises(clients.ClientError) as write:
            client._request('POST', '/v2/capabilities/inventory.message.process/invoke', {
                'operation_id': 'external-reader-write', 'params': {'project_id': 'P1', 'raw_text': 'Used 5 g of Sodium chloride'}})
        assert write.value.status == 403
        assert lab.stock() == (100.0, 0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_existing_config_is_preserved_and_does_not_create_principal(lab, tmp_path):
    path = tmp_path / 'existing.json'
    path.write_text('keep this', encoding='utf-8')
    before = _counts(lab)
    with pytest.raises(setup.SetupError, match='already exists'):
        setup.create_external_reader(lab.db_path, 'P1', path)
    assert path.read_text(encoding='utf-8') == 'keep this'
    assert _counts(lab) == before


def test_missing_database_and_unknown_project_are_not_created(lab, tmp_path):
    missing = tmp_path / 'missing.db'
    path = tmp_path / 'new.json'
    before = _counts(lab)
    with pytest.raises(setup.SetupError, match='database must already exist'):
        setup.create_external_reader(missing, 'P1', path)
    assert not missing.exists() and not path.exists()
    with pytest.raises(setup.SetupError, match='project does not exist'):
        setup.create_external_reader(lab.db_path, 'new-project', path)
    assert not path.exists() and _counts(lab) == before
    assert lab.scalar("SELECT COUNT(*) FROM projects WHERE project_id='new-project'") == 0


def test_config_write_failure_rolls_back_and_removes_only_created_file(lab, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'reader.json'
    unrelated = tmp_path / 'unrelated.txt'
    unrelated.write_text('preserve me', encoding='utf-8')
    before = _counts(lab)
    fixed_secret = 'test-secret-that-must-not-be-printed'
    monkeypatch.setattr(setup.secrets, 'token_urlsafe', lambda _: fixed_secret)
    def fail_dump(*args, **kwargs):
        args[1].write('{partial secret')
        raise OSError(fixed_secret)
    monkeypatch.setattr(setup.json, 'dump', fail_dump)
    assert setup.main(['--db', lab.db_path, '--project-id', 'P1', '--config', str(path)]) == 1
    output = capsys.readouterr()
    assert fixed_secret not in output.out + output.err
    assert _counts(lab) == before and not path.exists()
    assert unrelated.read_text(encoding='utf-8') == 'preserve me'


def test_commit_failure_removes_config_and_rolls_back_identity(lab, tmp_path, monkeypatch):
    path = tmp_path / 'reader.json'
    before = _counts(lab)
    real_connect = sqlite3.connect
    class FailingCommitConnection(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError('injected commit failure')
    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingCommitConnection)
    monkeypatch.setattr(setup.sqlite3, 'connect', connect)
    with pytest.raises(setup.SetupError, match='no identity was committed'):
        setup.create_external_reader(lab.db_path, 'P1', path)
    assert not path.exists()
    assert _counts(lab) == before


def test_each_issuance_is_separate_and_default_url_is_saved(lab, tmp_path):
    first, second = tmp_path / 'first.json', tmp_path / 'second.json'
    result1 = setup.create_external_reader(lab.db_path, 'P1', first)
    result2 = setup.create_external_reader(lab.db_path, 'P1', second)
    cfg1, cfg2 = json.loads(first.read_text()), json.loads(second.read_text())
    assert cfg1['base_url'] == 'http://127.0.0.1:8876'
    assert result1['principal_id'] != result2['principal_id']
    assert cfg1['credential_id'] != cfg2['credential_id'] and cfg1['api_key'] != cfg2['api_key']
    assert 'api_key' not in result1 and 'api_key' not in result2
