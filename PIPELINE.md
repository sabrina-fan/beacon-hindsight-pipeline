# 全链路运行手册：Beacon → Jev → Hindsight → SKILL.md

本文是**可复现的操作手册**。背景与踩坑记录见 `README.md`，本文只讲"怎么跑"。

---

## 0. 链路总览

```
①采集/导入  agent 会话 → ~/.beacon/endpoint/logs/runtime.jsonl
     ├─ hooks 实时捕获（Claude Code/opencode/Grok/dsh/... 自动）
     ├─ beacon sync（自带本地存储的 harness）
     └─ 自写导入器（codex_import.py / zcode_import.py）
②评估       Jev 打分 → memory.db 的 evaluations（task_success /
            reusable_correction / evidence_supported 三个概率）
③候选       Jev 高分 trace → candidates（state=candidate）
④审核       approve → memories（可被 MCP search_memory 检索）
⑤提炼       digest → Hindsight retain → 原子事实库
⑥综合       Hindsight reflect → SKILL.md 写回项目 .agents/skills/
```

关键设计：**Beacon 负责"哪些值得学"（便宜的筛选器），Hindsight 负责"学成什么样"（贵的提炼器）**。

---

## 1. 前置：服务与配置

```bash
# Beacon collector（launchd 常驻）
beacon endpoint status                 # running=true 即可

# Jev（官方 TypeSafe）
#   ~/.zshrc 里需要：BEACON_JEV_ENDPOINT / BEACON_JEV_MODEL / BEACON_JEV_API_KEY
#   beacon 1.3.x 不认这两个环境变量，只认 flag → 用 shell 函数注入（见 README）
source ~/.zshrc
beacon memory evaluations run --dry-run --limit 5     # 离线预览，不发请求

# Hindsight（Docker + GLM）
~/.hindsight-docker/start.sh           # 内含 --restart unless-stopped
curl -s localhost:8888/v1/default/banks | head -c 100
```

Hindsight 环境变量（在 `start.sh` 里）：
`HINDSIGHT_API_LLM_PROVIDER=anthropic`、`_BASE_URL=https://open.bigmodel.cn/api/anthropic`、
`_MODEL=glm-5.3-flash`、`_MAX_CONCURRENT=40`、`_TIMEOUT=120`。

---

## 2. ①采集/导入

### 2.1 实时捕获（新会话）
装好即用，无需操作。验证：`beacon traces | head`。

### 2.2 官方 sync（有本地库的 harness）
```bash
beacon sync opencode      # 或 dsh / grok / cursor / hermes / pi / prime / fx / factory / copilot
```
增量、幂等；cursor 无历史时返回 0 属正常。

### 2.3 自写导入器（官方不支持的 harness）

| harness | 脚本 | 源 |
|---|---|---|
| Codex（含 cockpit 多实例） | `~/.beacon/tools/codex_import.py` | `~/.codex` + `~/.antigravity_cockpit/instances/codex/*/` 的 `sessions/`、`archived_sessions/` 下 `rollout-*.jsonl` |
| ZCode | `~/.beacon/tools/zcode_import.py` | `~/.zcode/cli/db/db.sqlite`（session/message/part 三表） |
| Devin CLI | `~/.beacon/tools/devin_import.py` | `~/.local/share/devin/cli/sessions.db`（sessions + message_nodes 两表；810 份 markdown 摘要是派生物，不用它） |
| Kimi Code | `~/.beacon/tools/kimi_import.py` | `~/.kimi-code/sessions/*/session_*/agents/*/wire.jsonl` + `session_index.jsonl`（workDir 映射） |
| opencode | `~/.beacon/tools/opencode_import.py` | `~/.local/share/opencode/opencode.db`（与 zcode 同族 schema；官方采集器找错路径 `~/.config/opencode`） |

```bash
# 试跑单条（--print 不写盘；注意 --print 也会被增量 state 跳过，调试要加 --force）
python3 ~/.beacon/tools/zcode_import.py --print --session sess_xxxx --force

# 全量/增量
python3 ~/.beacon/tools/codex_import.py          # 增量（state 记 size+mtime）
python3 ~/.beacon/tools/zcode_import.py          # 增量（state 记 part rowid 水位线）
python3 ~/.beacon/tools/devin_import.py          # 增量（state 记 message_nodes row_id 水位线）
python3 ~/.beacon/tools/zcode_import.py --force  # 全量重导
```

