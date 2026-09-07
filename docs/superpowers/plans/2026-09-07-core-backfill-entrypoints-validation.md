# Core hardening 第六批：E1c pack / CLI 入口统一

- 日期：2026-09-07；基线 `a25fb93`。
- 隔离分支 `codex/core-audit-hardening`，目录
  `/private/tmp/rka-core-hardening-5OnbMY/source`。
- [具体设计与兼容边界](../specs/2026-09-07-backfill-entrypoint-unification.md)。
- 本轮仅本机开发/验收/本地提交，不推送、合并、部署或修改运行环境。

## 实现与精确语义

1. Pack 的 graph、严格 FTS、import receipt 和可选 embedding intent 在同一
   managed transaction 中提交。FTS 失败、入队失败及晚于文件发布的失败都回滚；
   原有文件 staging/归属/哈希/完整性门保留，只撤回该导入自己创建的文件。
2. Receipt 是现有 `jobs` 表中的 terminal `pack_import` system record，记录
   API 事务已完成的 lexical 工作，并链接 worker 的 backfill job。
   Insert 与 terminal update 原子提交，worker 不可能领取中间 pending 状态。
   不新建表、不做 schema migration，不把运行时 receipt/job 导出到研究 pack。
3. 去掉 import route 的 API background vector loop 和 volatile registry 依赖。
   `index_project` 现在只做严格、128 行 keyset 分页的 lexical repair；cluster FTS
   保留，cluster vectors 仍 parked。`defer_indexing` 兼容参数两种值均不执行模型。
   普通 BaseService FTS 的旧容错默认不变；只有本次 import/repair 显式启用 strict。
4. 原 import status URL 读取 durable receipt 与关联 job。分别返回 lexical_state、
   semantic_state、embedding_job_id、semantic_ready 和 generation/index_state。
   没有 backend 时是 lexical complete / semantic disabled，不伪装 semantic ready。
   Receipt 保留历史关联；后来重建不会悄悄改写历史 receipt，旧 `imp_` ID 不可恢复。
5. 复用现有每 generation 单 active intent 的 dedupe/lease/source/finalize 边界。
   增加 project/type/force/batch-cap scope；不把项目请求扩大到全库。不同范围或
   更紧的 batch cap 返回 busy。Import 若遇 busy 会返回 409，整个 import 回滚，
   需在当前 owner 完成或取消后重新上传；本轮没有增加跨任务等待调度器。
6. 旧 `backfill-embeddings` CLI 和 Python helper 不再有独立向量循环，只入队。
   CLI 读取持久配置、检查已有 generation，不用 env 模型 fallback、不初始化或
   reshape 向量表、不探测模型。保留 project / artifact / figure / claim / force；
   batch-size 接受 1–128 并由 worker resource limits 进一步收紧。返回 durable
   job 而不是 completed counts，是明确的兼容行为变化。
   保留旧默认的 content-hash 检查：由 worker 分页计数/处理变化及缺失行，跳过
   未变化且空间兼容的行；不能用纯 missing-metadata anti-join 替代旧行为。
7. `--force` 只在当前空间逐行原子替换，不删表或全量失效 metadata。普通 missing
   retry 保留已提交好向量；force retry 可重新访问成功行，但受既有有限 attempts
   限制。推理失败保留旧向量，不等于 global search 仍 ready。
8. Scoped 完成校验选中范围覆盖和现有 coherence 门。其他项目缺向量时 scoped
   job 可以 complete，但整个 generation 仍 reindexing；只有原全库覆盖率门能
   宣布 ready。新增行为不越过旧 generation/lease 写入保护。
9. 新增 `embedding_backfill_v2` task type + payload version 2，承载 scoped/force/
   hash-check/batch-cap 语义。旧 E1b worker 会拒绝未知类型，不能忽略新字段后
   按全库执行。当前 worker 对 v1/v2 共用 generation 所有权查找/取消/恢复，
   并在推理前拒绝未知 payload version。仍要求 API/worker 同版本协调升级，
   旧 worker 可消耗失败 attempts，这不是混合版本 scheduler 的支持承诺。

## 验收记录

所有输入均为 synthetic。RKA_DATA_DIR/DB 明确指向临时目录，LLM 和真实 embedding
默认关闭，HF_HUB_OFFLINE=1，默认 API 指向 loopback 端口 1。配置测试使用假的
HTTP/model；未连接生产 API、LM Studio、Docker volume 或现有模型 cache。

- 首批 5 项回归先红：暴露 CLI/project scope 不受支持、import 当场推理、没有
  durable receipt、scope busy 不回滚、receipt failure 不回滚；实现后专项 22 passed。
- 新旧组合首轮 **105 passed / 5 failed**，26.26 秒：4 项旧 fixture 未绑定 generation
  并期待 inline vector writes；1 项锁定 `imp_`。调整为真实 queue/worker 执行后，
  仍验证目标项目 metadata、source/target vector 独立、artifact/figure 可检索性。
- 随后组合 **125 passed / 1 failed**，28.98 秒；剩余失败为 CLI 测试把 legacy
  constructor signature 与 persisted-config signature 混用。修正 fixture 为真实
  config-derived generation，而非放松产品的 mismatch 拒绝。CLI 6 passed，1.23 秒。
