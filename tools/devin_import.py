#!/usr/bin/env python3
"""Backfill Devin CLI sessions into the Beacon endpoint runtime log.

Reads ~/.local/share/devin/cli/sessions.db (sessions + message_nodes)
and maps it to Beacon's normalized event schema appended to runtime.jsonl,
marked harness.name=devin-cli, collection_method=poll-import.

Mapping:
  session row                      -> session.started + session.context
  user chat_message                -> prompt.submitted
  assistant chat_message           -> agent.message (+ thinking -> agent.reasoning,
                                      tool_calls[] -> tool.invoked / command.executed)
  tool chat_message                -> tool.completed / tool.failed
  assistant metrics                -> token.usage

Incremental: highest imported message_nodes.row_id is tracked per session in
~/.beacon/endpoint/state/devin-import.json; re-runs only import newer rows.
Pass --force to redo everything.

Usage:
  devin_import.py [--print] [--session <id>] [--force] [--limit <n>]
"""

import argparse
import datetime
import getpass
import hashlib
import json
import os
import socket
import sqlite3
import sys
import uuid

BEACON_DIR = os.path.expanduser("~/.beacon/endpoint")
RUNTIME_LOG = os.path.join(BEACON_DIR, "logs", "runtime.jsonl")
STATE_PATH = os.path.join(BEACON_DIR, "state", "devin-import.json")
DB_PATH = os.path.expanduser("~/.local/share/devin/cli/sessions.db")

NS = uuid.UUID("6f0d3b9e-2c51-4b9e-9a6d-9b3a5d2c7e01")

MAX_TEXT = 16000
MAX_EVENT = 48000

SHELL_TOOLS = {"shell", "bash", "exec", "run_command", "terminal", "exec_command"}


