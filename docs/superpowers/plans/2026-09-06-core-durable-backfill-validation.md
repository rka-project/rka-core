# Core hardening 第五批：E1b 主回填链路持久化

- 日期：2026-09-06；基线：`ba7f5ea`。
- 隔离目录：`/private/tmp/rka-core-hardening-5OnbMY/source`。
- 分支：`codex/core-audit-hardening`；本机验证，不推送、合并或部署。
- [设计与精确边界](../specs/2026-09-06-durable-embedding-backfill.md)。

## 本批实现

1. 启动、PUT embedding 配置和显式 backfill POST 只提交 durable intent，由
   独立 `rka worker` 执行；删除 API 的 `_run_backfill_safely` 与本地回填锁。
   缺失维度时启动不探测模型，明确 lexical 降级，需 Settings test/save 补齐维度。
2. 复用 migration 008/037 的 `jobs`：generation dedupe、有限 attempts、退避、
   worker/lease token、续租和 attempt-local progress。没有新表或数据库迁移；
   `jobs` 已在 pack registry 中列为不可导出的 system data。Intent 不复制配置
   密钥或研究原文。显式重试保留上一条 terminal job，不伪造完整逐 attempt 历史。
3. 跨连接原子去重；active job 已覆盖的请求复用 ID。扩大或改变范围返回 409；
   非法/超长 entity_types 在入队前返回 422。新 generation 终止旧 job 的执行权。
   PUT 的 queued intent 与 generation 变更在原事务内，配置保存失败一起回滚。
4. 增加 heartbeat、写入时 lease proof、source snapshot 和最终完成时核对。
   取消/过期 owner 不能落库、更新进度或宣布 ready；普通 entity job 也受 lease
   和 source guard 保护。推理中或向量已提交但 job 未完成时的编辑会保留可重试
   状态，不能把 coalesced edit 当作完成而丢失。
5. 进程丢失后回收过期 lease，使用 missing-row cursor 接续，保留已提交好向量。
   最后一次 attempt 过期会持久化 failed，并把 generation 标为失败；启动不会
   重新创建无限尝试。完成仍由原覆盖率/一致性门判断，不以 cursor 结束代替验收。
6. 新增 `POST /api/config/embedding/backfill/{job_id}/cancel`，将 active job
   记为 failed / `embedding_backfill_cancelled` 并原子撤销 lease，不承诺停止远端
   或原生推理。状态用原枚举、durable `job_` ID、附加 generation/attempt/lease
   字段；设置页刷新后会读取最近的持久化进度。只运行 API 会显示 pending。
7. 连带修复实际复现的事务取消漏洞：aiosqlite 等待者被取消并不撤销已排队 BEGIN。
   心跳采用协作退出；Database.transaction 在 BEGIN 阶段的取消也回滚，且重复
   cancellation 不能在 rollback 完成前释放连接所有权。不改变正常事务/保存点语义。
8. 兼容旧 NULL project_id 行的来源复核使用 `IS` 匹配原始归属，仍按既有规则写入
   fallback project；没有借此重写旧研究记录或改变迁移政策。

## 红灯与验收记录

所有数据库和进程都是临时、合成数据。环境统一关闭 LLM/真实 embedding 调用，
`HF_HUB_OFFLINE=1`，默认 API 指向 loopback 端口 1。HTTP 使用 MockTransport；
原生子进程使用合成向量，不加载模型。没有访问生产 API、LM Studio 或 Docker 卷。

- 初始 6 项 durable/heartbeat 回归均失败，暴露缺少服务和续租/进度接口；实现后
  6 passed。随后 API/worker 组合 47 passed / 4 failed：旧测试仍依赖内存 registry
  或期待 POST 当场完成，已改成验证 pending → worker → durable read-back；
  覆盖率失败与失败后恢复断言保留，改由真实 queue/worker 驱动。
- 加来源复核后 94 passed / 4 failed，定位到既有 NULL project_id fixture 被新查询
  误拒绝；修正 `IS` 对原始归属的比较，不取消来源变化检查。后续组合 67 passed。
- API/新 durable 专项 25 passed；追加 heartbeat 存储失败、vector→job-completion
  之间的编辑、disabled worker 分类后，28 passed。
- 真实临时子进程提交 2 条后被终止；替代 worker 用新 lease 只处理第 3 条。
  同时验证已持久化进度可从另一数据库连接读取，清空旧内存 registry 无影响。
