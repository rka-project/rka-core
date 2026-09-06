# RKA Core 审计复核与开发计划

- 日期：2026-09-05
- 状态：PI 已认可复核方向并授权整体设计、开始实施；当前隔离分支推进首批安全修复，尚未合并或部署。
- 基线：`rka-project/rka-core`，`f8db01b33acc76cfa6f9fff804868c21cd08d58b`。
- 请求：重新阅读最新审计报告，并准备接下来的开发计划。
- 约束：本地优先、研究者控制；测试隔离；保留当前运行环境及其他 worktree。
- 上位方向：[当前 roadmap](../../../ROADMAP.md)、[2026-09-03 执行计划](2026-09-03-rka-ecosystem-active-roadmap.md)、ADR 0012、0013、0016、0017、0018。
- 总体设计：[Core hardening design](../specs/2026-09-05-core-hardening-design.md)；后续各工作包仍按各自退出门验收，不代表整份审计已经关闭。

## 1. 决策依据与复核边界

原报告：`RKA Core 设计与代码审计报告 / Design & Code Audit`，2026-09-05。
输入文件 SHA-256：`f85187886fda3062b01de0a4e5f814515f1c676f1e32488a26d8c407f8049756`。
报告针对当前 main，同一提交已通过 GitHub API 重新确认。

本次复核支持把安全、升级可靠性和记录完整性置于近期主线。保留已经验证的
Core 分层、项目作用域、向量空间身份、事务抽象、备份和 public-contract 工作。
E5 完成前不把现有 main 当作已验证发布产物交给 App 或公开 Demo。

证据分为：独立复现、源码确认、外部状态确认、待补证。
原报告的完整 `CODE_MAP.md`、复现脚本和历史数据分析产物没有随附件提供，
所以不能把“165 条、约 23 条高危”直接视为 165 个已独立确认的 bug。
原报告的 3230 个测试通过和 OOM 峰值是原审计/issue 的证据，本次未重跑全套
测试或实际模型 OOM 实验，也未核验生产库中“至少 40 条过期记录”的统计。

### 本次证据摘要

| 报告项 | 本次结论 | 证据与范围 |
|---|---|---|
| H-1 SQL hook | 独立端到端复现 | 临时 SQLite + ASGI：项目 A 创建 hook 返回 201，fire 返回 200，项目 B 的 synthetic decision 被修改。`hook_dispatcher.py:177` 直接执行请求提供的 SQL。 |
| H-2 SPA 路径穿越 | 独立 HTTP 复现 | 编码后的点路径返回静态目录外的 synthetic sentinel，HTTP 200。`app.py:481` 没有目录 containment。 |
| H-9 BibTeX | 两个路径均独立复现 | 本地 `bibtexparser==2.0.0b9` 没有 `parse`，有 `parse_string`；regex 默认导入路径被 `actor='import'` 拒绝。 |
| H-14 typed MCP 丢字段 | 独立 dispatch 复现 | `register_source.provenance`、`record_decision.tags` 变为 `None`；literature 默认新增分支未传 tags/status/related_decisions。下游替身只记录参数，无外部写入。 |
| H-11 journal 归属 | 独立 service 复现 | executor 记录可更新为 pi，verbatim_input 为 NULL；audit 的 actor 为 system，details 只有字段名。存在修改痕迹，但无法还原旧归属或识别此次修改者。 |
| C-1 / H-7 embedding | 源码 + 有界输入复现 | 默认批次完整传递 8 × 20,535 字符；API 启动调度 backfill；旧 CLI 没有绑定 generation。真实 RSS、SIGKILL 重启循环仍以 issue #158 为外部证据。 |
| H-10 directive 生命周期 | 源码及 #153 相互支持 | live supersede 与 admin repair 均只标记派生 claims/clusters；未处理 directive 的生命周期。 |
| H-12/H-13 currentness | 源码 + 判定函数复现 | red claim 在 entity resolver 为 non-current，在 search 的 retired predicate 中未体现；030 resolution 字段无 Python 读写链路。 |
| H-15 pack FK | helper 复现、端到端待补 | unresolved checkpoint.mission_id 变为 NULL，notification.hook_id 保持原值；完整 import 的 422/500/rollback 仍需专门测试。 |
| H-3/H-5 文件入口 | 源码确认边界缺口 | workspace 接受客户端根路径；source filepath 读取服务器文件，虽有大小及末级 symlink 检查，仍缺获授权根目录边界。 |
| H-6 remote connector | 源码确认、威胁链待完整测试 | OAuth proxy 验 token 后透明转发 MCP；同意页不展示 client/redirect；SSH 脚本转发整个 REST 端口。 |
| H-8 发布门 | GitHub 状态确认 | 最新正式 release 为 v2.8.1，未见 v3.0.0 tag/release，publish-container workflow 运行列表为空。尚未独立枚举全部 GHCR package 历史。 |
| H-16 测试覆盖 | 接受缺口，修正绝对表述 | lifespan 测试归 Agentic、smoke 禁用 embedding；已有 legacy adoption 单元测试；recovery smoke 接受真实旧库并升级副本，缺的是固定旧库集成验收证据。 |

