# Core hardening 第三批：导入与字段写入修复

- 日期：2026-09-06
- 基线：S2 提交 `802c633`，隔离分支 `codex/core-audit-hardening`
- I1 本地提交：`bb4f73b`；BOM 兼容补充：`2924d29`。
- 工作目录：`/private/tmp/rka-core-hardening-5OnbMY/source`
- 状态：实现、完整 Core/base-profile 及启动 smoke 本机验收通过；未推送、合并、发布或部署。
- 范围：I1 与 I2 已确认的输入/转发/持久化缺陷，以及沿同一调用链复现的缺陷。
- 设计：[总体设计](../specs/2026-09-05-core-hardening-design.md)；兼容说明：[Import behavior](../../IMPORT_BEHAVIOR.md)。

## 修复边界

1. 学术完整安装使用锁定的 `bibtexparser` v2 `parse_string` API，拒绝解析失败的
   blocks。只有依赖确实缺失时使用基础解析器；代码中的 AttributeError 不触发降级。
2. 基础安装使用平衡括号解析子集，保留嵌套/引号/数字字段和原始 BibTeX；不支持的
   宏/拼接、重复字段/键、坏块会在写入前明确报错。不是完整 BibTeX 引擎。
   接受文件开头的 UTF-8 BOM，不跳过第一条 entry，不全局删除正文中的 BOM 字符。
3. 导入来源 `added_by=import` 不改名，导入执行 actor 为 `system`。
   混合 batch 也使用这个映射，保留逐条成功/失败语义。其他非法 actor 仍拒绝。
4. `NoteService.create` 分离 `data.source` 与显式执行 actor：前者是声明作者，
   后者进入事件、关系与审计。既有记录不迁移，不声称实现了 I3 的归属修正历史。
5. typed BibTeX 的 `default_status` 贯通到创建；MCP 导入结果显示错误原因。
6. I2 修复 source.provenance、decision.tags（创建和替换）、literature 的
   status/tags/related_decisions；旧 `rka_add_literature` 增加可选 keyword-only 参数，
   保持旧位置参数兼容。修复 hint.confidence 被 common 参数提取后丢失的问题。
7. 更新路由记录字段是否实际给出，再应用创建默认值；typed/raw/legacy 都不再
   注入未给出的作者/置信度/重要性，也不再吞掉明确的 `hypothesis`/`normal`。
   这些可选更新字段的 null 仍视作省略；列表 `[]` 仍可清空；不改变 manuscript 等
   操作的独立 null 契约。数值 hint 缺省仍是 0.5，明确的 0.0 不会被当作未给出。
8. 不给 decision 增加 confidence 存储列：旧 adapter 已明确将它作为历史兼容参数
   接受但不传给 `DecisionCreate`。修复不把它误当成 journal 的 confidence。

## 证据与验证

所有数据库是 pytest 合成 fixture；统一设置独立 `RKA_DATA_DIR`，关闭 LLM 与
embedding，`HF_HUB_OFFLINE=1`，未替换的 MCP 目标固定为 loopback 端口 1。
没有连接生产 API、LM Studio 或 Docker 卷，没有下载模型。

- I1 初始失败基线：9 failed / 2 passed，复现 v2 API、基础解析器和导入 actor 问题。
  聚焦验收 34 passed；含既有写字段、journal/事务回归的中间组合 58 passed。
- 真正独立基础 venv：`base-venv`，`uv sync --frozen --extra dev`，确认未安装
  bibtexparser。首轮 I1 29 passed / 5 skipped；跳过的 5 项只适用于完整学术解析器。
  创建 venv 时沙箱 uv 系统配置错误、离线缓存缺轮子；经权限审核仅下载锁定依赖
  到临时环境，未安装/重启生产运行环境，也未改变 lockfile。
- I2 修正测试自身 GET 返回结构后，失败基线 15 failed / 3 passed，覆盖真实写后
  回读而不是 mock 的参数记录。新增 legacy 包装层测试又捕获 2 项默认值注入错误，
  同步修复后新增文件 29 passed；含既有 create/update/dispatch 回归组合 99 passed
  （该组合运行于增加最后 3 项默认创建回读前）。
