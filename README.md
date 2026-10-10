# Beacon 安装配置与使用经验

> Beacon（agent-beacon，Asymptote Labs 开源，MIT）：跨 agent 的遥测 + 记忆层。
> 捕获各 coding agent 的会话历史 → Jev 评估 → 提炼成可复用知识（Agent Skills / MCP）。
> 仓库：https://github.com/Asymptote-Labs/agent-beacon 文档：https://docs.beacon.sh

本仓库是 **Beacon + Hindsight + Jev 记忆管线的整合层**：Beacon 和 Hindsight 都是开源组件，这里放的是把它们串起来的自写工具和两份手册。

```
README.md        安装、各 harness 接入、坑与实测记录（本文）
PIPELINE.md      全链路操作手册：会话 → Jev 评估 → 人审 → Hindsight → SKILL.md
tools/
  *_import.py    官方不支持的 harness 会话导入器（codex / zcode / devin / kimi / opencode）
  run_*_evals.sh 按项目分区跑 Jev 评估的驱动脚本
  zen_free_proxy.py  OpenAI 兼容 shim，把 Hindsight 的 LLM 调用转给 opencode Zen 免费模型
```

涉及的模型都是可替换配置：Jev 评估端点（官方 TypeSafe API / OpenCode Zen 免费模型 / 自建代理）、Hindsight 内部的事实抽取 LLM（`_BASE_URL` / `_MODEL` 环境变量，GLM / Claude / GPT / 本地模型都行）。

## 一、安装（macOS）

```bash
brew trust asymptote-labs/tap
brew tap asymptote-labs/tap
brew install beacon        # 会连带装独立的 beacon-vector 依赖
beacon endpoint install    # 非交互执行 = 纯本地模式，不登录不上传
```

- 已装版本：1.3.22；collector 由 launchd 常驻（`com.beacon.endpoint.collector.user`）
- OTLP 接收：127.0.0.1:4317 (gRPC) / 4318 (HTTP)；日志落盘 `~/.beacon/endpoint/logs/runtime.jsonl`
- **未连接 Beacon Managed（纯本地）**；要开云端：`beacon endpoint connect`，断开：`beacon endpoint disconnect`
- 日志轮转：`rotate_bytes=10MB`，保留 5 个 archive（runtime.jsonl.1~.5）

## 二、Jev 配置（OpenCode Zen 免费模型）

- Endpoint `https://opencode.ai/zen/v1/systemone`，Model `jev-1.13-free`，Key 用 opencode 已有的 zen key
- 已写入 `~/.zshrc`：`BEACON_JEV_ENDPOINT` / `BEACON_JEV_MODEL` / `BEACON_JEV_API_KEY`

**坑：beacon 1.3.x 不读 `BEACON_JEV_ENDPOINT`/`BEACON_JEV_MODEL` 环境变量**（文档超前于实现），
只认命令行 flag。不设会打到默认 `api.typesafe.ai` 报 401。
已在 `.zshrc` 加 `beacon()` shell 函数，跑 `beacon memory evaluations run` 时自动注入
`--jev-endpoint`/`--jev-model`（显式传 flag 仍可覆盖）。`BEACON_JEV_API_KEY` 环境变量是生效的。

## 三、已接入的 harness

| 方式 | harness |
|---|---|
| install 自动接入（hooks/plugin） | Claude Code、opencode、Pi、Oh My Pi、Grok Build、DeepSeek Harness、Kimi Code、Muse Code、Hermes、VS Code、Cursor、Antigravity、Devin CLI、Devin Desktop |
| 检测到但未接入 | Copilot CLI（OTLP 需启动环境变量）、Claude Cowork（需管理员配置） |
| 手动接入 | **Codex**：PATH 上没有 `codex` 二进制（走的是 cockpit 的 `opencodex`），未被自动检测；且 beacon 的 hooks 安装器不认 `CODEX_HOME`，只会写 `~/.codex/hooks.json`。已手动把 `SessionStart` hook 复制进 5 个实例目录的 `hooks.json`（`~/.antigravity_cockpit/instances/codex/*/`），新会话可被捕获 |
| 不支持 | **ZCode**：harness 列表里没有，存储格式私有（`~/.zcode/v2/`），暂不做 |

## 四、历史会话回灌

官方 `beacon endpoint <harness> sync` 只支持有本地存储的部分 harness：

| harness | 结果 |
|---|---|
| opencode | 64 会话 / 6592 事件（`--data-dir ~/.local/share/opencode`，默认路径是错的要手动指） |
| dsh | 37 会话 / 19340 事件 |
| grok | 71 会话 / 6021 事件 |
| cursor / hermes / pi / prime / fx / factory / copilot | 0 |

**Codex 无官方 sync**，自写转换器 `~/.beacon/tools/codex_import.py`：
解析 `rollout-*.jsonl`（`session_meta`/`response_item`/`event_msg`）→ beacon 事件 schema
（prompt.submitted / agent.message / agent.reasoning / tool.invoked / tool.completed /
command.executed / token.usage / session.started），append 到 runtime.jsonl。

- 覆盖 `~/.codex` + 全部 cockpit 实例的 `sessions/`、`archived_sessions/`
- 共回灌 **1290 个文件 / 57.2 万事件**；beacon 识别为 `session:codex_cli:*`
- 增量：state 在 `~/.beacon/endpoint/state/codex-import.json`；alias：`beacon-import-codex`
- 损耗：codex reasoning 多加密存储，只有带 summary 的能导；`<environment_context>` 等注入已剥离

