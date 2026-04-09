#!/usr/bin/env python3
"""
OCNDP sync worker.

Implements discovery sync for suggestion #1:
- Poll Discord registry channel messages
- Parse OCNDP registration payloads from JSON code blocks
- Apply protocol filters (version, nodeId format, staleness)
- De-duplicate by nodeId (keep newest timestamp)
- Upsert records in memory/known-nodes.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


OCNDP_VERSION = "ocndp/1.0"
MAX_REGISTRATION_AGE_SECONDS = 48 * 60 * 60
FUTURE_SKEW_SECONDS = 5 * 60
NODE_ID_PATTERN = re.compile(r"^[a-z0-9-]{3,64}$")
JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STATE_FILE = ROOT_DIR / "memory/ocndp-state.json"
DEFAULT_KNOWN_FILE = ROOT_DIR / "memory/known-nodes.json"


@dataclass
class SyncStats:
    fetched_messages: int = 0
    no_payload: int = 0
    invalid_payload: int = 0
    stale_payload: int = 0
    future_payload: int = 0
    skipped_self: int = 0
    deduped_older: int = 0
    valid_candidates: int = 0
    new_nodes: int = 0
    updated_nodes: int = 0


def now_epoch() -> int:
    return int(time.time())


def load_json_file(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return fallback.copy()
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def extract_json_objects_from_message(content: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for match in JSON_BLOCK_PATTERN.finditer(content or ""):
        raw = match.group(1)
        try:
            candidate = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payloads.append(candidate)
    return payloads


def validate_registration_payload(payload: dict[str, Any], now_ts: int) -> tuple[bool, str]:
    if payload.get("version") != OCNDP_VERSION:
        return False, "invalid-version"

    required_fields = ("nodeId", "owner", "gatewayUrl", "timestamp")
    for field in required_fields:
        value = payload.get(field)
        if value is None or value == "":
            return False, f"missing-{field}"

    node_id = payload["nodeId"]
    if not isinstance(node_id, str) or not NODE_ID_PATTERN.match(node_id):
        return False, "invalid-nodeId"

    ts = payload["timestamp"]
    if not isinstance(ts, (int, float)):
        return False, "invalid-timestamp-type"
    ts = int(ts)

    if ts > now_ts + FUTURE_SKEW_SECONDS:
        return False, "future"
    if (now_ts - ts) > MAX_REGISTRATION_AGE_SECONDS:
        return False, "stale"

    return True, "ok"


def parse_ocndp_payload(message: dict[str, Any], now_ts: int) -> tuple[dict[str, Any] | None, str]:
    candidates = extract_json_objects_from_message(str(message.get("content", "")))
    if not candidates:
        return None, "no-payload"

    saw_stale = False
    saw_future = False
    for payload in candidates:
        valid, reason = validate_registration_payload(payload, now_ts)
        if valid:
            return payload, "ok"
        if reason == "stale":
            saw_stale = True
        elif reason == "future":
            saw_future = True
    if saw_stale:
        return None, "stale"
    if saw_future:
        return None, "future"
    return None, "invalid"


def fetch_discord_messages(channel_id: str, bot_token: str, limit: int) -> list[dict[str, Any]]:
    query = urlencode({"limit": max(1, min(limit, 100))})
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?{query}"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "User-Agent": "clawsocial-ocndp-sync/1.0",
    }
    request = Request(url=url, headers=headers, method="GET")
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Discord API returned non-list payload for messages endpoint")
    return payload


def load_messages_from_input_file(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        messages = data
    elif isinstance(data, dict) and isinstance(data.get("messages"), list):
        messages = data["messages"]
    else:
        raise ValueError("Input file must be a list of messages or an object with `messages` list")
    return [m for m in messages if isinstance(m, dict)]


def _to_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str)]


def upsert_nodes(
    known_nodes_doc: dict[str, Any],
    candidate_payloads: dict[str, dict[str, Any]],
    now_ts: int,
    stats: SyncStats,
) -> dict[str, Any]:
    nodes = known_nodes_doc.get("nodes")
    if not isinstance(nodes, list):
        nodes = []
        known_nodes_doc["nodes"] = nodes

    existing_by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if isinstance(node, dict) and isinstance(node.get("nodeId"), str):
            existing_by_id[node["nodeId"]] = node

    for node_id, payload in candidate_payloads.items():
        if node_id in existing_by_id:
            existing = existing_by_id[node_id]
            existing["owner"] = payload.get("owner", existing.get("owner"))
            existing["gatewayUrl"] = payload.get("gatewayUrl", existing.get("gatewayUrl"))
            existing["discordHandle"] = payload.get("discordHandle")
            existing["capabilities"] = _to_str_list(payload.get("capabilities"))
            existing["description"] = str(payload.get("description", "")) if payload.get("description") is not None else ""
            existing["lastSeenRegistration"] = int(payload["timestamp"])
            existing["lastDiscoveryAt"] = now_ts
            existing["source"] = "discord-registry"
            stats.updated_nodes += 1
            continue

        node_record = {
            "nodeId": node_id,
            "owner": payload.get("owner"),
            "gatewayUrl": payload.get("gatewayUrl"),
            "discordHandle": payload.get("discordHandle"),
            "capabilities": _to_str_list(payload.get("capabilities")),
            "description": str(payload.get("description", "")) if payload.get("description") is not None else "",
            "status": "discovered",
            "trustScore": 0,
            "trustReason": "Not evaluated yet",
            "firstSeen": now_ts,
            "lastSeenRegistration": int(payload["timestamp"]),
            "lastDiscoveryAt": now_ts,
            "spreadSent": False,
            "spreadOptOut": False,
            "source": "discord-registry",
        }
        nodes.append(node_record)
        existing_by_id[node_id] = node_record
        stats.new_nodes += 1

    known_nodes_doc["lastDiscovery"] = now_ts
    known_nodes_doc["totalFriends"] = sum(
        1
        for n in nodes
        if isinstance(n, dict) and n.get("status") in {"friend", "trusted"}
    )
    return known_nodes_doc


def run_sync(
    state_file: Path,
    known_file: Path,
    messages: list[dict[str, Any]],
    self_node_id: str | None,
    dry_run: bool,
) -> SyncStats:
    state_doc = load_json_file(state_file, fallback={})
    known_doc = load_json_file(
        known_file,
        fallback={"nodes": [], "lastDiscovery": None, "totalFriends": 0},
    )

    stats = SyncStats(fetched_messages=len(messages))
    now_ts = now_epoch()
    self_node = self_node_id or state_doc.get("nodeId")
    latest_by_node: dict[str, dict[str, Any]] = {}

    for message in messages:
        payload, reason = parse_ocndp_payload(message, now_ts)
        if payload is None:
            if reason == "no-payload":
                stats.no_payload += 1
            elif reason == "stale":
                stats.stale_payload += 1
            elif reason == "future":
                stats.future_payload += 1
            else:
                stats.invalid_payload += 1
            continue

        node_id = payload["nodeId"]
        if self_node and node_id == self_node:
            stats.skipped_self += 1
            continue

        existing = latest_by_node.get(node_id)
        if existing is None or int(payload["timestamp"]) > int(existing["timestamp"]):
            if existing is not None:
                stats.deduped_older += 1
            latest_by_node[node_id] = payload
        else:
            stats.deduped_older += 1

    stats.valid_candidates = len(latest_by_node)
    updated_known_doc = upsert_nodes(known_doc, latest_by_node, now_ts, stats)
    state_doc["lastDiscovery"] = now_ts

    if not dry_run:
        write_json_file(known_file, updated_known_doc)
        write_json_file(state_file, state_doc)

    return stats


def print_summary(stats: SyncStats, dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "WRITE"
    print(f"[ocndp-sync] mode={mode}")
    print(f"[ocndp-sync] fetched_messages={stats.fetched_messages}")
    print(f"[ocndp-sync] no_payload={stats.no_payload}")
    print(f"[ocndp-sync] invalid_payload={stats.invalid_payload}")
    print(f"[ocndp-sync] stale_payload={stats.stale_payload}")
    print(f"[ocndp-sync] future_payload={stats.future_payload}")
    print(f"[ocndp-sync] skipped_self={stats.skipped_self}")
    print(f"[ocndp-sync] deduped_older={stats.deduped_older}")
    print(f"[ocndp-sync] valid_candidates={stats.valid_candidates}")
    print(f"[ocndp-sync] new_nodes={stats.new_nodes}")
    print(f"[ocndp-sync] updated_nodes={stats.updated_nodes}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync OCNDP nodes from Discord registry channel.")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--known-file", default=str(DEFAULT_KNOWN_FILE))
    parser.add_argument("--channel-id", default=os.getenv("OCNDP_DISCORD_CHANNEL_ID"))
    parser.add_argument("--bot-token", default=os.getenv("DISCORD_BOT_TOKEN"))
    parser.add_argument("--self-node-id", default=os.getenv("OCNDP_SELF_NODE_ID"))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--input-file",
        help="Optional local JSON file for testing (list of Discord message objects).",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    state_file = Path(args.state_file).resolve()
    known_file = Path(args.known_file).resolve()
    state_doc_for_config = load_json_file(state_file, fallback={})

    try:
        if args.input_file:
            messages = load_messages_from_input_file(Path(args.input_file).resolve())
        else:
            channel_id = args.channel_id or state_doc_for_config.get("discordChannelId")
            bot_token = args.bot_token
            if not channel_id:
                raise ValueError(
                    "Missing channel id. Set OCNDP_DISCORD_CHANNEL_ID, --channel-id, or state.discordChannelId."
                )
            if not bot_token:
                raise ValueError(
                    "Missing bot token. Set DISCORD_BOT_TOKEN or pass --bot-token."
                )
            messages = fetch_discord_messages(channel_id=channel_id, bot_token=bot_token, limit=args.limit)

        stats = run_sync(
            state_file=state_file,
            known_file=known_file,
            messages=messages,
            self_node_id=args.self_node_id,
            dry_run=bool(args.dry_run),
        )
        print_summary(stats, dry_run=bool(args.dry_run))
        return 0
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[ocndp-sync] error={exc}", file=sys.stderr)
        return 2
    except HTTPError as exc:
        print(f"[ocndp-sync] discord_http_error status={exc.code} reason={exc.reason}", file=sys.stderr)
        return 3
    except URLError as exc:
        print(f"[ocndp-sync] discord_network_error reason={exc.reason}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