本次所有可变测试数据位于 `/private/tmp/rka-audit-review-67cmub`。
`probe.py`、`probe_secondary.py` 和测试数据库仅用于本地复核，未装入生产环境。
没有连接生产 API、LM Studio 或生产 Docker volume。

## 2. 修复前必须保留的设计约束

1. **威胁模型决定安全优先级。** loopback 单人运行、共享主机、远程 OAuth、公共
   Space 是不同边界。能访问 API 的调用方可执行任意 SQL/读任意可读文件，是
   已确认缺陷；不把所有本地文件能力笼统称作远程认证绕过。actor 是归属信息，
   不能通过要求 `actor='system'` 或 `X-RKA-Actor` 来完成授权。
2. **故障状态不能混淆。** 普通 embedding 异常已有 failed 状态；本次替身测试也
   返回 failed。需要修复的是 SIGKILL/OOM 后的持久进度、重复自动启动、资源预算
   与恢复路径。已有完整健康索引不一定全量重嵌，legacy adoption 路径确实存在。
3. **向量重用必须证明同一空间。** 只有身份兼容、输入配方和内容哈希可验证时才
   逐行保留；模型、模板、token 截断策略改变后不能为了少重嵌而混用旧向量。
   输入配方变化要进入空间身份或明确的编码版本，所有写路径遵守同一规则。
4. **PI 原文和 agent 转述要分别建模。** `JournalEntryCreate` 明确允许原始文档
   摄取没有 verbatim_input；不能简单把 MCP 的转述规则强加到所有 REST 创建。
   历史数据缺原文必须报告 unknown，不能补造 PI 原话。
5. **被引用不等于依赖成立。** journal 给 decision 提供证据，与 directive 的
   有效性依赖 decision，是不同方向。supersede 不能自动撤销所有 references /
   justified_by 邻居，更不能凭文本相似性批量宣布 PI 指令失效。
6. **统一判定策略，保留不同语义轴。** 结构失效、复核警告、人工处置、提取可信度、
   科学支持程度保持可区分。共享 currentness 规则应解释这些字段的优先级，
   而非把它们全部压成一个布尔值或把“已完成复核”理解为“恢复有效”。
7. **版本与发布分别记录。** `pyproject` 的 3.0.0 可以是待发布源码版本；已验证的是
   GitHub release/container 发布门未完成。应纠正 changelog/安装文档的完成态表述，
   无需为此先回退整个代码版本。未跑 publish workflow 也不能单独证明所有可能的
   GHCR 发布方式从未使用。
8. **哈希证明内容一致性，不证明身份真实性。** pack 重映射后重算哈希可能是必要的；
   应先验证输入的原始完整性，再重映射、重算并验证目标，而不是禁止一切重算。

## 3. 分阶段执行与 PR 边界

每项首先把本次复现或报告样例改为可维护的回归测试，再实施最小修复。
下面是工作包，实际 PR 数量以是否可独立验证和回滚为准。

### 阶段 0：固定验收基线

**B0 — 风险台账和测试隔离契约**

- 以当前提交固定本次已确认项；完整 CODE_MAP 到位后逐条追加证据与去重关系。
- 台账字段：原 ID、入口/实际后果、证据等级、部署前提、受影响契约、work package、
  regression test、关闭证据。未提供的余下风险保持待补证，不阻塞已确认问题的修复。