### 踩过的坑（重要）

1. **bufio.Scanner token too long**：beacon 读日志的行上限 ~64KB，超标行直接让
   `evaluations`/`traces` 崩掉。转换器需限长：文本字段 ≤16KB、整事件 ≤48KB、
   超限剥 `raw` 再不行丢弃。**注意按 UTF-8 字节数判断**，中文按字符数算会漏。
2. beacon 官方 sync 自己也会写出 >60KB 的行（dsh 的 agent.message/reasoning），已清理。
3. 写日志前先 `launchctl bootout gui/$UID/com.beacon.endpoint.collector.user` 停 collector，
   写完 `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.beacon.endpoint.collector.user.plist` 重启，避免轮转冲突。

## 五、日常使用

```bash
beacon traces                              # 终端浏览 trace
beacon endpoint dashboard                  # 本地 web dashboard
beacon endpoint status / beacon doctor     # 健康检查
beacon token-usage                         # token 统计

# 记忆提炼（唯一联网的步骤是 evaluations run 非 dry-run）
cd <项目目录>                               # memory 按项目隔离！在哪跑就归哪个 repo
beacon memory evaluations run --dry-run    # 预览，离线
beacon memory evaluations run --limit 20   # Jev 评估（--harness/--since/-q 可过滤）
beacon memory candidates list --state pending
beacon memory candidates approve <id>      # 人工把关
beacon memory skills install <id>          # 写入 .agents/skills/<slug>/SKILL.md
```

MCP 检索：`beacon mcp serve`（stdio）已接入 `opencode.jsonc`，工具：
`search_memory` / `get_memory` / `get_memory_context`（+ `search_activity` 等）。

## 六、实测：评估与提炼结果（2026-09-24）

在 `岁阅传家/heirloom-miniprogram-reference-clone` 目录跑了 48 条 Jev 评估
（codex 12 / opencode 12 / grok 12 / dsh 12），产出 **13 个候选知识**。

**各 harness 有效会话量**（有真实 prompt 的）：codex 409 · grok 69 · opencode 16 · dsh 14。

**打分规律**：
- codex 会话 `reusable_correction` 普遍 0.78-0.85（含量最高），综合分 0.5-0.76
- opencode 会话 `task_success` 高（0.8+）但 `reusable_correction` 普遍 <0.1
- dsh 多数 `task_success` 极低（0.02-0.2），个别高分
- 噪音会话（探测模型版本、"回复两个字正常"这类测试）稳定被打到 0.1 以下，Jev 筛选有效

**候选内容的局限**：Jev Noul 只回概率不给理由，所以 candidate 没有提炼出的 lesson 文本，
生成的 SKILL.md 实质是"指向高分 trace 的指针"（含 trace id + 分数 + 原始 prompt 摘要）。
价值在于：未来 agent 经 MCP `get_memory_context` 命中后可回读完整 trace。

**已 approve + install 示例**（在 heirloom-miniprogram-reference-clone 下）：
- `beacon-correction-分析审查表-...`（opencode trace，0.68/0.80/0.57）
- `beacon-correction-使用域名构建小程序-...`（dsh trace，0.76/0.65/0.54）

**数据里看到的会话主题**（供参考）：岁阅传家小程序开发/评审占绝对大头；
环境工具安装类（homebrew/docker/zcode/cli 工具）；千川 Excel 数据处理；
模型探测类噪音会话不少。

**使用建议**：
- `evaluations run` 默认选中最新事件，可能选到 `agent.detected` 心跳事件——
  务必加 `--harness` 过滤
- 提炼某项目经验先 `cd` 到该 repo 再跑（`--project` 默认取 cwd）
- codex 历史多但 reasoning 加密缺失，适合做"工作流事实"提炼而非推理链复盘

## 七、补充验证结果（2026-09-24）

- **codex hook 链路**：手动喂 `beacon-hooks --platform codex codex-session-context` 一条
  SessionStart JSON → `runtime.jsonl` 成功落 `session.context` 事件
  （`harness.name=codex_cli, collection_method=hook`）。beacon 侧链路通；
  opencodex 实例是否真触发 hooks.json 待下次真实会话确认。
- **MCP server**：stdio 握手验证通过——`initialize` 返回 `beacon 1.3.22`，
  `tools/list` 返回 7 个工具（search_activity / summarize_activity /
  get_activity_event / list_activity_filters / search_memory / get_memory /
  get_memory_context）。HTTP transport 有 "Local demo requests only" 限制，
  客户端用 stdio 即可。
- **Jev 调用量**：共 ~560 次评估调用（其中第二批全量 537 次），
  免费模型实际花费 $0。
- **覆盖率**：581 条可选 trace 中 564 条为真实 session（其余为 agent.detected
  心跳垃圾），其中 562 条成功归属到项目目录，最终评估 537 个唯一 session
  （25 条 trace 因跨 harness 共享 session.id 折叠），**≈95% 覆盖**。

## 八、全量 trace 评估 + skill 提炼（第二批，2026-09-24）

**做法**：选择器不按项目过滤 trace（`--project` 只管记忆归属不管选择），
所以先把 `runtime.jsonl*` 按 `session.working_directory`（dsh 从 `raw.cwd`）
拆成 93 个按项目的日志文件 `~/.beacon/endpoint/eval-logs/*.jsonl`，
再逐目录跑 `evaluations run --log-path <文件> --project <目录>`。
驱动脚本：`~/.beacon/tools/run_all_evals.sh`，计划文件 `/tmp/eval-plan.tsv`。