Devin 映射要点：`role=user`→prompt.submitted、`assistant.content`→agent.message、
`assistant.thinking`→agent.reasoning、`assistant.tool_calls`→tool.invoked
（`exec/shell/bash` 等归 command.executed）、`role=tool`→tool.completed、
`metadata.metrics`→token.usage；`role=system` 跳过。
注意 Devin 库是**活库**——正在运行的会话会持续写新行，水位线幂等表现为
"重跑还有几十条新事件"，属正常；确定性事件 id 保证不产生重复。

导入器共同约定：
- 事件 id 用 `uuid5(NS, "harness-import:<源标识>:<序号>")` → **确定性**，重跑不产生重复
- 单行 ≤48KB（beacon 用 `bufio.Scanner`，>64KB 直接报错中断整条流）
- 中文按 **UTF-8 字节**判长度，不是字符数
- 孤立代理字符要 sanitize（`encode('utf-8','replace').decode()`）
- `session.id` 传**裸 id**，beacon 自己加 `session:<harness>:` 前缀（加错会双重前缀）
- 写日志前停 collector，写完重启：
  ```bash
  launchctl bootout gui/$(id -u)/com.beacon.endpoint.collector.user
  python3 ~/.beacon/tools/zcode_import.py
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.beacon.endpoint.collector.user.plist
  ```

### ⚠️ 保留期陷阱（实测踩到）

`logs/` 是**滚动+有界**的：runtime.jsonl 轮转成 `.1`…`.N`，总量超限时**旧的整份被删**。
codex 导入的 1.8GB（57 万事件）在后续 zcode 导入推挤轮转后**已被清理**，
`traces.db` 里对应的 `trace_search`/`trace_events` 索引也一并被裁。

对策（按重要性）：
1. **导入后尽快评估**，别攒着
2. 大批量导入前先归档：`cp -a ~/.beacon/endpoint/logs ~/.beacon/archive/logs-$(date +%F)`
3. 需要重取正文时，**回到源头**（rollout 文件 / zcode sqlite），不要指望日志还在
4. 判断索引是否还在：`sqlite3 traces.db "SELECT count(*) FROM trace_search WHERE trace_id LIKE 'session:<harness>:%'"`

---

## 3. ②评估（Jev）

`--project` **只决定记忆归属，不过滤选中哪些 trace** —— 所以必须按项目分区。

```bash
# 1) 按 session.working_directory 把日志拆成每项目一个文件
python3 - <<'EOF'
import json, os, hashlib
from collections import defaultdict
src = os.path.expanduser("~/.beacon/endpoint/logs/runtime.jsonl")   # 必要时含 .1/.2
outdir = os.path.expanduser("~/.beacon/endpoint/eval-logs"); os.makedirs(outdir, exist_ok=True)
by, lines = defaultdict(set), defaultdict(list)
for line in open(src, errors="replace"):
    try: e = json.loads(line)
    except ValueError: continue
    if (e.get("harness") or {}).get("name") != "<harness>": continue
    s = e.get("session") or {}
    proj = s.get("working_directory") or "(unknown)"
    by[proj].add(s.get("id")); lines[proj].append(line)
plan = []
for proj, ls in lines.items():
    slug = "<harness>-" + hashlib.md5(proj.encode()).hexdigest()[:12]
    open(os.path.join(outdir, slug + ".jsonl"), "w").writelines(ls)
    plan.append((slug, len(by[proj]), os.path.isdir(proj), proj))
plan.sort(key=lambda r: -r[1])
with open("/tmp/eval-plan.tsv", "w") as f:
    for slug, n, ex, proj in plan: f.write(f"{slug}\t{n}\t{int(ex)}\t{proj}\n")
print(len(plan), "projects", sum(r[1] for r in plan), "sessions")
EOF

# 2) 逐项目评估（驱动脚本见 ~/.beacon/tools/run_zcode_evals_retry.sh）
while IFS=$'\t' read -r slug nsess exists proj; do
  beacon memory evaluations run \
    --jev-endpoint "$BEACON_JEV_ENDPOINT" --jev-model "$BEACON_JEV_MODEL" \
    --log-path "/Users/sunwentong/.beacon/endpoint/eval-logs/${slug}.jsonl" \
    --project "$proj" --limit "$nsess"
done < /tmp/eval-plan.tsv
```

