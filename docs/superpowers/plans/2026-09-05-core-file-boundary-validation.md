# Core hardening 第二批：文件权限边界

- 日期：2026-09-05 开始；2026-09-06 完成最终本机验收
- 基线：首批修复 `decc44f0cfb9adc83faf5c34a28ed2cd81fbbc88`
- 分支：`codex/core-audit-hardening`
- 工作目录：`/private/tmp/rka-core-hardening-5OnbMY/source`
- 状态：实现、聚焦测试及全量 Core 本机回归完成；原生跨平台 CI 待运行；未推送、合并、发布或部署。
- 范围：审计 H-3/H-5 对应 S2 的输入路径边界；不是整个安全审计关闭证明。
- 设计/配置：[FILE_ACCESS.md](../../FILE_ACCESS.md)、[总体设计](../specs/2026-09-05-core-hardening-design.md)。

## 实现边界

1. 共享 `FileAccessPolicy`，主机与服务器的授权根目录独立、默认关闭。
   REST payload、project_dir、actor、manifest 均不能扩大操作者权限。
2. 路径逐层打开，拒绝 symlink/reparse、目录冒充文件、FIFO、UNC/device、
   drive-relative 和跨平台歧义路径。解析器读取私有有界副本；记录保留原路径。
3. workspace 流式遍历并剪枝；数量、字节、深度、协作式时间与并发预算明确。
   不能先物化整个目录树；超限计数明确为 partial，跳过文件有警告。
4. 主机 bootstrap 只允许服务器返回本地已扫描文件的原始路径及描述符。
   所有 manifest 路径在批量写入前预检；后续文件变化单独失败，保留先前成功 ID。
5. Source/Artifact 服务、学术文件入口、REST、typed/legacy MCP 和本地
   `rka bootstrap` CLI 共享策略；CLI 使用 server roots（本地直连服务路径），
   权限失败不会打开数据库，扫描异常会可靠关闭连接。
   显式上传与内容传输在服务器 path 关闭时仍能正常工作。
6. 安装文档、操作发现、ADR 0016 addendum、changelog、REST contract 的输入
   上限同步。CI 加入 Linux/macOS/Windows × Python 3.11/3.13 原生边界测试。

## 隔离与验收

沿用首批一次性 worktree/venv；pytest 的数据库均是合成 fixture，另设
`RKA_DATA_DIR`、关闭 embeddings/LLM、启用 `HF_HUB_OFFLINE=1`；非 mock MCP
目标固定为 loopback 端口 1。没有连接生产 API、LM Studio、Docker 卷或下载模型。

- 新增负例在实现前确认路径入口缺少默认拒绝/目录授权；补丁后对应检查通过。
- 聚焦回归：**182 passed, 1 skipped**，包括原 workspace、MCP navigator/verb
  测试与新增权限/快照/路径替换/伪造 manifest/大小/遍历/并发/正常导入回读。
  跳过项是必须在原生 Windows 执行的 junction/handle sharing 测试。
- 第一轮完整 Core：115 failed、3206 passed、294 deselected。114 项由新 API
  测试在 collection 时过早 import MCP server、抢先固定旧兼容工具模式造成；
  改为运行时 fixture import 后对应聚焦测试全部通过。另一项是有意新增的
  REST 长度/数量限制，已审阅并生成 contract snapshot（仅 7 行新增约束）。
- 第二轮完整 Core：**3327 passed, 1 skipped, 294 deselected, 5 subtests passed**，
  233.62 秒；JUnit 为 `file-boundary-core-final.xml`。随后发现 CLI 服务构造
  需要显式传递政策，补上 6 项 CLI 回归，全部通过；包括真实导入 SQLite 回读，
  默认拒绝、不跨用 host 权限和异常时关闭数据库。
- 包含 CLI 补充的最终全量：**3333 passed, 1 skipped, 294 deselected,
  5 subtests passed**，234.83 秒；JUnit 保存在隔离目录
  `file-boundary-core-cli-final.xml`。294 项为 Core gate 明确排除的 Writer/Agentic
  测试；5 项 warning 为既有 PyMuPDF/SWIG 弃用提示。
- `core_startup_smoke.py --require-web --require-vec`：**通过**。初次沙箱不允许
  socket bind；经权限审核后用随机 loopback 端口重跑，通过 REST/MCP/worker、
  migration、Phase-2 文件锁、sqlite-vec 及现有构建的网页资源。结束关闭子进程。
- Ruff：新增文件及涉及执行逻辑的修改文件通过；`server.py` 为 10 项既有诊断，
  对比基线 14 项没有新增；不为本批修改无关 legacy 代码。
- 公共 REST/MCP contract snapshots 只读检查：通过。
- `git diff --check`：通过。

复跑全量 Core（从此隔离目录执行）：

```sh
RKA_DATA_DIR=/private/tmp/rka-core-hardening-5OnbMY/test-data \
RKA_EMBEDDINGS_ENABLED=false RKA_LLM_ENABLED=false \
RKA_API_URL=http://127.0.0.1:1 HF_HUB_OFFLINE=1 \
.venv/bin/python -m pytest -q --tb=short --strict-markers \
  -m 'not writer and not agentic'
```

## 兼容性与未关闭项

升级后，以路径为输入的调用必须由操作者显式配置 roots，并重启相应 API/MCP
进程。不会自动授权用户目录或改变运行配置；不需要 schema/data migration。
批量导入本来就是逐文件结果，不宣称全局原子性。扫描 hash 保留既有大文件
采样配方，不把它声称为逐字节不变证明。

本机为 macOS/Python 3.13；Windows/Linux 原生 CI 尚未运行。不能把新加了
workflow 当作通过证据，也不能用本机 smoke 替代旧库升级/发布矩阵。
输入根目录不是多租户 ACL 或操作系统沙箱；压缩文档展开内存、解析器 CPU、
全部 HTTP body 缓冲、受阻文件系统调用和恶意本机操作者仍在此批验证范围外。

按已批准设计，下一阶段是 I1 BibTeX parser/actor 与 I2 MCP 字段持久化。
E1/E2 的 embedding 预算和恢复、记录生命周期/归属、remote boundary 及 pack
完整性仍分别推进；本批没有夹带修复或改变生产运行状态。