**结果**：

- 537 次 Jev 评估，0 失败，全程约 9.5 分钟（~65 条/分钟）。
- 产出 **225 个候选**；按分数（rc≥0.7 且 es≥0.6）+ 项目目录真实存在
  过滤后 **批量 approve 了 90 个**（approve 即生成可检索 memory，
  MCP `search_memory` 已验证可查）。其余 135 个低分/临时目录的留 pending。
- 高分候选（rc≥0.8 & es≥0.7）14 条逐条回读 `trace_search` 原文提炼，
  **安装/手写了 14 个真实 SKILL.md**（保留 beacon_memory_id 溯源字段）：
  - `岁阅传家/heirloom-miniprogram-reference-clone/.agents/skills/` ×10：
    dsh 后台进程被 SIGSTOP（dev:stack 假死根因）、封面/返回键 UI 修正、
    题库二级目录数据校正、豆包切 ARK Agent Plan、作品详情状态判读、
    域名构建+CLI 上传、聊天图片上下文单次解析、腾讯实时 ASR、
    Jenkins Compose 容器名冲突、聊天输入框状态机。
  - `岁阅传家/.agents/skills/`、`抖店运营中台/`、`paw-franchise/`、
    `插件安装/` 各 1 个（部署数据库来源规则、Jenkinsfile 修复、
    dsh 绝对路径等）。

**本轮新踩的坑**：

- **`--log-path` 会改变 store 位置**：日志在哪个目录，memory.db/traces.db
  就建在它旁边（eval-logs/memory.db）。批量跑完后需把里面的
  evaluations/candidates/memories 三表 `INSERT OR IGNORE` 合回主库
  `endpoint/memory.db`（schema 一致，eval id 内容寻址自动去重）。
- **候选 slug 冲突**：`skills install` 用候选标题生成目录名，
  codex 会话首行全是 `<recommended_plugins>` 模板 → 8 个候选装到同一个目录
  互相覆盖。手写 SKILL.md 时必须自起有意义的名字。
- **内容 hash 化**：日志里 `content` 只存 hash+bytes，正文要去
  `traces.db` 的 `trace_search` 表按 `trace_id` 查（body 是多行拼接，
  event_type 为 `user_message`/`agent_message`/`command` 等）。

## 九、ZCode 会话回灌方案（已探明，待实现）

ZCode 不被 beacon 官方支持，但**本地存储是完全结构化的**（opencode 同源模型）：

| 存储 | 内容 | 可用性 |
|---|---|---|
| `~/.zcode/cli/db/db.sqlite` (1.1GB) | `session` 表 531 会话（id/directory/title/时间）；`message` 表 63,749 条（role、modelId、tokens）；`part` 表 226,238 条（type: tool/text/reasoning/step-start/step-finish/file/compaction，tool 部分含 callID/tool/state.input/output） | ✅ 全量字段，可做高保真转换 |
| `~/.zcode/v2/tasks-index.sqlite` | `tasks` 表 220 条（task_id/title/workspace_path/model/status/`searchable_text`） | 备选：`searchable_text` 是拍平的对话转写（无角色边界、无工具细节），低保真但简单 |
| `~/.zcode/cli/rollout/model-io-*.jsonl` | 模型 IO 日志 | 只有 3 个文件且多为报错，忽略 |

**实现路径**：照 `codex_import.py` 模式写 `zcode_import.py`——

```
session(id→session.id, directory→working_directory)
  └─ message(role=user → text part → prompt.submitted)
  └─ message(role=assistant → text part → agent.message
                              → reasoning part → agent.reasoning
                              → tool part → tool.invoked + tool.completed/failed
                                  (Bash→command.executed, file→file.*)
             tokens → token.usage)
  └─ compaction part → session.compacting
```

增量用 `time_updated`/`sequence` 水位线记进 `~/.beacon/endpoint/state/zcode-import.json`。
预计产出 ~15-20 万事件；同样要遵守 64KB 行上限规则（按 UTF-8 字节截断）。

## 十、待办 / 可选

- [ ] ZCode 转换器（方案见上节，半天量级工作）
- [ ] Copilot CLI / Claude Cowork 接入
- [ ] `beacon scan` + rules 本地威胁检测
- [ ] Beacon Managed 云端（团队共享记忆时用）
- [ ] 定期跑 `beacon endpoint opencode/dsh/grok sync` + `beacon-import-codex` 补漏

## 十一、Beacon + Hindsight 组合实验（2026-09-29）

把 5 条高分 trace（SIGSTOP 假死、实时 ASR、图片上下文、腾讯云部署、
ARK 切换）的「用户请求 + 最终总结」喂给本地 Hindsight（Docker，
`localhost:8888` API / `9999` UI，bank=`heirloom-traces`）。

**LLM 配置踩坑**：zen 免费档只放行 OpenCode 客户端
（`FreeTierError: free tier can only be used from within OpenCode`，
伪装 UA/x-opencode-* 头无效）；opencode-go key 订阅已失效。
最终用 zen 主端点的付费模型 `deepseek-v4-flash`（openai provider +
`HINDSIGHT_API_LLM_BASE_URL=https://opencode.ai/zen/v1`）。
embeddings/reranker 走容器内置 local。