要点：
- **`--log-path` 会把 memory.db 建到日志同目录**（`eval-logs/memory.db`），跑完要合回主库（见 §4.1）
- eval id 是内容寻址 → 重跑幂等，重复评估不会重复计数
- api.typesafe.ai 偶发 `context deadline exceeded`：重跑该分区即可
- 选 trace 时**必须带 `--harness`**（否则会选中 `agent.detected` 心跳垃圾）
- zen 免费 Jev（`jev-1.13-free`）配额耗尽会返回 `429 FreeUsageLimitError`，官方 key 无此问题

---

## 4. ③④候选与审核

### 4.1 合回主库
```bash
cd ~/.beacon/endpoint
python3 - <<'EOF'
import sqlite3
dst = sqlite3.connect('memory.db'); dst.execute("ATTACH 'eval-logs/memory.db' AS src")
for t in ('evaluations','candidates','memories'):
    print(t, dst.execute(f"INSERT OR IGNORE INTO {t} SELECT * FROM src.{t}").rowcount)
dst.commit()
EOF
```

### 4.2 筛高分候选
分数**不在结构化字段里，在 `candidate_json.body` 的文本里**（形如 `- reusable_correction: 0.85`）。
项目路径在 `candidate_json.project.path`（`project_id` 是 sha256，不可直接判目录存在）。

```bash
cd ~/.beacon/endpoint
python3 - <<'EOF'
import json, sqlite3, os, re
con = sqlite3.connect('memory.db')
rows = con.execute("""SELECT c.id, c.candidate_json, e.trace_id FROM candidates c
    JOIN evaluations e ON c.source_evaluation_id=e.id WHERE c.state='candidate'""").fetchall()
pat = {k: re.compile(rf'{k}: ([\d.]+)') for k in ('reusable_correction','evidence_supported')}
for cid, cj, tid in rows:
    j = json.loads(cj); body = j.get('body') or ''
    s = {k: float(p.search(body).group(1)) if p.search(body) else 0 for k,p in pat.items()}
    path = (j.get('project') or {}).get('path') or ''
    if s['reusable_correction'] >= 0.7 and s['evidence_supported'] >= 0.6 and os.path.isdir(path):
        print(cid)          # tier1：可 approve
EOF
```

### 4.3 批准
```bash
while read cid; do beacon memory candidates approve "$cid" --reason "tier1 rc>=0.7 es>=0.6"; done < tier1.txt
beacon memory candidates reject <id> --reason "rc<0.5 low reuse value"   # 低分
```
approve 即写入 memories（MCP `search_memory` 可检索）。

**已知噪音**：codex 会话的候选标题常是 `<recommended_plugins>` 模板串
→ 同名候选会让 `beacon memory skills install` 互相覆盖，**不要用自动 install**，
按内容自己命名 skill（见 §6）。

---

## 5. ⑤Hindsight retain

### 5.1 构造 digest
每个 approved 候选 → 取其 trace 的 `user_message` + `agent_message` 正文（各截 3000 字，
总 ≤12000 字），拼成一条 item：

```python
{"content": f"Coding-agent会话经验（项目 {proj}）。候选知识: {title}\n\n[USER] …\n\n[AGENT] …",
 "context": f"trace:{trace_id}",
 "metadata": {"source_trace_id": tid, "candidate_id": cid, "project": proj}}
```

取正文两条路（**索引被裁时走第二条**）：
- `traces.db.trace_search`（按 `trace_id` 查，`event_type in ('user_message','agent_message')`；
  注意 `text_factory=bytes` 后 `event_type` 也是 bytes，比较前要 decode；
  `body` 可能有非法 UTF-8，用 `decode('utf-8','replace')`）
- 源头：codex `rollout-*.jsonl` 的 `response_item.payload.type=='message'`；
  zcode `part.data.type=='text'` + `message.data.role`

### 5.2 批量提交（异步）
```bash
# 每批 8 条，async=true
curl -s -X POST http://localhost:8888/v1/default/banks/heirloom-traces/memories \
  -H 'Content-Type: application/json' \
  -d '{"items":[…8条…],"async":true}'
```
- 用 `source_trace_id` 去重，避免重复提交
- GLM 限流 ~50 并发（429 code 1302）→ `MAX_CONCURRENT=40` 可跑但有重试损耗，敏感场景降 20
- 速度参考：GLM 约 36s/条（同步），免费 zen 约 400s/条
- 监控：`docker logs hindsight --since 3m | grep -oE "stage=[a-z_.+]+" | sort | uniq -c`

