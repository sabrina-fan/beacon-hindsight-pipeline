#!/usr/bin/env python3
"""Backfill Codex rollout sessions into the Beacon endpoint runtime log.

Reads rollout-*.jsonl files from every Codex home (~/.codex and each
antigravity-cockpit instance), maps them to Beacon's normalized event schema,
and appends them to runtime.jsonl marked harness.name=codex,
collection_method=poll-import.

Incremental: processed files are tracked in
~/.beacon/endpoint/state/codex-import.json (path -> size/mtime/count), so
re-runs only import new or changed files. Pass --force to redo everything.

Usage:
  codex_import.py [--print] [--file <path>] [--force] [--limit <n>]
"""

import argparse
import getpass
import glob
import hashlib
import json
import os
import re
import socket
import sys
import uuid

BEACON_DIR = os.path.expanduser("~/.beacon/endpoint")
RUNTIME_LOG = os.path.join(BEACON_DIR, "logs", "runtime.jsonl")
STATE_PATH = os.path.join(BEACON_DIR, "state", "codex-import.json")

CODEX_HOMES = [os.path.expanduser("~/.codex")] + sorted(
    glob.glob(os.path.expanduser("~/.antigravity_cockpit/instances/codex/*/"))
)

NS = uuid.UUID("6f0d3b9e-2c51-4b9e-9a6d-9b3a5d2c7e01")

ENV_BLOCK = re.compile(
    r"<(environment_context|permissions instructions|user_instructions|collaboration_mode|app-context)[^>]*>.*?</\1>",
    re.DOTALL,
)

SHELL_TOOLS = {"shell", "exec_command", "local_shell", "local_shell_exec", "unified_exec", "container.exec", "write_stdin"}