**结果**：retain 抽取了 30 条原子事实（自动带 When/Involving 元数据），
reflect 对"端口监听但请求挂起"返回了结构化答案（进程 T 态诊断 +
setsid 脱离会话组修复 + 30s/60s 复查验证）。
用量：retain ~64k tokens / 5 条，reflect ~46k tokens / 次。

**结论**：Beacon 负责"哪些 trace 值得学"（Jev 打分，~5k tokens/条、免费），
Hindsight 负责"学成什么样"（真 LLM 提炼成事实库）。两者互补而非替代；
想全量接的话，把 approve 的候选 trace 批量 retain 进 Hindsight 即可。

### 追加：免费模型管线（zen free → OpenCode → shim → Hindsight）

付费 `deepseek-v4-flash` 只是验证路径用的。用户要求全免费，最终实现：

```
Hindsight (docker) --OpenAI协议--> zen_free_proxy.py (:8081)
    --> opencode serve (:8901) --> zen 免费模型 nemotron-3-ultra-free
```

- `zen_free_proxy.py`（`~/.beacon/tools/`）：把 `POST /v1/chat/completions`
  翻译成 opencode server 的 `POST /session + /session/:id/message`，
  只取 `type=text` 的 part（reasoning 分离丢弃）。免费模型全部在
  OpenCode 进程内发起，不违反 free tier 限制。
- 模型选择坑：`nemotron-3.5-lightning-free` 会把思维链写进输出导致
  Hindsight JSON 解析失败；`nemotron-3-ultra-free` 输出干净可用。
  `mimo-v2.5-free` 在 serve 模式报 UnknownError 不可用。
- Hindsight 侧需要 `HINDSIGHT_API_LLM_MAX_CONCURRENT=2` 和
  `HINDSIGHT_API_LLM_TIMEOUT=300`，否则并发打爆/超时。
- **速度**：每条 trace 的 retain 约 400s（多次抽取 LLM 调用串行），
  89 条全量约需数小时，只能异步跑（`async=true`）。
  进度查看：`curl localhost:8888/v1/default/banks/heirloom-traces/memories/list`
  或 UI `localhost:9999`。
- 配套进程：`opencode serve --port 8901` + `python3 zen_free_proxy.py`
  + `docker start hindsight`，三个都要活着。

### 再追加：切换到 GLM（用户提供 bigmodel.cn key）

zen 免费管线可用但太慢（~400s/条）。用户提供了智谱 GLM key，
切换为 **anthropic provider + glm-5.3-flash**，同步 retain ~36s/条。

- 启动脚本固化在 `~/.hindsight-docker/start.sh`（chmod 700，含 key；
  `--restart unless-stopped`，容器随 Docker 自启）。密钥不落本 README。
- 并发教训：GLM 账号限流 ~50 并发（429 code 1302）。默认并发会被打满，
  `HINDSIGHT_API_LLM_MAX_CONCURRENT=40` 可跑但有重试损耗；
  对限流敏感可降到 20。
- 验证：89 条 approved 候选 digest 已全量 retain（异步批量），
  bank `heirloom-traces` 事实总数 1500+；`recall` 实测命中
  SIGSTOP/setsid 等原子事实，实体/时间元数据齐全。
- zen→OpenCode shim（zen_free_proxy.py）保留作纯免费备选路径。

## 九、ZCode 导入（已实现）

`~/.beacon/tools/zcode_import.py` — 读 `~/.zcode/cli/db/db.sqlite`
（session/message/part 三表，opencode 同源结构）：

- session → `session.started`；user text → `prompt.submitted`；
  assistant text → `agent.message`；reasoning → `agent.reasoning`；
  tool part → `tool.invoked`+`tool.completed`/`failed`（Bash → `command.executed`）；
  compaction → `session.compacting`；assistant tokens → `token.usage`
- 增量：`state/zcode-import.json` 按 session 记 part rowid 水位线；
  `--force` 全量重导（event id 用 uuid5(part_id,kind) 确定性去重）
- 坑：`session.id` 传裸 `sess_*`，beacon 自己加 `session:zcode:` 前缀
  （加错一次导致双重前缀，已清库重导）；孤立代理字符要 sanitize；
  collector 会把 runtime.jsonl 轮转，导入事件可能落在 `.1` 里——轮转文件仍在扫描集

**结果**：533 会话 / 173,484 事件 / 111 条超 48KB 跳过，23 个项目分区，
全部目录存在（heirloom 占 271 会话）。

### ZCode Jev 评估状态

写稿时 zen 免费 Jev 配额打满（`429 FreeUsageLimitError`，同批已耗 ~560 次）。
重试脚本 `~/.beacon/tools/run_zcode_evals_retry.sh` 挂后台：
每个分区每 5min 重试直到成功，跳过已评估分区，24h 截止。
日志 `/tmp/zcode-eval-run.log`，完成标记 `/tmp/zcode-eval-run.done`。
跑完后需把 `eval-logs/memory.db` 合回主库（INSERT OR IGNORE）再筛候选。

### 全链路（最终形态）

```
Beacon hooks/导入器 → runtime.jsonl → Jev 免费打分(zen)
  → candidates → approve → memory.db (MCP 可查)
  → digest → Hindsight(GLM glm-5.3-flash) retain → 原子事实库
  → reflect 综合 → SKILL.md 回写 .agents/skills/
```

## 十、ZCode 全量评估完成（官方 TypeSafe Jev）