def eid(node_id, kind):
    return str(uuid.uuid5(NS, f"devin-import:{node_id}:{kind}"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def iso(ts):
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            if ts > 1e12:
                ts = ts / 1000
            return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
        return str(ts)
    except Exception:
        return None


def clean(text):
    if not text:
        return text
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def trunc(text):
    text = clean(text)
    if not text:
        return text
    if len(text) <= MAX_TEXT:
        return text
    b = text.encode("utf-8", "replace")
    cut = b[: MAX_TEXT * 4]
    while True:
        try:
            out = cut.decode("utf-8")
            break
        except UnicodeDecodeError as e:
            cut = cut[: e.start]
    return out + f"\n…[truncated {len(text) - len(out)} chars]"


def base_event(ts, nid, kind, sid, cwd, action, category, message):
    return {
        "timestamp": ts,
        "vendor": "beacon",
        "product": "endpoint-agent",
        "schema_version": "1.0",
        "event": {
            "id": eid(nid, kind),
            "kind": "agent_runtime",
            "action": action,
            "category": category,
            "fidelity": "observed",
        },
        "severity": "info",
        "endpoint": {"hostname": socket.gethostname(), "os": "darwin"},
        "user": {"name": getpass.getuser()},
        "harness": {"name": "devin-cli", "collection_method": "poll"},
        "origin": "local",
        "session": {"id": sid, "working_directory": cwd},
        "content": {"retention": "full", "included": True},
        "message": message,
        "raw": {"devin": {"node_id": nid}},
    }


def with_content(ev, text):
    ev["content"]["hash"] = sha(text)
    ev["content"]["bytes"] = len(text.encode("utf-8", "replace"))
    return ev


def convert_session(con, sess, min_rowid=0, emit_started=True):
    """Yield beacon events for one devin-cli session."""
    sid_raw, cwd, title, t_created, model = sess
    sid = sid_raw  # beacon prepends "session:devin-cli:"
    ts0 = iso(t_created)

    if emit_started:
        e = base_event(ts0, sid_raw, "session.started", sid, cwd,
                       "session.started", "session", f"devin session started: {title or sid_raw}")
        if model:
            e["model"] = model
        yield e

    rows = con.execute(
        """SELECT row_id, node_id, chat_message, created_at
           FROM message_nodes WHERE session_id=? AND row_id>? ORDER BY row_id""",
        (sid_raw, min_rowid)).fetchall()

    for rowid, nid, cmsg, created in rows:
        try:
            m = json.loads(cmsg)
        except ValueError:
            continue
        role = m.get("role")
        ts = iso((m.get("metadata") or {}).get("created_at")) or iso(created) or ts0
        model = model or None

        def ev(action, category, message, text=None, kind=None):
            e = base_event(ts, nid, kind or action, sid, cwd, action, category, message)
            if model:
                e["model"] = model
            if text is not None:
                with_content(e, text)
            return e

        if role == "system":
            continue

        if role == "user":
            text = trunc(m.get("content") or "")
            if not text.strip():
                continue
            e = ev("prompt.submitted", "prompt", "devin prompt submitted", text)
            e["prompt"] = {"text": text}
            e["gen_ai"] = {"input": {"messages": [{"role": "user", "parts": [{"type": "text", "content": text}]}]}}
            yield e
            continue

        if role == "assistant":
            think = (m.get("thinking") or {}).get("thinking") or ""
            think = trunc(think)
            if think.strip():
                yield ev("agent.reasoning", "session", "devin agent reasoning", think, kind="reasoning")
            content = trunc(m.get("content") or "")
            if content.strip():
                e = ev("agent.message", "session", "devin agent message", content)
                e["gen_ai"] = {"output": {"messages": [{"role": "assistant", "parts": [{"type": "text", "content": content}]}]}}
                yield e
            for tc in m.get("tool_calls") or []:
                name = tc.get("name") or (tc.get("function") or {}).get("name") or "tool"
                args = tc.get("arguments") or (tc.get("function") or {}).get("arguments") or ""
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                action, cat = ("command.executed", "command") if name in SHELL_TOOLS else ("tool.invoked", "tool")
                e = ev(action, cat, f"devin {action}: {name}", kind=f"tool:{tc.get('id') or nid}")
                e["tool"] = {"name": name, "call": {"arguments": trunc(args)}}
                yield e
            met = (m.get("metadata") or {}).get("metrics") or {}
            if met.get("input_tokens") or met.get("output_tokens"):
                e = ev("token.usage", "metric", "devin token usage", kind="token.usage")
                e["gen_ai"] = {"usage": {"input_tokens": met.get("input_tokens"),
                                         "output_tokens": met.get("output_tokens"),
                                         "cache_read_tokens": met.get("cache_read_tokens")}}
                yield e
            continue

        if role == "tool":
            text = trunc(m.get("content") or "")
            if not text.strip():
                continue
            e = ev("tool.completed", "tool", "devin tool completed", text, kind="tool.completed")
            e["tool"] = {"name": (m.get("name") or "tool"), "call": {"result": {"output": text}}}
            yield e
            continue


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="dry", action="store_true")
    ap.add_argument("--session", help="import a single session id")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    state = {} if args.force else load_state()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    sessions = con.execute(
        "SELECT id, working_directory, title, created_at, model FROM sessions ORDER BY created_at").fetchall()
    if args.session:
        sessions = [s for s in sessions if s[0] == args.session]

    total_sessions = total_events = skipped = 0
    out = sys.stdout if args.dry else open(RUNTIME_LOG, "a", errors="replace")
    try:
        for sess in sessions:
            sid_raw = sess[0]
            wm = 0 if args.force else state.get(sid_raw, {}).get("watermark", 0)
            n = 0
            for e in convert_session(con, sess, min_rowid=wm, emit_started=(wm == 0)):
                line = json.dumps(e, ensure_ascii=False)
                if len(line.encode("utf-8", "replace")) > MAX_EVENT:
                    e.pop("raw", None)
                    line = json.dumps(e, ensure_ascii=False)
                if len(line.encode("utf-8", "replace")) > MAX_EVENT:
                    skipped += 1
                    continue
                out.write(line + "\n")
                n += 1
            new_wm = con.execute(
                "SELECT max(row_id) FROM message_nodes WHERE session_id=?", (sid_raw,)).fetchone()[0] or wm
            if n or args.force:
                state[sid_raw] = {"watermark": new_wm,
                                  "events": state.get(sid_raw, {}).get("events", 0) + n}
            total_sessions += 1
            total_events += n
            if args.limit and total_sessions >= args.limit:
                break
    finally:
        if not args.dry:
            out.close()

    if not args.dry:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        json.dump(state, open(STATE_PATH, "w"), indent=1)

    print(f"devin import: {total_sessions} sessions, {total_events} events, {skipped} oversized skipped",
          file=sys.stderr)


if __name__ == "__main__":
    main()