- 记录生产的实际端口、卷及镜像身份时只做最小只读检查；测试启动必须证明没有引用它们。
- 退出：每个首批修复有明确的失败断言和基线；不能用一个总代码覆盖率数字代替。

### 阶段 1：阻断严重故障与越界访问

**S1 — 禁用不受约束的 SQL hook 执行**

- 修改 `hooks_service`、`hook_dispatcher`、模型/契约与对应路由；默认拒绝新增和执行
  SQL handler。存量 hook 和执行记录保留为历史，并报告 unsupported/disabled。
- 阻断位置覆盖直接 service 调用、事件触发、REST、typed MCP 和 legacy 工具；
  不以隐藏菜单或不广播工具充当控制。不把 SQL 参数化当作任意 SQL 的解决方案。
- 不扩大 Agentic 功能；占位的 mcp_tool handler 不能记录未实际发生的 success。
- 验收：跨项目 synthetic UPDATE 被阻断；已有危险 hook 不能在 journal 创建时执行；
  不出现部分事务提交；正常 journal 写入及保留的通知行为通过。

**S2 — 修 SPA containment，并审查文件入口的授权根目录**

- SPA 单独做小 PR：规范化根路径和候选路径，越界、编码点路径、绝对路径与
  symlink 逃逸不返回目录外内容；正常资源和 SPA navigation 保持可用。
- 随后的文件边界 PR 覆盖 workspace scan/ingest、source filepath、BibTeX-file、
  MCP workspace/bootstrap 及其他同类入口。服务端文件读取默认关闭或受操作者
  配置的根目录限制，不能让请求自己声明一个“允许根目录”。
- 保留 ADR 0016 的主机侧读文件、传有界字节路径；逐层验证 symlink、Windows drive /
  UNC、Linux 路径、编码、文件类型、字节数、枚举数量、并发与 TOCTOU 控制。
- 验收：允许目录内正常导入通过；目录外 sentinel、父级 symlink、伪造 manifest
  全部被拒；遍历在达到预算后停止，不能先 `sorted(rglob(...))` 物化整个目录树。

**E1 — #158 资源预算与 backfill 生命周期**

- 第一提交补合成旧索引和长文输入回归；随后给所有 embedding 入口统一的有界
  输入策略、批次预算、并发限制、超时和可观测拒绝/截断行为。原始研究文本不改写。
- 总 token 数之外，还要限制单条长度、批次数及最长序列造成的 padding；按后端
  能力明确精确 token 计数或保守上限，字符数不能冒充精确 token 数。
- 将全量推理从 API 请求进程移到 Core 自有的受控 worker/独立 job runner，复用
  现有队列与 transaction；App 负责进程监督，不接管 Core 的索引算法。
- API 首启保持健康并明确 lexical 降级。任务持久化 generation、进度、attempt、
  lease/heartbeat、退避、取消和耗尽状态；OOM 后由外部监督/过期 lease 识别，
  不能指望被 SIGKILL 的进程自己执行 finally。
- 禁止启动、PUT 配置、显式 backfill、普通写队列同时拥有同一 generation 的重建。
- 验收：#158 尺度的合成语料在默认支持的内存配置下有实际进度；记录 API/worker
  各自 peak RSS；重启、provider 断连、单条毒性长文均不能让 API 反复重启。
  健康完整索引启动不重嵌；坏行不会阻止其他可处理记录推进。

**E2 — 可执行的离线重建与保守索引采用**

- 依赖 E1 的输入策略和唯一任务所有权。实现 Core CLI 的状态检查、dry-run、
  受监督离线重建、resume；旧 backfill CLI 委托同一 generation-aware 服务。
- 使用持久配置而非忽略它后改用 env 默认。索引作用域遵守当前全库 generation
  模型，不能让单项目重建误清其他项目向量。
- 重建前备份并确认 API/worker 已停止/不能访问旧 schema，独占维护状态下改变维度；
  失败保持明确降级，可恢复。不会指示用户手工 DELETE 生产 embedding 表。
- 同空间下只重算不可验证的行；未知或不同空间继续拒绝复用。统一文本组装与 hash
  配方，同时保留原有“新 generation 不混空间、ready 前覆盖率检查”门。