- ownership/transaction/API/CLI 专项 **55 passed**，10.16 秒。
- 本批早期新专项 **20 passed**，4.74 秒。包含跨项目 force、global readiness
  不越权、receipt 跨连接恢复、非 receipt ID 拒绝、取消后保留 lexical 数据、
  strict FTS 原子失败、force 失败保留旧向量、晚期 receipt 失败的文件/graph/FTS/
  queue/generation 回滚、CLI 保存配置选择和非法输入不入队。
- 末轮兼容复查发现：旧 CLI 默认也检查 content hash，最初的队列委托仅查缺失
  metadata，可能跳过过期向量。新增变更一行/保留一行的回归先红；补上 worker
  内 keyset hash-check 模式，未改全局输入配方，也不靠 force 全量重嵌掩盖问题。
  其修复后的专项及完整 Core 另行重跑，以上 20 项数字为追加前的记录。
- 追加 hash compatibility 后，scoped/API/CLI/resource/旧 adapter/ownership 组合
  **76 passed**，13.45 秒；变化行重嵌、未变化行保留，processed/total 均为 1。
- 追加前完整 Core **3534 passed / 1 skipped / 294 deselected**，268.77 秒，
  5 subtests passed；追加后版本再跑完整 Core，不能用此结果替代最终门。
- Hash compatibility 完整重跑 **3535 passed / 1 skipped / 294 deselected**，
  270.16 秒，5 subtests passed。随后在升级兼容检查中增加 versioned task 防护，
  所以这仍不是最终版本验收数字。
- 从 `a25fb93:rka/services/worker.py` 读取真实 E1b worker 源码，编译到测试命名
  空间，在新建临时 synthetic DB 上处理 v2 job：明确 Unsupported job_type，
  **0 inference / 0 vector writes**。当前 worker 用同一 job 新 attempt 完成，
  只嵌入预期的一条记录。测试仅终结/清理自己的临时库，不接触已安装 worker。
- Versioned ownership/API/CLI 组合 81 passed，25.68 秒；追加未知版本拒绝后
  ownership/API/CLI 新专项组合 **39 passed**，8.54 秒；最终版本再跑完整 Core。
- Base-plus-sqlite-vec 环境 **77 passed**，19.24 秒。不含 FastEmbed/bibtexparser；
  覆盖本批专项、旧 import progress、E1b lifecycle/API 和 transaction 取消回归。
  Hash compatibility 追加后重跑 **78 passed**，19.05 秒；versioned job 另行重跑。
  Versioned job 最终基础安装门 **79 passed**，19.34 秒。
- Startup smoke 通过：installed entry point、迁移、Phase-2 文件锁、REST workflow、
  MCP、worker、sqlite-vec、Web dashboard；final migration sweep 为 0。
- REST/MCP snapshot 的只读校验均通过，无 snapshot diff，无新增端点或契约数量变化。
  已有 import JSON response 为开放 schema，新增 receipt/status 字段另以回读测试覆盖。
- 本批不改 Web 源码；startup smoke 使用上一批已构建的 dashboard。
- 涉及 Python 文件 Ruff、workflow YAML 和 `git diff --check` 校验通过。
- **最终 versioned job 版本完整 Core：3536 passed / 1 skipped / 294 deselected**，
  **271.14 秒，5 subtests passed**。5 个 warning 为既有 SWIG 类型的
  DeprecationWarning；比上批 3514 passed 增加 22 个回归案例。基础安装最终
  **79 passed**；版本化 worker 的 startup smoke 也已重新通过。
- 新专项已接入原生 Linux/macOS/Windows × Python 3.11/3.13 的 base-plus-vec CI；
  本机编辑 CI 配置不等于远端矩阵执行成功。

复跑完整 Core（隔离目录内）：

```sh
RKA_DATA_DIR=/private/tmp/rka-core-hardening-5OnbMY/test-data \
RKA_EMBEDDINGS_ENABLED=false RKA_LLM_ENABLED=false \
RKA_API_URL=http://127.0.0.1:1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m 'not writer and not agentic'
```

JUnit 位于 `/private/tmp/rka-core-hardening-5OnbMY/e1c-*-results.xml`，不是发布产物。

## 保留的门与保护状态

- E2 仍需统一 input/hash recipe、旧库保守采用、可执行备份/独占维护/离线维度
  转换和恢复。本批 scoped force 不是离线换空间命令。
- 原生推理 kill/RSS sandbox、真实模型受限内存实测、原生 Windows/Linux 运行与
  发布门仍待验收；不关闭 #158、不宣称所有 API 推理均已搬走。
- query/probe 和部分 artifact 单条推理仍可在 API 中运行；本批移除的是 pack 与
  legacy CLI 的独立全量向量循环。
- 主工作区核对仍为 `main / f8db01b`，保留原有未跟踪审计计划；journal
  `fa59b4e`、portable embedding `4912eaa`、story `c8281a4` 均未修改。
- 实现、22 个新案例、兼容断言调整、CI 与本文在同一 E1c 本地提交中审查。
  没有推送、合并、部署、重启生产、写入生产 RKA 或更新用户记忆。
