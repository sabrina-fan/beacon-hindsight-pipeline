#!/usr/bin/env python3
"""Backfill ZCode CLI sessions into the Beacon endpoint runtime log.

Reads ~/.zcode/cli/db/opencode.db (opencode-style schema: session/message/part)
and maps it to Beacon's normalized event schema appended to runtime.jsonl,
marked harness.name=opencode, collection_method=poll-import.

Mapping:
  session row                      -> session.started + session.context
  user message text parts          -> prompt.submitted
  assistant text parts             -> agent.message
  reasoning parts                  -> agent.reasoning (skipped when empty/encrypted)
  tool parts                       -> tool.invoked + tool.completed/failed
                                      (Bash -> command.executed on invoke)
  compaction parts                 -> session.compacting
  assistant message token usage    -> token.usage
  file parts                       -> skipped (200 rows, reference-only)

Incremental: highest imported part rowid is tracked in
~/.beacon/endpoint/state/opencode-import.json; re-runs only import newer parts.
Pass --force to redo everything.

Usage:
  zcode_import.py [--print] [--session <sess_id>] [--force] [--limit <n>]
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
STATE_PATH = os.path.join(BEACON_DIR, "state", "opencode-import.json")
DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")

NS = uuid.UUID("3f8a2e5c-7b1d-4c9a-8e2f-5d6a7b8c9d0e")

MAX_TEXT = 16000          # cap stored text fields
MAX_EVENT = 48000         # beacon scanner rejects lines >= ~64KB

SHELL_TOOLS = {"Bash", "bash", "Shell", "shell", "run_command"}


def eid(part_id, kind):
    return str(uuid.uuid5(NS, f"opencode-import:{part_id}:{kind}"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def iso(ms):
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).isoformat()


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


def base_event(ts, pid, kind, sid, cwd, action, category, message):
    return {
        "timestamp": ts,
        "vendor": "beacon",
        "product": "endpoint-agent",
        "schema_version": "1.0",
        "event": {
            "id": eid(pid, kind),
            "kind": "agent_runtime",
            "action": action,
            "category": category,
            "fidelity": "observed",
        },
        "severity": "info",
        "endpoint": {"hostname": socket.gethostname(), "os": "darwin"},
        "user": {"name": getpass.getuser()},
        "harness": {"name": "opencode", "collection_method": "poll"},
        "origin": "local",
        "session": {"id": sid, "working_directory": cwd},
        "content": {"retention": "full", "included": True},
        "message": message,
        "raw": {"opencode": {"part_id": pid}},
    }


def with_content(ev, text):
    ev["content"]["hash"] = sha(text)
    ev["content"]["bytes"] = len(text.encode("utf-8", "replace"))
    return ev


def convert_session(con, sess, only_session=None, min_rowid=0, emit_started=True):
    """Yield beacon events for one opencode session."""
    sid_raw, directory, title, t_created = sess
    if only_session and sid_raw != only_session:
        return
    sid = sid_raw  # beacon prepends "session:opencode:" itself
    cwd = directory
    ts0 = iso(t_created)

    if emit_started:
        e = base_event(ts0, sid_raw, "session.started", sid, cwd,
                       "session.started", "session", f"opencode session started: {title or sid_raw}")
        yield e

    rows = con.execute(
        """SELECT p.rowid, p.id, p.rowid, p.data, m.rowid, m.data
           FROM part p JOIN message m ON p.message_id = m.id
           WHERE p.session_id = ? AND p.rowid > ?
           ORDER BY m.time_created, p.time_created""",
        (sid_raw, min_rowid)).fetchall()

    last_msg = None
    for rowid, pid, pseq, pdata, mseq, mdata in rows:
        try:
            p = json.loads(pdata)
            m = json.loads(mdata)
        except ValueError:
            continue
        ptype = p.get("type")
        role = m.get("role")
        ts = iso((p.get("time") or {}).get("start")
                 or (m.get("time") or {}).get("created")
                 or t_created)
        model = (m.get("model") or {}).get("modelID") or m.get("modelID")
        cwd = cwd or (m.get("path") or {}).get("cwd") \
            or ((m.get("contextSnapshot") or {}).get("envInfo") or {}).get("cwd")

        def ev(action, category, message, text=None, kind=None):
            e = base_event(ts, pid, kind or action, sid, cwd, action, category, message)
            if model:
                e["model"] = model
            if text is not None:
                with_content(e, text)
            return e

        # assistant message token usage, once per message
        if role == "assistant" and m.get("id") != last_msg:
            last_msg = m.get("id")
            tok = m.get("tokens")
            if isinstance(tok, dict) and (tok.get("input") or tok.get("output")):
                e = ev("token.usage", "metric", "opencode token usage", kind="token.usage")
                e["gen_ai"] = {"usage": tok}
                yield e

        if ptype == "text":
            text = trunc(p.get("text") or "")
            if not text.strip():
                continue
            if role == "user":
                e = ev("prompt.submitted", "prompt", "opencode prompt submitted", text)
                e["prompt"] = {"text": text}
                e["gen_ai"] = {"input": {"messages": [{"role": "user", "parts": [{"type": "text", "content": text}]}]}}
                yield e
            else:
                e = ev("agent.message", "session", "opencode agent message", text)
                e["gen_ai"] = {"output": {"messages": [{"role": "assistant", "parts": [{"type": "text", "content": text}]}]}}
                yield e
            continue

        if ptype == "reasoning":
            text = trunc(p.get("text") or "")
            if not text.strip():
                continue
            yield ev("agent.reasoning", "session", "opencode agent reasoning", text)
            continue

        if ptype == "tool":
            name = p.get("tool") or "tool"
            state = p.get("state") or {}
            status = state.get("status") or "completed"
            inp = state.get("input")
            args = json.dumps(inp, ensure_ascii=False) if isinstance(inp, (dict, list)) else str(inp or "")
            action, cat = ("command.executed", "command") if name in SHELL_TOOLS else ("tool.invoked", "tool")
            e = ev(action, cat, f"opencode {action}: {name}", kind="tool.invoked")
            e["tool"] = {"name": name, "call": {"arguments": trunc(args)}}
            yield e
            out = state.get("output") or state.get("error") or ""
            if isinstance(out, (dict, list)):
                out = json.dumps(out, ensure_ascii=False)
            out = trunc(str(out))
            failed = status in ("error", "failed") or bool(state.get("error"))
            e = ev("tool.failed" if failed else "tool.completed", "tool",
                   f"opencode tool {'failed' if failed else 'completed'}: {name}", out,
                   kind="tool.completed")
            e["tool"] = {"name": name, "call": {"arguments": trunc(args), "result": {"output": out}}}
            yield e
            continue

        if ptype == "compaction":
            yield ev("session.compacting", "session", "opencode context compaction",
                     trunc(p.get("text") or p.get("summary") or ""))
            continue

        # step-start/step-finish/timeline/file: skipped


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="dry", action="store_true", help="print events, do not write")
    ap.add_argument("--session", help="import a single session id")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max sessions to process")
    args = ap.parse_args()

    state = {} if args.force else load_state()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    sessions = con.execute(
        "SELECT id, directory, title, time_created FROM session ORDER BY time_created").fetchall()
    if args.session:
        sessions = [s for s in sessions if s[0] == args.session]

    total_sessions = total_events = skipped_oversize = 0
    out = sys.stdout if args.dry else open(RUNTIME_LOG, "a", errors="replace")

    try:
        for sess in sessions:
            sid_raw = sess[0]
            wm = 0 if args.force else state.get(sid_raw, {}).get("watermark", 0)
            n = 0
            max_rowid = wm
            for e in convert_session(con, sess, min_rowid=wm, emit_started=(wm == 0)):
                line = json.dumps(e, ensure_ascii=False)
                if len(line.encode("utf-8", "replace")) > MAX_EVENT:
                    e.pop("raw", None)
                    line = json.dumps(e, ensure_ascii=False)
                if len(line.encode("utf-8", "replace")) > MAX_EVENT:
                    skipped_oversize += 1
                    continue
                out.write(line + "\n")
                n += 1
            new_wm = con.execute(
                "SELECT max(rowid) FROM part WHERE session_id=?", (sid_raw,)).fetchone()[0] or wm
            max_rowid = max(max_rowid, new_wm)
            if n or args.force:
                state[sid_raw] = {"watermark": max_rowid, "events": (state.get(sid_raw, {}).get("events", 0) + n)}
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

    print(f"opencode import: {total_sessions} sessions, {total_events} events, {skipped_oversize} oversized skipped",
          file=sys.stderr)


if __name__ == "__main__":
    main()