zen 免费配额耗尽后切换 `https://api.typesafe.ai/v1/systemone` + `jev-latest`
（key 在 `~/.zshrc` 的 `BEACON_JEV_API_KEY`）。

- **533/533 zcode 会话全部评估完成**（个别分区因 api.typesafe.ai 偶发
  `context deadline exceeded` 超时重跑，eval id 内容寻址幂等）
- 产出 **301 个候选**；tier1 过滤（rc>=0.7 & es>=0.6 & 项目目录存在）
  **approve 143 个**
- 主库累计：evaluations 1094 / candidates 526 / memories 233
- 143 条 zcode digest 已 retain 进 Hindsight（GLM 后台消化）

### Jev 周边接入

- `jev` CLI（~/.local/bin，官方 provider，credential store 已配 key）：
  `jev noul/choice/score/run`；`jev install-skills -g` 已装
  `~/.agents/skills/jev-cli`（含 jev-mcp stdio 用法）
- opencode.jsonc MCP：`jev-review`、`jev-compaction`（均已改指官方端点）、
  `jev`（jev-mcp）、`beacon`——下次 opencode 会话直接可用

### Hindsight 记忆盘点与去重

- 峰值 1520 条：world 746 / experience 702 / observation 72，全 valid
- 去重：按前 60 字符归一化聚类，72 组近似重复、89 条多余副本；
  PATCH `state=invalidated` 失效 **61 条**（可逆，审计保留）；
  ~27 条失败项是 observation 派生行（不可直接 curate，随源记忆再生）
- 无 <15 字符的垃圾条目

### 两套产物的定位（实测结论）

| | Beacon skill（.agents/skills/） | Hindsight 事实库 |
|---|---|---|
| 产出 | 16 个 SKILL.md 文件 | 1500+ 原子事实 |
| 粒度 | 完整工作流/排错 SOP | 单条事实+实体+时间 |
| 优点 | agent 直接读到，行为可执行 | recall/reflect 语义检索，reflect 能综合成文 |
| 缺点 | 需人工/reflect 提炼成文 | 不落文件，不能直接当 skill 用 |
| 结论 | **互补**：Beacon 给指针+分数，Hindsight 给内容；reflect 输出可直接生成 SKILL.md |

## 十一、整套 skill 用 reflect 重整理

14 个手工版 SKILL.md 的正文（此前靠人肉回读 trace_search 写）已全部
经 Beacon→Jev→Hindsight 链路重生成：

```
原 skill 的 beacon_memory_id/candidate_id 溯源保留
  → Hindsight reflect（按 skill 主题提问）
  → reflect 输出作为新正文写回同一 SKILL.md
  → frontmatter 补 hindsight_bank + hindsight-reflect 标签
```

- **12/12 重整理成功**（`beacon-tencent-realtime-asr` 因该主题记忆簇过大
  两次超时，改用窄化查询「Ticket 计算方式」后成功）
- 质量对比：reflect 版显著更丰富——例如 `interview-catalog-data-fix`
  的 reflect 版自动带出题库基线（10/64/563）、`--sync` 双重挂载缺陷、
  1371 项回归、测试库硬约束等原手工版没有的事实；
  `tencent-realtime-asr` reflect 版给出了 4002（签名错误）vs 4004（额度耗尽）
  的错误码判据和真实文件路径
- 2 个 `beacon-correction-*` 指针型 skill（beacon 自动生成、正文是评分样板，
  无 lesson 内容）已删除。溯源未丢，仍可在库里查：
  - `candidate_72813161dfae1e03bc73be52ca6c6e55` / `memory_452d6b44c3bedda323eee422792b9341`
    （trace `session:deepseek_harness:session-a023dc34-…`，与
    `beacon-mp-domain-build-cli-upload` 同源重复）
  - `candidate_88e48654d1e0be356f63a120a233327a` / `memory_8aaad02cdc0a0b938eb3fb1065017996`
    （trace `session:opencode:ses_ff08497b1ffe…`，审查表 md/doc 重写会话）
  - 用 `beacon memory candidates show <id>` 可回看

**脚本**：`/tmp/regen_skills.py`（一次性；按 skill 目录→主题查询映射，
reflect 后保留 frontmatter 与 Beacon Evidence 块）

## 十二、运行手册与保留期事故

**`PIPELINE.md`（同目录）是可复现的全链路操作手册**：采集/导入 → Jev 评估 →
候选审核 → Hindsight retain → reflect 写 SKILL.md，含每步命令、判据和坑。
本文（README）保留背景与事故记录，操作细节看 PIPELINE.md。

### 保留期事故（实测，重要）

codex 导入的 571,657 个事件（1.8GB）在后续 zcode 导入推挤日志轮转后
**被 beacon 的滚动保留清理**，`traces.db` 的 `trace_search`/`trace_events`
对应索引一并被裁（`traces` 表从 560+ 行降到 836 行、全是最近重建的）。

- 影响：133 条 codex pending 候选的**证据正文已不可回读**（需先 `--force` 重灌）
- 已补救：89 条 approved codex 候选的 digest 改从**源头 rollout 文件**重建并
  重新提交 Hindsight（成功，`items_count: 89`）
- 教训：**导入后尽快评估**；大批量导入前先
  `cp -a ~/.beacon/endpoint/logs ~/.beacon/archive/logs-$(date +%F)`；
  需要正文时优先回源头（rollout / zcode sqlite），别指望日志还在