- 验收：populated 768→384 等维度转换、空库首次配置、旧库采用、单条 hash 漂移、
  query-only template 变化、document policy 变化、崩溃恢复、多项目均有测试。

### 阶段 2：可靠写入、归属、生命周期和可移植性

**I1 — BibTeX 与导入 actor**

- 独立 PR 修正确 parser API；使用锁定依赖和无 academic extra 两种环境验收。
- 保留 literature 的 `added_by='import'` 来源语义，事件执行 actor 显式规范为 system；
  不把两个词表机械合并，也不吞掉所有 AttributeError 后静默降级。
- 验收：BibTeX 文本/文件、batch、REST/MCP、重复 DOI、嵌套花括号、解析失败均
  有可解释结果；单条失败统计正确，事务不留下半条 literature。

**I2 — MCP/REST 字段持久化对等**

- 修 register_source、record_decision、record_literature 的每一跳字段转发，
  同时审查 lifted common fields 与后续 legacy adapters 的字段消费。
- 测试覆盖 tags、provenance、status、related_decisions、归属及 omission/null
  语义；使用 service read-back 验证真正保存结果，不能仅比较工具 schema。
- 所有 stable 写操作增加有代表性的字段 round-trip；需要新增契约时更新 snapshot
  和兼容测试，不能删除 failing assertion 以保持绿灯。

**I3 — Journal 归属修正与审计**

- 先明确 asserted author/source、执行此次修改的 actor、原文/转述的区别。
- 普通更新不得无审计改写 source/verbatim；需要修正时保留前值、后值、执行者、
  理由和 revision，冲突更新拒绝或显式重试。来源归属不能靠伪造 HTTP header 验真。
- 覆盖 REST、typed/legacy MCP、bulk 和 service；原文摄取与 agent 转述走明确契约。
- 验收：重放和并发修正不丢历史；失败整笔回滚；#152 已恢复的 journal status /
  confidence / bulk 行为保持可用。历史缺失原文只标识，不推断补全。

**I4 — #141 currentness 策略与 resolve_stale**

- 先固定真值表，再实现共享判定并接入 resolver、search、graph、freshness、
  maintenance、MCP 读投影；兼容已有字段而不是同步大规模改 schema。
- 复用 migration 030 实现有理由、有 actor、可重试的 resolve_stale；处置记录、
  审计、可选 journal 关系和关闭提示必须原子完成。新 stale 信号重新开启复核。
- 覆盖 current/dismissed 与 historical/retired/superseded/retracted；需要复核、
  结构失效和 inactive 处置有明确优先级，任何 green/清提示不能偷偷抹掉硬失效。
- 原始 `stale=false` 更新必须委托可审计处置，或返回明确兼容错误；不得保留绕路。
- 验收：同一实体在各入口 currentness 一致；inactive 数据仍可追溯但不抢占等价
  current 结果；错误项目、空理由、失败注入、精确重试、reflag、pack 往返通过。

**I5 — #153 directive 依赖和 supersede 事务**

- 依赖 I3/I4。先用小 ADR/契约说明哪些明确生命周期依赖允许使 directive 失效；
  普通引用、背景文献和独立 PI 原則不触发自动 supersede。
- 缺少依赖关系时输出候选复核提示；语义相似性只能辅助发现，不自动回填权威关系。
- 把 live 和 admin repair 的影响发现/执行抽为同一 helper，覆盖边方向及 source_type；
  不在两处继续复制修复逻辑。覆盖 journal 更新引发的派生失效。
- 收紧二次 supersede 的状态前置、revision/idempotency，并将新记录与旧记录
  转移纳入能证明的原子聚合，或显式持久的可恢复协议；不能留下无提示的双 active。
- 验收：仅目标依赖变化；独立 directive 和其他项目不变；A→B→C、重复请求、
  并发替代、过程中异常、admin dry-run 与 apply 一致；历史知识不被删除。

**I6 — Knowledge Pack 引用与 hash 完整性**

- 缺失 FK 在输入/预检阶段给结构化 issue。对可能的有意排除项制定有记录的策略；
  必要关系丢失则拒绝导入，不能默认置 NULL 并返回空 issue 列表。
