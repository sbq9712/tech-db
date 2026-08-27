#!/usr/bin/env python3
"""Phase06 RT-060..RT-075 named behavioral acceptance."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))

from entity_admin import AdminAuthError, EntityAdminService
from entity_resolution_types import ResolutionState, normalize_strong_id
from entity_resolver_v2 import (CanonicalEntityResolver, CandidateGenerator,
                                ConstrainedLLMAdjudicator, QueryEntityResolver,
                                resolve_query_from_runtime_snapshot)
from entity_shadow import EntityShadowMonitor
from identity_migration import (materialize_mention, materialize_relation,
                                migrate_legacy_registry, rematerialization_plan)
from identity_snapshot import (IdentitySnapshotView, build_identity_snapshot,
                               validate_identity_snapshot, write_identity_snapshot)
from identity_store import (DependentMutationConflict, IdentityConflict,
                            IdentityStore)
from runtime_snapshot import RuntimeSnapshot
from runtime_safety import (RequestCancelled, RequestExecutionContext,
                            RuntimeSafetyProfile)
from budget_guard import QueryBudget
from source_snapshot import EvidenceLocator, SourceSnapshot

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED.append(name); print(f"  FAIL {name} {detail}")


def raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return True
    return False


def store_at(root, name="identity.db"):
    return IdentityStore(Path(root) / name)


def seeded(root):
    store = store_at(root)
    nvidia, _ = store.create_entity("NVIDIA", "ORG", lifecycle="ACTIVE",
        aliases=["英伟达"], provenance="fixture", actor="fixture",
        reason="seed", creation_key="fixture:nvidia")
    apple_org, _ = store.create_entity("Apple Inc.", "ORG", lifecycle="ACTIVE",
        aliases=["Apple"], provenance="fixture", actor="fixture",
        reason="seed", creation_key="fixture:apple-org")
    apple_product, _ = store.create_entity("Apple fruit", "OTHER_DOMAIN", lifecycle="ACTIVE",
        aliases=["Apple"], provenance="fixture", actor="fixture",
        reason="seed", creation_key="fixture:apple-fruit")
    store.add_alias(nvidia["entity_id"], "NVDA", alias_type="ACRONYM",
                    provenance="official", actor="fixture", reason="acronym")
    store.add_alias(nvidia["entity_id"], "Yingweida", alias_type="TRANSLITERATION",
                    provenance="reviewed", actor="fixture", reason="transliteration")
    store.add_strong_id(nvidia["entity_id"], "EXCHANGE_TICKER", "NASDAQ:NVDA",
                        provenance="exchange", actor="fixture", reason="ticker")
    return store, nvidia, apple_org, apple_product


def test_rt060_rt061_identity_store():
    print("RT-060/061 — opaque identity + transactional store")
    with tempfile.TemporaryDirectory() as root:
        store = store_at(root)
        a, created_a = store.create_entity("Same Legal Name", "ORG", lifecycle="ACTIVE",
            actor="op", reason="first", creation_key="jurisdiction:A")
        b, created_b = store.create_entity("Same Legal Name", "ORG", lifecycle="ACTIVE",
            actor="op", reason="second", creation_key="jurisdiction:B")
        check("RT060.same_name_distinct_legal_entities", created_a and created_b
              and a["entity_id"] != b["entity_id"])
        check("RT060.opaque_id_has_no_name", "same" not in a["entity_id"].lower()
              and a["entity_id"].startswith("ent_") and len(a["entity_id"]) == 30)
        before = a["entity_id"]
        renamed = store.update_entity(before, canonical_name="Renamed Legal Name",
                                      actor="op", reason="rename")
        corrected = store.update_entity(before, entity_type="OTHER_DOMAIN",
                                        actor="op", reason="type correction")
        check("RT060.rename_preserves_id", renamed["entity_id"] == before)
        check("RT060.type_correction_preserves_id", corrected["entity_id"] == before)
        store.update_entity(before, lifecycle="TOMBSTONED", actor="op", reason="retire")
        winner, created = store.create_entity("Renamed Legal Name", "OTHER_DOMAIN",
            actor="op", reason="retry", creation_key="jurisdiction:A")
        check("RT060.tombstoned_id_never_reused", not created and winner["entity_id"] == before)
        ids = {store.create_entity(f"Bulk {i}", "ORG", actor="op", reason="bulk",
                                   creation_key=f"bulk:{i}")[0]["entity_id"] for i in range(1000)}
        check("RT060.high_volume_id_uniqueness", len(ids) == 1000)
        check("RT061.sqlite_wal_enabled", store.journal_mode() == "WAL")
        check("RT061.unsafe_multiwriter_topology_rejected", raises(RuntimeError,
              lambda: IdentityStore(Path(root) / "bad.db", topology="MULTI_NODE")))
        before_count = len(store.list_entities())
        def fail(_conn): raise RuntimeError("injected transaction failure")
        check("RT061.failed_transaction_raises", raises(RuntimeError,
              lambda: store.create_entity("Rollback Corp", "ORG", aliases=["RB"],
                  actor="op", reason="rollback", failure_hook=fail)))
        check("RT061.rollback_leaves_no_partial_state",
              len(store.list_entities()) == before_count
              and not store.search_entities("Rollback Corp"))
        reopened = IdentityStore(Path(root) / "identity.db")
        check("RT061.reopen_preserves_store", reopened.get_entity(b["entity_id"]) is not None
              and reopened.revision() == store.revision())


def test_rt062_rt063_rt064_resolution():
    print("RT-062/063/064 — alias/strong IDs + canonical decisions/candidates")
    with tempfile.TemporaryDirectory() as root:
        store, nvidia, apple_org, apple_product = seeded(root)
        snapshot = IdentitySnapshotView(build_identity_snapshot(store))
        resolver = CanonicalEntityResolver(snapshot)
        apple = resolver.resolve("Apple")
        check("RT062.one_alias_maps_multiple_entities", apple.decision == ResolutionState.AMBIGUOUS
              and len(apple.candidates) == 2)
        typed = resolver.resolve("Apple", required_type="ORG")
        check("RT062.explicit_type_constraint_disambiguates", typed.decision == ResolutionState.LINK
              and typed.selected_entity_id == apple_org["entity_id"])
        check("RT062.chinese_english_alias", resolver.resolve("英伟达").selected_entity_id == nvidia["entity_id"])
        check("RT062.transliteration_alias", resolver.resolve("Yingweida").selected_entity_id == nvidia["entity_id"])
        check("RT062.invalid_strong_id_validation", raises(ValueError,
              lambda: normalize_strong_id("EXCHANGE_TICKER", "NVDA")))
        strong = resolver.resolve("unknown", strong_ids=[{"id_type": "EXCHANGE_TICKER",
                                                           "value": "NASDAQ:NVDA"}])
        check("RT062.typed_strong_id_link", strong.decision == ResolutionState.LINK
              and strong.selected_entity_id == nvidia["entity_id"])
        wrong_type = resolver.resolve("NASDAQ:NVDA", required_type="PERSON",
            strong_ids=[{"id_type": "EXCHANGE_TICKER", "value": "NASDAQ:NVDA"}])
        check("RT062.strong_id_respects_explicit_type_constraint",
              wrong_type.decision == ResolutionState.BLOCKED
              and "ER_STRONG_ID_TYPE_CONFLICT" in wrong_type.reason_codes)
        doi_entity, _ = store.create_entity("Resolver Paper", "OTHER_DOMAIN",
            lifecycle="ACTIVE", actor="fixture", reason="doi", creation_key="paper")
        store.add_strong_id(doi_entity["entity_id"], "DOI", "10.1234/resolver.1",
            provenance="crossref", actor="fixture", reason="doi")
        doi_snapshot = IdentitySnapshotView(build_identity_snapshot(store))
        doi_wrong = CanonicalEntityResolver(doi_snapshot).resolve(
            "10.1234/resolver.1", required_type="PRODUCT_MODEL",
            strong_ids=[{"id_type": "DOI", "value": "10.1234/resolver.1"}])
        check("RT062.doi_product_type_conflict_blocks_link",
              doi_wrong.decision == ResolutionState.BLOCKED
              and doi_wrong.selected_entity_id is None)
        check("RT062.strong_id_ownership_constraint", raises(IdentityConflict,
              lambda: store.add_strong_id(apple_org["entity_id"], "EXCHANGE_TICKER",
                  "NASDAQ:NVDA", provenance="bad", actor="op", reason="conflict")))
        block = store.add_rule("BLOCK", {"mention": "malicious"}, actor="op",
                               reason="unsafe mutation input")
        snapshot2 = IdentitySnapshotView(build_identity_snapshot(store))
        blocked = CanonicalEntityResolver(snapshot2).resolve("malicious")
        check("RT063.blocked_terminal_state", blocked.decision == ResolutionState.BLOCKED
              and block in blocked.override_block_findings)
        check("RT063.new_provisional_proposal", resolver.resolve("Novel Quantum Widget").decision == ResolutionState.NEW
              and resolver.resolve("Novel Quantum Widget").provisional_proposal["lifecycle"] == "PROVISIONAL")
        check("RT063.low_confidence_is_diagnostic_not_terminal",
              "LOW_CONFIDENCE" in resolver.resolve("NVIDA").diagnostic_flags
              and resolver.resolve("NVIDA").decision == ResolutionState.AMBIGUOUS)
        decisions = {resolver.resolve("英伟达").decision, resolver.resolve("new thing").decision,
                     resolver.resolve("Apple").decision, blocked.decision}
        check("RT063.terminal_truth_table", decisions == set(ResolutionState))
        generated = CandidateGenerator(snapshot).generate("NVDA")
        check("RT064.stage_attribution_and_dedup", generated.candidates[0].stage == "acronym"
              and len({c.entity_id for c in generated.candidates}) == len(generated.candidates))
        typo = CandidateGenerator(snapshot).generate("NVIDA")
        check("RT064.typo_fuzzy_recall", typo.candidates
              and typo.candidates[0].entity_id == nvidia["entity_id"])


def test_rt065_llm_constraints():
    print("RT-065 — constrained LLM adjudication/runtime")
    with tempfile.TemporaryDirectory() as root:
        store, nvidia, *_ = seeded(root)
        snapshot = IdentitySnapshotView(build_identity_snapshot(store))
        candidates = CandidateGenerator(snapshot).generate("NVIDA")
        llm = ConstrainedLLMAdjudicator("fake-v1", "prompt-v1")
        state, selected, reasons = llm.validate_output(
            {"decision": "LINK", "entity_id": "ent_FABRICATED"}, candidates,
            entities=snapshot.entities)
        check("RT065.fabricated_id_never_links", state == ResolutionState.BLOCKED
              and selected is None and "ER_LLM_FABRICATED_ID" in reasons)
        state2, _, reasons2 = llm.validate_output("not-json", candidates,
                                                  entities=snapshot.entities)
        check("RT065.malformed_output_fails_safe", state2 == ResolutionState.AMBIGUOUS
              and "ER_LLM_MALFORMED" in reasons2)
        key1 = llm.cache_key("NVIDA", "ctx", candidates, snapshot.snapshot_id, "p1")
        altered = CandidateGenerator(snapshot).generate("NVIDA", top_k=1)
        key2 = llm.cache_key("NVIDA", "ctx", altered, snapshot.snapshot_id, "p2")
        check("RT065.cache_binds_candidates_and_versions", key1 != key2)
        state3, selected3, _ = llm.validate_output(
            {"decision": "LINK", "entity_id": nvidia["entity_id"]}, candidates,
            required_type="PERSON", entities=snapshot.entities)
        check("RT065.wrong_type_blocked", state3 == ResolutionState.BLOCKED and selected3 is None)
        check("RT065.raw_model_confidence_not_authority",
              "confidence" not in llm.validate_output.__code__.co_varnames)

        async def runtime_cases():
            policy = resolver_policy = CanonicalEntityResolver(snapshot).policy
            profile = RuntimeSafetyProfile(verifier=.02, fast_total=.2,
                                           backoff_seconds=.001)
            budget = QueryBudget(limit=2)
            retry_context = RequestExecutionContext(profile=profile,
                                                    query_budget=budget)
            calls = 0
            class RateLimited(RuntimeError):
                status_code = 429
                headers = {"Retry-After": "0.001"}
            async def rate_limited(request):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RateLimited("retry")
                return {"decision": "LINK", "entity_id": nvidia["entity_id"]}
            recovered = await llm.adjudicate(rate_limited, mention="NVIDA",
                context="ctx", candidates=candidates, snapshot=snapshot,
                policy=resolver_policy, execution_context=retry_context)
            check("RT065.429_bounded_retry_via_request_context",
                  recovered[0] == ResolutionState.LINK and calls == 2
                  and budget.breakdown.get("entity_adjudicator") == 2)

            timeout_context = RequestExecutionContext(profile=profile)
            async def timeout(_request):
                raise asyncio.TimeoutError("model timeout")
            timed_out = await llm.adjudicate(timeout, mention="NVIDA-timeout",
                context="ctx", candidates=candidates, snapshot=snapshot,
                policy=policy, execution_context=timeout_context)
            check("RT065.timeout_never_trusted_link",
                  timed_out[0] == ResolutionState.AMBIGUOUS
                  and timed_out[1] is None)

            cancel_context = RequestExecutionContext(profile=profile)
            cancelled_calls = 0
            async def cancel(_request):
                nonlocal cancelled_calls
                cancelled_calls += 1
                cancel_context.cancel("disconnect")
                raise asyncio.TimeoutError("cancel race")
            try:
                await llm.adjudicate(cancel, mention="NVIDA-cancel",
                    context="ctx", candidates=candidates, snapshot=snapshot,
                    policy=policy, execution_context=cancel_context)
            except RequestCancelled:
                pass
            check("RT065.cancel_propagates_without_retry",
                  cancelled_calls == 1 and cancel_context.cancelled.is_set())

        asyncio.run(runtime_cases())


def test_rt066_rt067_lifecycle_rules():
    print("RT-066/067 — provisional lifecycle and rules")
    with tempfile.TemporaryDirectory() as root:
        store, nvidia, apple_org, _ = seeded(root)
        provisional, _ = store.create_entity("Proposal", "TECHNOLOGY", actor="resolver",
                                             reason="NEW proposal")
        check("RT066.new_defaults_provisional", provisional["lifecycle"] == "PROVISIONAL")
        admin = EntityAdminService(store, operator_key="operator-secret-123")
        promoted = admin.promote("operator-secret-123", provisional["entity_id"],
            actor="alice", reason="reviewed", provenance="source review")
        check("RT066.promotion_transaction_audited", promoted["lifecycle"] == "ACTIVE"
              and any(a["action"] == "ENTITY_UPDATE" for a in store.audit_records()))
        rejected, _ = store.create_entity("Rejected", "ORG", actor="resolver", reason="NEW")
        admin.reject("operator-secret-123", rejected["entity_id"], actor="alice", reason="invalid")
        check("RT066.rejection_lifecycle", store.get_entity(rejected["entity_id"])["lifecycle"] == "REJECTED")
        rule = admin.override("operator-secret-123", {"mention": "GPU leader"}, nvidia["entity_id"],
                              actor="alice", reason="reviewed mapping")
        decision = CanonicalEntityResolver(IdentitySnapshotView(build_identity_snapshot(store))).resolve("GPU leader")
        check("RT067.override_precedence", decision.selected_entity_id == nvidia["entity_id"]
              and rule in decision.override_block_findings)
        stale = store.add_rule("OVERRIDE", {"mention": "stale alias"},
            target_entity_id=apple_org["entity_id"], actor="alice", reason="old",
            review_due_at="2000-01-01T00:00:00+00:00")
        stale_decision = CanonicalEntityResolver(
            IdentitySnapshotView(build_identity_snapshot(store))).resolve("stale alias")
        check("RT067.stale_review_cannot_be_overwritten", stale_decision.decision == ResolutionState.BLOCKED
              and "ER_STALE_REVIEW_REQUIRED" in stale_decision.reason_codes)
        before = len(store.audit_records())
        check("RT067.audit_update_delete_forbidden", raises(sqlite3.DatabaseError,
              lambda: _tamper_audit(store)))
        check("RT067.audit_remains_append_only", len(store.audit_records()) == before)


def _tamper_audit(store):
    with store.transaction() as conn:
        conn.execute("DELETE FROM audit_records")


def test_rt068_mutations():
    print("RT-068 — merge/split/unmerge controlled mutations")
    with tempfile.TemporaryDirectory() as root:
        store, nvidia, apple_org, apple_product = seeded(root)
        admin = EntityAdminService(store, operator_key="operator-secret-123")
        preview = admin.merge_dry_run("operator-secret-123", [apple_product["entity_id"]],
            apple_org["entity_id"], actor="alice", reason="reviewed legal merge")
        check("RT068.merge_dry_run_nonmutating",
              store.get_entity(apple_product["entity_id"])["lifecycle"] == "ACTIVE"
              and preview.plan["impact"]["snapshot_rebuild_required"])
        check("RT068.confirmation_binds_plan_hash", raises(IdentityConflict,
              lambda: admin.confirm_merge("operator-secret-123", preview, "bad-token")))
        result = admin.confirm_merge("operator-secret-123", preview, preview.confirmation_token)
        check("RT068.merge_tombstone_redirect", store.get_entity(apple_product["entity_id"])["lifecycle"] == "TOMBSTONED"
              and store.get_entity(apple_product["entity_id"])["redirect_entity_id"] == apple_org["entity_id"])
        check("RT068.merge_builds_new_unpublished_snapshot", result["snapshot"]["identity_snapshot_id"]
              and result["serving_changed"] is False)
        store.update_entity(nvidia["entity_id"], canonical_name="NVIDIA Corporation",
                            actor="bob", reason="later dependent mutation")
        unmerge = admin.unmerge_dry_run("operator-secret-123", preview.operation_id,
                                       actor="alice", reason="reverse")
        check("RT068.dependent_mutation_stops_blind_unmerge",
              unmerge.plan["dependent_event_ids"] and raises(DependentMutationConflict,
              lambda: admin.confirm_unmerge("operator-secret-123", unmerge,
                                            unmerge.confirmation_token)))
    with tempfile.TemporaryDirectory() as root:
        store, _, destination, source = seeded(root)
        admin = EntityAdminService(store, operator_key="operator-secret-123")
        preview = admin.merge_dry_run("operator-secret-123",
            [source["entity_id"]], destination["entity_id"], actor="alice",
            reason="reversible merge")
        admin.confirm_merge("operator-secret-123", preview,
                            preview.confirmation_token)
        merged_snapshot = build_identity_snapshot(store)
        later_decision = CanonicalEntityResolver(
            IdentitySnapshotView(merged_snapshot)).resolve("Apple")
        later_mention = materialize_mention(store, record_id="rec-later",
            source_snapshot_id="ss-later", canonical_text="Apple announced.",
            surface="Apple", start_offset=0, decision=later_decision,
            resolver_version="er-v2.0",
            identity_snapshot_id=merged_snapshot["identity_snapshot_id"])
        unmerge = admin.unmerge_dry_run("operator-secret-123",
            preview.operation_id, actor="alice", reason="reviewed reversal")
        restored = admin.confirm_unmerge("operator-secret-123", unmerge,
                                         unmerge.confirmation_token)
        conn = store._connect()
        try:
            later_row = dict(conn.execute(
                "SELECT * FROM mentions WHERE mention_id=?",
                (later_mention["mention_id"],)).fetchone())
        finally:
            conn.close()
        check("RT068.unmerge_reconstructs_from_event_history",
              restored["later_mentions_re_resolve"]
              and store.get_entity(source["entity_id"])["lifecycle"] == "ACTIVE"
              and any(alias["entity_id"] == source["entity_id"]
                      and alias["surface"] == "Apple"
                      for alias in store.aliases()))
        check("RT068.later_mention_returns_to_resolution_queue",
              later_row["entity_id"] is None
              and later_row["decision"] == "AMBIGUOUS")
    with tempfile.TemporaryDirectory() as root:
        store, _, destination, source = seeded(root)
        serving = {"identity_snapshot_id": "ids_previous"}
        def publish(snapshot):
            serving["identity_snapshot_id"] = snapshot["identity_snapshot_id"]
            return "manifest-resumed"
        admin = EntityAdminService(store, operator_key="operator-secret-123",
                                   publish_callback=publish)
        preview = admin.merge_dry_run("operator-secret-123",
            [source["entity_id"]], destination["entity_id"], actor="alice",
            reason="crash recovery")
        def crash(stage):
            if stage == "after_build_before_switch":
                raise RuntimeError("injected crash")
        crashed = raises(RuntimeError, lambda: admin.confirm_merge(
            "operator-secret-123", preview, preview.confirmation_token,
            crash_hook=crash))
        pending = admin.pending_operations("operator-secret-123")
        check("RT068.crash_after_build_leaves_serving_unchanged",
              crashed and serving["identity_snapshot_id"] == "ids_previous"
              and pending and pending[0]["snapshot_built"])
        resumed = admin.resume_publish("operator-secret-123",
            preview.operation_id, actor="alice", reason="resume checkpoint")
        check("RT068.checkpoint_resume_atomic_publish",
              resumed["published_manifest_id"] == "manifest-resumed"
              and serving["identity_snapshot_id"] ==
                  resumed["snapshot"]["identity_snapshot_id"]
              and not admin.pending_operations("operator-secret-123"))
    with tempfile.TemporaryDirectory() as root:
        store, nvidia, *_ = seeded(root)
        snap = build_identity_snapshot(store)
        decision = CanonicalEntityResolver(IdentitySnapshotView(snap)).resolve("NVIDIA")
        text = "NVIDIA announced Blackwell."
        mention = materialize_mention(store, record_id="rec-1", source_snapshot_id="ss-1",
            canonical_text=text, surface="NVIDIA", start_offset=0, decision=decision,
            resolver_version="er-v2.0", identity_snapshot_id=snap["identity_snapshot_id"])
        admin = EntityAdminService(store, operator_key="operator-secret-123")
        split = admin.split_dry_run("operator-secret-123", nvidia["entity_id"],
            new_name="NVIDIA Product Identity", entity_type="PRODUCT_MODEL",
            mention_ids=[mention["mention_id"]], actor="alice", reason="separate product")
        split_result = admin.confirm_split("operator-secret-123", split, split.confirmation_token)
        check("RT068.split_explicit_mention_reassignment", split_result["new_entity_id"] != nvidia["entity_id"]
              and split_result["relation_rematerialization_required"])


def test_rt068_rt072_real_rematerialization():
    print("RT-068/072 — real bounded relation rematerialization")
    with tempfile.TemporaryDirectory() as root:
        store = store_at(root)
        source, _ = store.create_entity("Old NVIDIA", "ORG", lifecycle="ACTIVE",
            actor="fixture", reason="seed", creation_key="old")
        destination, _ = store.create_entity("NVIDIA", "ORG", lifecycle="ACTIVE",
            actor="fixture", reason="seed", creation_key="dest")
        product, _ = store.create_entity("Blackwell", "PRODUCT_MODEL", lifecycle="ACTIVE",
            actor="fixture", reason="seed", creation_key="product")
        snap = build_identity_snapshot(store)
        resolver = CanonicalEntityResolver(IdentitySnapshotView(snap))
        text = "Old NVIDIA introduced Blackwell."
        source_snapshot = SourceSnapshot.from_record("record-remat", {"fb": text})
        sm = materialize_mention(store, record_id="record-remat",
            source_snapshot_id=source_snapshot.source_snapshot_id,
            canonical_text=source_snapshot.raw_text, surface="Old NVIDIA", start_offset=0,
            decision=resolver.resolve("Old NVIDIA"), resolver_version="er-v2.0",
            identity_snapshot_id=snap["identity_snapshot_id"])
        om = materialize_mention(store, record_id="record-remat",
            source_snapshot_id=source_snapshot.source_snapshot_id,
            canonical_text=source_snapshot.raw_text, surface="Blackwell", start_offset=22,
            decision=resolver.resolve("Blackwell"), resolver_version="er-v2.0",
            identity_snapshot_id=snap["identity_snapshot_id"])
        evidence = EvidenceLocator(source_snapshot).locate_text_span(
            "Old NVIDIA introduced Blackwell")
        evidence["evidence_id"] = "ev-remat-1"
        relation = materialize_relation(store, subject_mention=sm,
            predicate="INTRODUCED", object_mention=om, evidence_refs=[evidence],
            extraction_version="relation-extract-v7", resolver_version="er-v2.0",
            legacy_edge_hint="old-edge")
        serving = {"manifest": "manifest-before"}
        def publish(snapshot):
            serving["manifest"] = "manifest-" + snapshot["identity_snapshot_id"]
            return serving["manifest"]
        admin = EntityAdminService(store, operator_key="operator-secret-123",
                                   publish_callback=publish)
        preview = admin.merge_dry_run("operator-secret-123", [source["entity_id"]],
            destination["entity_id"], actor="alice", reason="lineage merge")
        remat_plan = preview.plan["rematerialization"]
        check("RT068.rematerialization_dry_run_is_bounded_and_hashed",
              remat_plan["bounded"] and remat_plan["plan_hash"]
              and remat_plan["source_snapshot_ids"] == [source_snapshot.source_snapshot_id]
              and remat_plan["evidence_ref_ids"] == ["ev-remat-1"]
              and set(remat_plan["lineage_mention_ids"]) ==
                  {sm["mention_id"], om["mention_id"]})
        def crash(stage):
            if stage == "during_rematerialization":
                raise RuntimeError("injected rematerialization crash")
        failed = raises(RuntimeError, lambda: admin.confirm_merge(
            "operator-secret-123", preview, preview.confirmation_token,
            crash_hook=crash))
        conn = store._connect()
        try:
            after_failure = [dict(row) for row in conn.execute(
                "SELECT * FROM relation_assertions ORDER BY created_at")]
        finally:
            conn.close()
        check("RT068.rematerialization_failure_keeps_previous_manifest",
              failed and serving["manifest"] == "manifest-before"
              and len(after_failure) == 1
              and after_failure[0]["subject_entity_id"] == source["entity_id"])
        merged = admin.confirm_merge("operator-secret-123", preview,
                                     preview.confirmation_token)
        rebuilt = merged["rematerialization"]["rebuilt_relations"][0]
        conn = store._connect()
        try:
            old_row = dict(conn.execute("SELECT * FROM relation_assertions WHERE relation_id=?",
                                        (relation["relation_id"],)).fetchone())
            new_row = dict(conn.execute("SELECT * FROM relation_assertions WHERE relation_id=?",
                                        (rebuilt["new_relation_id"],)).fetchone())
        finally:
            conn.close()
        provenance = json.loads(new_row["provenance"])
        check("RT072.rebuilt_relation_is_not_endpoint_rewrite",
              old_row["subject_entity_id"] == source["entity_id"]
              and old_row["assertion_status"] == "SUPERSEDED"
              and new_row["relation_id"] != old_row["relation_id"]
              and new_row["subject_entity_id"] == destination["entity_id"])
        check("RT072.rebuilt_relation_preserves_exact_authority",
              new_row["source_snapshot_id"] == source_snapshot.source_snapshot_id
              and json.loads(new_row["evidence_refs_json"])[0]["evidence_id"] == "ev-remat-1"
              and json.loads(new_row["source_mention_ids_json"]) ==
                  [sm["mention_id"], om["mention_id"]]
              and new_row["extraction_version"] == "relation-extract-v7"
              and provenance["legacy_edge_is_authority"] is False
              and provenance["rematerialized_from_relation_id"] == relation["relation_id"])
        split = admin.split_dry_run("operator-secret-123", destination["entity_id"],
            new_name="NVIDIA Product Context", entity_type="PRODUCT_MODEL",
            mention_ids=[sm["mention_id"]], actor="alice", reason="lineage split")
        split_result = admin.confirm_split("operator-secret-123", split,
                                           split.confirmation_token)
        split_rebuilt = split_result["rematerialization"]["rebuilt_relations"][0]
        check("RT072.split_rebuilds_relation_from_mention_lineage",
              split_rebuilt["old_relation_id"] == new_row["relation_id"]
              and split_rebuilt["subject_entity_id"] == split_result["new_entity_id"]
              and split_rebuilt["source_snapshot_id"] == source_snapshot.source_snapshot_id
              and split_rebuilt["evidence_ref_ids"] == ["ev-remat-1"])


def test_rt070_snapshot_runtime():
    print("RT-070 — immutable snapshot and request pin")
    with tempfile.TemporaryDirectory() as root:
        store, nvidia, *_ = seeded(root)
        snap_a = build_identity_snapshot(store, created_at="2026-08-26T00:00:00+00:00")
        path = write_identity_snapshot(snap_a, Path(root) / "identity-a.json")
        check("RT070.snapshot_build_validate_hash", path.is_file()
              and not validate_identity_snapshot(snap_a))
        malformed = dict(snap_a); malformed["entities"] = []
        check("RT070.malformed_snapshot_rejected", bool(validate_identity_snapshot(malformed)))
        runtime_a = RuntimeSnapshot("manifest-a", {"artifacts": {}},
                                    {"identity_snapshot": snap_a})
        result_a = resolve_query_from_runtime_snapshot("NVIDIA", runtime_a)
        store.update_entity(nvidia["entity_id"], canonical_name="NVIDIA Corp",
                            actor="op", reason="rename")
        snap_b = build_identity_snapshot(store, created_at="2026-08-26T00:01:00+00:00")
        runtime_b = RuntimeSnapshot("manifest-b", {"artifacts": {}},
                                    {"identity_snapshot": snap_b})
        result_b = resolve_query_from_runtime_snapshot("NVIDIA Corp", runtime_b)
        result_a_again = resolve_query_from_runtime_snapshot("NVIDIA", runtime_a)
        check("RT070.request_pin_survives_concurrent_reload",
              result_a["identity_snapshot_id"] == result_a_again["identity_snapshot_id"]
              and result_a["identity_snapshot_id"] != result_b["identity_snapshot_id"])
        check("RT070.runtime_never_reads_mutable_store",
              result_a["mutable_store_read"] is False)
        from release_manifest import (REQUIRED_ARTIFACTS, ReleaseCatalog,
                                      build_global_manifest, build_source_catalog)
        from runtime_snapshot import load_release_resources
        release_root = Path(root) / "release"; release_root.mkdir()
        record = {"record_id": "record-er-1", "fb": "identity release fixture",
                  "evidence_eligibility": "CITATION_ELIGIBLE"}
        source_catalog = build_source_catalog([{
            "record_id": record["record_id"], "source_snapshot_id": "ss-er-1",
            "evidence_text": record["fb"],
            "evidence_eligibility": "CITATION_ELIGIBLE"}])
        payloads = {
            "dataset": {"schema_version":"1.0.0", "records":[record]},
            "record_id_map": {"schema_version":"1.0.0", "by_record_id":{"record-er-1":"record-er-1"}},
            "source_catalog": source_catalog,
            "evidence_metadata": {"schema_version":"1.0.0", "record-er-1":{"evidence_eligibility":"CITATION_ELIGIBLE"}},
            "identity_snapshot": snap_b,
            "vector_index": {"schema_version":"1.0.0", "documents":[{"record_id":"record-er-1","vector":[1.0,0.0]}]},
            "bm25_index": {"schema_version":"1.0.0", "documents":[{"record_id":"record-er-1","tokens":["identity","release"]}]},
            "chunk_index": {"schema_version":"1.0.0", "chunks":[]},
            "graph_index": {"schema_version":"1.0.0", "results_by_query":{}},
            "numeric_index": {"schema_version":"1.0.0", "facts":[]},
            "prompts": {"schema_version":"1.0.0", "versions":{}},
        }
        artifacts = {}
        for name, payload in payloads.items():
            artifact = release_root / f"{name}.json"
            artifact.write_text(json.dumps(payload, sort_keys=True), "utf-8")
            artifacts[name] = artifact
        manifest = build_global_manifest(release_root=release_root,
            artifacts=artifacts, profile={"name":"phase06-test", "vector_dim":2},
            models={"embedding_dim":2})
        catalog = ReleaseCatalog(release_root / "manifests", release_root)
        catalog.store(manifest); catalog.activate(manifest["manifest_id"])
        loaded = load_release_resources(catalog.load(manifest["manifest_id"]),
                                        release_root=release_root)
        check("RT070.global_manifest_binds_validated_identity_snapshot",
              set(artifacts) == REQUIRED_ARTIFACTS
              and loaded["identity_snapshot"]["identity_snapshot_id"] == snap_b["identity_snapshot_id"]
              and catalog.pointer("current") == manifest["manifest_id"])


def test_rt071_migration():
    print("RT-071 — legacy migration")
    payload = {"entities": [
        {"entity_id": "org:nvidia", "canonical_name": "NVIDIA", "entity_type": "ORG", "aliases": ["英伟达"], "mention_count": 500},
        {"entity_id": "org:apple", "canonical_name": "Apple", "entity_type": "ORG"},
        {"entity_id": "concept:apple", "canonical_name": "Apple", "entity_type": "OTHER_DOMAIN"},
    ]}
    with tempfile.TemporaryDirectory() as root:
        store = store_at(root)
        first = migrate_legacy_registry(store, payload)
        second = migrate_legacy_registry(store, payload)
        check("RT071.legacy_ids_map_to_opaque_or_ambiguous",
              all((not m.get("entity_id") or m["entity_id"].startswith("ent_")) for m in first["mappings"]))
        check("RT071.ambiguous_alias_not_forced_merge", first["ambiguous"] >= 1)
        check("RT071.high_impact_review_queue", first["high_impact_review_required"] == 1
              and first["graph_v2_activation_ready"] is False)
        check("RT071.migration_rerun_idempotent", second["rerun_existing"] == 3
              and len(store.list_entities()) == first["migrated_unique"])


def test_rt072_relation_lineage():
    print("RT-072 — source-backed mention/relation lineage")
    with tempfile.TemporaryDirectory() as root:
        store = store_at(root)
        subject, _ = store.create_entity("NVIDIA", "ORG", lifecycle="ACTIVE",
            actor="fixture", reason="seed", creation_key="n")
        obj, _ = store.create_entity("Blackwell", "PRODUCT_MODEL", lifecycle="ACTIVE",
            actor="fixture", reason="seed", creation_key="b")
        snap = build_identity_snapshot(store)
        resolver = CanonicalEntityResolver(IdentitySnapshotView(snap))
        source = SourceSnapshot.from_record("record-1", {"fb": "NVIDIA introduced Blackwell."})
        locator = EvidenceLocator(source).locate_text_span("NVIDIA introduced Blackwell")
        sm = materialize_mention(store, record_id="record-1", source_snapshot_id=source.source_snapshot_id,
            canonical_text=source.raw_text, surface="NVIDIA", start_offset=0,
            decision=resolver.resolve("NVIDIA"), resolver_version="er-v2.0",
            identity_snapshot_id=snap["identity_snapshot_id"])
        om = materialize_mention(store, record_id="record-1", source_snapshot_id=source.source_snapshot_id,
            canonical_text=source.raw_text, surface="Blackwell", start_offset=18,
            decision=resolver.resolve("Blackwell"), resolver_version="er-v2.0",
            identity_snapshot_id=snap["identity_snapshot_id"])
        locator["evidence_id"] = "ev-1"
        relation = materialize_relation(store, subject_mention=sm, predicate="INTRODUCED",
            object_mention=om, evidence_refs=[locator], legacy_edge_hint="NVIDIA--Blackwell")
        check("RT072.source_mention_relation_evidence_chain",
              relation["source_snapshot_id"] == source.source_snapshot_id
              and relation["evidence_refs"][0]["start_offset"] == 0
              and len(relation["source_mention_ids"]) == 2)
        check("RT072.legacy_edge_without_evidence_rejected", raises(ValueError,
              lambda: materialize_relation(store, subject_mention=sm,
                  predicate="USES", object_mention=om, evidence_refs=[],
                  legacy_edge_hint="legacy")))
        plan = rematerialization_plan(store, [subject["entity_id"], obj["entity_id"]])
        check("RT072.merge_split_rematerialization_lineage", relation["relation_id"] in plan["relation_ids"]
              and plan["source_of_truth"] == "SourceSnapshot+MentionOffsets+EvidenceRef")


def test_rt073_query_resolution():
    print("RT-073 — query resolver production seam")
    with tempfile.TemporaryDirectory() as root:
        store, nvidia, *_ = seeded(root)
        snap = build_identity_snapshot(store)
        resolver = QueryEntityResolver(snap)
        decisions = resolver.resolve_query("英伟达 NVIDIA NVDA roadmap")
        check("RT073.exact_acronym_query_parse", decisions
              and any(d.selected_entity_id == nvidia["entity_id"] for d in decisions))
        check("RT073.query_resolution_is_read_only", store.revision() == snap["source_store_revision"])
        paper, _ = store.create_entity("Known DOI", "OTHER_DOMAIN", lifecycle="ACTIVE",
            actor="fixture", reason="doi", creation_key="known-doi")
        store.add_strong_id(paper["entity_id"], "DOI", "10.5555/known.2026",
            provenance="crossref", actor="fixture", reason="doi")
        typed_snap = build_identity_snapshot(store)
        typed_resolver = QueryEntityResolver(typed_snap)
        parsed = typed_resolver.parse("compare DOI:10.5555/known.2026 with NASDAQ:NVDA")
        typed = typed_resolver.resolve_query(
            "compare DOI:10.5555/known.2026 with NASDAQ:NVDA")
        check("RT073.parser_emits_typed_strong_identifiers",
              any(item["strong_ids"] == [{"id_type": "DOI", "value": "10.5555/known.2026"}]
                  for item in parsed)
              and any(item["strong_ids"] == [{"id_type": "EXCHANGE_TICKER", "value": "NASDAQ:NVDA"}]
                  for item in parsed)
              and {d.selected_entity_id for d in typed} >=
                  {paper["entity_id"], nvidia["entity_id"]})
        unknown = typed_resolver.resolve_query("DOI:10.5555/unknown.2026")
        malformed = typed_resolver.resolve_query("DOI:10.bad/not-a-doi")
        check("RT073.unknown_and_malformed_strong_ids_fail_safe",
              unknown and unknown[0].decision == ResolutionState.BLOCKED
              and unknown[0].selected_entity_id is None
              and malformed and malformed[0].decision == ResolutionState.BLOCKED
              and malformed[0].selected_entity_id is None)
        typed_runtime = RuntimeSnapshot("manifest-typed", {"artifacts": {}},
                                        {"identity_snapshot": typed_snap})
        production_typed = resolve_query_from_runtime_snapshot(
            "compare DOI:10.5555/known.2026 with NASDAQ:NVDA", typed_runtime)
        production_wrong_type = resolve_query_from_runtime_snapshot(
            "PERSON: NASDAQ:NVDA", typed_runtime)
        check("RT073.production_pinned_snapshot_consumes_typed_ids",
              production_typed["identity_snapshot_id"] == typed_snap["identity_snapshot_id"]
              and {d["selected_entity_id"] for d in production_typed["decisions"]
                   if d["decision"] == "LINK"} >=
                  {paper["entity_id"], nvidia["entity_id"]}
              and any(d["decision"] == "BLOCKED"
                      and "ER_STRONG_ID_TYPE_CONFLICT" in d["reason_codes"]
                      for d in production_wrong_type["decisions"]))
        runtime = RuntimeSnapshot("manifest-pinned", {"artifacts": {}},
                                  {"identity_snapshot": snap})
        production = resolve_query_from_runtime_snapshot("Yingweida", runtime)
        check("RT073.production_caller_uses_pinned_snapshot",
              production["identity_snapshot_id"] == snap["identity_snapshot_id"]
              and production["mutable_store_read"] is False)
        server_source = (HERE / "server.py").read_text("utf-8")
        check("RT073.actual_server_wiring_and_vector_fallback",
              "resolve_query_from_runtime_snapshot" in server_source
              and "ER_RESOLVER_DEGRADED_NO_IDENTITY_ASSUMED" in server_source
              and "Graph-V2 remains production-off" in server_source)


def test_rt074_admin_security():
    print("RT-074 — authenticated admin + sanitization/audit")
    with tempfile.TemporaryDirectory() as root:
        store, nvidia, *_ = seeded(root)
        admin = EntityAdminService(store, operator_key="operator-secret-123")
        check("RT074.unauthorized_rejected", raises(AdminAuthError,
              lambda: admin.rename("wrong", nvidia["entity_id"], "x", actor="x", reason="x")))
        renamed = admin.rename("operator-secret-123", nvidia["entity_id"],
            "<script>alert(1)</script>\x00 NVIDIA", actor="alice", reason="sanitize")
        check("RT074.xss_control_chars_sanitized", "<script>" not in renamed["canonical_name"]
              and "\x00" not in renamed["canonical_name"])
        check("RT074.mutation_audited_with_actor_reason",
              any(a["actor"] == "alice" and a["reason"] == "sanitize"
                  for a in store.audit_records()))
        cli_source = (HERE / "entity_admin_cli.py").read_text("utf-8")
        check("RT074.operator_cli_uses_server_environment_auth",
              "TECH_DB_OPERATOR_KEY" in cli_source
              and 'add_argument("--key"' not in cli_source
              and "EntityAdminService" in cli_source)


def test_rt075_shadow():
    print("RT-075 — shadow non-interference and activation honesty")
    monitor = EntityShadowMonitor(window_type="CI_REPLAY")
    monitor.observe(serving_decision={"decision": "BLOCKED"},
        shadow_decision={"decision": "LINK", "selected_entity_id": "ent_x"},
        entity_class="ORG", latency_ms=3, model_calls=1, cost_proxy=.1,
        labeled_truth_entity_id="ent_y", source="query")
    report = monitor.report(duration_days=0,
                            equivalent_replay_explicitly_approved=False)
    check("RT075.shadow_non_interference", all(v == 0 for v in report["non_interference"].values()))
    check("RT075.injected_false_link_and_block_alert",
          report["false_link_candidates"] == 1
          and report["rollback_triggers"]["block_rule_violation"])
    check("RT075.activation_evidence_honest", report["representative_event_count"] == 1
          and report["activation_gate_satisfied"] is False
          and report["production_activation_claim"] is False)
    check("RT075.report_schema_metrics", report["schema_version"] == "entity-shadow-report-1.0"
          and report["model_calls"] == 1 and report["report_hash"])
    ingest_source = (HERE / "ingest.py").read_text("utf-8")
    check("RT075.actual_ingest_shadow_wiring",
          "TECH_DB_IDENTITY_SHADOW_SNAPSHOT" in ingest_source
          and "resolve_ingest_shadow" in ingest_source
          and "equivalent_replay_explicitly_approved=False" in ingest_source)
    server_source = (HERE / "server.py").read_text("utf-8")
    check("RT075.actual_query_shadow_wiring",
          "TECH_DB_ENTITY_QUERY_SHADOW" in server_source
          and "_ENTITY_QUERY_SHADOW.observe" in server_source
          and 'source="query"' in server_source)


def main():
    test_rt060_rt061_identity_store()
    test_rt062_rt063_rt064_resolution()
    test_rt065_llm_constraints()
    test_rt066_rt067_lifecycle_rules()
    test_rt068_mutations()
    test_rt068_rt072_real_rematerialization()
    test_rt070_snapshot_runtime()
    test_rt071_migration()
    test_rt072_relation_lineage()
    test_rt073_query_resolution()
    test_rt074_admin_security()
    test_rt075_shadow()
    print("=" * 64)
    print(f"  Phase 06: {PASSED} passed, {len(FAILED)} failed")
    print("=" * 64)
    if FAILED:
        print("FAILED:", ", ".join(FAILED)); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