### 候选队列最终分诊

| 分类 | 数量 | 处理 |
|---|---|---|
| tier1（rc≥0.7 & es≥0.6 & 目录存在） | 12 | ✅ approve（补齐） |
| 低分（rc<0.5） | 7 | ✅ reject |
| tier2 中等（0.5≤rc<0.7） | 269 | 留 pending，可随时批量 reject 清队列 |
| tier1 但项目目录不存在 | 5 | 留 pending（目录已删，无归属） |

主库最终：evaluations 1094 / candidates 526（approved 245 / pending 274 / rejected 7）
/ memories 245；Hindsight 事实 2000+。

## 十三、覆盖度补漏（Claude Code 历史 + 新项目 skill）

### 发现：官方采集器不止 sync 子命令

`beacon endpoint <harness> status|sync` 覆盖 claude / cline / codex / copilot /
cursor / dsh / factory / fx / grok / hermes / opencode / pi / prime。

- **Claude Code 历史此前完全没导**：`beacon endpoint claude status` 显示 30 个会话 pending
  → `beacon endpoint claude sync` 导入 30 会话 / 1201 事件，评估 43 trace，
  产出 2 候选（1 条 tier1 rc=0.83 已 approve）
- **Codex 也有官方采集器** `beacon endpoint codex`（只读 `~/.codex`，56 会话；
  自写 `codex_import.py` 覆盖更全，含 5 个 cockpit 实例共 1290 文件）
- 其余：cursor 0 会话、hermes 无 state.db、pi 无会话目录 → 无缺口

### 仍未纳入链路的 harness（已于十四节补齐大部分）

| harness | 状态 |
|---|---|
| ~~Devin CLI~~ | ✅ 十四节：`devin_import.py` 296 会话/102 万事件 |
| ~~Kimi Code~~ | ✅ 十五节：`kimi_import.py` 40 会话/3.3 万事件 |
| ~~opencode~~ | ✅ 十五节：`opencode_import.py` 345 会话/8671 事件 |
| Muse Code | 仅 1 个会话，不值得写导入器 |
| Qwen / Antigravity CLI | 本机无会话存储（Antigravity 的 codex 实例已由 codex 导入器覆盖） |
| Cline/Copilot/Cursor/Factory/fx/Hermes/Pi/Prime | 官方采集器查无历史（`endpoint <h> status` 均为 none/0） |

### 项目级补漏：有记忆但没 skill 的项目

按 approved 记忆数排序，挑出内容成型的 3 个项目补了 skill（reflect 生成）：

| 项目 | 记忆数 | 新 skill |
|---|---|---|
| 视频下载工具 | 13 | `beacon-video-stitch-sync`（拼接时间同步验证） |
| skill安装 | 11 | `beacon-zcode内存占用与docker清理` |
| minimax-skill | 4 | `beacon-minimax-h3-runninghub`（RunningHub API + H3 提示词） |

reflect 超时 2 次 → 换窄问题（"60GB 内存怎么解决" / "RunningHub API 怎么调"）后成功，
再次验证"问题要窄"这条经验。

**其余低价值未出 skill**：`Codex/2026-0*` 各 1 条一次性会话、一菜封神（7 条多为
第三方库调研）、jiandanshuziren（5 条）、高光时刻（2 条）、门店加盟管理（1 条）等。

**当前 skill 总数：17 个（8 个项目）**。

## 十四、Devin CLI 首导 + Codex 重灌复评（2026-09-29）

### Devin CLI 导入器

`~/.beacon/tools/devin_import.py`，读 `~/.local/share/devin/cli/sessions.db`
（296 sessions / 44.5 万 message_nodes——比 810 份 markdown 摘要全得多）：

- `role=user`→prompt.submitted、`assistant`→agent.message/reasoning、
  `tool_calls`→tool.invoked（exec 系→command.executed）、`role=tool`→
  tool.completed、`metadata.metrics`→token.usage
- 增量水位线 = `message_nodes.row_id`（活库，重跑会带几十条新行属正常；
  确定性事件 id 兜底不重复）
- 首导 **296 会话 / 1,017,769 事件**（2169 条超 48KB 跳过），harness=`devin-cli`
- 按 cwd 分区成 10 个文件（heirloom-lite 占 161 会话/89 万事件）

### 评估与审核

- 官方 Jev 全量：devin 298 trace（10 分区 ~7 分钟）、codex 92 分区全量复评
- devin 产出 114 候选 → tier1 approve 19、reject 3
- codex 复评产出 164 新候选；连同历史 pending 统一分诊：
  tier1（rc≥0.7 & es≥0.6 & 目录存在）approve 72、rc<0.5 reject 6
- 新 approve 的 91 条 digest 已从分区日志重建正文提交 Hindsight
  （不依赖 trace_search）

### Codex `--force` 重灌

- `codex_import.py --force`：**1293 文件 / 572,010 事件**（导入前已归档 logs 至
  `~/.beacon/archive/logs-2026-09-29-1911`）
- 原 133 条 pending 的证据随重灌恢复可读；评估按 eval id 内容寻址去重，
  复评只新增 29 条 evaluations
- `beacon endpoint traces reindex` 重建检索索引：806 traces / 191k trace_search 行

### 当前基线

evaluations **1461** / candidates **806**（approved 337 / pending 454 / rejected 15）
/ memories **337** / Hindsight **~2360 事实**（72+19 条新 digest 消化中）
/ skill 17 个不变（新增记忆集中于已覆盖的 heirloom 项目，未达出新 skill 的成簇阈值）。