- 快速失败与 legacy-drain 暴露心跳取消打断 BEGIN 的真实交界错误。添加普通/
  migration BEGIN 取消、重复取消 3 项确定性回归，并改为回滚完成后再释放连接。
  DB/新任务/API 组合 47 passed，32.72 秒；契约/旧 job drain/新任务/DB 组合
  47 passed，32.04 秒。
- 首轮完整 Core：**2 failed / 3506 passed / 1 skipped / 294 deselected**，
  651.24 秒，5 subtests passed。失败为 REST 新增 endpoint 的锁定计数和上述
  legacy-drain 事务取消问题。计数调整为 Core 166、stable 138，并显式断言 cancel
  endpoint 存在；Writer/Agentic 数量与保护断言不变。最终版本另行完整重跑。
- REST snapshot 的唯一功能新增为取消路由及对应计数；MCP snapshot 无 diff。
- 早期 base-plus-sqlite-vec 组合 62 passed，39.85 秒；最终组合 **84 passed**，
  116.83 秒，包含新 lifecycle/API、原配置、note worker 和事务取消回归。
  临时基础 venv 不含 FastEmbed/bibtexparser；本批没有安装生产依赖或下载模型。
- `npm run build` 通过（TypeScript + Vite），保留既有 >500 KiB bundle 提示。
  设置页改动仅恢复持久化状态读取/显示及 idle 停止轮询；本批未做浏览器交互验收。
- 涉及业务与测试 Ruff、workflow YAML 和 `git diff --check` 通过。
- CI 已加入 Linux/macOS/Windows × Python 3.11/3.13 的基础安装加 sqlite-vec
  队列、子进程恢复和事务测试；配置已写，不代表远端矩阵已通过。
- 安装启动 smoke 通过：installed entry point、迁移、Phase-2 文件锁、REST
  workflow、MCP、worker、sqlite-vec、Web dashboard；最终 migration sweep 为 0。
  使用随机 loopback 端口及临时库；worker 报告 No jobs available。
- 最终完整 Core：**3514 passed / 1 skipped / 294 deselected**，705.99 秒，
  **5 subtests passed**。5 项 warning 为现有 SWIG 类型的 DeprecationWarning。
  本轮比 E1a 的 3483 passed 增加 31 个通过案例；Writer/Agentic 按 Core 门排除。
- 只读 snapshot 校验通过：REST 与 MCP 均匹配；未通过改写契约隐藏失败。

复跑命令（隔离目录内）：

```sh
RKA_DATA_DIR=/private/tmp/rka-core-hardening-5OnbMY/test-data \
RKA_EMBEDDINGS_ENABLED=false RKA_LLM_ENABLED=false \
RKA_API_URL=http://127.0.0.1:1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m 'not writer and not agentic'
```

JUnit 保存在 `/private/tmp/rka-core-hardening-5OnbMY/e1b-*-results.xml`，
不是生产数据或发布产物。

## 本地提交与保护状态

- `44ee33f`：事务 BEGIN/rollback 取消修复及 3 项确定性回归，单独提交便于审查。
- E1b 主链路、REST 契约、设置页、CI 与文档随本记录一起提交；本机门全部通过。
- 提交前重新读取主工作区：仍为 `main / f8db01b`，仅原有未跟踪审计计划。
  journal `fa59b4e`、portable embedding `4912eaa`、story `c8281a4` 均未修改。
- 未推送、合并、发布或部署；不把本地分支提交等同 GitHub main 或运行环境更新。

## 未关闭的边界

- 本批是 startup/PUT/manual 的 E1b 主链路，不宣称全部 embedding 入口收口。
  Pack 导入仍使用另一条 API 内索引/向量循环和 volatile `imp_` status；旧项目级
  force CLI 仍需 E2 兼容委托。没有悄悄改变 pack 完成状态的含义。
- 单次 query/probe、部分 artifact 路径仍可能在 API 内执行；不是 ONNX kill/RSS
  sandbox。真实模型峰值内存、受限硬件、独立进程监督及原生跨平台仍需验收。
- E2：统一 input/hash recipe、旧库保守采用、可执行的备份/独占/离线维度变更与
  resume。上述范围通过之前，不关闭 #158 或宣布整个 E1/E2 完成。
- 同版本 API/worker 要一起升级，但本轮不部署、不重启、不迁移生产数据。