def eid(path, lineno):
    return str(uuid.uuid5(NS, f"codex-import:{path}:{lineno}"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def texts(content):
    out = []
    if isinstance(content, str):
        return content
    for c in content or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            out.append(c["text"])
    return "\n".join(out)


def clean_user_text(text):
    t = ENV_BLOCK.sub("", text).strip()
    return t


MAX_TEXT = 16000          # cap stored text fields
MAX_EVENT = 48000         # beacon scanner rejects lines >= ~64KB


def trunc(text):
    if text and len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + f"\n…[truncated {len(text) - MAX_TEXT} chars]"
    return text


def base_event(r, path, lineno, sid, cwd, home, action, category, message):
    return {
        "timestamp": r.get("timestamp"),
        "vendor": "beacon",
        "product": "endpoint-agent",
        "schema_version": "1.0",
        "event": {
            "id": eid(path, lineno),
            "kind": "agent_runtime",
            "action": action,
            "category": category,
            "fidelity": "observed",
        },
        "severity": "info",
        "endpoint": {"hostname": socket.gethostname(), "os": "darwin"},
        "user": {"name": getpass.getuser()},
        "harness": {"name": "codex", "collection_method": "poll"},
        "origin": "local",
        "session": {"id": sid, "working_directory": cwd},
        "content": {"retention": "full", "included": True},
        "message": message,
        "raw": {"codex": {
            "codex_home": home,
            "record_type": r.get("type"),
            "payload_type": (r.get("payload") or {}).get("type"),
            "lineno": lineno,
        }},
    }


def with_content(ev, text):
    ev["content"]["hash"] = sha(text)
    ev["content"]["bytes"] = len(text.encode("utf-8", "replace"))
    return ev


def convert_file(path, home):
    """Yield beacon events from one codex rollout file."""
    sid = cwd = originator = model = None
    pending = {}          # call_id -> (lineno, record, emitted invoked?)
    call_order = []
    last_tokens = None
    started = False

    def ev(r, lineno, action, category, message, text=None):
        e = base_event(r, path, lineno, sid, cwd, home, action, category, message)
        if model:
            e["model"] = model
        if text is not None:
            with_content(e, text)
        return e

    with open(path, errors="replace") as f:
        for lineno, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            rt = r.get("type")
            p = r.get("payload")
            if not isinstance(p, dict):
                continue
            pt = p.get("type")

            if rt == "session_meta":
                sid = p.get("session_id") or p.get("id") or sid
                cwd = p.get("cwd") or cwd
                originator = p.get("originator")
                yield ev(r, lineno, "session.context", "session",
                         f"codex session context ({originator or 'codex'})",
                         json.dumps({k: p.get(k) for k in
                                     ("originator", "cli_version", "source", "model_provider")},
                                    ensure_ascii=False))
                continue

            if rt == "turn_context":
                model = p.get("model") or model
                cwd = p.get("cwd") or cwd
                continue

            if rt == "event_msg":
                if pt == "task_started" and not started:
                    started = True
                    yield ev(r, lineno, "session.started", "session", "codex session started")
                elif pt == "token_count":
                    last_tokens = (r.get("timestamp"), (p.get("info") or {}).get("total_token_usage"))
                continue

            if rt != "response_item":
                continue

            if pt == "message":
                role = p.get("role")
                text = clean_user_text(texts(p.get("content"))) if role == "user" else texts(p.get("content"))
                text = trunc(text)
                if not text.strip():
                    continue
                if role == "user":
                    e = ev(r, lineno, "prompt.submitted", "prompt", "codex prompt submitted", text)
                    e["prompt"] = {"text": text}
                    e["gen_ai"] = {"input": {"messages": [{"role": "user", "parts": [{"type": "text", "content": text}]}]}}
                    yield e
                elif role == "assistant":
                    e = ev(r, lineno, "agent.message", "session", "codex agent message", text)
                    e["gen_ai"] = {"output": {"messages": [{"role": "assistant", "parts": [{"type": "text", "content": text}]}]}}
                    yield e
                continue

            if pt == "reasoning":
                stext = trunc(texts(p.get("summary")))
                if not stext.strip():
                    continue  # encrypted-only reasoning carries no usable content
                yield ev(r, lineno, "agent.reasoning", "session", "codex agent reasoning", stext)
                continue

            if pt in ("function_call", "custom_tool_call", "local_shell_call"):
                call_id = p.get("call_id") or p.get("id")
                name = p.get("name") or "tool"
                args = p.get("arguments") or p.get("input") or ""
                pending[call_id] = (lineno, r, name)
                call_order.append(call_id)
                action, cat = ("command.executed", "command") if name in SHELL_TOOLS or _is_exec_input(name, args) else ("tool.invoked", "tool")
                e = ev(r, lineno, action, cat, f"codex {action}: {name}")
                e["tool"] = {"name": name, "call": {"arguments": trunc(args)}}
                yield e
                continue

            if pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                call_id = p.get("call_id")
                out = texts(p.get("output"))
                lineno_c, rc, name = pending.pop(call_id, (lineno, r, "tool"))
                failed = _is_error_output(out)
                out = trunc(out)
                e = ev(rc, lineno, "tool.failed" if failed else "tool.completed", "tool",
                       f"codex tool {'failed' if failed else 'completed'}: {name}", out)
                e["tool"] = {"name": name, "call": {"arguments": trunc(rc.get("payload", {}).get("arguments") or rc.get("payload", {}).get("input") or ""), "result": {"output": out}}}
                yield e
                continue

    # calls with no recorded output
    for call_id in call_order:
        if call_id in pending:
            lineno_c, rc, name = pending.pop(call_id)
            e = ev(rc, lineno_c, "tool.completed", "tool", f"codex tool completed: {name} (no output recorded)")
            e["tool"] = {"name": name}
            yield e

    if last_tokens:
        ts, usage = last_tokens
        e = ev({"timestamp": ts}, 0, "token.usage", "metric", "codex token usage")
        e["gen_ai"] = {"usage": usage}
        yield e


def _is_exec_input(name, args):
    return name == "exec" and '"cmd"' in (args or "")


def _is_error_output(out):
    head = out[:2000].lower()
    return any(s in head for s in ("traceback (most recent call last)", "command failed", "exit code 1", '"error"'))


def iter_rollout_files():
    for home in CODEX_HOMES:
        for sub in ("sessions", "archived_sessions"):
            for p in sorted(glob.glob(os.path.join(home, sub, "**", "rollout-*.jsonl"), recursive=True)):
                yield p, home


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="dry", action="store_true", help="print events, do not write")
    ap.add_argument("--file", help="import a single rollout file")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    state = {} if args.force else load_state()
    files = [(args.file, os.path.dirname(args.file))] if args.file else list(iter_rollout_files())

    total_files = total_events = 0
    skipped_oversize = 0
    out = sys.stdout if args.dry else open(RUNTIME_LOG, "a")

    try:
        for path, home in files:
            st = os.stat(path)
            prev = state.get(path)
            if prev and prev.get("size") == st.st_size and prev.get("mtime") == st.st_mtime:
                continue
            n = 0
            for e in convert_file(path, home):
                line = json.dumps(e, ensure_ascii=False)
                if len(line.encode("utf-8", "replace")) > MAX_EVENT:
                    e.pop("raw", None)
                    line = json.dumps(e, ensure_ascii=False)
                if len(line.encode("utf-8", "replace")) > MAX_EVENT:
                    skipped_oversize += 1
                    continue
                out.write(line + "\n")
                n += 1
            state[path] = {"size": st.st_size, "mtime": st.st_mtime, "events": n}
            total_files += 1
            total_events += n
            print(f"{path}: {n} events", file=sys.stderr)
            if args.limit and total_files >= args.limit:
                break
    finally:
        if not args.dry:
            out.close()

    if not args.dry:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        json.dump(state, open(STATE_PATH, "w"), indent=1)

    print(f"codex import: {total_files} files, {total_events} events, {skipped_oversize} oversized skipped", file=sys.stderr)


if __name__ == "__main__":
    main()