## 十五、Kimi Code + OpenCode 导入（2026-09-30）

### `~/.beacon/tools/kimi_import.py`

读 `~/.kimi-code/sessions/*/session_*/agents/*/wire.jsonl`（协议级 wire 日志，
subagent 各自一份），`session_index.jsonl` 提供 workDir 映射：

- `turn.prompt`→prompt.submitted、`content.part[think]`→reasoning、
  `content.part[text]`→message、`tool.call`→invoked（Bash→command.executed）、
  `tool.result`→completed/failed、`usage.record`→token.usage
- 增量水位线 = 每 wire 文件的字节 offset
- 首导 **196 wire 文件 / 32,912 事件**（54 超大跳过），40 会话/8 项目
- 评估 40/40，11 候选 → tier1 approve 4

### `~/.beacon/tools/opencode_import.py`

Beacon 官方 opencode 采集器找错路径（`~/.config/opencode` 不存在），
真库在 `~/.local/share/opencode/opencode.db`（350MB，session/message/part
与 zcode 同族 schema，clone zcode 导入器改路径+harness+排序）：

- 无 `sequence` 列 → `ORDER BY m.time_created, p.time_created`
- 首导 **345 会话 / 8,671 事件**；评估 345/345，21 候选 → tier1 approve 8

### Hindsight 积压事故（重要）

批量 retain 期间 GLM 持续 429（code 1302）：`MAX_CONCURRENT=40` 下
89 个 operation 全部 3-4 次重试后 **failed**。已修：

- `HINDSIGHT_API_LLM_MAX_CONCURRENT` 40→**8**（`~/.hindsight-docker/start.sh`）
- `POST /operations/{id}/retry` 批量重排队（脚本见下）；digest 按
  `metadata.candidate_id` 对账重提缺失项（63+4+7 条）

```bash
curl -s "localhost:8888/v1/default/banks/heirloom-traces/operations?status=failed&limit=150" \
 | python3 -c "import json,sys;[print(o['operation_id']) for o in json.load(sys.stdin)['operations']]" \
 | while read op; do curl -s -X POST "localhost:8888/v1/default/banks/heirloom-traces/operations/$op/retry"; done
```

**教训**：批量 retain 后必须用 operations API 验收，不能只看提交响应
（响应 `operation_ids` 常为空且不代表落库）。顽固条目共 40 条最后改
`async:false` 同步单条直送全部成功——此路径绕过 op 队列，~90-120s/条。

### 终态基线（2026-09-30）

evaluations **1844** / candidates **838**（approved **350** / pending 473 /
rejected 15）/ memories **350** / Hindsight **~3880 事实**
/ skill **17 个**（8 个项目）。harness 覆盖：claude、codex（含 cockpit）、
zcode、devin-cli、kimi-code、opencode、dsh、grok 全量入库评估；
Muse（1 会话）、Qwen/Antigravity CLI（无本地存储）无可导历史。

## 十五、记忆治理与 Skill 合并（2026-10-02）

清理 350 条 approved 候选：
- **46 条噪音 reject + 删 memory**：注入型标题（`<recommended_plugins>`、`# AGENTS.md`、`<user_info>`、`Handoff` 等）且 Hindsight 无对应事实的死指针；琐碎问答（你好/继续/出口ip/什么模型/ssh命令等）。判据 = 标题明显非任务 **且** `metadata.candidate_id` 查无事实。
- **原则修正**：琐碎标题但 Hindsight 有实质事实的**不删**——`Output a summary`/`继续` 类会话的事实往往是真实知识（如 devin 内存看门狗、memoir_locked 语义）；Beaco n candidate/memory 只是包装器，价值在 facts 层。
- 80 条"无 facts、真实标题"的 approved 保留为 Beacon 记忆行（标题即知识面包屑，trace 已轮转不可恢复，留作检索锚点）。

Skill 合并与新增（去重后 18 个，物理副本 25 个）：
- `chat-composer-state-ui` + `chat-image-context-once` → **`beacon-chat-page-rules`**（一个文件两条契约）
- `rebuild-miniprogram-flow` → 并入 **`beacon-mp-domain-build-cli-upload`** 第 0 节（本地重建 SOP）
- 新增 **`beacon-prod-readonly-triage`**（heirloom-clone）：约 10 个会话的"生产只读排障"簇合成——跳板 SSH 白名单、用户编号≠users.id、generation_exhausted=熔断非欠费、宿主机不映射 3000
- 新增 **`beacon-agent-done-sound`**（全局 `~/.agents/skills/`）：zcode hooks.enabled+Stop→afplay；devin 合成 wav 三件套

新基线：candidates **approved 304 / pending 473 / rejected 61**；memories 304；Hindsight 3878 事实；skill 18 个。

遗留：Hindsight 容器重启后需等 HF 嵌入模型加载完成才响应 8888（首次冷启动曾失败：`RuntimeError: Cannot send a request`；已加 `HF_ENDPOINT=hf-mirror.com` + `hf-cache` 持久卷到 `~/.hindsight-docker/start.sh`）。

## 十六、记忆可达性接线（2026-10-02）

**问题**：记忆整理得再好，agent 不主动去查就等于没有。接线后现状：

