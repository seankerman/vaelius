#!/usr/bin/env python3
"""Run a synthetic trace through the installed client, actual API, worker and MCP."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid
from urllib.parse import urlsplit


def rpc(base, token, project, name, arguments):
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': name, 'arguments': arguments}}
    request = urllib.request.Request(base + '/mcp', data=json.dumps(payload).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25',
                 'X-AgentNetwork-Project': project})
    with urllib.request.urlopen(request, timeout=15) as response:
        envelope = json.load(response)
    result = envelope['result']
    if result.get('isError'):
        raise RuntimeError('demo_mcp_operation_failed:' + name)
    return json.loads(result['content'][0]['text'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--base-url', help='use an already running explicit loopback API')
    args = parser.parse_args()
    state = args.state.expanduser().resolve()
    if not json.loads((state / 'services.json').read_text()).get('disposable'):
        raise ValueError('demo_requires_explicit_disposable_services')
    profile = state / 'profile'
    settings = json.loads((profile / 'runtime.json').read_text())
    port = settings['api_port']
    base = args.base_url or ('http://127.0.0.1:' + str(port))
    selected = urlsplit(base)
    if (selected.scheme != 'http' or selected.hostname not in ('127.0.0.1', 'localhost', '::1')
            or selected.username or selected.password or selected.path not in ('', '/')
            or selected.query or selected.fragment):
        raise ValueError('demo_requires_explicit_loopback_api')
    base = base.rstrip('/')
    run_id = 'demo-' + uuid.uuid4().hex
    token_path = profile / 'credentials/acme-alice.token'
    token = token_path.read_text().strip()
    client_home = state / run_id
    client_home.mkdir(mode=0o700)
    project = 'enrolled-test'
    config = {'sessions': {run_id: project}, 'projects': {},
              'knowledge_backend': {'mode': 'enterprise_local', 'api_version': 'cloud-local-1',
                                    'capture_version': 'enterprise-local-2', 'url': base,
                                    'credential_file': str(token_path), 'connection_id': 'captured-agent',
                                    'capture_owner': 'hooks'}}
    config_path = client_home / 'config.json'
    config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
    log_path = state / (run_id + '-api.log')
    with log_path.open('w') as log:
        api = None if args.base_url else subprocess.Popen(
            [sys.executable, '-m', 'agenthub.cloud_runtime', '--profile', str(profile), '--port', str(port)],
            stdout=log, stderr=log, cwd='/tmp')
        try:
            deadline = time.monotonic() + 30
            while True:
                if api is not None and api.poll() is not None:
                    raise RuntimeError('demo_api_exited_see_private_log')
                try:
                    with urllib.request.urlopen(base + '/ready', timeout=1) as response:
                        if json.load(response).get('ready'):
                            break
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError('demo_api_readiness_timeout')
                time.sleep(.1)
            from agentclient.hooks import handle
            from agentclient.capture_outbox import Outbox
            from agentclient.transport import enterprise_client
            fake_secret = 'sk-proj-' + 'A' * 40
            message = ('The Maple demo dataset is saved at /data/maple/import.csv. '
                       'CSV retains the stable header required by diagnostics. Temporary token: ' + fake_secret)
            handle(client_home, {'hook_event_name': 'Stop', 'event_id': run_id, 'session_id': run_id,
                                 'turn_id': run_id, 'timestamp': '2026-09-01T12:00:00Z',
                                 'last_assistant_message': message})
            box = Outbox(client_home)
            try:
                box.drain(enterprise_client(config, timeout=5), max_events=20, max_seconds=15)
                capture_status = box.status()
            finally:
                box.close()
            worker = subprocess.run([sys.executable, '-m', 'agenthub.cloud_local', 'worker-run',
                                     '--profile', str(profile), '--tenant', 'acme', '--max-jobs', '20',
                                     '--max-seconds', '30'], capture_output=True, text=True, timeout=45, cwd='/tmp')
            if worker.returncode:
                raise RuntimeError('demo_worker_failed:' + worker.stderr[-1000:])
            search = rpc(base, token, project, 'search_memory', {'query': 'Where is the Maple demo dataset saved?', 'project': project})
            records = search.get('records', [])
            if not records:
                raise AssertionError('demo_capture_not_indexed_or_delivered')
            card = records[0]
            evidence = rpc(base, token, project, 'fetch_memory', {'id': card['id'], 'revision': card['revision'], 'include_sources': True})
            encoded = json.dumps(evidence)
            if '/data/maple/import.csv' not in encoded or fake_secret in encoded:
                raise AssertionError('demo_evidence_or_redaction_failed')
            source = evidence['sources'][0]['source_id']
            backend = enterprise_client(config, timeout=5)
            backend.request('/enterprise/v1/lifecycle', {'version': 'enterprise-local-1', 'target_id': source,
                            'expected_revision': '1', 'operation': 'withdraw', 'idempotency_key': run_id,
                            'reason': 'synthetic demo completed'})
            after = rpc(base, token, project, 'search_memory', {'query': 'Maple demo dataset', 'project': project})
            if any(item['id'] == card['id'] for item in after.get('records', [])):
                raise AssertionError('demo_withdrawn_record_was_delivered')
            report = {'capture': 'actual client hook', 'transport': 'actual loopback HTTP API',
                      'index': 'installed source-first PostgreSQL worker', 'delivery': 'actual backend MCP',
                      'evidence_path_verified': True, 'synthetic_secret_redacted': True,
                      'withdrawal_verified': True, 'provider_calls': 0, 'real_agent_model_use': False,
                      'capture_status': capture_status}
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
            print(json.dumps(report, sort_keys=True, indent=2))
        finally:
            if api is not None:
                api.terminate()
                try:
                    api.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    api.kill(); api.wait(timeout=5)
    log_path.chmod(0o600)


if __name__ == '__main__':
    os.umask(0o077)
    main()