- 测试中纠正两类不成立的预期：禁止重复 DOI 是现有数据库约束，不因
  `skip_duplicates=false` 而取消；RegisteredSourceDetail 是平铺对象，不嵌套 source。
  这些测试调整不改变生产约束，也没有移除真正的字段丢失断言。
- AST 回归禁止从残余 `**kw` 再读取已经提升为具名参数的 common fields。
- 首轮完整 Core：1 failed / 3395 passed / 1 skipped / 294 deselected，248.94 秒。
  唯一失败是既有测试把保护规则绑定到旧的签名默认值比较方式；现在改为字段是否
  给出的跟踪，因此签名必须保留 None。守卫已更新为检查统一 dispatcher 与旧版
  wrapper 都不注入默认值，并核对创建默认值与 JournalEntryCreate 一致。
  新增 5 个更新路由的省略/明确默认值测试；原 PI 归属回读断言保留且通过。
  此次测试调整后，journal write-path + 新端到端字段测试为 53 passed。
- 第二轮完整 Core：3401 passed / 1 skipped / 294 deselected，252.77 秒。
  随后的解析器复核又发现 BOM 前缀在 base 路径被误当作隐式注释，导致首条漏导。
  文本/文件 × academic/base 四项先运行得到 2 failed / 2 passed，再补上只识别
  文件开头 BOM 的兼容处理。包含该修复的最终全量已单独复跑，不沿用第二轮计数。
- BOM 补充后的 I1 专项：38 passed。最终实际基础环境组合（I1、journal 保护、
  I2）：84 passed / 7 skipped，16.85 秒；7 项跳过均为 academic 分支实例。
  JUnit：隔离目录下 `write-repairs-base-final.xml`。
- REST/MCP 公共快照只读检查通过，无需更新已发布的 typed/REST schema 快照。
- 新增及涉及的业务文件 Ruff 通过；`server.py` 与 I1 基线同为 10 项既有诊断，
  无新增；不顺带重构。
- 临时实例启动 smoke 通过（`--require-web --require-vec`），覆盖入口、迁移、
  Phase-2 文件锁、REST、MCP、worker、sqlite-vec 和已构建的网页资源。只在权限
  审核后使用随机 loopback 端口启动，结束关闭测试子进程。
  BOM 补充后的最终代码也已复跑通过。
- CI 的 Linux/macOS/Windows × Python 3.11/3.13 矩阵增加真实无 academic extra 的
  import/write 回读 gate。配置已添加，不等于远端原生矩阵已经通过。

最终完整 Core：**3405 passed, 1 skipped, 294 deselected, 5 subtests passed**，
253.49 秒，进程退出码 0。JUnit：隔离目录下 `write-repairs-core-final.xml`。
1 项跳过是原生 Windows 边界测试，294 项为 Core profile 明确排除的
Writer/Agentic 测试；5 项 warning 为既有 PyMuPDF/SWIG 弃用提示。
最终快照、涉及业务文件 Ruff、workflow YAML 和 `git diff --check` 均通过。

复跑完整 Core（隔离目录内执行）：

```sh
RKA_DATA_DIR=/private/tmp/rka-core-hardening-5OnbMY/test-data \
RKA_EMBEDDINGS_ENABLED=false RKA_LLM_ENABLED=false \
RKA_API_URL=http://127.0.0.1:1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m 'not writer and not agentic'
```

主工作区仍为 `main / f8db01b`；原有未跟踪审计计划文件未修改。所有代码和
验收记录只保存在隔离 worktree，本次未向生产 RKA 写入研究记录。

## 退出边界与后续

本批不部署到生产、不重写旧记录；声明来源与执行 actor 的分离不是身份认证。
I3 历史归属修正、revision/理由审计，以及 E1/E2 embedding 资源预算/恢复均未实现。
I2 此处是已确认缺陷与共同参数链的代表性回读验收，不是全 stable 操作所有字段的
穷举矩阵；其余覆盖扩展、原生跨平台 CI、历史升级/发布矩阵仍须各自验收。

按已批准交付顺序，下一施工阶段进入 E1 的资源预算与 backfill 生命周期。
