# Core hardening 第四批：E1a embedding 资源入口

- 日期：2026-09-06；基线：`f659be0`。
- 隔离分支：`codex/core-audit-hardening`。
- 工作目录：`/private/tmp/rka-core-hardening-5OnbMY/source`。
- 设计：[E1a 资源边界](../specs/2026-09-06-embedding-resource-boundary.md)；
  使用与兼容说明：[Embedding backends](../../embedding_backends.md#resource-limits-unreleased-hardening)。
- 本批只实现 E1a，不关闭 #158，不宣称 E1/E2 完成。不推送、合并或部署。
- 状态：本批实现、完整 Core/base-profile、启动 smoke 本机验收通过。

## 实现与兼容边界

1. 三个内置 backend 的单条、query/document、batch、connection test 使用同一
   prepared-input 校验。默认单条 8192 UTF-8 bytes，包含模板或 Nomic 前缀；
   不以字符数冒充 token，不截断或重写原文。未知/非法/超上限配置被拒绝。
2. 全逻辑调用先完整校验，再按条数、总字节、最长输入 × 条数的 padding 代理预算
   分批，保持输出顺序。一次逻辑调用最多 128 条、256 KiB；provider batch 最多
   8 条、16 KiB，总调用截止时间最多 120 秒。配置只能收紧，不能解除上限。
3. 每进程只准入一个 provider 调用，没有等待队列。HTTP 截止时间包含重试/退避；
   取消清理真正结束才归还名额。FastEmbed 显式传入有界 `batch_size`，使用专用
   单线程执行器，原生 future 结束才归还名额，调用者超时/取消不能触发第二次推理。
4. 输入拒绝/忙碌不谎报 provider 不可达；超时有独立错误码。现有检索路径仍可
   lexical 降级。不能处理的文档保留 pending/failed，不写虚假向量元数据。
5. Backfill 逐条预检，跳过并统计超长/非法输入与 compose 失败，正常行继续；
   错误样本最多 3 条，不输出任意异常中的原文。既有 provider-wide 故障仍中止
   当前类型，不逐行放大重试。默认保守数据库 fetch cap 为 2，旧 hash 检查每页 8。
6. 不改变 embedding-space identity，不因准入限制重置健康向量。不修改数据库
   schema、已有记录、generation 算法或生产配置。已有超长向量可保留；新的超长
   输入会明确失败，仅重试不能解决，后续 chunking/编码迁移需独立设计。

## 验证证据

全部使用合成数据库、HTTP MockTransport 或假原生模型。临时 `RKA_DATA_DIR`，
关闭生产 embedding/LLM，`HF_HUB_OFFLINE=1`，未替换的 API 固定 loopback 端口 1。
未连接生产 API/LM Studio、下载模型、重建运行环境或访问生产 Docker 卷。

- 初始资源回归：18 failed / 1 passed，实际 adapter 暴露没有输入/批次限制的问题。
  回填红灯用 8 × 20.5 KB 合成行和 10 条短行复现坏行阻断；同时复现全量 hash
  fetch 与不受限 fetch size。不是复现或测量真实模型 OOM/RSS。
- 修正测试 fixture 的必填 journal.type、project_id，以及 FakeModel 对锁定
  FastEmbed `batch_size` 参数的签名；未删除维度漂移或持久化断言。
- 新增 78 项用例，包括 UTF-8、多字节、prefix/template、整批预检、padding、
  顺序、配置、总调用截止时间、原生取消后仍忙碌、HTTP 取消清理、connection probe。
  数据库测试确认：8 条长行失败、10 条短行落库；重跑不重嵌健康行；原文不变；
  durable entity worker 记录失败后仍完成下一个短文任务；错误计数和样本有界。
- 聚焦组合：**147 passed**，7.89 秒（新增用例加既有 backend/backfill 回归）。
  修正 audit-symmetry 假模型后，扩展聚焦组合 **154 passed**，8.69 秒。
- 真正无 FastEmbed、无 sqlite-vec、无 academic extra 的基础 venv：适配器与
  资源入口 **112 passed**，3.20 秒。首次误将向量落库套件放入这个环境，得到
  19 failed / 126 passed / 2 skipped，均来自缺失 sqlite-vec 的前提，不能算通过。
- 随后仅向临时 base venv 安装锁定 `sqlite-vec==0.1.6`，向量落库/回填套件
  **35 passed**，5.49 秒；再确认 FastEmbed 和 bibtexparser 仍未安装。
  离线缓存缺包，权限审核后只下载该扩展；未改变项目依赖清单/lockfile。
- CI 已分开基础适配器 gate 和加 sqlite-vec 的回填 gate，复用
  Linux/macOS/Windows × Python 3.11/3.13 原生矩阵；仅配置完成，未远端执行。
- REST/MCP 快照只读检查通过，无 schema 快照变更。涉及业务文件和新增测试 Ruff、
  workflow YAML 解析、`git diff --check` 通过。audit-symmetry 旧测试文件保留
  2 项既有 unused-import 诊断，与 `f659be0` 基线逐项相同，无新增。
- 隔离启动 smoke（`--require-web --require-vec`）通过：入口、迁移、Phase-2 锁、
  REST、MCP、worker、sqlite-vec、网页资源。权限审核后绑定随机 loopback 端口，
  测试退出关闭子进程；没有占用 9712。
- 第一轮完整 Core：1 failed / 3482 passed / 1 skipped / 294 deselected，
  260.39 秒，5 subtests passed。唯一失败是 audit-symmetry 旧假模型不接受显式
  `batch_size`；锁定 FastEmbed 的真实签名接受该参数。修正假模型并增加批次上限
  断言，保留原有维度和向量落库检查。
- 最终完整 Core：**3483 passed, 1 skipped, 294 deselected, 5 subtests passed**，
  259.60 秒，退出码 0；`e1a-core-final-results.xml`。1 skipped 为原生 Windows
  文件边界用例，294 deselected 为 Core 排除的 Writer/Agentic；5 个 warning
  是既有 PyMuPDF/SWIG 弃用提示。代码与最终完整重跑版本一致。

JUnit 记录位于隔离目录的 `e1a-*-results.xml`。完整 Core 复跑命令：

```sh
RKA_DATA_DIR=/private/tmp/rka-core-hardening-5OnbMY/test-data \
RKA_EMBEDDINGS_ENABLED=false RKA_LLM_ENABLED=false \
RKA_API_URL=http://127.0.0.1:1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m 'not writer and not agentic'
```

## 下一批 E1b / 未完成的验收

- API backfill 仍在 API 进程，status registry 仍易失。本批不能终止 ONNX，也不是
  硬 RSS sandbox；一个挂死原生调用会一直占用本进程名额。跨进程仍可能各跑一个。
- 下一批复用 durable queue，实现 generation 唯一任务所有权、lease/heartbeat、
  attempt、退避、取消/恢复及状态回读；全量推理由受控 Core worker/job runner 执行。
- 需覆盖启动/PUT/显式回填/普通写入竞争、进程消失、provider 断连、真实模型 RSS、
  旧库升级、API 健康保持及原生跨平台门。E2 另行实现可执行的保守离线重建。
- 主工作区仍为 `main / f8db01b`，原有未跟踪审计计划保留。未向生产 RKA 写入记录。