- 核对 SQLite FK schema 与逻辑/多态引用目录，包含 recommended_option_id、hook_id、
  checkpoint.mission_id 等。JSON 文本不因为像 ULID 就被无条件改写。
- 原始 hash 验证 → 受控 ID 重映射 → 重算目标 hash → 目标验证 → 原子发布；
  中途失败不留下项目/文件残片。未签名包的 hash 不等于来源认证。
- 验收：旧/新 pack、dangling FK、被修改 manifest、任意嵌套无关字符串、错项目引用、
  artifacts 与 graph links 往返、失败注入；记录严格拒绝或显式降级原因。

**I7 — 项目边界与状态机补洞**

- 报告列出的 parent_mission、review_queue、calibration、checkpoint resolve、
  submit_report 按实际可复现后果分成小 PR；高影响写入不能因被列 Medium 而忽略。
- 每个来源/目标都要在同一 project；重复 resolve/report 不能产生孤立决策或静默
  覆盖已接受报告。对确属设计允许的重新提交保留版本、理由和冲突规则。
- 跨项目 read SQL 需要完整查询/调用链证明；缺局部 WHERE 不自动等于实际泄漏。

### 阶段 3：远程边界与发布收口

**R1 — 远程执行策略**

- 对 remote MCP/Space 明确操作 allowlist、项目范围和 host filesystem 能力。
  query 不天然只读；legacy 工具和 load_tools 必须经过同一执行策略。
- 公共 Demo 使用服务端强制的固定数据只读策略；隐藏按钮或取消工具广播不足以
  达到只读。必须约束不经 MCP 的 REST 入口及配置变更/导入等副作用入口。
- OAuth 同意展示真实 client/redirect/权限，限制注册和尝试次数并覆盖 PKCE、
  redirect URI、state/授权请求完整性；SSE/JSON-RPC 的实际执行语义要做端到端测试。
- 若首个 Core release 不能完成远程安全验收，对这些可选入口默认关闭并声明
  unsupported；不能在能力未隔离时继续推荐 tunnel 脚本。
- 验收：凭据缺失/权限不足拒绝；只读 token 不能借任意工具、REST 或批请求写入/
  读宿主文件；允许的本机 Codex/Claude 工作流不受远程策略误伤。

**R2 — 安装、成熟度和旧库验收**

- 修安装链中的 credential version pin/失效 probe，明确最低兼容版本与当前版本。
- 对冻结能力复用一个 maturity/deprecation 元数据源映射 REST/OpenAPI、MCP、Web、
  skills。已有 Writer REST deprecation middleware 保留；不能声称所有 REST 都无标记。
- 建立合成、可重复生成的 pre-3.0 数据库 fixtures：标明历史 commit、schema、
  生成步骤、校验值、代表性行/边/索引；不把真实研究库提交到 GitHub。
- 先覆盖最后一个正式 release 和 #158 对应的升级前版本，再按迁移风险扩展；
  从早期 migration 升级的合成场景与真正旧版软件生成的 fixtures 分别标识。
- 补 Core-owned lifespan embedding 测试；保持无 LLM SDK 安装门；fresh vs upgraded
  schema 的差异按明确预期验证，包含 indexes/triggers/FK，不只比较迁移数量。
- 对实际变更的冻结面运行相应测试。Ruff/lock 检查分清新增错误与已有基线，避免
  为小修复混入全仓格式化。大 schema 压缩和大规模删除死代码放后续版本。

**R3 — E5 发布并验证产物**

- 前置：S/E/I 工作包的 release-blocking 项通过；其他台账项有具体证据和延期理由。
  会跨项目写、泄露文件、丢失溯源或导致无法升级/恢复的缺陷不能以“Demo 数据是假的”豁免。
- 本地验证 → Core 全套门 → macOS/Windows/Linux wheel smoke → 升级/备份/
  恢复/pack 测试 → 受限资源真实 embedding 测试 → 合约/检索非回归。
- 记录测试环境、提交、依赖、实际 RSS、恢复结果。标准配置默认 API 2g / worker 4g
  必须有证据；不同配置的可用性不能由单次健康检查外推。
