#!/usr/bin/env python3
"""Backfill Kimi Code sessions into the Beacon endpoint runtime log.

Reads ~/.kimi-code/sessions/*/session_*/agents/*/wire.jsonl and maps it to
Beacon's normalized event schema appended to runtime.jsonl, marked
harness.name=kimi-code, collection_method=poll-import.

Mapping (wire protocol):
  turn.prompt                              -> prompt.submitted
  context.append_loop_event[content.part]  -> agent.reasoning (think) / agent.message (text)
  context.append_loop_event[tool.call]     -> tool.invoked (Bash -> command.executed)
  context.append_loop_event[tool.result]   -> tool.completed / tool.failed (isError)
  usage.record                             -> token.usage
  context.append_message                   -> skipped (duplicates prompt/parts/results)

Incremental: byte offset per wire file in
~/.beacon/endpoint/state/kimi-import.json. --force redoes everything.

Usage:
  kimi_import.py [--print] [--session <dir-name>] [--force]
"""

import argparse
import datetime
import getpass
import hashlib
import json
import os
import socket
import sys
import uuid

BEACON_DIR = os.path.expanduser("~/.beacon/endpoint")
RUNTIME_LOG = os.path.join(BEACON_DIR, "logs", "runtime.jsonl")
STATE_PATH = os.path.join(BEACON_DIR, "state", "kimi-import.json")
SESSIONS_DIR = os.path.expanduser("~/.kimi-code/sessions")
INDEX_PATH = os.path.expanduser("~/.kimi-code/session_index.jsonl")

NS = uuid.UUID("7a2c1f8b-9e3d-4a5c-b6e7-1d2f3a4b5c6d")

MAX_TEXT = 16000
MAX_EVENT = 48000

SHELL_TOOLS = {"Bash", "bash", "shell", "exec", "run_command", "terminal"}


