#!/usr/bin/env python3
"""Explicit disposable local PostgreSQL/S3 services; no source capture or model calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def run(engine, *args):
    result = subprocess.run([engine, *args], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError('local_container_operation_failed:' + args[0] + '\n' + result.stderr[-2000:])
    return result.stdout.strip()


def start(state, engine, *, postgres_image='docker.io/pgvector/pgvector:0.8.6-pg17', moto_image='ghcr.io/getmoto/motoserver:5.2.3'):
    from psycopg.conninfo import make_conninfo
    from agenthub.cloud_profile import setup_profile
    from agenthub.postgres import connect
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    receipt = state / 'dev-services.json'
    if receipt.exists():
        data = json.loads(receipt.read_text())
        manifest = json.loads((state / 'services.json').read_text())
        if not manifest.get('disposable'):
            raise ValueError('existing_services_are_not_disposable')
        for name in data['containers']:
            actual = json.loads(run(data['engine'], 'inspect', name))[0]
            if actual['Config']['Labels'].get('vaelius.disposable') != data['namespace']:
                raise ValueError('container_ownership_mismatch')
            run(data['engine'], 'start', name)
        runtime = json.loads((state / 'profile/runtime.json').read_text())
        return {'services': str(state / 'services.json'), 'profile': str(state / 'profile'),
                'api_port': runtime['api_port'], 'resumed': True, 'provider_calls': 0}
    if (state / 'profile/runtime.json').exists():
        raise ValueError('destroyed_services_require_a_new_state_directory_no_implicit_profile_reset')
    namespace = 'vaelius-dev-' + hashlib.sha256(str(state).encode()).hexdigest()[:10]
    port, moto_port = free_port(), free_port()
    while port == moto_port:
        moto_port = free_port()
    password = secrets.token_urlsafe(32)
    envfile = state / 'postgres.env'
    envfile.write_text('POSTGRES_PASSWORD=' + password + '\n')
    envfile.chmod(0o600)
    private_json(receipt, {'engine': engine, 'namespace': namespace, 'containers': []})
    names = []
    try:
        for service, image, container_port, host_port, options in (
            ('postgres', postgres_image, 5432, port, ['--env-file', str(envfile)]),
            ('moto', moto_image, 5000, moto_port, []),
        ):
            name = namespace + '-' + service
            run(engine, 'run', '-d', '--name', name, '--label', 'vaelius.disposable=' + namespace,
                '-p', '127.0.0.1:' + str(host_port) + ':' + str(container_port), *options, image)
            names.append(name)
            private_json(receipt, {'engine': engine, 'namespace': namespace, 'containers': names})
        dsn = make_conninfo(host='127.0.0.1', port=port, dbname='postgres', user='postgres', password=password)
        deadline = time.monotonic() + 45
        while True:
            try:
                with connect(dsn) as db:
                    db.execute('SELECT 1')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError('disposable_postgres_start_timeout')
                time.sleep(.25)
        bootstrap = state / 'bootstrap.json'
        private_json(bootstrap, {'admin_dsn': dsn})
        setup_profile(state / 'profile', bootstrap, namespace='vaelius_dev')
        operator = json.loads((state / 'profile/operator.json').read_text())
        services = {'disposable': True, 'namespace': namespace, 'admin_dsn': dsn,
                    'tenants': operator['tenants'], 'moto_endpoint': 'http://127.0.0.1:' + str(moto_port)}
        private_json(state / 'services.json', services)
        runtime_path = state / 'profile/runtime.json'
        runtime = json.loads(runtime_path.read_text())
        runtime['objects'].update(endpoint=services['moto_endpoint'], bucket='vaelius-dev-sources')
        runtime['api_port'] = free_port()
        private_json(runtime_path, runtime)
        from agenthub.object_config import objects_from_settings
        adapter = objects_from_settings(runtime)
        deadline = time.monotonic() + 30
        while True:
            try:
                adapter.client.create_bucket(Bucket=adapter.bucket)
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError('disposable_s3_start_timeout')
                time.sleep(.25)
        return {'services': str(state / 'services.json'), 'profile': str(state / 'profile'),
                'api_port': runtime['api_port'], 'synthetic_only': True, 'provider_calls': 0}
    except BaseException:
        # Remove only the exact containers created by this invocation.
        for name in names:
            run(engine, 'rm', '-f', '-v', name)
        receipt.unlink(missing_ok=True)
        raise


def stop(state, *, destroy=False):
    receipt = state / 'dev-services.json'
    data = json.loads(receipt.read_text())
    for name in data['containers']:
        actual = json.loads(run(data['engine'], 'inspect', name))[0]
        if actual['Config']['Labels'].get('vaelius.disposable') != data['namespace']:
            raise ValueError('container_ownership_mismatch')
        if destroy:
            run(data['engine'], 'rm', '-f', '-v', name)
        else:
            run(data['engine'], 'stop', name)
    if destroy:
        receipt.unlink()
    return {'removed' if destroy else 'stopped': data['containers']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('start', 'stop', 'destroy'))
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--engine', default=shutil.which('podman') or shutil.which('docker'))
    parser.add_argument('--postgres-image', default='docker.io/pgvector/pgvector:0.8.6-pg17')
    parser.add_argument('--moto-image', default='ghcr.io/getmoto/motoserver:5.2.3')
    args = parser.parse_args()
    if not args.engine:
        raise ValueError('docker_or_podman_required')
    os.umask(0o077)
    state = args.state.expanduser().resolve()
    result = start(state, args.engine, postgres_image=args.postgres_image, moto_image=args.moto_image) if args.action == 'start' else stop(state, destroy=args.action == 'destroy')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