- 依 ADR 0018，从验证后的 tag 创建正式 release，执行 amd64/arm64 镜像发布、
  manifest digest smoke、SBOM/attestation、包可见性与匿名 digest pull read-back。
  prerelease workflow 若需要另作小修改，不能假设现有 stable-only workflow 已支持。
- 发布验收完成后，App 再固定该 digest。发布前的 main 合并、代码版本字符串、
  workflow YAML 存在、Docker build 通过都不足以宣布这一门完成。

## 4. 依赖顺序与首轮范围

| 顺序 | 首选交付 | 主要依赖 |
|---|---|---|
| 第一轮 | S1 SQL hook、S2 SPA 小修、E1 #158、I1 BibTeX、I2 MCP 字段 | B0；这些修改可各自验证，适合独立 PR |
| 第二轮 | 文件根目录控制、E2 离线重建、I3 journal 审计、I6 pack | E2 依赖 E1；其他按文件所有权分别推进 |
| 第三轮 | I4 currentness/resolve_stale、I5 directive、I7 项目/状态机 | I5 依赖 I3/I4；currentness 先定真值表 |
| 发布收口 | R1 remote 安全或显式关闭、R2 安装/升级矩阵、R3 产物 | 受影响契约与所有阻断项通过 |

第一轮从 **S1/S2 小修与 E1 旧库长文回归** 开始。I1/I2 是明确局部正确性修复，
不需要等待全局生命周期设计。每个 PR 的结果包含失败用例、修复后断言、受影响
契约和回滚说明。工作包可独立推进不等于本计划已启动其他 agent 或任务。

不承诺固定完成日期：#158 的真实模型资源验证、旧库样本与跨平台执行是主要
不确定性。第一轮完成后，以实际 PR 和测试结果估算后续轮次。

## 5. 隔离、回滚与运行环境保护

- 每个实现工作包从当时核验的 GitHub main 建立 `codex/` 分支及独立 worktree。
  不重置、清理或借用现有 journal/portable-embedding/research-story worktree。
- venv、数据根目录、DB、模型 cache、临时目录、日志明确指向测试环境。
  测试不依赖个人 shell 中默认的 RKA endpoint 或 LM Studio 配置。
- 优先使用 ASGI 与 synthetic DB；容器测试使用唯一 Compose project、唯一卷、
  独立 network。无 host port 或使用动态 loopback 端口，不能绑定当前运行端口。
- 不运行会命中生产的默认 `docker compose up/down`；清理必须按已核对的测试
  容器/卷名称操作，不能用 prune 或 broad cleanup。
- 真实模型资源实验最后在受限测试容器执行，记录并限制可用资源，避免耗尽本机
  Docker VM；不用生产 corpus/cache/后端做压力复现。
- 数据迁移只跑旧版生成的 synthetic fixture 或经授权的备份副本。代码回滚与
  数据回滚分别验证；旧 binary 不能直接启动新 schema 的生产库。
- 历史开发库清理另列任务：只读候选清单 → 人工确认 → 带理由的更正/处置 → read-back。
  本轮修服务不顺手重写历史决策、补造 provenance 或删除报告提及的测试记录。

## 6. 对 App、Hugging Face 与 Writer 的影响

- **Core**：E5 的前置补上明确的安全、升级与完整性门，原来的 repo 边界保留。
- **App F0/A0**：独立测试/骨架工作可继续；面向用户的发行依赖通过验证的 Core digest。
  单容器 supervisor 不能修复 Core 索引/溯源语义；它只消费 Core 的恢复契约。
- **A1 公开 Demo**：严格按 2026-09-03 roadmap，固定合成样本、只读检索/关系/
  导出，无上传、访客持久记录或用户 secrets。执行层的限制属于上线门。
- **A2 自有 Space 模板**：独立的无数据模板，用户自己设置数据、权限、后端和持久化；
  不把公共 Demo 实例当作个人云服务。保持本地部署为敏感研究的推荐路径。
- **Writer W0**：设计与 schema/fixture 工作可继续，声明依赖的 Core 契约；涉及
  上游失效的集成验收必须等待 I4/I5 的语义稳定，不能假定当前 currentness 已一致。

本计划产物仅为审计复核和实施安排。没有因此修改生产设置、创建 GitHub issue、
合并/发布代码、购买云服务或部署公共 Space。