def eid(key, kind):
    return str(uuid.uuid5(NS, f"kimi-import:{key}:{kind}"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def iso_ms(ts):
    if ts is None:
        return None
    try:
        ts = float(ts)
        if ts > 1e12:
            ts /= 1000
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
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


def base_event(ts, key, kind, sid, cwd, action, category, message):
    return {
        "timestamp": ts,
        "vendor": "beacon",
        "product": "endpoint-agent",
        "schema_version": "1.0",
        "event": {
            "id": eid(key, kind),
            "kind": "agent_runtime",
            "action": action,
            "category": category,
            "fidelity": "observed",
        },
        "severity": "info",
        "endpoint": {"hostname": socket.gethostname(), "os": "darwin"},
        "user": {"name": getpass.getuser()},
        "harness": {"name": "kimi-code", "collection_method": "poll"},
        "origin": "local",
        "session": {"id": sid, "working_directory": cwd},
        "content": {"retention": "full", "included": True},
        "message": message,
        "raw": {"kimi": {"node_id": key}},
    }


def with_content(ev, text):
    ev["content"]["hash"] = sha(text)
    ev["content"]["bytes"] = len(text.encode("utf-8", "replace"))
    return ev


def load_workdirs():
    wd = {}
    try:
        for line in open(INDEX_PATH):
            try:
                r = json.loads(line)
                if r.get("sessionId") and r.get("workDir"):
                    wd[r["sessionId"]] = r["workDir"]
            except ValueError:
                pass
    except OSError:
        pass
    return wd


def wire_files(only_session=None):
    out = []
    for wd in sorted(os.listdir(SESSIONS_DIR)):
        wdp = os.path.join(SESSIONS_DIR, wd)
        if not os.path.isdir(wdp):
            continue
        for sess in sorted(os.listdir(wdp)):
            if only_session and sess != only_session:
                continue
            agents = os.path.join(wdp, sess, "agents")
            if not os.path.isdir(agents):
                continue
            for agent in sorted(os.listdir(agents)):
                f = os.path.join(agents, agent, "wire.jsonl")
                if os.path.isfile(f):
                    out.append((sess, agent, f))
    return out


def convert_wire(sess_id, agent, path, cwd, start=0):
    """Yield beacon events for one wire.jsonl from byte offset `start`.
    Returns new offset."""
    ts0 = None
    first = True
    with open(path, errors="replace") as fh:
        fh.seek(start)
        n = 0
        for line in fh:
            n += 1
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = iso_ms(e.get("time")) or ts0
            if ts0 is None:
                ts0 = ts
            key = f"{sess_id}:{agent}:{start+n}"
            t = e.get("type")

            if first and n == 1:
                yield base_event(ts0, key, "session.started", sess_id, cwd,
                                "session.started", "session", f"kimi session started: {sess_id}")
                first = False

            def ev(action, category, message, text=None, kind=None):
                x = base_event(ts, key, kind or action, sess_id, cwd, action, category, message)
                if text is not None:
                    with_content(x, text)
                return x

            if t == "turn.prompt":
                parts = e.get("input") or []
                text = trunc("\n".join(p.get("text", "") for p in parts if p.get("type") == "text"))
                if not text.strip():
                    continue
                x = ev("prompt.submitted", "prompt", "kimi prompt submitted", text)
                x["prompt"] = {"text": text}
                x["gen_ai"] = {"input": {"messages": [{"role": "user", "parts": [{"type": "text", "content": text}]}]}}
                yield x

            elif t == "context.append_loop_event":
                ev2 = e.get("event") or {}
                et = ev2.get("type")
                if et == "content.part":
                    part = ev2.get("part") or {}
                    pt = part.get("type")
                    if pt == "think":
                        txt = trunc(part.get("think") or "")
                        if txt.strip():
                            yield ev("agent.reasoning", "session", "kimi agent reasoning", txt,
                                     kind=f"reasoning:{ev2.get('uuid')}")
                    elif pt == "text":
                        txt = trunc(part.get("text") or "")
                        if txt.strip():
                            x = ev("agent.message", "session", "kimi agent message", txt,
                                   kind=f"message:{ev2.get('uuid')}")
                            x["gen_ai"] = {"output": {"messages": [{"role": "assistant", "parts": [{"type": "text", "content": txt}]}]}}
                            yield x
                elif et == "tool.call":
                    name = ev2.get("name") or "tool"
                    args = ev2.get("args")
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    action, cat = ("command.executed", "command") if name in SHELL_TOOLS else ("tool.invoked", "tool")
                    x = ev(action, cat, f"kimi {action}: {name}", kind=f"tool:{ev2.get('toolCallId') or ev2.get('uuid')}")
                    x["tool"] = {"name": name, "call": {"arguments": trunc(args)}}
                    yield x
                elif et == "tool.result":
                    res = ev2.get("result") or {}
                    out = res.get("output")
                    if not isinstance(out, str):
                        out = json.dumps(out, ensure_ascii=False)
                    out = trunc(out)
                    failed = bool(res.get("isError"))
                    action = "tool.failed" if failed else "tool.completed"
                    x = ev(action, "tool", f"kimi {action}", out,
                           kind=f"toolres:{ev2.get('toolCallId') or ev2.get('parentUuid')}")
                    x["tool"] = {"name": "tool", "call": {"result": {"output": out, "is_error": failed}}}
                    yield x

            elif t == "usage.record":
                u = e.get("usage") or {}
                x = ev("token.usage", "metric", "kimi token usage", kind=f"usage:{n}")
                x["gen_ai"] = {"usage": {"input_tokens": (u.get("inputOther") or 0) + (u.get("inputCacheRead") or 0),
                                         "output_tokens": u.get("output"),
                                         "cache_read_tokens": u.get("inputCacheRead")}}
                if e.get("model"):
                    x["model"] = e["model"]
                yield x
        return fh.tell()


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="dry", action="store_true")
    ap.add_argument("--session", help="import a single session dir name")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    state = {} if args.force else load_state()
    workdirs = load_workdirs()

    total_files = total_events = skipped = 0
    out = sys.stdout if args.dry else open(RUNTIME_LOG, "a", errors="replace")
    try:
        for sess_id, agent, path in wire_files(args.session):
            cwd = workdirs.get(sess_id)
            if not cwd:
                # fall back to wd_* prefix name
                cwd = "/" + sess_id.split("_")[1].replace("_", "/") if "_" in sess_id else "/"
            key = f"{sess_id}/{agent}"
            start = 0 if args.force else state.get(key, {}).get("offset", 0)
            n = 0
            for e in convert_wire(sess_id, agent, path, cwd, start=start):
                line = json.dumps(e, ensure_ascii=False)
                if len(line.encode("utf-8", "replace")) > MAX_EVENT:
                    e.pop("raw", None)
                    line = json.dumps(e, ensure_ascii=False)
                if len(line.encode("utf-8", "replace")) > MAX_EVENT:
                    skipped += 1
                    continue
                out.write(line + "\n")
                n += 1
            new_off = os.path.getsize(path)
            state[key] = {"offset": new_off, "events": state.get(key, {}).get("events", 0) + n,
                          "workdir": cwd}
            total_files += 1
            total_events += n
    finally:
        if not args.dry:
            out.close()

    if not args.dry:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        json.dump(state, open(STATE_PATH, "w"), indent=1)

    print(f"kimi import: {total_files} wire files, {total_events} events, {skipped} oversized skipped",
          file=sys.stderr)


if __name__ == "__main__":
    main()