### 5.3 去重
```bash
# 按前 60 字符归一化聚类，保留 proof_count/entities/文本长度最大者，其余 PATCH 失效
PATCH /v1/default/banks/{bank}/memories/{id}   body: {"state":"invalidated","reason":"duplicate of …"}
```
- 软删（可逆、留审计），不是硬删
- `observation` 类型**不可 curate**（派生行，随源记忆再生）——只能靠失效其源记忆或
  `DELETE /observations` 整库清空（不推荐）

---

## 6. ⑥reflect → SKILL.md

```bash
curl -s -X POST http://localhost:8888/v1/default/banks/heirloom-traces/reflect \
  -H 'Content-Type: application/json' \
  -d '{"query":"<skill 主题，越窄越好>"}'
# 返回 {"text": "…markdown…"}
```

写回模板（保留溯源）：
```markdown
---
name: <项目>-<工作流名>
description: "<一句话：做什么、避免什么>"
beacon_memory_id: "memory_…"          # 从 memory.db 取
beacon_candidate_id: "candidate_…"
hindsight_bank: "heirloom-traces"
tags: ["beacon", "hindsight-reflect", "<harness>"]
---

<reflect 输出>

## Beacon Evidence
- Trace `session:…`
- Jev signals: task_success=… / reusable_correction=… / evidence_supported=…
```

**reflect 用法要点**：
- **问题要窄**。问"整体怎么做"会因命中记忆过多而超时（实测 280s 超时 3 次）；
  换成"Ticket 怎么算"这种具体点就成功
- 批量重生成整套 skill：脚本 `/tmp/regen_skills.py` 模式 ——
  `skill 目录 → 主题查询` 映射表，读原 frontmatter，reflect 后替换正文、保留 Evidence 块
- 位置：项目内 `.agents/skills/<name>/SKILL.md`（本仓库规范，`.devin/` 只放 MCP 配置）

---

## 7. 日常巡检命令

```bash
# Beacon
beacon endpoint status
beacon traces | head
beacon token-usage
sqlite3 ~/.beacon/endpoint/memory.db "SELECT state, count(*) FROM candidates GROUP BY 1;"

# Hindsight
curl -s localhost:8888/v1/default/banks/heirloom-traces/memories/list?limit=1 | python3 -c "import json,sys;print(json.load(sys.stdin)['total'])"
open http://localhost:9999          # UI

# Jev 周边
jev auth test                        # {"ok":true,"valid":true,"model":"jev-1.13.0"}
jev noul --provider official -q "…" -s "…"
```

---

## 8. 当前产物基线（截至本手册写作时）

| 项 | 数量 |
|---|---|
| 主库 evaluations | 1844（zcode 533 + devin 298 + codex 475 + kimi 40 + opencode 345 + claude 等） |
| candidates | 838（approved 350 / pending 473 / rejected 15） |
| memories（MCP 可检索） | 350 |
| Hindsight 事实 | ~3880（104 条新 digest 全部落库，含对账补提） |
| 落地 SKILL.md | 17 个（8 个项目，全部经 reflect 重整理） |
| traces.db 索引 | 806 traces / 191k trace_search 行（`beacon endpoint traces reindex` 已重建） |

**仍未整理**：
- 473 条 pending（中等分 + 目录不存在）——低优先，可批量 reject 清队列
- 派生 observation 重复行（不可 curate，忽略即可）

**已覆盖 harness**：Claude Code、Codex（含 cockpit 实例）、ZCode、Devin CLI、
Kimi Code、opencode、DeepSeek Harness、Grok Build——官方 sync 或自写导入器
全部入库并评完。未覆盖：Muse Code（仅 1 会话）、Qwen/Antigravity CLI（无本地存储）。

**Hindsight retain 验收**：提交后必须用
`GET /v1/default/banks/{bank}/operations?status=failed` 对账——异步 op 的
payload 可能丢失（`payload_null` 死 op）或 GLM 429 耗尽重试后 failed；
失败项用 `POST /operations/{id}/retry` 重排，顽固项改 `async:false` 同步
单条直送（~90-120s/条，绕过 op 队列）。
