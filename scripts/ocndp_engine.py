#!/usr/bin/env python3
"""
OCNDP executable protocol engine.

Provides an operational CLI for discovery, trust scoring, and social graph output.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import ipaddress
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ocndp_sync import (
    DEFAULT_KNOWN_FILE,
    DEFAULT_STATE_FILE,
    MAX_REGISTRATION_AGE_SECONDS,
    SyncStats,
    fetch_discord_messages,
    load_json_file,
    load_messages_from_input_file,
    run_sync,
    write_json_file,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_GRAPH_FILE = ROOT_DIR / "memory/social-graph.json"
DEFAULT_EVENTS_FILE = ROOT_DIR / "memory/trust-events.jsonl"
DEFAULT_TASK_INBOX_FILE = ROOT_DIR / "memory/task-results-inbox.jsonl"
DEFAULT_AUTO_CURSOR_FILE = ROOT_DIR / "memory/auto-cursors.json"
OCNDP_SKILL_URL = "https://github.com/yuquan2088/ClawSocial"
OCNDP_SKILL_VERSION = "1.0"
NODE_ID_SPAM_PATTERN = re.compile(r"^\d+$")
LIFECYCLE_INACTIVE_SECONDS = 7 * 24 * 60 * 60
LIFECYCLE_ARCHIVE_SECONDS = 30 * 24 * 60 * 60
LIFECYCLE_MISSED_PINGS_THRESHOLD = 3
AUTO_DISCOVERY_INTERVAL_SECONDS = 6 * 60 * 60
AUTO_PING_INTERVALS = {
    "trusted": 24 * 60 * 60,
    "friend": 48 * 60 * 60,
    "inactive": 7 * 24 * 60 * 60,
}


@dataclass
class TrustEvalStats:
    evaluated: int = 0
    skipped_blocked: int = 0
    declined_red_flag: int = 0
    friend_requested: int = 0
    pending: int = 0
    declined_low_score: int = 0


@dataclass
class AutoCycleStats:
    discovery_run: bool = False
    discovery_new_nodes: int = 0
    discovery_updated_nodes: int = 0
    task_events_ingested: int = 0
    task_events_skipped: int = 0
    pings_sent: int = 0
    pings_success: int = 0
    pings_failure: int = 0
    evaluated_nodes: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0


def now_epoch() -> int:
    return int(time.time())


def clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, value))


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def parse_capability_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return []


def parse_env_capabilities() -> list[str]:
    return parse_capability_list(os.getenv("OCNDP_CAPABILITIES", ""))


def looks_valid_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_private_or_local_gateway(value: str) -> bool:
    if not looks_valid_http_url(value):
        return True
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def load_documents(state_file: Path, known_file: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    state_doc = load_json_file(state_file, fallback={})
    known_doc = load_json_file(known_file, fallback={"nodes": [], "lastDiscovery": None, "totalFriends": 0})
    if not isinstance(known_doc.get("nodes"), list):
        known_doc["nodes"] = []
    return state_doc, known_doc


def load_events(events_file: Path) -> list[dict[str, Any]]:
    if not events_file.exists():
        return []
    events: list[dict[str, Any]] = []
    with events_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
    return events


def apply_event_history(nodes: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
    index = {str(n.get("nodeId")): n for n in nodes if isinstance(n, dict) and n.get("nodeId")}
    counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    last: dict[str, dict[str, Any]] = defaultdict(dict)
    last_ping_marker: dict[str, tuple[int, int]] = {}

    for seq, event in enumerate(events):
        node_id = str(event.get("nodeId", "")).strip()
        if not node_id or node_id not in index:
            continue
        ts = as_int(event.get("timestamp"))
        etype = str(event.get("type", ""))
        result = str(event.get("result", ""))
        if etype == "task_result":
            if result == "success":
                counters[node_id]["successfulTasks"] += 1
            elif result == "failure":
                counters[node_id]["failedTasks"] += 1
            if ts > as_int(last[node_id].get("lastTaskAt")):
                last[node_id]["lastTaskAt"] = ts
        elif etype == "ping_result":
            if result in {"success", "pong"}:
                counters[node_id]["successfulPings"] += 1
                counters[node_id]["missedPings"] = 0
                if ts > as_int(last[node_id].get("lastContact")):
                    last[node_id]["lastContact"] = ts
            elif result in {"failure", "timeout"}:
                counters[node_id]["failedPings"] += 1
                counters[node_id]["missedPings"] += 1
            if ts > as_int(last[node_id].get("lastPingAttempt")):
                last[node_id]["lastPingAttempt"] = ts
            marker = (ts, seq)
            if marker >= last_ping_marker.get(node_id, (-1, -1)):
                last_ping_marker[node_id] = marker
                last[node_id]["lastPingResult"] = "success" if result in {"success", "pong"} else "failure"
                last[node_id]["lastPingResultAt"] = ts

    for node_id, node in index.items():
        node["successfulTasks"] = as_int(counters[node_id].get("successfulTasks"))
        node["failedTasks"] = as_int(counters[node_id].get("failedTasks"))
        node["successfulPings"] = as_int(counters[node_id].get("successfulPings"))
        node["failedPings"] = as_int(counters[node_id].get("failedPings"))
        node["missedPings"] = as_int(counters[node_id].get("missedPings"))
        if "lastContact" in last[node_id]:
            node["lastContact"] = as_int(last[node_id]["lastContact"])
        if "lastTaskAt" in last[node_id]:
            node["lastTaskAt"] = as_int(last[node_id]["lastTaskAt"])
        if "lastPingAttempt" in last[node_id]:
            node["lastPingAttempt"] = as_int(last[node_id]["lastPingAttempt"])
        if "lastPingResult" in last[node_id]:
            node["lastPingResult"] = str(last[node_id]["lastPingResult"])
        if "lastPingResultAt" in last[node_id]:
            node["lastPingResultAt"] = as_int(last[node_id]["lastPingResultAt"])


def lifecycle_status_for_node(node: dict[str, Any], now_ts: int) -> tuple[str, str]:
    status = str(node.get("status", "discovered"))
    last_contact = as_int(node.get("lastContact"))
    missed_pings = as_int(node.get("missedPings"))
    last_ping_result = str(node.get("lastPingResult", "")).strip().lower()
    last_ping_result_at = as_int(node.get("lastPingResultAt"))

    if status == "blocked":
        return "blocked", "Blocked manually"

    if (
        status in {"inactive", "archived"}
        and last_ping_result == "success"
        and last_ping_result_at > 0
    ):
        return "friend", "Recovered: latest ping succeeded"

    if status == "inactive" and last_contact > 0 and (now_ts - last_contact) >= LIFECYCLE_ARCHIVE_SECONDS:
        return "archived", "No response for 30+ days"

    if missed_pings >= LIFECYCLE_MISSED_PINGS_THRESHOLD:
        return "inactive", "3+ unanswered pings"

    if last_contact > 0 and (now_ts - last_contact) >= LIFECYCLE_INACTIVE_SECONDS and status in {
        "friend",
        "trusted",
        "friend-requested",
    }:
        return "inactive", "No contact for 7+ days"

    return status, ""


def refresh_known_summary(known_doc: dict[str, Any], now_ts: int) -> None:
    nodes = [n for n in known_doc.get("nodes", []) if isinstance(n, dict)]
    known_doc["totalFriends"] = count_total_friends(nodes)
    known_doc["totalInactive"] = sum(1 for n in nodes if n.get("status") == "inactive")
    known_doc["totalArchived"] = sum(1 for n in nodes if n.get("status") == "archived")
    known_doc["lastLifecycleCheck"] = now_ts


def should_run_discovery(state_doc: dict[str, Any], now_ts: int, interval_seconds: int) -> bool:
    last_discovery = as_int(state_doc.get("lastDiscovery"))
    if last_discovery <= 0:
        return True
    return (now_ts - last_discovery) >= max(1, interval_seconds)


def load_cursor_doc(cursor_file: Path) -> dict[str, Any]:
    return load_json_file(cursor_file, fallback={"taskInboxLine": 0})


def ingest_task_events_from_inbox(
    inbox_file: Path,
    cursor_file: Path,
    events_file: Path,
    state_doc: dict[str, Any],
    dry_run: bool,
) -> tuple[int, int]:
    if not inbox_file.exists():
        return 0, 0

    cursor_doc = load_cursor_doc(cursor_file)
    start_line = max(0, as_int(cursor_doc.get("taskInboxLine")))
    lines = inbox_file.read_text(encoding="utf-8").splitlines()
    if start_line >= len(lines):
        return 0, 0

    ingested = 0
    skipped = 0
    for raw in lines[start_line:]:
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(item, dict):
            skipped += 1
            continue

        node_id = str(item.get("nodeId", "")).strip()
        result = str(item.get("result", "")).strip().lower()
        if not node_id or result not in {"success", "failure"}:
            skipped += 1
            continue

        event = {
            "timestamp": as_int(item.get("timestamp"), default=now_epoch()),
            "type": "task_result",
            "nodeId": node_id,
            "result": result,
            "verified": bool(item.get("verified")),
            "source": "task-inbox",
            "externalTaskId": str(item.get("taskId", "")).strip(),
            "note": str(item.get("note", "")),
        }
        append_event(events_file=events_file, event=event, dry_run=dry_run)
        ingested += 1

    cursor_doc["taskInboxLine"] = len(lines)
    if not dry_run:
        write_json_file(cursor_file, cursor_doc)
        state_doc["taskEventCount"] = as_int(state_doc.get("taskEventCount")) + ingested
    return ingested, skipped


def ping_gateway_url(gateway_url: str, timeout_seconds: int) -> tuple[bool, int, str]:
    if not gateway_url:
        return False, 0, "missing-gateway"
    if is_private_or_local_gateway(gateway_url):
        return False, 0, "private-or-local-gateway"

    start = time.time()
    headers = {"User-Agent": "clawsocial-ocndp-engine/1.0"}
    for method in ("HEAD", "GET"):
        try:
            req = Request(gateway_url, headers=headers, method=method)
            with urlopen(req, timeout=max(1, timeout_seconds)) as response:
                code = int(getattr(response, "status", 200) or response.getcode())
            latency_ms = int((time.time() - start) * 1000)
            return (code < 500), latency_ms, f"http-{code}-{method.lower()}"
        except HTTPError as exc:
            if method == "HEAD" and exc.code in {405, 501}:
                continue
            latency_ms = int((time.time() - start) * 1000)
            if exc.code < 500:
                return True, latency_ms, f"http-{exc.code}-{method.lower()}"
            return False, latency_ms, f"http-{exc.code}-{method.lower()}"
        except URLError as exc:
            if method == "HEAD":
                continue
            latency_ms = int((time.time() - start) * 1000)
            return False, latency_ms, f"network-error:{exc.reason}"
    latency_ms = int((time.time() - start) * 1000)
    return False, latency_ms, "unreachable"


def run_auto_ping_cycle(
    known_doc: dict[str, Any],
    events_file: Path,
    now_ts: int,
    ping_timeout_seconds: int,
    disable_auto_ping: bool,
    dry_run: bool,
) -> tuple[int, int, int]:
    nodes = [n for n in known_doc.get("nodes", []) if isinstance(n, dict)]
    sent = 0
    success_count = 0
    failure_count = 0

    for node in nodes:
        status = str(node.get("status", "")).strip()
        interval = AUTO_PING_INTERVALS.get(status)
        if interval is None:
            continue
        last_ping = max(as_int(node.get("lastPingAttempt")), as_int(node.get("lastContact")))
        if last_ping > 0 and (now_ts - last_ping) < interval:
            continue

        if disable_auto_ping:
            continue

        gateway_url = str(node.get("gatewayUrl", "")).strip()
        ok, latency_ms, detail = ping_gateway_url(gateway_url=gateway_url, timeout_seconds=ping_timeout_seconds)
        event = {
            "timestamp": now_ts,
            "type": "ping_result",
            "nodeId": node.get("nodeId"),
            "result": "success" if ok else "failure",
            "transport": "gateway-auto",
            "latencyMs": latency_ms,
            "note": detail,
        }
        append_event(events_file=events_file, event=event, dry_run=dry_run)
        sent += 1
        if ok:
            success_count += 1
        else:
            failure_count += 1

    return sent, success_count, failure_count


def count_total_friends(nodes: list[dict[str, Any]]) -> int:
    return sum(1 for node in nodes if node.get("status") in {"friend", "trusted"})


def red_flags_for_node(node: dict[str, Any], self_gateway: str, now_ts: int) -> list[str]:
    flags: list[str] = []
    node_id = str(node.get("nodeId", ""))
    gateway = str(node.get("gatewayUrl", ""))
    desc = str(node.get("description", "")).strip()
    caps = parse_capability_list(node.get("capabilities"))
    reg_ts = as_int(node.get("lastSeenRegistration") or node.get("timestamp"))

    if NODE_ID_SPAM_PATTERN.match(node_id):
        flags.append("nodeId looks spammy (numeric-only)")
    if gateway and is_private_or_local_gateway(gateway):
        flags.append("gatewayUrl is private/local")
    if reg_ts > 0 and (now_ts - reg_ts) > MAX_REGISTRATION_AGE_SECONDS:
        flags.append("registration is stale (>48h)")
    if self_gateway and gateway and gateway == self_gateway:
        flags.append("gatewayUrl matches our own gateway")
    if not caps and not desc:
        flags.append("empty capabilities and description")
    return flags


def evaluate_node_trust(
    node: dict[str, Any],
    self_capabilities: set[str],
    self_gateway: str,
    now_ts: int,
) -> tuple[int, str, str]:
    flags = red_flags_for_node(node, self_gateway=self_gateway, now_ts=now_ts)
    if flags:
        return 0, "declined", f"Red flag: {', '.join(flags)}"

    score = 0
    reasons: list[str] = []

    reg_ts = as_int(node.get("lastSeenRegistration") or node.get("timestamp"))
    if reg_ts > 0 and (now_ts - reg_ts) <= 24 * 60 * 60:
        score += 20
        reasons.append("+20 active")

    gateway = str(node.get("gatewayUrl", ""))
    if gateway and looks_valid_http_url(gateway):
        score += 15
        reasons.append("+15 gateway")

    if str(node.get("discordHandle", "")).strip():
        score += 10
        reasons.append("+10 discord")

    caps = set(parse_capability_list(node.get("capabilities")))
    if caps & self_capabilities:
        score += 15
        reasons.append("+15 overlap")
    if caps - self_capabilities:
        score += 10
        reasons.append("+10 complement")

    if str(node.get("description", "")).strip():
        score += 5
        reasons.append("+5 description")
    if str(node.get("owner", "")).strip():
        score += 5
        reasons.append("+5 owner")

    first_seen = as_int(node.get("firstSeen"))
    if first_seen > 0 and first_seen < (now_ts - 60 * 60):
        score += 10
        reasons.append("+10 known-age")

    successful_pings = as_int(node.get("successfulPings"))
    failed_pings = as_int(node.get("failedPings"))
    successful_tasks = as_int(node.get("successfulTasks"))
    failed_tasks = as_int(node.get("failedTasks"))
    total_history = successful_pings + failed_pings + successful_tasks + failed_tasks
    if total_history > 0:
        success_count = successful_pings + successful_tasks
        success_rate = success_count / total_history
        if success_rate >= 0.8:
            score += 10
            reasons.append("+10 response-history(>=80%)")
        elif success_rate >= 0.6:
            score += 5
            reasons.append("+5 response-history(>=60%)")
        elif success_rate < 0.4:
            score -= 10
            reasons.append("-10 poor-history(<40%)")

    score = clamp(score)
    if score >= 80:
        status = "friend-requested"
    elif score >= 60:
        status = "pending"
    else:
        status = "declined"

    reason = f"Trust score: {score}/100 ({', '.join(reasons) if reasons else 'no positive signals'})"
    return score, status, reason


def evaluate_all_nodes(
    known_doc: dict[str, Any],
    state_doc: dict[str, Any],
    events_file: Path,
) -> TrustEvalStats:
    now_ts = now_epoch()
    stats = TrustEvalStats()
    nodes = known_doc.get("nodes", [])
    events = load_events(events_file)
    apply_event_history([n for n in nodes if isinstance(n, dict)], events)
    self_caps = set(parse_capability_list(state_doc.get("capabilities"))) | set(parse_env_capabilities())
    self_gateway = str(
        state_doc.get("gatewayUrl") or state_doc.get("selfGateway") or os.getenv("OCNDP_GATEWAY_URL", "")
    ).strip()

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("status") == "blocked":
            stats.skipped_blocked += 1
            continue

        lifecycle_status, lifecycle_reason = lifecycle_status_for_node(node, now_ts)
        if lifecycle_status in {"inactive", "archived"}:
            node["status"] = lifecycle_status
            node["trustReason"] = f"Lifecycle: {lifecycle_reason}"
            node["lastTrustEvaluation"] = now_ts
            node["redFlag"] = False
            node["trustReasonChangedAt"] = now_ts
            stats.evaluated += 1
            continue
        if lifecycle_status == "friend" and node.get("status") in {"inactive", "archived"}:
            node["status"] = "friend"

        trust_score, status, reason = evaluate_node_trust(
            node,
            self_capabilities=self_caps,
            self_gateway=self_gateway,
            now_ts=now_ts,
        )

        previous_reason = str(node.get("trustReason", ""))
        node["trustScore"] = trust_score
        node["status"] = status
        node["trustReason"] = reason
        node["lastTrustEvaluation"] = now_ts
        if reason.startswith("Red flag:"):
            node["redFlag"] = True
            stats.declined_red_flag += 1
        else:
            node["redFlag"] = False
            if status == "friend-requested":
                stats.friend_requested += 1
            elif status == "pending":
                stats.pending += 1
            else:
                stats.declined_low_score += 1
        if previous_reason != reason:
            node["trustReasonChangedAt"] = now_ts
        stats.evaluated += 1

    refresh_known_summary(known_doc, now_ts)
    known_doc["lastTrustEvaluation"] = now_ts
    known_doc["eventBacked"] = True
    return stats


def update_social_graph(
    state_doc: dict[str, Any],
    known_doc: dict[str, Any],
    graph_file: Path,
    dry_run: bool,
) -> dict[str, Any]:
    now_ts = now_epoch()
    self_node_id = str(state_doc.get("nodeId") or os.getenv("OCNDP_SELF_NODE_ID", "local-node")).strip()
    nodes_in = [n for n in known_doc.get("nodes", []) if isinstance(n, dict)]

    graph_nodes: list[dict[str, Any]] = [
        {
            "id": self_node_id,
            "type": "self",
            "owner": state_doc.get("owner"),
            "capabilities": parse_capability_list(state_doc.get("capabilities")),
        }
    ]
    edges: list[dict[str, Any]] = []
    for node in nodes_in:
        node_id = str(node.get("nodeId", "")).strip()
        if not node_id:
            continue
        graph_nodes.append(
            {
                "id": node_id,
                "type": "peer",
                "status": node.get("status"),
                "trustScore": as_int(node.get("trustScore")),
                "capabilities": parse_capability_list(node.get("capabilities")),
            }
        )
        interactions = (
            as_int(node.get("successfulTasks"))
            + as_int(node.get("failedTasks"))
            + as_int(node.get("successfulPings"))
            + as_int(node.get("failedPings"))
        )
        edges.append(
            {
                "source": self_node_id,
                "target": node_id,
                "status": node.get("status"),
                "weight": as_int(node.get("trustScore")),
                "interactions": interactions,
            }
        )

    graph_doc = {
        "generatedAt": now_ts,
        "selfNodeId": self_node_id,
        "nodeCount": len(graph_nodes),
        "edgeCount": len(edges),
        "nodes": graph_nodes,
        "edges": edges,
    }

    if not dry_run:
        write_json_file(graph_file, graph_doc)
    return graph_doc


def append_event(events_file: Path, event: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with events_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False))
        f.write("\n")


def print_sync_stats(stats: SyncStats) -> None:
    print(f"[ocndp-engine] fetched_messages={stats.fetched_messages}")
    print(f"[ocndp-engine] valid_candidates={stats.valid_candidates}")
    print(f"[ocndp-engine] new_nodes={stats.new_nodes}")
    print(f"[ocndp-engine] updated_nodes={stats.updated_nodes}")
    print(f"[ocndp-engine] skipped_self={stats.skipped_self}")
    print(f"[ocndp-engine] stale_payload={stats.stale_payload}")
    print(f"[ocndp-engine] invalid_payload={stats.invalid_payload}")


def print_eval_stats(stats: TrustEvalStats) -> None:
    print(f"[ocndp-engine] evaluated={stats.evaluated}")
    print(f"[ocndp-engine] friend_requested={stats.friend_requested}")
    print(f"[ocndp-engine] pending={stats.pending}")
    print(f"[ocndp-engine] declined_red_flag={stats.declined_red_flag}")
    print(f"[ocndp-engine] declined_low_score={stats.declined_low_score}")
    print(f"[ocndp-engine] skipped_blocked={stats.skipped_blocked}")


def print_auto_cycle_stats(stats: AutoCycleStats, cycle_idx: int) -> None:
    print(f"[ocndp-engine] cycle={cycle_idx}")
    print(f"[ocndp-engine] discovery_run={stats.discovery_run}")
    print(f"[ocndp-engine] discovery_new_nodes={stats.discovery_new_nodes}")
    print(f"[ocndp-engine] discovery_updated_nodes={stats.discovery_updated_nodes}")
    print(f"[ocndp-engine] task_events_ingested={stats.task_events_ingested}")
    print(f"[ocndp-engine] task_events_skipped={stats.task_events_skipped}")
    print(f"[ocndp-engine] pings_sent={stats.pings_sent}")
    print(f"[ocndp-engine] pings_success={stats.pings_success}")
    print(f"[ocndp-engine] pings_failure={stats.pings_failure}")
    print(f"[ocndp-engine] evaluated_nodes={stats.evaluated_nodes}")
    print(f"[ocndp-engine] graph_nodes={stats.graph_nodes}")
    print(f"[ocndp-engine] graph_edges={stats.graph_edges}")


def resolve_messages(args: argparse.Namespace, state_doc: dict[str, Any]) -> list[dict[str, Any]]:
    if args.input_file:
        return load_messages_from_input_file(Path(args.input_file).resolve())
    channel_id = args.channel_id or state_doc.get("discordChannelId")
    bot_token = args.bot_token
    if not channel_id:
        raise ValueError("Missing channel id. Set OCNDP_DISCORD_CHANNEL_ID, --channel-id, or state.discordChannelId.")
    if not bot_token:
        raise ValueError("Missing bot token. Set DISCORD_BOT_TOKEN or pass --bot-token.")
    return fetch_discord_messages(channel_id=channel_id, bot_token=bot_token, limit=args.limit)


def run_auto_cycle_once(
    args: argparse.Namespace,
    state_file: Path,
    known_file: Path,
    events_file: Path,
    graph_file: Path,
    task_inbox_file: Path,
    task_cursor_file: Path,
    disable_auto_ping: bool,
) -> AutoCycleStats:
    stats = AutoCycleStats()
    now_ts = now_epoch()
    state_doc, known_doc = load_documents(state_file, known_file)

    if should_run_discovery(state_doc=state_doc, now_ts=now_ts, interval_seconds=args.discover_interval_seconds):
        try:
            messages = resolve_messages(args, state_doc)
            sync_stats = run_sync(
                state_file=state_file,
                known_file=known_file,
                messages=messages,
                self_node_id=args.self_node_id or state_doc.get("nodeId"),
                dry_run=False,
            )
            stats.discovery_run = True
            stats.discovery_new_nodes = sync_stats.new_nodes
            stats.discovery_updated_nodes = sync_stats.updated_nodes
            state_doc, known_doc = load_documents(state_file, known_file)
        except ValueError as exc:
            print(f"[ocndp-engine] autopilot_discovery_skipped={exc}")

    ingested, skipped = ingest_task_events_from_inbox(
        inbox_file=task_inbox_file,
        cursor_file=task_cursor_file,
        events_file=events_file,
        state_doc=state_doc,
        dry_run=False,
    )
    stats.task_events_ingested = ingested
    stats.task_events_skipped = skipped

    sent, success_count, failure_count = run_auto_ping_cycle(
        known_doc=known_doc,
        events_file=events_file,
        now_ts=now_ts,
        ping_timeout_seconds=args.ping_timeout_seconds,
        disable_auto_ping=disable_auto_ping,
        dry_run=False,
    )
    stats.pings_sent = sent
    stats.pings_success = success_count
    stats.pings_failure = failure_count
    if sent > 0:
        state_doc["pingCount"] = as_int(state_doc.get("pingCount")) + sent
        state_doc["lastFriendPing"] = now_ts

    eval_stats = evaluate_all_nodes(
        known_doc=known_doc,
        state_doc=state_doc,
        events_file=events_file,
    )
    stats.evaluated_nodes = eval_stats.evaluated

    graph_doc = update_social_graph(
        state_doc=state_doc,
        known_doc=known_doc,
        graph_file=graph_file,
        dry_run=False,
    )
    stats.graph_nodes = as_int(graph_doc.get("nodeCount"))
    stats.graph_edges = as_int(graph_doc.get("edgeCount"))

    current_ts = now_epoch()
    state_doc["lastTrustEvaluation"] = current_ts
    state_doc["lastLifecycleCheck"] = current_ts
    state_doc["lastGraphBuild"] = current_ts

    write_json_file(known_file, known_doc)
    write_json_file(state_file, state_doc)
    return stats


def cmd_autopilot(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).resolve()
    known_file = Path(args.known_file).resolve()
    events_file = Path(args.events_file).resolve()
    graph_file = Path(args.graph_file).resolve()
    task_inbox_file = Path(args.task_inbox_file).resolve()
    task_cursor_file = Path(args.task_cursor_file).resolve()

    cycle_idx = 0
    max_cycles = max(0, int(args.max_cycles))
    disable_auto_ping = bool(args.disable_auto_ping or args.dry_run)

    try:
        while True:
            cycle_idx += 1

            if args.dry_run:
                with tempfile.TemporaryDirectory(prefix="ocndp-autopilot-") as tempdir:
                    tdir = Path(tempdir)
                    active_state = tdir / "state.json"
                    active_known = tdir / "known.json"
                    active_events = tdir / "events.jsonl"
                    active_graph = tdir / "graph.json"
                    active_cursor = tdir / "cursor.json"

                    write_json_file(active_state, load_json_file(state_file, fallback={}))
                    write_json_file(
                        active_known,
                        load_json_file(known_file, fallback={"nodes": [], "lastDiscovery": None, "totalFriends": 0}),
                    )
                    if events_file.exists():
                        active_events.write_text(events_file.read_text(encoding="utf-8"), encoding="utf-8")
                    if task_cursor_file.exists():
                        write_json_file(active_cursor, load_json_file(task_cursor_file, fallback={"taskInboxLine": 0}))

                    stats = run_auto_cycle_once(
                        args=args,
                        state_file=active_state,
                        known_file=active_known,
                        events_file=active_events,
                        graph_file=active_graph,
                        task_inbox_file=task_inbox_file,
                        task_cursor_file=active_cursor,
                        disable_auto_ping=disable_auto_ping,
                    )
            else:
                stats = run_auto_cycle_once(
                    args=args,
                    state_file=state_file,
                    known_file=known_file,
                    events_file=events_file,
                    graph_file=graph_file,
                    task_inbox_file=task_inbox_file,
                    task_cursor_file=task_cursor_file,
                    disable_auto_ping=disable_auto_ping,
                )

            print_auto_cycle_stats(stats, cycle_idx=cycle_idx)
            if args.once:
                break
            if max_cycles > 0 and cycle_idx >= max_cycles:
                break
            time.sleep(max(1, int(args.loop_interval_seconds)))
    except KeyboardInterrupt:
        print("[ocndp-engine] autopilot_stopped=keyboard_interrupt")

    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).resolve()
    known_file = Path(args.known_file).resolve()
    graph_file = Path(args.graph_file).resolve()
    state_doc, known_doc = load_documents(state_file, known_file)

    messages = resolve_messages(args, state_doc)
    active_state_file = state_file
    active_known_file = known_file
    active_graph_file = graph_file
    cleanup_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.dry_run:
        cleanup_dir = tempfile.TemporaryDirectory(prefix="ocndp-discover-")
        active_state_file = Path(cleanup_dir.name) / "state.json"
        active_known_file = Path(cleanup_dir.name) / "known.json"
        active_graph_file = Path(cleanup_dir.name) / "graph.json"
        write_json_file(active_state_file, state_doc)
        write_json_file(active_known_file, known_doc)

    sync_stats = run_sync(
        state_file=active_state_file,
        known_file=active_known_file,
        messages=messages,
        self_node_id=args.self_node_id or state_doc.get("nodeId"),
        dry_run=False,
    )
    print_sync_stats(sync_stats)

    # Reload after sync to ensure trust evaluation uses latest nodes.
    state_doc, known_doc = load_documents(active_state_file, active_known_file)
    eval_stats = evaluate_all_nodes(
        known_doc=known_doc,
        state_doc=state_doc,
        events_file=Path(args.events_file).resolve(),
    )
    if args.dry_run:
        pass
    else:
        write_json_file(known_file, known_doc)
    print_eval_stats(eval_stats)

    graph_doc = update_social_graph(
        state_doc=state_doc,
        known_doc=known_doc,
        graph_file=active_graph_file,
        dry_run=False,
    )
    print(f"[ocndp-engine] graph_nodes={graph_doc['nodeCount']}")
    print(f"[ocndp-engine] graph_edges={graph_doc['edgeCount']}")
    if not args.dry_run:
        now_ts = now_epoch()
        state_doc["lastTrustEvaluation"] = now_ts
        state_doc["lastLifecycleCheck"] = now_ts
        state_doc["lastGraphBuild"] = now_ts
        write_json_file(state_file, state_doc)
    if cleanup_dir is not None:
        cleanup_dir.cleanup()
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).resolve()
    known_file = Path(args.known_file).resolve()
    state_doc, known_doc = load_documents(state_file, known_file)
    stats = evaluate_all_nodes(
        known_doc=known_doc,
        state_doc=state_doc,
        events_file=Path(args.events_file).resolve(),
    )
    if not args.dry_run:
        write_json_file(known_file, known_doc)
        now_ts = now_epoch()
        state_doc["lastTrustEvaluation"] = now_ts
        state_doc["lastLifecycleCheck"] = now_ts
        write_json_file(state_file, state_doc)
    print_eval_stats(stats)
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    known_file = Path(args.known_file).resolve()
    known_doc = load_json_file(known_file, fallback={"nodes": []})
    nodes = [n for n in known_doc.get("nodes", []) if isinstance(n, dict)]

    capability = args.capability.strip().lower() if args.capability else None
    statuses = {s.strip() for s in args.status.split(",")} if args.status else set()
    filtered: list[dict[str, Any]] = []
    for node in nodes:
        score = as_int(node.get("trustScore"))
        if score < args.min_trust:
            continue
        if statuses and str(node.get("status")) not in statuses:
            continue
        caps = [c.lower() for c in parse_capability_list(node.get("capabilities"))]
        if capability and capability not in caps:
            continue
        filtered.append(node)

    filtered.sort(key=lambda n: as_int(n.get("trustScore")), reverse=True)
    if not filtered:
        print("[ocndp-engine] no matching nodes")
        return 0

    for node in filtered:
        print(
            f"{node.get('nodeId')} | trust={as_int(node.get('trustScore'))} | "
            f"status={node.get('status')} | caps={','.join(parse_capability_list(node.get('capabilities')))}"
        )
    return 0


def cmd_record_task(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).resolve()
    known_file = Path(args.known_file).resolve()
    events_file = Path(args.events_file).resolve()
    state_doc, known_doc = load_documents(state_file, known_file)
    nodes = [n for n in known_doc.get("nodes", []) if isinstance(n, dict)]

    target = None
    for node in nodes:
        if node.get("nodeId") == args.node_id:
            target = node
            break
    if target is None:
        raise ValueError(f"nodeId not found: {args.node_id}")

    now_ts = now_epoch()
    success = args.result == "success"
    verified_bonus = 2 if (success and args.verified) else 0
    recorded_delta = (8 + verified_bonus) if success else -12
    trust_before = as_int(target.get("trustScore"))
    status_before = str(target.get("status"))

    event = {
        "timestamp": now_ts,
        "type": "task_result",
        "nodeId": args.node_id,
        "result": args.result,
        "verified": bool(args.verified),
        "recordedDelta": recorded_delta,
        "trustBeforeAtRecord": trust_before,
        "note": args.note,
    }
    append_event(events_file=events_file, event=event, dry_run=args.dry_run)

    # Recompute trust from the canonical event-backed model so this command
    # is consistent with `evaluate-trust` and does not create score drift.
    evaluate_all_nodes(known_doc=known_doc, state_doc=state_doc, events_file=events_file)
    target_after = None
    for node in known_doc.get("nodes", []):
        if isinstance(node, dict) and node.get("nodeId") == args.node_id:
            target_after = node
            break
    trust_after = as_int((target_after or {}).get("trustScore"), default=trust_before)
    status_after = str((target_after or {}).get("status", status_before))
    if isinstance(target_after, dict):
        target_after["lastInteraction"] = now_ts

    if not args.dry_run:
        write_json_file(known_file, known_doc)
        state_doc["taskEventCount"] = as_int(state_doc.get("taskEventCount")) + 1
        write_json_file(state_file, state_doc)
    print(
        f"[ocndp-engine] node={args.node_id} result={args.result} "
        f"trust={trust_before}->{trust_after} status={status_before}->{status_after} "
        f"recordedDelta={recorded_delta:+d}"
    )
    return 0


def cmd_record_ping(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).resolve()
    known_file = Path(args.known_file).resolve()
    events_file = Path(args.events_file).resolve()
    state_doc = load_json_file(state_file, fallback={})
    known_doc = load_json_file(known_file, fallback={"nodes": []})
    nodes = [n for n in known_doc.get("nodes", []) if isinstance(n, dict)]

    target = None
    for node in nodes:
        if node.get("nodeId") == args.node_id:
            target = node
            break
    if target is None:
        raise ValueError(f"nodeId not found: {args.node_id}")

    now_ts = now_epoch()
    success = args.result in {"success", "pong"}

    event = {
        "timestamp": now_ts,
        "type": "ping_result",
        "nodeId": args.node_id,
        "result": "success" if success else "failure",
        "transport": args.transport,
        "latencyMs": args.latency_ms,
        "note": args.note,
    }
    append_event(events_file=events_file, event=event, dry_run=args.dry_run)

    # Apply lightweight immediate lifecycle transitions; full state is recalculated by evaluate-trust.
    if success:
        target["lastContact"] = now_ts
        target["missedPings"] = 0
        if target.get("status") in {"inactive", "archived"}:
            target["status"] = "friend"
    else:
        target["lastPingAttempt"] = now_ts
        target["missedPings"] = as_int(target.get("missedPings")) + 1
        if as_int(target.get("missedPings")) >= LIFECYCLE_MISSED_PINGS_THRESHOLD:
            target["status"] = "inactive"

    refresh_known_summary(known_doc, now_ts)
    if not args.dry_run:
        write_json_file(known_file, known_doc)
        state_doc["pingCount"] = as_int(state_doc.get("pingCount")) + 1
        state_doc["lastFriendPing"] = now_ts
        write_json_file(state_file, state_doc)
    print(f"[ocndp-engine] node={args.node_id} ping={event['result']} status={target.get('status')}")
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).resolve()
    known_file = Path(args.known_file).resolve()
    graph_file = Path(args.graph_file).resolve()
    state_doc, known_doc = load_documents(state_file, known_file)
    graph_doc = update_social_graph(
        state_doc=state_doc,
        known_doc=known_doc,
        graph_file=graph_file,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        state_doc["lastGraphBuild"] = now_epoch()
        write_json_file(state_file, state_doc)
    print(f"[ocndp-engine] graph_nodes={graph_doc['nodeCount']}")
    print(f"[ocndp-engine] graph_edges={graph_doc['edgeCount']}")
    print(f"[ocndp-engine] graph_file={graph_file}")
    return 0


def cmd_registration_payload(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file).resolve()
    state_doc = load_json_file(state_file, fallback={})

    node_id = args.node_id or state_doc.get("nodeId") or os.getenv("OCNDP_SELF_NODE_ID")
    owner = args.owner or state_doc.get("owner") or os.getenv("USER")
    gateway_url = args.gateway_url or state_doc.get("gatewayUrl") or os.getenv("OCNDP_GATEWAY_URL")
    capabilities = parse_capability_list(args.capabilities) or parse_capability_list(state_doc.get("capabilities"))
    if not capabilities:
        capabilities = parse_env_capabilities()

    if not node_id:
        raise ValueError("Missing nodeId. Set state.nodeId, OCNDP_SELF_NODE_ID, or --node-id.")
    if not owner:
        raise ValueError("Missing owner. Set state.owner or --owner.")
    if not gateway_url:
        raise ValueError("Missing gateway URL. Set state.gatewayUrl, OCNDP_GATEWAY_URL, or --gateway-url.")

    payload = {
        "version": "ocndp/1.0",
        "nodeId": node_id,
        "owner": owner,
        "gatewayUrl": gateway_url,
        "discordHandle": args.discord_handle or state_doc.get("discordHandle"),
        "capabilities": capabilities,
        "description": args.description or state_doc.get("description", ""),
        "timestamp": now_epoch(),
        "status": args.status,
        "ocndpSkill": OCNDP_SKILL_URL,
        "ocndpSkillVersion": OCNDP_SKILL_VERSION,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OCNDP executable protocol engine")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="discover, autopilot, evaluate-trust, find, record-task, record-ping, build-graph, registration-payload",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    common.add_argument("--known-file", default=str(DEFAULT_KNOWN_FILE))
    common.add_argument("--events-file", default=str(DEFAULT_EVENTS_FILE))
    common.add_argument("--dry-run", action="store_true")

    discover = subparsers.add_parser("discover", parents=[common], help="Run discovery sync + trust evaluation + graph update")
    discover.add_argument("--channel-id", default=os.getenv("OCNDP_DISCORD_CHANNEL_ID"))
    discover.add_argument("--bot-token", default=os.getenv("DISCORD_BOT_TOKEN"))
    discover.add_argument("--self-node-id", default=os.getenv("OCNDP_SELF_NODE_ID"))
    discover.add_argument("--limit", type=int, default=50)
    discover.add_argument("--input-file", help="Local Discord message fixture JSON file.")
    discover.add_argument("--graph-file", default=str(DEFAULT_GRAPH_FILE))
    discover.set_defaults(func=cmd_discover)

    autopilot = subparsers.add_parser(
        "autopilot",
        parents=[common],
        help="Fully automatic loop: discover + ingest tasks + ping + evaluate + graph",
    )
    autopilot.add_argument("--channel-id", default=os.getenv("OCNDP_DISCORD_CHANNEL_ID"))
    autopilot.add_argument("--bot-token", default=os.getenv("DISCORD_BOT_TOKEN"))
    autopilot.add_argument("--self-node-id", default=os.getenv("OCNDP_SELF_NODE_ID"))
    autopilot.add_argument("--limit", type=int, default=50)
    autopilot.add_argument("--input-file", help="Optional local fixture for discovery messages.")
    autopilot.add_argument("--graph-file", default=str(DEFAULT_GRAPH_FILE))
    autopilot.add_argument("--task-inbox-file", default=str(DEFAULT_TASK_INBOX_FILE))
    autopilot.add_argument("--task-cursor-file", default=str(DEFAULT_AUTO_CURSOR_FILE))
    autopilot.add_argument("--discover-interval-seconds", type=int, default=AUTO_DISCOVERY_INTERVAL_SECONDS)
    autopilot.add_argument("--loop-interval-seconds", type=int, default=300)
    autopilot.add_argument("--max-cycles", type=int, default=0, help="0 means run indefinitely.")
    autopilot.add_argument("--once", action="store_true")
    autopilot.add_argument("--disable-auto-ping", action="store_true")
    autopilot.add_argument("--ping-timeout-seconds", type=int, default=6)
    autopilot.set_defaults(func=cmd_autopilot)

    evaluate = subparsers.add_parser("evaluate-trust", parents=[common], help="Evaluate trust for known nodes")
    evaluate.set_defaults(func=cmd_evaluate)

    find = subparsers.add_parser("find", parents=[common], help="Find nodes by capability/trust")
    find.add_argument("--capability", default="")
    find.add_argument("--min-trust", type=int, default=0)
    find.add_argument("--status", default="")
    find.set_defaults(func=cmd_find)

    record = subparsers.add_parser("record-task", parents=[common], help="Record task outcome and update trust")
    record.add_argument("--node-id", required=True)
    record.add_argument("--result", choices=["success", "failure"], required=True)
    record.add_argument("--verified", action="store_true")
    record.add_argument("--note", default="")
    record.set_defaults(func=cmd_record_task)

    ping = subparsers.add_parser("record-ping", parents=[common], help="Record ping outcome for lifecycle/trust history")
    ping.add_argument("--node-id", required=True)
    ping.add_argument("--result", choices=["success", "failure", "pong", "timeout"], required=True)
    ping.add_argument("--transport", default="discord")
    ping.add_argument("--latency-ms", type=int, default=0)
    ping.add_argument("--note", default="")
    ping.set_defaults(func=cmd_record_ping)

    graph = subparsers.add_parser("build-graph", parents=[common], help="Build social graph JSON from known nodes")
    graph.add_argument("--graph-file", default=str(DEFAULT_GRAPH_FILE))
    graph.set_defaults(func=cmd_graph)

    payload = subparsers.add_parser("registration-payload", help="Render OCNDP registration JSON payload")
    payload.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    payload.add_argument("--node-id", default="")
    payload.add_argument("--owner", default="")
    payload.add_argument("--gateway-url", default="")
    payload.add_argument("--discord-handle", default="")
    payload.add_argument("--capabilities", default="")
    payload.add_argument("--description", default="")
    payload.add_argument("--status", default="online")
    payload.set_defaults(func=cmd_registration_payload)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        print(f"[ocndp-engine] error={exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[ocndp-engine] unexpected_error={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
