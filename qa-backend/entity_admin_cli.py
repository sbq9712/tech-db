#!/usr/bin/env python3
"""Operator-only CLI for the canonical IdentityStore control plane (RT-074)."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from entity_admin import EntityAdminService, MutationPreview
from identity_store import IdentityStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tech-DB Entity Admin")
    parser.add_argument("--db", default=os.environ.get("TECH_DB_IDENTITY_DB"))
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", default="operator inspection")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search"); search.add_argument("query")
    inspect = sub.add_parser("inspect"); inspect.add_argument("entity_id")
    rename = sub.add_parser("rename"); rename.add_argument("entity_id"); rename.add_argument("name")
    promote = sub.add_parser("promote"); promote.add_argument("entity_id"); promote.add_argument("--provenance", required=True)
    reject = sub.add_parser("reject"); reject.add_argument("entity_id")
    alias_add = sub.add_parser("alias-add"); alias_add.add_argument("entity_id"); alias_add.add_argument("surface")
    alias_unlink = sub.add_parser("alias-unlink"); alias_unlink.add_argument("alias_id")
    strong_link = sub.add_parser("strong-link"); strong_link.add_argument("entity_id"); strong_link.add_argument("id_type"); strong_link.add_argument("value"); strong_link.add_argument("--provenance", required=True)
    strong_unlink = sub.add_parser("strong-unlink"); strong_unlink.add_argument("strong_id_id")
    block = sub.add_parser("block"); block.add_argument("condition_json")
    unblock = sub.add_parser("unblock"); unblock.add_argument("rule_id")
    override = sub.add_parser("override"); override.add_argument("condition_json"); override.add_argument("target_entity_id")
    merge = sub.add_parser("merge-dry-run"); merge.add_argument("destination_id"); merge.add_argument("source_ids", nargs="+")
    split = sub.add_parser("split-dry-run"); split.add_argument("source_id"); split.add_argument("new_name"); split.add_argument("entity_type"); split.add_argument("mention_ids", nargs="+")
    unmerge = sub.add_parser("unmerge-dry-run"); unmerge.add_argument("merge_operation_id")
    confirm = sub.add_parser("confirm"); confirm.add_argument("preview_json_file")
    audit = sub.add_parser("audit")
    args = parser.parse_args(argv)
    operator_key = os.environ.get("TECH_DB_OPERATOR_KEY", "")
    if not args.db:
        parser.error("--db or TECH_DB_IDENTITY_DB is required")
    # The key is accepted only from the server/operator environment and is
    # never printed, persisted, or accepted as a public query parameter.
    service = EntityAdminService(IdentityStore(Path(args.db)),
                                 operator_key=operator_key)
    if args.command == "search":
        result = service.search(operator_key, args.query)
    elif args.command == "inspect":
        result = service.inspect(operator_key, args.entity_id)
    elif args.command == "rename":
        result = service.rename(operator_key, args.entity_id, args.name,
                                actor=args.actor, reason=args.reason)
    elif args.command == "promote":
        result = service.promote(operator_key, args.entity_id,
            actor=args.actor, reason=args.reason, provenance=args.provenance)
    elif args.command == "reject":
        result = service.reject(operator_key, args.entity_id,
                                actor=args.actor, reason=args.reason)
    elif args.command == "alias-add":
        result = {"alias_id": service.add_alias(operator_key, args.entity_id,
            args.surface, actor=args.actor, reason=args.reason)}
    elif args.command == "alias-unlink":
        result = service.unlink_alias(operator_key, args.alias_id,
            actor=args.actor, reason=args.reason)
    elif args.command == "strong-link":
        result = {"strong_id_id": service.link_strong_id(operator_key,
            args.entity_id, args.id_type, args.value, provenance=args.provenance,
            actor=args.actor, reason=args.reason)}
    elif args.command == "strong-unlink":
        result = service.unlink_strong_id(operator_key, args.strong_id_id,
            actor=args.actor, reason=args.reason)
    elif args.command == "block":
        result = {"rule_id": service.block(operator_key,
            json.loads(args.condition_json), actor=args.actor, reason=args.reason)}
    elif args.command == "unblock":
        result = service.revoke_rule(operator_key, args.rule_id,
            actor=args.actor, reason=args.reason)
    elif args.command == "override":
        result = {"rule_id": service.override(operator_key,
            json.loads(args.condition_json), args.target_entity_id,
            actor=args.actor, reason=args.reason)}
    elif args.command == "merge-dry-run":
        result = asdict(service.merge_dry_run(operator_key, args.source_ids,
            args.destination_id, actor=args.actor, reason=args.reason))
    elif args.command == "split-dry-run":
        result = asdict(service.split_dry_run(operator_key, args.source_id,
            new_name=args.new_name, entity_type=args.entity_type,
            mention_ids=args.mention_ids, actor=args.actor, reason=args.reason))
    elif args.command == "unmerge-dry-run":
        result = asdict(service.unmerge_dry_run(operator_key,
            args.merge_operation_id, actor=args.actor, reason=args.reason))
    elif args.command == "confirm":
        raw = json.loads(Path(args.preview_json_file).read_text("utf-8"))
        preview = MutationPreview(**raw)
        if preview.operation == "MERGE":
            result = service.confirm_merge(operator_key, preview,
                                           preview.confirmation_token)
        elif preview.operation == "SPLIT":
            result = service.confirm_split(operator_key, preview,
                                           preview.confirmation_token)
        elif preview.operation == "UNMERGE":
            result = service.confirm_unmerge(operator_key, preview,
                                             preview.confirmation_token)
        else:
            parser.error("unsupported preview operation")
    else:
        service.authenticate(operator_key)
        result = service.store.audit_records()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