- **Beacon MCP（7 个工具）**：`search_memory` / `get_memory` / `get_memory_context` / `search_activity` / `summarize_activity` / `get_activity_event` / `list_activity_filters`。已接入：OpenCode（此前）、**Devin CLI**（`~/.config/devin/mcp_config.json`）、**Codex**（`~/.codex/config.toml` `[mcp_servers.beacon]`）、**Claude Code**（`~/.claude.json` 顶层 mcpServers）、**ZCode**（`~/.zcode/cli/config.json` mcp.servers）。Kimi/Grok 未找到 MCP 配置入口。
- **全局规则提示**：`~/.codex/AGENTS.md`、`~/.zcode/AGENTS.md`、`~/.claude/CLAUDE.md`（新建）、`~/.codeium/windsurf/memories/global_rules.md` 各加一节「历史经验检索」——agent 遇到不确定的项目约定/历史修法时先 `search_memory`。
- **Skill 可达性**：`.agents/skills/` 是跨 harness 约定目录，Devin/ZCode/Codex 都会扫（会话标题里的 `[$finesse-ui](项目路径)` 即证据）；全局层 `~/.agents/skills/`。Claude Code 原生目录是 `.claude/skills`——跨 harness 兼容靠 `.agents/` 约定。
- Codex 的 `mcp_servers.jev-*` 顺带从失效 zen key 换成官方 TypeSafe key。

**诚实边界**：MCP 给的是"能查"，规则给的是"会想起来查"；仍依赖 agent 自觉调用，无法强制。剩余增量缺口：devin sessions.db 是活库（又增 11 会话/~3.9 万节点），定期重跑对应 importer（增量幂等）即可；473 条 pending 候选仍是有意保留的中档池。

## 十七、增量收割 + 全 harness MCP 接线（2026-10-02 第二拨）

**增量导入**（幂等重跑）：devin +352,035 事件（11 新会话）、claude +150（6 会话）；zcode/kimi/opencode/codex/dsh/grok 无增量。28 trace 评完（eval 幂等去重）→ tier1 approve 2、其余 pending。

**记忆查询面接线**（这次的重点）：
- beacon MCP 新接入：Devin（`~/.config/devin/mcp_config.json`）、Codex（`~/.codex/config.toml`）、Claude（`~/.claude.json`）、ZCode（`~/.zcode/cli/config.json`）、**Grok**（`grok mcp add beacon --scope user`）、**Kimi Code**（`~/.kimi-code/mcp.json`）、**Cursor**（`~/.cursor/mcp.json`）。加上 OpenCode 共 8 端可查记忆。
- 不支持/未验证：pi（扩展制，无 mcp.json）、dsh（settings.yaml 无 mcp 字段）、muse/qwen/antigravity。
- **Skill 镜像到 Claude 目录**：所有 `beacon-*` 项目 skill 复制到各项目 `.claude/skills/`（28 份），全局 `~/.claude/skills/` 补 `beacon-agent-done-sound`+`jev-cli`。
- 全局规则提示已写入：codex/zcode `AGENTS.md`、`~/.claude/CLAUDE.md`（新建）、windsurf `global_rules.md`。

**运维教训**：批量导入中 `beacon endpoint * sync` 会重启 collector 触发日志轮转——导入事件可能跨 runtime.jsonl 分片，分区时要扫全部 `runtime.jsonl*` 而非单文件。Hindsight retain 路径是 `POST /v1/default/banks/{bank}/memories`（不是 /retain）。

基线：memories **306** / Hindsight ~3882 / skill **18**。

## 十八、增量收割 2 + 重构蜂群 skill（2026-10-11）

**增量导入**：devin sessions.db 又增 **+1 会话 / +~4.7 万事件**（10-02 后持续活跃）；claude +51 会话分区评估。11 个分区按 cwd 拆分后官方 Jev 全部评完（devin-type4me-android/devin-clone 两分区曾间歇性 451，拆单 trace 重试即过——确认是瞬时故障非内容审查）。

**候选**：206 条新候选合入；tier1 **approve 51**（heirloom-lite 审查子代理任务卡簇为主）、低分 reject 21。digest 已批量 retain；GLM 429 限流仍在，后台 `/tmp/hs_retry_today.sh` 每 120s 重试今日 failed ops（上限 20 轮）。对账口径注意：Hindsight 合并近似事实后 `metadata.candidate_id` 会丢链接，"按 cid 缺失"会高估真缺口，内容级重复已自然去重。

**新 skill**：`beacon-refactor-swarm`（heirloom-lite `.agents/skills/` + `.claude/skills/` 镜像）——`.scratch/refactor-plan` 任务卡体系 + Herdr 窗格派发纪律 + run-wave.sh 流水线 + jev 预检 + Muse 审查 + sha256 合入核对。内容对齐根 AGENTS.md 现行版（初稿按 facts 写漏了 Herdr/muse 细节，已修正——教训：**facts 可能滞后于 AGENTS.md，写 skill 时先核现行规则文件**）。

**基线**：evaluations **2179** / candidates（approved **357** / pending 610 / rejected 82）/ memories **357** / Hindsight **3953+** 事实（增长中）/ skill **19** 个。

**Devin 2026-10 月 token 用量**（sessions.db 按日聚合）：输入 **235.9M** / 输出 **43.4M** / 缓存读 **7.31B** / 缓存写 **18.2M**；峰值 10-08~09（重构蜂群期，单日输入 50.9M/72.8M）。全表累计 346.3M 非缓存。
