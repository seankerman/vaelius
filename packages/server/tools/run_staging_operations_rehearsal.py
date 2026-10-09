"""Bounded synthetic normal ingestion and offline recovery on fresh databases.

Run with the accepted installed Python, from /tmp -I, in a coordinated PostgreSQL
window. The default performs no model/embedding calls. This small functional
concurrency exercise does not substitute for the 10k/100k performance gates.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, wait
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

FIXTURE = Path(__file__).parent / "fixtures/local_staging_readiness_v1/normal_ingest.json"
FIXTURE_HASH = "7e839c65f5e13f0d7fdb3c29dc4cc7c5de2cf0caaf6e1a7a667378d8bd9e6c26"


def load_fixture(path=FIXTURE):
    path = Path(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FIXTURE_HASH:
        raise ValueError("staging_operations_fixture_changed")
    fixture = json.loads(raw)
    documents = fixture["documents"]
    if len(documents) != 20 or len({d["id"] for d in documents}) != 20:
        raise ValueError("staging_document_inventory")
    if sum("replacement" in d for d in documents) != 5:
        raise ValueError("staging_replacement_inventory")
    for doc in documents:
        for item in [doc, *([doc["replacement"]] if "replacement" in doc else [])]:
            if hashlib.sha256(item["text"].encode()).hexdigest() != item["sha256"]:
                raise ValueError("staging_original_checksum")
            if item["expected_answer"] not in item["text"]:
                raise ValueError("staging_answer_not_in_original")
    if sum(len(d["text"].encode()) for d in documents) > 200000:
        raise ValueError("staging_document_byte_bound")
    return fixture


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def fresh_profile(path):
    path = Path(path).expanduser().resolve()
    private_root = (Path.home() / ".local/share/agentnetwork/enterprise-local/local-staging-readiness-v1").resolve()
    if not path.is_relative_to(private_root) or path == private_root:
        raise ValueError("owned_staging_operations_profile_required")
    if path.exists():
        raise ValueError("staging_operations_target_exists")
    path.mkdir(parents=True, mode=0o700)
    return path


def provision(profile, services_path, fixture, semantic=None, retrieval=None):
    """Reuse canonical provisioning helpers, with a new route and tenant namespace."""
    from agenthub.cloud_profile import _database, _grant
    from agenthub.postgres import migrate, TenantRegistry
    from agenthub.cloud_runtime import registry_from_settings
    services = json.loads(Path(services_path).read_text())
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    bootstrap = services["admin_dsn"]
    if conninfo_to_dict(bootstrap).get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("local_staging_postgres_required")
    namespace = "s7_ops_" + uuid.uuid4().hex[:12]
    operator = {"kind": "owned_staging_operations_v1", "namespace": namespace, "tenants": {}}
    control_admin, control_app = _database(bootstrap, namespace + "_control",
        namespace + "_router", secrets.token_urlsafe(32))
    control_app = make_conninfo(control_app, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=2000")
    operator["control_admin_dsn"] = control_admin
    migrate(control_admin, control=True)
    _grant(control_admin, namespace + "_router", control=True)
    registry = TenantRegistry(control_admin, profile / "provision-state")
    for number, tenant in enumerate(fixture["tenants"]):
        role, database = namespace + "_t" + str(number) + "_app", namespace + "_t" + str(number)
        admin, dsn = _database(bootstrap, database, role, secrets.token_urlsafe(32))
        migrate(admin)
        _grant(admin, role)
        dsn = make_conninfo(dsn, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=2000")
        registry.register(tenant, dsn)
        operator["tenants"][tenant] = {"admin_dsn": admin, "dsn": dsn,
            "database": database, "role": role}
    # Held restore target is intentionally absent from the registry.
    admin, dsn = _database(bootstrap, namespace + "_offline", namespace + "_offline_app", secrets.token_urlsafe(32))
    migrate(admin)
    _grant(admin, namespace + "_offline_app")
    dsn = make_conninfo(dsn, connect_timeout=5, options="-c statement_timeout=10000 -c lock_timeout=2000")
    operator["offline"] = {"admin_dsn": admin, "dsn": dsn}
    runtime = {"provider_mode": "off", "control_dsn": control_app, "max_stores": 4,
        "semantic": semantic or {"enabled": False}, "objects": {"kind": "file", "root": str(profile / "objects")},
        "allowed_hosts": ["127.0.0.1", "localhost"], "allowed_origins": []}
    runtime["retrieval"] = retrieval or {}
    private_json(profile / "operator.json", operator)
    private_json(profile / "runtime.json", runtime)
    return operator, registry_from_settings(runtime, profile / "runtime-state")


def seed_identity(store, fixture):
    tenant, project = store.tenant_id, fixture["project"]
    store.create_organization(tenant)
    store.create_project(tenant, project)
    contexts, tokens = {}, {}
    for actor in (fixture["principal"], fixture["reader"]):
        store.create_principal(tenant, actor)
        store.set_membership(tenant, project, actor, True)
        tokens[actor] = store.enroll(tenant, actor, "staging-" + actor,
            ["ingest", "read", "source_read", "policy", "withdraw", "correct"])
        contexts[actor] = store.authenticate(tokens[actor])
    store.enroll_connection(contexts[fixture["principal"]], fixture["connection"],
        "staging-operations", project, ["document"], visibility="team",
        reader_ids=[fixture["principal"], fixture["reader"]])
    return contexts, tokens


def ingest(documents, ctx, fixture, doc, replacement=False):
    value = doc["replacement"] if replacement else doc
    receipt = documents.ingest(ctx, fixture["connection"], doc["id"], value["version"],
        doc["filename"], io.BytesIO(value["text"].encode()), title=doc["title"])
    if receipt["sha256"] != value["sha256"] or receipt["parser_status"] != "indexed":
        raise AssertionError("normal_ingest_original_or_parser_mismatch:" + doc["id"])
    return receipt


def fetch_exact(documents, ctx, receipt, expected):
    stream, metadata = documents.fetch(ctx, receipt["source_id"])
    with stream:
        raw = stream.read()
    if hashlib.sha256(raw).hexdigest() != hashlib.sha256(expected).hexdigest() or raw != expected:
        raise AssertionError("normal_original_bytes_mismatch")
    return len(raw)


def query_expected(store, ctx, fixture, doc, receipt, replacement=False):
    value = doc["replacement"] if replacement else doc
    from agentclient.enterprise_contract import VERSION
    answer = store.search(ctx, {"version": VERSION, "query": doc["query"],
                               "project": fixture["project"]})
    # Compact cards expose document/revision IDs; join their canonical provenance
    # without manufacturing a source_ids field absent from the API contract.
    from vaelius_test_support.hub.retrieval_evaluation import canonical_evidence
    sources = set()
    for card in answer.get("results", []):
        evidence = canonical_evidence(store, ctx, card)
        if not evidence["allowed"]:
            raise AssertionError("normal_query_unauthorized_delivered_card")
        sources.update(evidence["source_ids"])
    text = " ".join(card.get("lesson", "") for card in answer.get("results", []))
    if replacement and doc["expected_answer"] in text:
        raise AssertionError("normal_query_stale_replacement_answer:" + doc["id"])
    if not answer.get("answerable") or receipt["source_id"] not in sources or value["expected_answer"] not in text:
        raise AssertionError("normal_query_missing_current_supported_answer:" + doc["id"])
    if len(json.dumps(answer, ensure_ascii=True)) > 4000:
        raise AssertionError("normal_query_delivery_bound")
    return len(json.dumps(answer, ensure_ascii=True))


def concurrent_reads(store, ctx, documents, fixture, current, *, workers, replacement_batch=None):
    """Stable reader targets share resource contention with disjoint replacements."""
    from agenthub.cloud_ops import percentiles
    stable = [doc for doc in fixture["documents"] if "replacement" not in doc]
    started = time.monotonic()
    def read(number):
        doc = stable[number % len(stable)]
        begin = time.monotonic()
        try:
            query_expected(store, ctx, fixture, doc, current[doc["id"]])
            fetch_exact(documents, ctx, current[doc["id"]], doc["text"].encode())
            return {"elapsed_seconds": time.monotonic() - begin, "accepted": True}
        except AssertionError as exc:
            return {"elapsed_seconds": time.monotonic() - begin, "accepted": False,
                    "error": str(exc), "document": doc["id"]}
    executor = ThreadPoolExecutor(max_workers=workers + int(replacement_batch is not None))
    try:
        reads = [executor.submit(read, number) for number in range(40)]
        update = executor.submit(replacement_batch) if replacement_batch is not None else None
        done, pending = wait([*reads, *([update] if update else [])], timeout=60)
        if pending:
            for future in pending:
                future.cancel()
            raise TimeoutError("normal_concurrency_completion_failed")
        outcomes = [future.result() for future in reads]
        latencies = [item["elapsed_seconds"] for item in outcomes]
        if update:
            update.result()
        failures = [item for item in outcomes if not item["accepted"]]
        return {"workers": workers, "completed": len(latencies), "errors": len(failures),
                "accepted": not failures, "failures": failures, "latency_unit": "query plus authorized provenance inspection plus original fetch",
                "elapsed_seconds": time.monotonic() - started, "latency": percentiles(latencies),
                "mixed_replacements": replacement_batch is not None, "queued_max": 40,
                "classification": "small provider-free functional contention; not 100k load acceptance"}
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


async def runtime_status(profile, token):
    import httpx
    from agenthub.cloud_runtime import app_from_profile
    app = app_from_profile(profile)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.get("/enterprise/v1/status", headers={"Authorization": "Bearer " + token})
        if response.status_code != 200:
            raise AssertionError("third_tenant_actual_runtime_status_failed")
        return response.json()


def installed_cli(profile, command, tenant=None):
    argv = [sys.executable, "-I", "-m", "agenthub.cloud_local", command,
            "--profile", str(profile)]
    if tenant is not None:
        argv += ["--tenant", tenant]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(argv, cwd="/tmp", env=environment, capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("installed_operator_command_failed:" + command)
    if len(result.stdout) > 65536:
        raise ValueError("installed_operator_output_bound")
    return json.loads(result.stdout)["result"]


def operator_routes(profile, operator, fixture):
    """Invoke the installed operational CLI; inactive stores must stay unresolved."""
    from agenthub.postgres import TenantRegistry
    admin = TenantRegistry(operator["control_admin_dsn"], profile / "operator-state")
    disabled = fixture["tenants"][1]
    admin.set_active(disabled, False)
    status = installed_cli(profile, "status")
    doctor = installed_cli(profile, "doctor")
    tenant = fixture["tenants"][2]
    readiness = installed_cli(profile, "readiness", tenant)
    usage = installed_cli(profile, "usage", tenant)
    for value in (status, doctor["status"]):
        if set(value["tenants"]) != set(fixture["tenants"]):
            raise AssertionError("registered_tenant_inventory_mismatch")
        if value["tenants"][disabled] != {"active": False}:
            raise AssertionError("disabled_store_resolved_or_enabled")
        if not value["tenants"][tenant]["active"]:
            raise AssertionError("third_tenant_status_inactive")
    if readiness != {"ready": True, "tenant": tenant} or usage["tenant"] != tenant:
        raise AssertionError("third_tenant_operator_route_substituted")
    return {"status": True, "doctor": True, "readiness": True, "usage": True,
            "tenant": tenant, "disabled_tenant": disabled,
            "disabled_metadata_only": True, "provider_dispatch": "off"}


def run(profile, services, fixture, semantic=None, retrieval=None, variant="baseline"):
    from agenthub.cloud_runtime import CloudStore
    from agenthub.backend_ops import build_identity
    from agenthub.pipeline_pin import verify
    from agenthub.document_ingest import DocumentStore
    from agenthub.source_objects import FileSourceObjects
    from agenthub.cloud_ops import Meter, AdmissionExhausted
    from agenthub.cloud_recovery import backup, restore_snapshot, reconcile_restore
    from agenthub.enterprise import Denied
    from agentclient.enterprise_contract import VERSION
    installed_identity, canonical_pin = build_identity(), verify()
    operator, registry = provision(profile, services, fixture, semantic, retrieval)
    try:
        contexts, tokens, stores = {}, {}, {}
        for tenant in fixture["tenants"]:
            stores[tenant] = registry.resolve(tenant)
            contexts[tenant], tokens[tenant] = seed_identity(stores[tenant], fixture)
        tenant = fixture["tenants"][2]
        store, ctx = stores[tenant], contexts[tenant][fixture["principal"]]
        objects = FileSourceObjects(profile / "objects")
        documents = DocumentStore(store, objects)
        receipt = {"fixture_sha256": FIXTURE_HASH, "tenant": tenant,
            "principal": fixture["principal"], "model_calls": 0, "embedding_calls": 0,
            "vectors_recomputed": 0, "embedding_measurement": "not run; models off",
            "object_adapter": "canonical individual FileSourceObjects", "quality_failures": [], "phases": {},
            "retrieval_variant": variant, "retrieval_configuration": retrieval or {},
            "quality_classification": "selected configuration only if explicit integrator admission precedes execution" if variant != "baseline" else "baseline normal-ingest functional quality; Nomic derivative indexing measured separately"}
        if semantic:
            receipt["embedding_before"] = store.semantic_embedder.stats()
        receipt["installed_runtime"] = installed_identity
        receipt["canonical_pin"] = canonical_pin
        start = time.monotonic()
        current = {doc["id"]: ingest(documents, ctx, fixture, doc) for doc in fixture["documents"]}
        receipt["phases"]["normal_ingest"] = {"documents": 20, "elapsed_seconds": time.monotonic() - start,
            "input_bytes": sum(len(doc["text"].encode()) for doc in fixture["documents"]),
            "passages": sum(item["passages"] for item in current.values())}
        if semantic:
            receipt["phases"]["initial_vectors"] = store.reindex_vectors(max_documents=1000)
        for doc in fixture["documents"]:
            fetch_exact(documents, ctx, current[doc["id"]], doc["text"].encode())
            try:
                query_expected(store, ctx, fixture, doc, current[doc["id"]])
            except AssertionError as exc:
                receipt["quality_failures"].append({"phase": "initial", "document": doc["id"], "error": str(exc)})
        # Runtime resolves a genuine third route and non-Alice/Bob identity.
        status = asyncio.run(runtime_status(profile, tokens[tenant][fixture["principal"]]))
        if status["tenant"] != tenant or status["principal"] != fixture["principal"]:
            raise AssertionError("third_tenant_identity_substituted")
        receipt["actual_runtime_identity"] = {"tenant": status["tenant"], "principal": status["principal"]}
        receipt["operator_routes"] = operator_routes(profile, operator, fixture)
        snapshot = profile / "before"
        backup(store, objects, snapshot)
        receipt["phases"]["concurrency"] = [concurrent_reads(store, ctx, documents, fixture, current, workers=w)
            for w in (2, 4)]
        def replacements():
            begin = time.monotonic()
            for doc in fixture["documents"]:
                if "replacement" in doc:
                    current[doc["id"]] = ingest(documents, ctx, fixture, doc, True)
            if semantic:
                receipt["phases"]["replacement_vectors"] = store.reindex_vectors(max_documents=1000)
            receipt["phases"]["replacements"] = {"documents": 5, "elapsed_seconds": time.monotonic() - begin,
                "input_bytes": sum(len(doc["replacement"]["text"].encode()) for doc in fixture["documents"] if "replacement" in doc)}
        receipt["phases"]["concurrency"].append(concurrent_reads(store, ctx, documents, fixture,
            current, workers=4, replacement_batch=replacements))
        for doc in fixture["documents"]:
            if "replacement" in doc:
                fetch_exact(documents, ctx, current[doc["id"]], doc["replacement"]["text"].encode())
                try:
                    query_expected(store, ctx, fixture, doc, current[doc["id"]], True)
                except AssertionError as exc:
                    receipt["quality_failures"].append({"phase": "replacement", "document": doc["id"], "error": str(exc)})
        # Over-limit reservation in a different tenant cannot exhaust this store's pool.
        noisy = stores[fixture["tenants"][0]]
        meter = Meter(noisy)
        meter.configure(max_parallel=1, max_attempts=2)
        meter.reserve("staging-held", "staging-noisy", "provider-free-admission")
        try:
            meter.reserve("staging-excess", "staging-noisy-2", "provider-free-admission")
        except AdmissionExhausted:
            receipt["noisy_tenant_rejected"] = True
        else:
            raise AssertionError("noisy_tenant_not_backpressured")
        try:
            query_expected(store, ctx, fixture, fixture["documents"][5], current[fixture["documents"][5]["id"]])
        except AssertionError as exc:
            receipt["quality_failures"].append({"phase": "healthy_tenant", "document": fixture["documents"][5]["id"], "error": str(exc)})
        meter.finish("staging-held", "cancelled")
        added = fixture["restore"]["added_after_backup"]
        added_result = documents.ingest(ctx, fixture["connection"], added["id"], "1", added["filename"],
                                      io.BytesIO(added["text"].encode()), title=added["title"])
        for operation in ("withdraw", "delete"):
            ident = fixture["lifecycle"][operation]
            store.lifecycle(ctx, {"version": VERSION, "target_id": current[ident]["source_id"],
                "expected_revision": "1", "operation": operation,
                "idempotency_key": "staging-" + operation, "reason": "synthetic post-snapshot lifecycle"})
        store.connection_policy(ctx, fixture["connection"], reader_ids=[fixture["principal"]])
        target = CloudStore(profile / "offline-state", operator["offline"]["dsn"], tenant)
        target_objects = FileSourceObjects(profile / "offline-objects")
        restored = restore_snapshot(snapshot, target, target_objects, admin_dsn=operator["offline"]["admin_dsn"])
        try:
            target.require_ready()
        except ValueError:
            pass
        else:
            raise AssertionError("unreconciled_restore_served")
        reconciled = reconcile_restore(snapshot, target, store, target_objects, objects,
            admin_dsn=operator["offline"]["admin_dsn"], destination=profile / "latest")
        target.require_ready()
        owner = target.authenticate(tokens[tenant][fixture["principal"]])
        reader = target.authenticate(tokens[tenant][fixture["reader"]])
        restored_docs = DocumentStore(target, target_objects)
        for doc in fixture["documents"]:
            if doc["id"] in fixture["lifecycle"].values():
                continue
            value = doc.get("replacement", doc)
            fetch_exact(restored_docs, owner, current[doc["id"]], value["text"].encode())
        fetch_exact(restored_docs, owner, added_result, added["text"].encode())
        for operation in ("withdraw", "delete"):
            try:
                restored_docs.fetch(owner, current[fixture["lifecycle"][operation]]["source_id"])
            except Denied:
                pass
            else:
                raise AssertionError("restore_lifecycle_denial_lost")
        try:
            restored_docs.fetch(reader, current[fixture["documents"][0]["id"]]["source_id"])
        except Denied:
            pass
        else:
            raise AssertionError("restore_reader_revocation_lost")
        receipt["phases"]["restore"] = {"initial_ready": restored["ready"], **reconciled,
            "current_originals_verified": len(fixture["documents"]) - 2 + 1, "withdrawn_denied": True, "deleted_denied": True,
            "revoked_reader_denied": True}
        receipt["metering"] = Meter(store).status()
        if semantic:
            receipt["embedding_after"] = store.semantic_embedder.stats()
            receipt["vectors_recomputed"] = sum(receipt["phases"][key]["documents"] for key in ("initial_vectors", "replacement_vectors"))
            receipt["embedding_calls"] = receipt["embedding_after"]["model_calls"] - receipt["embedding_before"]["model_calls"]
            receipt["embedding_measurement"] = "shared accounted cached Nomic, real full canonical rebuilds; cache hits do not imply new model work"
        receipt["accepted"] = not receipt["quality_failures"] and all(case["accepted"] for case in receipt["phases"]["concurrency"])
        receipt["limitations"] = ["models off: cached Nomic ingest/reindex cost is still required separately",
            "small functional concurrency is not 10k/100k performance or noisy-query isolation evidence",
            "FileSourceObjects is not S3 emulator protocol evidence", "logical held restore is not managed PITR/RPO/RTO",
            "old R1 read-only same-authority compatibility remains a separate check; never restore stale ACLs"]
        if semantic:
            receipt["limitations"].pop(0)
        return receipt
    except Exception as exc:
        if "receipt" in locals():
            exc.staging_partial_receipt = receipt
        raise
    finally:
        from agenthub.model_resources import release_models
        owned = dict(registry._stores)
        model = next((item.semantic_embedder for item in owned.values()
                      if item.semantic_embedder is not None), None)
        release_models(registry, owned, model)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--services", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--fixture", default=str(FIXTURE))
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--retrieval-variant", default="baseline", help="label for the explicitly selected runtime configuration")
    parser.add_argument("--semantic-manifest", help="runtime settings JSON with semantic/retrieval objects; existing cached model only")
    args = parser.parse_args(argv)
    fixture = load_fixture(args.fixture)
    if args.plan_only:
        print(json.dumps({"fixture_sha256": FIXTURE_HASH, "documents": 20, "replacements": 5,
            "third_tenant": fixture["tenants"][2], "read_workloads": ["2 workers/40", "4 workers/40", "4 workers/40 + five replacements"],
            "model_calls": 0, "database_calls": 0, "execution_pending_exclusive_window": True}))
        return
    if not sys.flags.isolated or Path.cwd().resolve() != Path("/tmp").resolve():
        raise ValueError("installed_isolated_tmp_execution_required")
    os.environ.pop("PYTHONPATH", None)
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise ValueError("staging_operations_receipt_exists")
    semantic, retrieval = None, {}
    if args.retrieval_variant != "baseline" and not args.semantic_manifest:
        raise ValueError("explicit_manifest_required_for_selected_retrieval")
    if args.semantic_manifest:
        settings=json.loads(Path(args.semantic_manifest).read_text())
        semantic=settings.get('semantic')
        retrieval=settings.get('retrieval',{})
        if not isinstance(semantic,dict) or not semantic.get('directory') or not semantic.get('accounting_root'):
            raise ValueError('existing_shared_embedding_accounting_required')
    profile = fresh_profile(args.profile)
    started = time.time()
    try:
        receipt = run(profile, args.services, fixture, semantic, retrieval, args.retrieval_variant)
    except Exception as exc:
        receipt = dict(getattr(exc, "staging_partial_receipt", {}))
        receipt.update({"accepted": False, "error_class": type(exc).__name__, "fixture_sha256": FIXTURE_HASH,
                   "model_calls": 0, "embedding_calls": None if semantic else 0, "profile": str(profile),
                   "embedding_status": "inspect shared ledger; failed-run work is not rolled back" if semantic else "off",
                   "retrieval_variant": args.retrieval_variant, "retrieval_configuration": retrieval})
        private_json(output, dict(receipt, start_unix=started, end_unix=time.time()))
        raise
    private_json(output, dict(receipt, profile=str(profile), start_unix=started, end_unix=time.time()))
    print(json.dumps({"accepted": receipt["accepted"], "profile": str(profile), "receipt": str(output), "model_calls": 0}))
    if not receipt["accepted"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
