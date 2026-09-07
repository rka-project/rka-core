# Core hardening 首批施工与验收记录

- 日期：2026-09-05
- 基线：`f8db01b33acc76cfa6f9fff804868c21cd08d58b`
- 分支：`codex/core-audit-hardening`
- 状态：首批修复及本机隔离验收完成；未推送、合并或部署。
- 设计：[Core hardening](../specs/2026-09-05-core-hardening-design.md)
- 工作包：[审计整改计划](2026-09-05-core-audit-remediation-plan.md) S1 与 S2 的 SPA 子项。

## 本批实际改动

1. `hook_policy` 是新增、重新启用和执行 hook 的共同限制。仅支持
   `brain_notify`；不能以 actor、legacy 工具或已有/导入记录绕过。
2. 删除任意 SQL 执行和 MCP 占位成功路径。旧 hook 仍可读取、导出、导入、
   禁用；尝试触发会留下 error 记录，原有历史日志不改写。
3. 启用检查与更新在同一项目作用域事务内完成。已有 journal 事务中的 hook
   日志随外层回滚，不提前提交调用方的写入。
4. SPA 以固定的已解析目录为根，拒绝编码路径穿越和 symlink 越界；检查
   index fallback 和 assets 根目录。循环 symlink、非法路径返回受控 404。
5. 同步 MCP 发现信息、wheel/插件内的使用文档、changelog 与 roadmap。

这是有意收紧旧行为：REST 新增/重新启用不支持的 hook 返回 HTTP 422 和
`unsupported_hook_handler`；MCP 沿用现有 API-error 工具错误映射。模型仍能
解析历史 handler 值，无 schema migration、历史记录删除或自动数据清理。

## 隔离边界

- 工作目录：`/private/tmp/rka-core-hardening-5OnbMY/source`，独立 git worktree。
- 单独的 `.venv`、测试数据和 npm 缓存；临时 SQLite / ASGI 合成数据。
- pytest 显式设置 `RKA_DATA_DIR`，禁用 LLM/embedding，设置
  `HF_HUB_OFFLINE=1`，未被 mock 的 MCP 默认连接目标置为 loopback 端口 1。
- 启动 smoke 自建一次性目录和随机 loopback 端口，结束关闭子进程。
- 不连接生产 API、LM Studio、Docker volume；不执行生产重建、迁移或安装。
- 原 main 的源码未修改；原先未跟踪的审计计划保留在原位置。

## 验收证据

- 首批 21 个测试在修复前产生 15 个预期失败、6 个通过；修复后 21 个通过。
  负例覆盖跨项目 SQL、旧 handler 重新启用、MCP 伪成功与静态路径越界。
- 扩展聚焦回归：**116 passed**，覆盖真实 REST、typed/legacy MCP、service、
  pack 导出/导入与执行、正常通知、journal 写入和公共 contract snapshots。
- 首轮完整 Core：3271 passed，1 failed，294 deselected。唯一失败是
  wheel 与插件的 `brain/workflows.md` 内容镜像尚未同步；已补齐该镜像。
- 最终完整 Core：**3274 passed, 294 deselected, 5 subtests passed**，232.16 秒。
  294 项是按 Core gate 排除的 Writer/Agentic 测试；5 个 warning 来自既有
  PyMuPDF/SWIG 弃用提示。最终 JUnit 产物位于隔离目录的 `core-tests-final.xml`。
- `npm run build`：通过，真实静态资源构建成功。保留现有大 bundle 警告；
  本机 Node 23 另有 ESLint 间接依赖的 engine 警告，未变更锁文件或升级依赖。
- `core_startup_smoke.py --require-web --require-vec`：通过，包含入口、迁移、
  Phase-2 文件锁、公共 REST 流程、MCP、worker、sqlite-vec、网页及其资源。
- Ruff：新文件与其他涉及执行逻辑的文件通过；`server.py` 中 14 个及原
  hooks integration 测试中的 2 个已有 lint 项保持原状，没有新增诊断。
- `git diff --check`：通过。

重现完整 Core 命令（须从本隔离 worktree 执行）：

```sh
RKA_DATA_DIR=/private/tmp/rka-core-hardening-5OnbMY/test-data \
RKA_EMBEDDINGS_ENABLED=false RKA_LLM_ENABLED=false \
RKA_API_URL=http://127.0.0.1:1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m 'not writer and not agentic'
```

## 未关闭的边界

本次不是整个安全阶段或整份审计的关闭证明。workspace/source/BibTeX-file
等主机文件入口的授权根目录、remote 权限、embedding #158、写入字段、记录
归属/currentness、#141/#153 和 pack 其他完整性问题仍按工作包推进。

静态目录被视为运维控制的只读构建产物；本补丁不宣称抵抗有权持续改写构建
目录的本机进程。跨 Windows/Linux 的真实运行和旧库升级矩阵仍属于发布门，
不能用这次 macOS 测试替代。当前工作不包含 push、merge、release 或部署。
