# 企业级多租户 RAG 知识库实施计划

## 1. 项目定位

构建一个面向企业内部文档的多租户知识库服务。系统支持文档上传、异步解析、增量索引、Hybrid Search、Rerank、ACL 权限过滤、引用溯源、多模型 Provider、流式回答、评测与可观测性。

这个项目的目标不是复刻 RAGFlow 或 Dify，而是用可控的个人项目规模证明：能够把 RAG 接入真实后端系统，并对数据隔离、检索质量、失败恢复、延迟和成本负责。

### 目标岗位信号

- Python/FastAPI 后端服务设计
- PostgreSQL 数据建模、事务与索引
- 异步任务、幂等性和失败恢复
- RAG 检索与生成质量评测
- 多租户、RBAC、ACL 和安全边界
- Docker 化部署、trace、metrics 和日志

### 非目标

- 不实现通用低代码工作流平台。
- 不实现完整 OCR/版面分析模型；优先集成成熟解析器。
- 不用“多 Agent 对话”替代确定性的检索与权限逻辑。
- MVP 不追求 Kubernetes、多区域容灾或互联网规模流量。

## 2. GitHub 调研基线

- [RAGFlow](https://github.com/infiniflow/ragflow)：参考复杂文档解析、多路召回、融合重排、引用与异步任务边界。
- [Haystack](https://github.com/deepset-ai/haystack)：参考模块化检索、路由、生成和评测流水线。
- [pgvector](https://github.com/pgvector/pgvector)：使用 PostgreSQL 内的向量搜索、HNSW、metadata filtering 和租户分区。
- [Ragas](https://github.com/vibrantlabsai/ragas)：参考 RAG 数据集、Context Precision、Faithfulness 和 Answer Relevance。

参考项目只用于理解设计和建立对照，不复制其产品范围或大规模架构。

## 3. 推荐技术栈

- API：Python、FastAPI、Pydantic
- 数据访问：SQLAlchemy 2、Alembic
- 主数据库：PostgreSQL + pgvector
- 缓存/队列：Redis
- 异步任务：Celery 或 Dramatiq，选定一种后保持单一实现
- 对象存储：MinIO（S3 API）
- 解析：PyMuPDF；复杂版面作为后续可选适配器
- 检索：PostgreSQL Full Text Search/BM25 等价实现 + Dense + RRF
- 重排：Cross-Encoder Reranker
- 观测：OpenTelemetry、Prometheus、结构化日志
- 部署：Docker Compose
- UI：Vue 3 最小管理与问答界面，后端完成后再做

项目环境使用 `.mise.toml` 固定 Python 版本，并通过 `_.file = ".env"` 自动加载本地环境变量。密钥只允许使用环境变量或 `op://...` 引用。

## 4. 系统边界

```text
Client
  -> FastAPI / JWT / RBAC
      -> PostgreSQL: users, tenants, ACL, documents, chunks, jobs, traces
      -> MinIO: original documents
      -> Redis: queue, cache, rate limit
      -> Index Worker
           -> parser -> cleaner -> chunker -> embedding -> pgvector
      -> Query Pipeline
           -> query rewrite (optional)
           -> lexical + dense retrieval
           -> tenant/ACL filter
           -> reciprocal rank fusion
           -> reranker
           -> LLM provider
           -> SSE answer + citations
```

### 核心数据实体

- `users`
- `tenants`
- `memberships`
- `knowledge_bases`
- `documents`
- `document_versions`
- `chunks`
- `document_acl`
- `index_jobs`
- `conversations`
- `messages`
- `retrieval_traces`
- `provider_configs`

所有业务表必须明确 `tenant_id` 归属。向量检索不得先跨租户召回再由应用层丢弃结果；过滤条件必须进入检索查询。

## 5. 里程碑

### M1：服务骨架与数据模型

#### 工作内容

- 建立 `apps/api`、`apps/worker`、`packages/core`、`tests` 结构。
- 配置 FastAPI、SQLAlchemy、Alembic、PostgreSQL、Redis、MinIO。
- 完成用户、租户、知识库、文档、ACL 和索引任务的数据模型。
- 实现 JWT 登录和 `owner/admin/member/viewer` 角色。
- 实现 health、readiness、版本信息和数据库迁移。
- 建立结构化日志、request ID 和统一错误响应。

#### 退出条件

- 空数据库能够一条命令完成迁移。
- 两个租户可创建同名知识库且数据完全隔离。
- 未授权用户访问知识库返回稳定的 401/403 响应。
- Docker Compose 启动后 readiness 检查通过。

#### 实施状态（2026-07-13）

**状态：已完成。** M1 初次实施未提前实现上传、解析、索引、检索或生成；开始 M2 前已按
原退出条件重新执行测试、迁移、readiness 和 smoke，确认基线仍然成立。

已实现内容：

- 建立 `apps/api`、`apps/worker`、`packages/core`、三层测试目录、脚本和 ADR 结构。
- 使用 `.mise.toml` 固定 Python 3.12.13，使用 `uv` 管理并锁定项目依赖。
- 提供 PostgreSQL/pgvector、Redis、MinIO、MinIO bucket 初始化和 API 的 Docker Compose。
- 建立 tenants、users、memberships、knowledge_bases、documents、document_acl、index_jobs
  模型和首个 Alembic migration；所有租户业务表显式带 `tenant_id`，跨租户父子关系由复合外键拒绝。
- 实现 JWT 登录、`owner/admin/member/viewer` 角色、租户 membership 校验和租户过滤的知识库
  create/list/get；路由只处理传输，服务处理用例，repository 持有 SQL。
- 实现 liveness、包含 PostgreSQL/Redis/MinIO 实际探测的 readiness、版本信息、JSON 日志、
  request ID 和统一错误响应。

实际验证记录：

| 命令 | 结果 |
|---|---|
| `mise exec -- uv sync` | 通过；58 个包解析，56 个包检查完成 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings |
| `mise exec -- uv run pytest -q` | 通过；18 passed、0 skipped、非 0 tests |
| `mise exec -- uv run pytest -q tests/integration` | 通过；6 passed；使用真实 PostgreSQL、Redis、MinIO |
| `mise exec -- uv run pytest -q tests/security` | 通过；4 passed；覆盖四角色、伪造租户和跨租户访问 |
| `docker compose up -d --build` | 通过；首次创建空 volumes；PostgreSQL、Redis、MinIO、API healthy，bucket init exit 0 |
| `mise exec -- uv run alembic upgrade head` | 通过；空库可升级到 `20260713_0001` |
| `mise exec -- uv run alembic check` | 通过；`No new upgrade operations detected` |
| `curl --fail http://localhost:18000/health/ready` | 通过；database、redis、minio 均为 `ok` |
| `mise exec -- uv run python scripts/smoke_test.py` | 通过；实际完成 readiness、登录、租户内创建和列表查询 |
| `gitleaks detect --source . --no-git --redact --verbose` | 通过；扫描约 356 KB，未发现泄漏 |

退出条件结论：

1. **空数据库迁移：通过。** migration 测试实际执行 `downgrade base` 后 `upgrade head`，并核对全部 8 张表。
2. **同名知识库与隔离：通过。** 两租户可保存同名知识库；同租户重名由唯一约束拒绝；列表和按 ID 查询都在 SQL 中带 `tenant_id`。
3. **稳定 401/403：通过。** 缺少 token 为稳定 401；非成员、伪造 tenant ID 和角色不足为稳定 403；跨租户资源 ID 返回 404 避免泄漏存在性。
4. **Compose readiness：通过。** 首次空 volume 启动后 API 容器为 healthy，readiness 实际探测三个依赖；Redis 不可用的失败路径返回 503。

M1 初次验收时，项目还没有已提交 schema，因此“从上一已提交 schema 升级”没有可用起点；
当时已验证新空库迁移、完整 downgrade/upgrade 和模型/迁移一致性。M2 现已在下节单独实现和
验收；检索、生成和评测仍未提前实现。

### M2：可靠的异步文档索引

#### 工作内容

- 支持 PDF、TXT、Markdown 上传。
- 原文件保存到 MinIO，数据库保存 checksum、版本和状态。
- 上传 API 返回 `202 Accepted` 和 `task_id`。
- 实现解析、清洗、chunk、embedding、入库流水线。
- 使用文件 checksum 和 job idempotency key 防止重复索引。
- 支持 pending/running/succeeded/failed/cancelled 状态。
- 对瞬时错误指数退避重试，对确定性解析错误直接失败。
- 支持文档增量更新、删除、重建索引和孤儿对象清理。

#### 退出条件

- 同一幂等键重复提交 10 次只生成一套 chunk。
- Worker 在解析、embedding、入库三个阶段被强制终止后均可恢复。
- 文档更新只替换对应版本，不重建其他文档。
- 删除文档后对象、chunk、向量和 ACL 无残留。

#### 实施状态（2026-07-13）

**状态：已完成。** 本次只新增 M2，没有实现 M3 的 lexical/dense/hybrid 检索、RRF 或
Rerank，也没有实现 M4/M5 的生成、评测和可观测性范围。

已实现内容：

- 新增 PDF、UTF-8 TXT、Markdown multipart 上传、更新、重建、删除、任务查询和取消 API；
  上传、更新和重建返回 `202 Accepted` 及稳定的 `task_id`、`document_id`、`version_id`。
- 原文件按 tenant/document/version/checksum 组成的不可变 key 保存到 MinIO；PostgreSQL 新增
  `document_versions`、`chunks`、pgvector `vector(16)`、任务 stage/attempt/lease/终态字段，并
  通过复合外键、唯一约束和 partial unique index 约束租户、幂等和唯一 current version。
- 选定 Dramatiq 2.2 + Redis 作为唯一队列实现。任务先提交 PostgreSQL 再投递；worker 启动时
  重投 pending、过期 lease 和旧 schema 中没有 lease 的 running 任务。
- 实现 PyMuPDF/严格 UTF-8 解析、Unicode 清洗、确定性 overlap chunk、embedding provider
  接口、明确命名的 deterministic test/development stub 以及真实 pgvector 写入。该 stub 不冒充
  生产语义 embedding 模型，也不会调用付费服务。
- 使用 PostgreSQL 行锁和时限 lease 认领任务；确定性解析错误直接失败，瞬时错误按有上限的
  指数退避重试，重试耗尽、取消和成功都会同步 job/version/document 终态。
- chunk 替换、current version 切换和任务成功在同一数据库事务中提交；重复投递、重复请求和
  worker 在 parse/embedding/database-write 阶段退出都不会产生重复 chunk。
- 删除先提交 PostgreSQL 的权威级联状态，再清理 MinIO；若进程在对象清理阶段退出，只会留下
  可由 orphan cleanup 扫描删除的对象，不会留下指向已删除对象的有效数据库记录。
- 新增 M1 -> M2 数据迁移测试、跨租户文档/job API 测试、恶意文件名、超大文件、幂等冲突、
  retry exhaustion、取消、重建、孤儿清理和真实子进程 kill/restart 测试。
- 主要设计决定记录在 `docs/adr/0002-reliable-document-indexing.md`。

实际验证记录：

| 命令 | 结果 |
|---|---|
| `mise exec -- uv sync` | 通过；61 个包解析，59 个包检查完成 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings、0 informations |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 47 tests，非 0 tests |
| `mise exec -- uv run pytest -q -ra` | 通过；47 passed、0 skipped；5 条 warning 均来自 PyMuPDF 1.28.0 的 SWIG 类型导入 |
| `mise exec -- uv run pytest -q -ra tests/integration` | 通过；18 passed；真实 PostgreSQL/pgvector、Redis、MinIO |
| `mise exec -- uv run pytest -q -ra tests/security` | 通过；9 passed；包含跨租户文档/job、恶意文件名和 10 MiB 上限 |
| `mise exec -- uv run pytest -q -ra tests/fault` | 通过；3 passed；parse、embedding、database-write 各 kill 一次后恢复，均只有一套 chunk |
| `mise exec -- uv run pytest -q tests/integration/test_migrations.py` | 通过；2 passed；覆盖空库到 head 和 M1 数据升级到 M2 |
| `docker compose up -d --build --wait` | 通过；PostgreSQL、Redis、MinIO、API、Dramatiq worker 全部 healthy |
| `mise exec -- uv run alembic upgrade head` | 通过；当前 schema 为 `20260713_0002` |
| `mise exec -- uv run alembic check` | 通过；`No new upgrade operations detected` |
| `curl --fail --silent http://127.0.0.1:18000/health/ready` | 通过；database、redis、minio 均为 `ok` |
| `mise exec -- uv run python scripts/smoke_test.py` | 通过；真实 HTTP -> Redis/Dramatiq -> worker -> PostgreSQL/pgvector/MinIO 链路完成上传、索引、幂等重交和删除 |
| `mise exec -- uv run python scripts/cleanup_orphans.py --dry-run` | 通过；`scanned=1 orphaned=0 removed=0`，保留数据库引用对象 |
| `gitleaks detect --source . --no-git --redact --verbose` | 通过；扫描约 683 KB，未发现泄漏 |

退出条件结论：

1. **重复幂等键：通过。** 同一上传请求连续提交 10 次得到同一个 task/document/version；数据库
   最终只有 1 个 document、1 个 version、1 套 chunk 和 1 个 MinIO object。不同 payload 复用
   同一 key 稳定返回 409。
2. **三个阶段 Worker 恢复：通过。** fault 测试在真实子进程到达 parse、embedding、
   database-write 后分别 `kill`，等待 lease 到期再启动 worker；三例均为 `succeeded`、2 attempts、
   1 套 chunk，无重复向量。
3. **增量更新隔离：通过。** 更新目标文档后有 2 个不可变 version、仅 1 个 current version；另一
   文档的 chunk ID、内容和 embedding 前后完全相同。重建仅替换 current version 的 chunk set。
4. **删除无残留：通过。** 成功删除后 `documents`、`document_versions`、`chunks`（含 vector）、
   `document_acl`、`index_jobs` 计数均为 0，租户 MinIO prefix 为空。

验证边界与 non-findings：

- 没有测试被 skip，也没有条件性环境 gating；缺失集成环境变量会明确失败，不会静默跳过。
- PyMuPDF 1.28.0 导入时产生 5 条 SWIG `DeprecationWarning`；没有对应功能失败，保留 warning
  可见性而未全局屏蔽。
- 默认 embedding 是明确标记的测试/开发 stub；真实远端或本地语义模型、质量指标属于后续
  provider/检索里程碑，本里程碑未声称验证。
- Redis 短暂中断期间的自动恢复、Provider 429 和跨服务 trace 是 M5 的明确退出条件，本次未
  提前实现或验证；M2 已验证 worker 进程终止与数据库 lease 恢复。

#### M3 开始前复审（2026-07-13）

**结论：M2 全部退出条件仍为 verified，无 failed 或 unverified 项。** 在加入 M3 代码前重新
执行了 47 个 M2 测试、三个 worker kill 恢复用例、迁移检查、真实 Compose smoke 和依赖
readiness。测试收集为非 0，0 skipped；代码中没有 `skip`、`skipif`、`xfail`、`importorskip`
或 collection gate。缺失 `TEST_*` 环境变量会明确失败而不是静默跳过。唯一 non-finding 仍是
PyMuPDF 1.28.0 导入时的 5 条第三方 SWIG `DeprecationWarning`。

复审结果逐项为：同一幂等键 10 次仅 1 套 chunk（verified）；parse、embedding、
database-write 三阶段 kill 后恢复且无重复（verified）；增量更新不改变其他文档（verified）；
删除后 object、chunk/vector、ACL 和 job 均无残留（verified）。因此没有需要在开始 M3 前
修复的 M2 缺口。

### M3：Hybrid Search 与 Rerank

#### 工作内容

- 建立 lexical、dense、hybrid 三种可切换检索器。
- 使用 RRF 融合 lexical 和 dense 排名。
- 在 SQL 查询中应用 tenant、knowledge base 和 ACL 过滤。
- 接入 Cross-Encoder Reranker。
- 保存每次检索的候选、分数、耗时、配置版本。
- 建立至少 200 条带相关文档标注的检索评测集。
- 完成 dense、hybrid、hybrid+rerank 消融实验。

#### 退出条件

- 评测集 Recall@5 不低于 0.85。
- Hybrid 相对 Dense 的 NDCG@10 提升至少 5%；未达到时保存 non-finding 和原因分析。
- Rerank 相对未重排的 MRR@10 提升至少 5%；未达到时不得虚构收益。
- 50 个跨租户检索用例中泄漏为 0。

#### 实施状态（2026-07-13）

**状态：已完成。** 本次只实现 M3；没有实现 M4 的生成、引用、Provider 切换、SSE 或拒答，
也没有提前实现 M5 的 metrics、5 万 chunk 负载测试或跨服务生成 trace。

已实现内容：

- 新增 lexical、dense、hybrid 三种可切换检索模式。lexical 使用 PostgreSQL 生成
  `tsvector` + GIN；dense 使用 pgvector cosine + HNSW；hybrid 使用确定性 RRF 融合。
- lexical 和 dense 两条 SQL 都在排序、limit 前应用 tenant、knowledge base、ready/current
  version 和 ACL predicate。owner/admin 可读取租户知识库全部文档；member/viewer 只读取无
  ACL 的公开文档或显式授予 read/write 的文档，应用层不会先跨租户召回再丢弃。
- lexical 查询将安全分词后的词项交给 `websearch_to_tsquery('simple', ...)` 并使用 OR 组合，
  避免描述性自然问句因默认全词 AND 而把 lexical 召回降为 0。
- 新增 `CrossEncoderReranker` 接口、默认测试用确定性 pair-scoring stub，以及实际用于评测的
  FlashRank `ms-marco-TinyBERT-L-2-v2` Cross-Encoder。
- 新增租户归属的 `retrieval_traces`，保存候选 ID、lexical/dense/fused/rerank 分数、耗时、
  retriever/embedding/reranker/dataset 版本。trace 不保存 chunk 原文；文档删除保留历史 ID/
  分数，知识库或租户删除级联 trace，用户删除将 actor ID 置空。
- 新增 M3 migration：为既有 chunk 回填持久化 `search_vector`，创建 GIN/HNSW 索引及
  retrieval trace 表；测试覆盖空库、M1 -> head、M2 -> M3 既有 chunk/索引迁移。
- 新增固定 `m3-controlled-synthetic-v1` 数据集：50 个受控合成 policy 文档、200 条显式相关
  文档标注；报告记录 SHA-256 并明确标记为非人工生产真值、非 LLM-as-judge。
- 新增真实 PostgreSQL/pgvector 消融脚本，评测 lexical、dense、hybrid、hybrid+rerank，报告
  Recall@5、MRR@10、NDCG@10、p50/p95、配置和数据集版本。固定派生的 resource/chunk UUID
  使连续两次运行的质量指标完全一致。
- 主要设计决定记录在 `docs/adr/0003-hybrid-retrieval-and-evaluation.md`；完整评测报告保存在
  `data/eval/reports/m3-controlled-synthetic-v1.json`。

实际验证记录：

| 命令 | 结果 |
|---|---|
| `mise exec -- uv sync` | 通过；74 个包解析，72 个包检查完成 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings、0 informations |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 57 tests，非 0 tests；无 skip/xfail/importorskip gate |
| `mise exec -- uv run pytest -q -ra` | 通过；57 passed、0 skipped；5 条 warning 均来自 PyMuPDF 1.28.0 的 SWIG 类型导入 |
| `mise exec -- uv run pytest -q -ra tests/integration` | 通过；21 passed；真实 PostgreSQL/pgvector、Redis、MinIO，包含三条 migration 路径和三种检索模式 |
| `mise exec -- uv run pytest -q -ra tests/security` | 通过；11 passed；50 次跨租户 Hybrid 查询及 trace 均为 0 泄漏，并覆盖 ACL-denied 高分文档 |
| `mise exec -- uv run pytest -q -ra tests/fault` | 通过；3 passed；M2 的 parse、embedding、database-write kill 恢复无回归 |
| `docker compose up -d --build --wait` | 通过；PostgreSQL、Redis、MinIO、API、Dramatiq worker 全部 healthy |
| `mise exec -- uv run alembic upgrade head` / `alembic current` | 通过；当前 schema 为 `20260713_0003 (head)` |
| `mise exec -- uv run alembic check` | 通过；`No new upgrade operations detected` |
| `mise exec -- uv run python scripts/smoke_test.py` | 通过；真实 HTTP -> Redis/worker -> pgvector 链路完成上传、索引、幂等重交、Hybrid/Rerank trace 和删除 |
| `curl --fail --silent http://127.0.0.1:18000/health/ready` | 通过；database、redis、minio 均为 `ok` |
| `mise exec -- uv run python scripts/evaluate_retrieval.py --dataset data/eval/retrieval.jsonl` | 通过；200 queries；Hybrid+Rerank Recall@5 0.865；Hybrid vs Dense NDCG@10 +699.49%；Rerank vs Hybrid MRR@10 +4.29%，记录为 non-finding |
| 连续执行两次评测并 diff 去除 latency 的质量结果 | 通过；Recall/MRR/NDCG、comparison status 和 quality gate 完全一致 |
| `gitleaks detect --source . --no-git --redact --verbose` | 通过；扫描约 943 KB，未发现泄漏 |

退出条件结论：

1. **Recall@5：verified。** 固定 200-query 数据集上 Hybrid+Rerank Recall@5 为 0.865，达到
   `>= 0.85` 门槛；对应 MRR@10 0.685323、NDCG@10 0.746053。
2. **Hybrid 相对 Dense：verified。** Dense NDCG@10 为 0.08955，Hybrid 为 0.715943，
   相对提升 699.49%，超过 5% 门槛。
3. **Rerank 相对未重排：non-finding（按退出条件保留）。** Hybrid MRR@10 为 0.657109，
   Hybrid+Rerank 为 0.685323，相对提升 4.29%，没有达到 5%。报告明确保存
   `non_finding_below_threshold`，未把它描述为达到收益门槛。
4. **跨租户检索：verified。** 安全测试为两个租户各写 50 个共享 query token、且未授权租户
   文档词频更高；租户 A 连续执行 50 次 Hybrid 查询，API 结果和落库 trace 候选均只有租户 A
   chunk，泄漏数为 0。另一个 ACL 测试确认 member 看不到同租户内更高分的 denied 文档。

验证边界与剩余风险：

- 该数据集是固定、受控的合成 regression set，以唯一词项为主，不是人工标注的生产查询，
  因此不能外推真实企业语义检索质量；lexical 在该集 Recall@5 为 1.0 也体现了这个偏置。
- dense 仍使用 M2 的确定性 SHA-256 development stub，Dense Recall@5 仅 0.095。真实语义
  embedding 的模型接入、基线重建和人工标注校准仍未验证，不能据此声称生产 Dense 质量。
- FlashRank Cross-Encoder 是实际 learned model，但本数据集上 MRR 相对收益只有 4.29%，属于
  明确保留的 non-finding。后续只能通过更可信数据集或模型选择重新验证，不能改写本报告。
- 评测仅有 50 个文档；记录的 Hybrid+Rerank p50 14.309ms、p95 15.683ms 不是 M5 的
  5 万 chunk/20 并发性能验收，未提前声称负载目标通过。
- 0 tests、skip 和环境 gating 已检查；缺少 `TEST_*` 环境变量会明确报错。仍保留 5 条
  PyMuPDF SWIG 第三方 warning，没有屏蔽。

#### 开始 M4 前复审（2026-07-15）

重新完整读取当前代码、测试、验证记录和 Git 状态后，使用真实 PostgreSQL/pgvector、Redis、
MinIO 与 Docker API/worker 重跑 M3。`pytest --collect-only` 收集 57 tests；全量 57、integration
21、security 11、fault 3 均通过，0 skipped/xfail，且没有环境 gate。迁移仍为
`20260713_0003 (head)` 且 `alembic check` 无漂移；M3 Docker smoke 通过。固定 200-query 评测
再次得到 Recall@5 0.865、Hybrid vs Dense NDCG@10 +699.49%、跨租户 50 次 0 泄漏。
Rerank vs Hybrid MRR@10 仍为 +4.29%，结论保持 **non-finding**，未虚构为达到 5%。因此 M3
全部必需退出条件成立，没有需要先修复的缺口；真实语义 embedding 生产质量仍为 unverified。

### M4：生成、引用和 Provider 抽象

#### 工作内容

- 定义 OpenAI-compatible Provider 接口。
- 支持至少两个远端 Provider 和一个本地 Provider。
- 支持 SSE 流式输出、客户端取消和请求超时。
- Prompt 强制答案引用 chunk ID，并在服务端验证引用存在。
- 返回文档、页码、标题层级和原文片段。
- 无足够证据时拒答。
- 保存 prompt、model、retriever、reranker 和数据集版本。

#### 退出条件

- Provider 切换不修改业务层代码。
- 客户端断开后生成任务可被取消且资源得到释放。
- 有答案样本中引用正确率不低于 90%。
- 无证据问题正确拒答率不低于 90%。

#### 实施状态（2026-07-15）

**状态：已完成。** 本次只新增 M4；没有实现 M5 的 metrics、OpenTelemetry、Provider 429
恢复、5 万 chunk/20 并发负载测试、CI 或运维手册。

已实现内容：

- 新增 provider-neutral `GenerationProvider` 接口；OpenAI-compatible HTTP/SSE payload 和
  delta 类型只存在于 adapter。注册表支持 OpenAI、DeepSeek 两个远端配置与 Ollama 本地配置，
  默认测试/smoke 使用明确标记的无成本 deterministic stub。Provider 名称由 allowlist 选择，
  不能由请求提供任意 base URL；API key 只从环境注入且不落库。
- 新增 `POST /api/v1/knowledge-bases/{id}/answers/stream`。SSE 分别发送 `meta`、`token`、
  服务端认可的 `citation`、`done` 和 `error`；设置 `Cache-Control: no-cache` 并关闭反向代理
  buffering。客户端 disconnect、async task cancellation、显式流关闭和 timeout 都设置取消信号、
  关闭上游 async generator，并将 trace 置为明确的 `cancelled` 或 `failed` 终态。
- Prompt 将授权检索结果封装为不可信 evidence，要求 `[[chunk:<UUID>]]`。流式 parser 可处理
  跨 delta 拆分的 marker，只将当前 tenant/ACL 过滤后 retrieval result 中存在的 chunk 转成
  citation event；未知、格式错误和跨租户 chunk ID 被丢弃，无任何有效引用的生成结果标为失败。
- 无证据策略不会把 deterministic dense stub 的任意近邻当作事实：lexical/hybrid 必须存在
  lexical hit，dense-only 必须达到显式相似度阈值，否则不调用 provider 并返回明确拒答。
- chunk 新增 nullable `page_number` 和 `heading_path`。PDF 索引保留一基页码；Markdown 索引
  保留标题层级。citation 返回 document ID、filename、页码、标题层级和最多 400 字原文片段。
- 新增 tenant-owned `generation_traces`，保存 rendered prompt、answer、验证后的 citations、终态、
  retrieval trace ID、provider/model/provider config、prompt、retriever、embedding、reranker 和
  dataset version。新的复合外键保证 generation trace 不能引用其他 tenant 的 retrieval trace。
- 新增 M4 migration，测试空库、M1 -> head、M2 -> head 和上一 schema M3 -> M4；M3 升级后
  既有 chunk 内容不丢失，新来源字段为 null。
- 新增固定 `m4-controlled-grounding-v1`：20 个受控文档、20 个有答案引用样本和 20 个无证据
  样本。报告保存 SHA-256、版本和配置，并明确它是 synthetic、非人工生产真值、非
  LLM-as-judge。正式报告位于 `data/eval/reports/m4-controlled-grounding-v1.json`。
- 主要设计决定记录在 `docs/adr/0004-grounded-streaming-generation.md`；README、Compose 和
  `.env.example` 已同步 provider、SSE 和运行方式。

实际验证记录：

| 命令 | 结果 |
|---|---|
| `mise exec -- uv sync` | 通过；74 个包解析，72 个包检查完成；HTTPX 0.28.1 已从仅 dev 提升为 runtime 依赖 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings、0 informations |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 73 tests，非 0 tests；无 skip/xfail/importorskip/collection gate |
| `mise exec -- uv run pytest -q -ra` | 通过；73 passed、0 skipped；5 条 warning 仍仅来自 PyMuPDF SWIG 类型导入 |
| `mise exec -- uv run pytest -q -ra tests/integration` | 通过；27 passed；真实 PostgreSQL/pgvector、Redis、MinIO，覆盖 4 条迁移路径、SSE、三 Provider 切换、timeout、disconnect/close |
| `mise exec -- uv run pytest -q -ra tests/security` | 通过；12 passed；既有 50 次跨租户检索 0 泄漏，并验证 prompt injection 无法将跨租户 chunk 变成 citation |
| `mise exec -- uv run pytest -q -ra tests/fault` | 通过；3 passed；parse、embedding、database-write worker kill 恢复无回归 |
| `docker compose up -d --build --wait` | 通过；重新构建 M4 API/worker 后 PostgreSQL、Redis、MinIO、API、worker 均 healthy |
| `mise exec -- uv run alembic upgrade head` / `alembic current` | 通过；当前 schema 为 `20260715_0004 (head)` |
| `mise exec -- uv run alembic check` | 通过；`No new upgrade operations detected` |
| `mise exec -- uv run python scripts/smoke_test.py` | 通过；真实 HTTP -> worker -> pgvector 路径完成上传、索引、Hybrid/Rerank、SSE token、验证 citation 和删除 |
| `mise exec -- uv run python scripts/evaluate_generation.py --dataset data/eval/generation.jsonl` | 通过；20/20 有答案样本 citation 正确，20/20 无证据样本正确拒答，两项均 100% |
| `mise exec -- uv run python scripts/evaluate_retrieval.py --dataset data/eval/retrieval.jsonl --output /tmp/enterprise-rag-m3-post-m4.json` | 通过；M3 Recall@5 仍 0.865，质量指标无回归，Rerank +4.29% 仍保留 non-finding |
| `gitleaks detect --source . --no-git --redact --verbose` | 通过；扫描约 1.17 MB，未发现泄漏 |
| API/worker `docker logs --since 10m` 搜索 error/traceback/exception | 通过；无匹配 |

退出条件结论：

1. **Provider 切换：verified。** 同一个 generation endpoint 和 `GenerationService` 通过 registry
   依次运行两个 remote-shaped stub 和一个 local-shaped stub；三次均成功且业务层无分支改动。
   正式注册表另有 OpenAI、DeepSeek、Ollama 三个 OpenAI-compatible adapter。
2. **断开取消与资源释放：verified。** route 层 disconnect 信号测试确认设置 cancellation event
   并 `aclose()` 上游；真实数据库集成测试在 provider 已开始输出后关闭客户端流，确认 provider
   `finally` 执行、资源释放，且 generation trace 为 `cancelled/client_disconnected`。timeout 测试
   另确认 provider 被取消释放且 trace 为 `failed/generation_timeout`。
3. **引用正确率：verified。** 固定 20 个有答案样本中 20 个均至少产生一个 citation；所有 citation
   的 document/chunk 属于显式 relevant set 且 excerpt 非空，sample-level accuracy 为 1.0，超过
   0.90 门槛。跨租户伪造 ID 不会进入 SSE 或落库 trace。
4. **无证据拒答率：verified。** 固定 20 个无 lexical evidence 样本全部返回 `abstained`、0
   citations 且不进入生成 provider，accuracy 为 1.0，超过 0.90 门槛。

验证边界与剩余风险：

- M4 质量集是固定的受控 synthetic regression set，provider 是 deterministic stub；100% 证明
  服务端 citation/abstention contract，不证明真实 LLM 的回答质量、faithfulness 或 marker 遵循率。
- 未注入付费 OpenAI/DeepSeek 凭据，也未安装或启动 Ollama，因此三个真实外部服务的 live call
  为 **unverified**；其 HTTP/SSE wire contract 使用官方兼容文档和 HTTPX mock 覆盖。缺少远端 key
  会返回明确 `provider_credentials_missing`，不会静默回退或泄漏 secret。
- disconnect 验证覆盖 ASGI disconnect 判定、生成流关闭、数据库终态和 provider resource cleanup，
  但未在不同反向代理/HTTP2 部署上做网络拔线矩阵；代理层异常传播仍是部署风险。
- evidence gate 针对当前 deterministic dense stub 采用保守策略。真实 semantic embedding 接入后必须
  用人工标注集重新校准 dense threshold，不能把当前 0.8 或 synthetic 结果当生产阈值。
- 未实现 Provider 429/500 重试、token/cost metrics、跨服务完整 trace 或负载目标；这些明确属于 M5。

#### M5 开始前复审（2026-07-16）

**结论：M4 的四个必需退出条件均为 verified，没有 failed 项。** 在加入 M5 代码前重新收集并
执行了 73 个测试，结果为 73 passed、0 skipped；另外单独执行 12 个 M4 provider/SSE/citation/
abstention 回归、真实 Compose smoke、20 个有答案和 20 个无证据样本的生成评测，以及 M3
检索质量回归。源码和测试目录没有 `skip`、`skipif`、`xfail`、`importorskip` 或 collection
gate，缺少集成环境变量会明确失败而非静默跳过。

复审逐项结果为：同一接口切换两个 remote-shaped 和一个 local-shaped provider（verified）；
客户端断开后取消上游、关闭 provider 并持久化 `cancelled`（verified）；20/20 有答案样本引用
正确（verified）；20/20 无证据样本明确拒答且不调用 provider（verified）。没有发现需要先修复
的 M4 缺口。真实 OpenAI/DeepSeek/Ollama live call 和不同反向代理的网络拔线行为仍为
**unverified**，但不属于 M4 必需退出条件；5 条 PyMuPDF SWIG `DeprecationWarning` 是可见的
第三方 **non-finding**，没有功能失败或被屏蔽的测试。

### M5：评测、可观测性与交付

#### 工作内容

- 接入独立 `llm-eval-platform` 或提供兼容的评测 API。
- 暴露请求数、错误率、检索耗时、TTFT、生成耗时、token 和成本指标。
- 将检索、重排、Provider 调用串到同一个 trace。
- 对 5 万 chunk 数据集执行负载测试。
- 编写运维手册、故障排查手册、架构决策记录和演示脚本。
- CI 执行 lint、类型检查、单元测试、集成测试和小型质量回归。

#### 退出条件

- 20 并发下检索 API p95 不高于 500ms，不含 LLM 时间。
- Worker 重启、Redis 短暂不可用、Provider 429 均有可验证的恢复行为。
- 任意一次问答可通过 trace 还原检索、重排和生成路径。
- 新机器按 README 能启动并完成上传到问答的 smoke test。

#### 实施状态（2026-07-16）

**状态：已完成。** 本次只实现 M5，并修复了 M5 fresh-stack 验收发现的空库 migration 竞争；
没有扩展为通用评测平台、工作流平台、UI 或其他后续范围。

已实现内容：

- 新增认证且 tenant-scoped 的评测 target API，使用独立 `llm-eval-platform` 通用 HTTP adapter
  所需的 `input`、`output`、`usage`、`metadata` 边界；两个项目不共享源码或数据库。
- 新增 Prometheus 请求/错误、HTTP 与检索延迟、TTFT、生成时长、token、估算成本、终态和
  provider retry 指标。metric label 不包含 tenant、用户、query、document、request 或 trace，
  避免敏感数据和无界 cardinality。
- 新增 OpenTelemetry request/retrieval/rerank/generation/provider span，并在 PostgreSQL trace
  中保存 root trace ID、span ID、request ID、候选/分数、版本、终态、token、usage source、
  cost、attempts 和 citation。tenant-scoped trace API 可在没有外部 collector 时完整还原问答路径；
  配置 OTLP/HTTP endpoint 时可选导出相同 span。
- OpenAI-compatible provider 请求 streamed usage；有 provider usage 时精确记录，否则明确标记
  `estimated`。429 与 5xx 只在尚未接收输出前执行有上限的 `Retry-After`/指数退避；输出开始后
  不重放，避免重复 token。成本只使用部署方提供的版本化费率，默认 0 不冒充实时价格。
- 保持 PostgreSQL 为 job 权威源。队列投递失败时 API 仍提交 `pending`；worker 启动扫描 pending
  和过期 lease 并幂等重投。真实停止 Redis、上传、恢复 Redis、重启 worker 的脚本验证只生成
  一套 chunk。
- 新增确定性的 50,000-chunk MinIO + PostgreSQL/pgvector 负载数据和 20 并发 Hybrid/Rerank
  HTTP gate。API 连接池基线为 20；dense SQL 使用可由 HNSW 满足的纯 cosine-distance 排序；
  压测查询具有受控基数，报告同时保存 client/server p50/p95/p99、版本和 `llm_included=false`。
- 新增 M5 Alembic migration，并覆盖空库以及 M4 既有 retrieval/generation trace 升级与 ID 回填。
- 新增运维手册、故障排查手册、ADR 0005、兼容评测说明、恢复/负载/fresh-stack 演示脚本和
  GitHub Actions CI。CI 包含冻结依赖、migration、lint、严格类型、unit、integration、security
  和小型生成质量回归。
- fresh-stack 首次真实运行发现 API 与 worker 同时迁移空库会竞争创建 `alembic_version`；修复为
  单一有限生命周期 `migrate` 服务，API/worker 等待其成功。第二套全新 Compose project 和
  volumes 从空库启动并完成全链路 smoke。

以下 host 测试命令均通过环境变量注入现有本地 Compose 测试凭据，未打印或写入 secret。

实际验证记录：

| 命令 | 结果 |
|---|---|
| `mise exec -- uv sync --frozen` | 通过；80 个锁定包检查完成 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings、0 informations |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 84 tests，非 0 tests；gate 扫描无 skip/xfail/importorskip/collection gate |
| `mise exec -- uv run pytest -q -ra` | 通过；84 passed、0 skipped；5 条 warning 仅为 PyMuPDF SWIG 导入 warning |
| `mise exec -- uv run pytest -q -ra tests/unit` | 通过；36 passed；含 429、503、部分输出不重试、usage 解析、load 统计边界 |
| `mise exec -- uv run pytest -q -ra tests/integration` | 通过；32 passed；真实 PostgreSQL/pgvector、Redis、MinIO，含 M4 -> M5 migration、trace、metrics、评测 API、timeout 和 dispatch outage |
| `mise exec -- uv run pytest -q -ra tests/security` | 通过；13 passed；既有 50 次跨租户检索 0 泄漏，并验证跨租户 generation trace 返回 404 |
| `mise exec -- uv run pytest -q -ra tests/fault` | 通过；3 passed；parse、embedding、database-write worker kill 恢复无回归 |
| `docker compose up -d --build --wait` | 通过；单一 migrate exit 0；PostgreSQL、Redis、MinIO、API、worker 均 healthy |
| `mise exec -- uv run alembic upgrade head` / `alembic current` / `alembic check` | 通过；`20260716_0005 (head)`；`No new upgrade operations detected` |
| `mise exec -- uv run python scripts/recovery_test.py` | 通过；真实 Redis stop -> upload pending -> Redis start -> worker restart -> succeeded，恰好 1 套 chunk |
| `mise exec -- uv run python scripts/load_test.py --chunks 50000 --concurrency 20 --requests 200 --max-p95-ms 500` | 通过；client p50/p95/p99 = 104.863/132.139/136.451 ms；server p95 = 43.750 ms；无 LLM；报告保存于 `data/eval/reports/m5-load-50000.json` |
| `mise exec -- uv run python scripts/smoke_test.py` | 通过；真实 upload/index -> Hybrid/Rerank -> SSE citation -> trace reconstruction -> evaluation target -> metrics -> delete |
| `mise exec -- uv run python scripts/demo.py --project enterprise-rag-m5-fresh2-20260716 ...` | 通过；全新 volumes 空库迁移，API/worker healthy，完成上述 M5 smoke；隔离栈留存供检查 |
| `mise exec -- uv run python scripts/evaluate_retrieval.py --dataset data/eval/retrieval.jsonl` | 通过；Recall@5 = 0.865；Hybrid NDCG 改善 +699.49%；Rerank MRR +4.29% 继续记录为 threshold 下 non-finding |
| `mise exec -- uv run python scripts/evaluate_generation.py --dataset data/eval/generation.jsonl` | 通过；citation 20/20、abstention 20/20，两项 100% |
| `curl --fail --silent http://127.0.0.1:18000/health/ready` | 通过；database、redis、minio 均为 `ok` |
| CI YAML parse + 本地等价命令 | 通过；workflow 可解析且全部 CI step 的本地命令已执行；未 push，因此 hosted GitHub Actions run 为 unverified |
| `gitleaks detect --source . --no-git --redact --verbose` | 通过；扫描约 1.41 MB，未发现泄漏 |
| API/worker/migrate 恢复测试后的日志错误扫描 | 通过；02:16:30 UTC 后无 error/traceback/exception/critical；此前匹配仅为故意停止 Redis 的预期恢复证据 |

退出条件结论：

1. **20 并发检索性能：verified。** 固定 50,000 chunks、20 并发、200 个 Hybrid/Rerank HTTP
   请求，client p95 为 132.139 ms，低于 500 ms；报告明确排除 LLM 时间。修复前 866.975 ms
   的失败结果没有被忽略，而是通过连接池、HNSW 可用排序和受控查询基数定位并修复后重测。
2. **恢复行为：verified。** worker restart 和真实 Redis 中断由恢复脚本验证，PostgreSQL pending
   job 最终成功且无重复 chunk；429 和 503 由真实 OpenAI-compatible adapter + HTTP mock 验证
   在第二次请求恢复，并另测 output 开始后绝不重放；timeout 会取消 provider 并落库失败终态。
3. **问答 trace：verified。** integration 与真实 smoke 都从 SSE meta 的 generation trace UUID
   读取 tenant-scoped trace，核对 root/request ID、retrieval candidates/scores、rerank span/version、
   provider span/attempt、生成终态、usage/cost、citation 和版本信息。
4. **全新环境 smoke：verified。** 第二个从空 volumes 创建的 Compose project 先由唯一 migrate
   服务升级至 M5，再启动 healthy API/worker，并完成上传到问答、引用、trace、metrics 和删除。

验证边界与剩余风险：

- 真实 OpenAI/DeepSeek/Ollama live call 仍为 **unverified**，未注入付费凭据或安装本地模型；429/
  503、SSE、usage 和 timeout wire behavior 使用明确 HTTP stub 覆盖。
- 可选 OTLP export 未连接真实外部 collector；本地 span ID 持久化和 trace reconstruction 已验证。
- 独立 `llm-eval-platform` 的完整 hosted/evaluator run 未执行；本项目提供并集成测试了其通用 HTTP
  adapter 的兼容 contract，没有引入跨仓库依赖。
- GitHub-hosted CI 未执行，因为按要求没有 commit/push；workflow YAML 和全部本地等价命令已验证。
- 负载结果只代表本机 Docker Compose 的固定 deterministic 数据与当前配置，不外推为生产多机
  SLA；成本默认费率为 0，真实部署必须显式配置并版本化费率。
- PyMuPDF 1.28.0 的 5 条 SWIG `DeprecationWarning` 是保留可见的第三方 **non-finding**；没有
  skip、0 tests 或环境 gating。第一次 fresh-stack 的失败 project/volumes 和最终通过的隔离栈均
  按非破坏原则留存供检查，未删除持久数据。

#### M5 完成后复审（2026-07-16 10:43 CST）

本次先按当前未提交工作树对 M5 做只读复审。审计发现原 fresh-machine 退出条件存在一个真实
缺口：README 声称宿主只需 Git、Docker 和 mise，但 `.mise.toml` 只固定 Python，`mise which uv`
明确失败；既有 demo 实际从宿主 Homebrew PATH 使用 `uv 0.11.28`。因此该退出条件在修复前为
**failed**，而不是 verified。

先新增 `tests/unit/test_project_delivery.py`，在未修改配置时真实得到 `KeyError: 'uv'`；随后在
`.mise.toml` 固定与 Dockerfile 一致的 `uv = "0.11.28"`。`mise install` 后 `mise which uv`
解析到 mise 安装目录，窄测试和全部验证均通过。没有修改 M5 的业务范围，也没有开始不存在于
当前计划中的后续里程碑。

复审后的实际验证记录：

| 命令 | 结果 |
|---|---|
| `mise install` / `mise which uv` / `mise exec -- uv --version` | 通过；项目激活 `uv 0.11.28`，路径位于 mise 安装目录，不再依赖 Homebrew PATH |
| `mise exec -- uv run pytest -q -ra tests/unit/test_project_delivery.py` | 修复前按预期 1 failed（缺少 `tools.uv`）；修复后 1 passed |
| `mise exec -- uv sync --frozen` | 通过；80 个锁定包检查完成 |
| `mise exec -- uv run ruff check .` | 通过；无 lint 错误 |
| `mise exec -- uv run pyright` | 通过；0 errors、0 warnings、0 informations |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 85 tests，非 0 tests |
| `mise exec -- uv run pytest -q -ra tests/unit` | 通过；37 passed、0 skipped |
| `mise exec -- uv run pytest -q -ra tests/integration` | 通过；32 passed、0 skipped；真实 PostgreSQL/pgvector、Redis、MinIO |
| `mise exec -- uv run pytest -q -ra tests/security` | 通过；13 passed、0 skipped |
| `mise exec -- uv run pytest -q -ra tests/fault` | 通过；3 passed、0 skipped；三种 worker kill stage 均恢复且无重复 chunk |
| `mise exec -- uv run pytest -q -ra` | 通过；85 passed、0 skipped；5 条 warning 仍仅为 PyMuPDF SWIG 导入 warning |
| pytest skip/gate 源码扫描 | 通过；无 skip、skipif、xfail、importorskip 或 collect-ignore；主动移除 `TEST_*` 后测试 exit 1 并明确报 `TEST_DATABASE_URL is required`，未静默跳过 |
| `docker compose up -d --build --wait` | 通过；migrate exit 0，PostgreSQL、Redis、MinIO、API、worker healthy |
| `mise exec -- uv run alembic upgrade head` / `current` / `check` | 通过；`20260716_0005 (head)`；`No new upgrade operations detected` |
| `mise exec -- uv run python scripts/smoke_test.py` | 通过；上传/索引、幂等、Hybrid/Rerank、SSE citation、trace reconstruction、评测 target、metrics、删除 |
| `mise exec -- uv run python scripts/recovery_test.py` | 通过；真实 Redis stop -> pending upload -> Redis restore -> worker restart -> succeeded，恰好一套 chunk |
| `mise exec -- uv run python scripts/load_test.py --chunks 50000 --concurrency 20 --requests 200 --max-p95-ms 500` | 通过；client p50/p95/p99 = 106.349/156.325/168.721 ms；server p95 = 73.597 ms；`llm_included=false` |
| `mise exec -- uv run python scripts/demo.py --project enterprise-rag-m5-reaudit-20260716 --postgres-port 55432 --redis-port 56380 --minio-port 59000 --minio-console-port 59001 --api-port 58000` | 通过；新 project、新 volumes、空库 migration，完整 M5 smoke；隔离栈留存供检查 |
| `mise exec -- uv run python scripts/evaluate_retrieval.py --dataset data/eval/retrieval.jsonl` | 通过；Recall@5 = 0.865；Rerank MRR +4.29% 仍为 threshold 下 **non-finding** |
| `mise exec -- uv run python scripts/evaluate_generation.py --dataset data/eval/generation.jsonl` | 通过；citation 20/20、abstention 20/20 |
| CI YAML parse / `gitleaks detect --source . --no-git --redact --verbose` | 通过；workflow 可解析；扫描约 1.43 MB 无泄漏 |
| 主栈与 fresh-stack 日志检查 | 恢复窗口仅出现预期 Redis dispatch/consumer error；恢复完成后无 error/traceback/exception/critical |

M5 退出条件复审结论：

1. **20 并发检索性能：verified。** 50,000 chunks、20 并发、200 个 Hybrid/Rerank HTTP 请求
   的 client p95 为 156.325 ms，低于 500 ms，且报告明确不包含 LLM。
2. **恢复行为：verified。** 真实 worker/Redis 恢复脚本、429/503 HTTP stub、三阶段 worker kill
   和 timeout 路径均执行；恢复后无重复 chunk，流已输出后不重试。
3. **问答 trace：verified。** integration 与两个真实 smoke 均从问答返回的 trace ID 还原
   retrieval candidates/scores、rerank、provider/generation、usage/cost、citation、终态和版本。
4. **全新环境 smoke：verified。** 修复 uv 固定后，新 Compose project 从空 volumes 启动，
   唯一 migrate 服务成功退出，并完成上传到问答的全链路 smoke。

复审边界：真实付费 OpenAI/DeepSeek 和本地 Ollama live call、外部 OTLP collector、独立
`llm-eval-platform` 的完整 hosted evaluator run、GitHub-hosted CI 仍为 **unverified**，原因分别是
未提供凭据/模型/collector、两个项目保持独立、以及当前仓库尚无 commit/push。这些均不是 M5
退出条件。PyMuPDF 的 5 条 SWIG warning 和 Rerank MRR 未达到 5% 改善阈值继续记录为
**non-finding**，没有被包装成通过项。

#### M5 第二次完成后复审（2026-07-16 12:18 CST）

本轮从当前未提交工作树重新完整审计 M5。仓库尚无任何 commit，全部 103 个项目文件均为
untracked，因此普通 `git diff` 与 cached diff 为空只表示没有可比较的 Git 基线，不表示工作树
无内容。`PLAN.md` 只定义 M1--M5，M5 是最后一个里程碑；本轮未发现新的必需退出条件缺口，
也没有自行创建或提前实施后续里程碑。

本轮实际验证记录：

| 命令 | 结果 |
|---|---|
| `mise exec -- uv lock --check` / `uv tree --locked --depth 1` | 通过；锁文件可解析，项目使用 mise 提供的 `uv 0.11.28` |
| `mise exec -- uv sync --frozen` / `ruff check .` / `pyright` | 通过；80 个锁定包，lint 无错误，类型检查 0 errors/0 warnings |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 85 tests，非 0 tests |
| `mise exec -- uv run pytest -q -ra tests/unit` | 通过；37 passed、0 skipped |
| `mise exec -- uv run pytest -q -ra tests/integration` | 通过；32 passed、0 skipped；真实 PostgreSQL/pgvector、Redis、MinIO |
| `mise exec -- uv run pytest -q -ra tests/security` | 通过；13 passed、0 skipped |
| `mise exec -- uv run pytest -q -ra tests/fault` | 通过；3 passed、0 skipped；parse、embedding、database-write kill 均恢复且无重复 chunk |
| `mise exec -- uv run pytest -q -ra` | 通过；85 passed、0 skipped；仅 5 条 PyMuPDF SWIG warning |
| pytest skip/gate 源码扫描与主动移除 `TEST_*` | 无 skip/skipif/xfail/importorskip/collect-ignore；缺少集成环境时 exit 1 并明确报告 `TEST_DATABASE_URL is required`，未静默跳过 |
| `docker compose up -d --build --wait` | 通过；主栈 PostgreSQL、Redis、MinIO、API、worker healthy，migrate exit 0 |
| `alembic upgrade head` / `current` / `check` | 通过；`20260716_0005 (head)`；无待生成 migration |
| `python scripts/smoke_test.py` | 通过；真实上传/索引、幂等、Hybrid/Rerank、SSE citation、trace、评测 target、metrics、删除 |
| `python scripts/recovery_test.py` | 通过；真实 Redis stop/restore 与 worker restart 后任务成功，恰好一套 chunk |
| `python scripts/load_test.py --chunks 50000 --concurrency 20 --requests 200 --max-p95-ms 500` | 通过；client p50/p95/p99 = 111.945/204.968/222.010 ms，server p95 = 39.803 ms，`llm_included=false` |
| `python scripts/demo.py --project enterprise-rag-m5-reaudit2-20260716 ...` | 通过；全新 project、volumes 和空库完成 migration 与完整 M5 smoke；隔离栈留存供检查 |
| `python scripts/evaluate_retrieval.py --dataset data/eval/retrieval.jsonl` | 通过；200 条查询，Recall@5 = 0.865；Rerank MRR +4.29% 仍为阈值下 non-finding |
| `python scripts/evaluate_generation.py --dataset data/eval/generation.jsonl` | 通过；citation 20/20、abstention 20/20 |
| CI YAML parse / `gitleaks detect --source . --no-git --redact --verbose` | 通过；workflow 可解析，扫描约 1.43 MB 无泄漏 |
| 主栈与本轮 fresh-stack 终态/日志检查 | PostgreSQL、Redis、MinIO、API、worker healthy，migrate exit 0；检查窗口无 error/traceback/exception/critical |

M5 退出条件第二次复审结论：

1. **20 并发检索性能：verified。** 固定 50,000 chunks、20 并发、200 个 Hybrid/Rerank HTTP
   请求的 client p95 为 204.968 ms，低于 500 ms，且报告明确不含 LLM 时间。
2. **恢复行为：verified。** 真实 Redis 中断与 worker restart、三阶段 worker kill、429/503 HTTP
   stub、timeout 与流开始后不重试路径均覆盖；恢复后没有重复 chunk。
3. **问答 trace：verified。** integration 和两个真实 smoke 可按问答返回的 trace ID 还原 retrieval、
   rerank、provider/generation、usage/cost、citation、终态和版本。
4. **全新环境 smoke：verified。** 新 Compose project 从空 volumes 启动，唯一 migrate 服务 exit 0，
   随后完成上传到问答的完整 smoke。

复审边界与非发现保持不变：真实付费 OpenAI/DeepSeek、本地 Ollama、外部 OTLP collector、独立
`llm-eval-platform` hosted evaluator 和 GitHub-hosted CI 为 **unverified**，但不属于 M5 必需退出
条件；PyMuPDF 5 条第三方 warning 与 Rerank MRR 未达到 5% 改善阈值为 **non-finding**。

#### M5 第三次完成后复审（2026-07-16 12:38 CST）

本轮再次从当前工作树逐文件读取并复验 M5。仓库仍无 `HEAD`，103 个项目文件全部为
untracked；因此空的 `git diff`/cached diff 仍然不是“无变更”证据。`PLAN.md` 仅定义
M1--M5，本轮未发现 M5 必需退出条件缺口，也没有创建或实施未批准的后续里程碑。

本轮实际验证记录：

| 命令 | 结果 |
|---|---|
| `mise exec -- uv lock --check` / `uv tree --locked --depth 1` / `uv sync --frozen` | 通过；项目使用 mise 固定的 Python 3.12.13 和 `uv 0.11.28`，80 个锁定包检查完成 |
| `mise exec -- uv run ruff check .` / `mise exec -- uv run pyright` | 通过；lint 无错误，类型检查 0 errors、0 warnings、0 informations |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 85 tests，非 0 tests |
| `pytest -q -ra tests/unit` / `tests/integration` / `tests/security` / `tests/fault` | 通过；分别 37/32/13/3 passed，均 0 skipped；后三组使用真实 PostgreSQL/pgvector、Redis 和 MinIO |
| `mise exec -- uv run pytest -q -ra` | 通过；85 passed、0 skipped；5 条 warning 均为 PyMuPDF SWIG 导入 warning |
| pytest skip/gate 扫描与主动移除 `TEST_*` | 无 skip/skipif/xfail/importorskip/collect-ignore；缺少集成环境时 exit 1 并明确报告 `TEST_DATABASE_URL is required`，未静默跳过 |
| `docker compose up -d --build --wait` | 通过；镜像使用固定的 Python/uv 重建，migrate 和 MinIO init exit 0，五个长运行服务 healthy |
| `alembic upgrade head` / `current` / `check` | 通过；`20260716_0005 (head)`，`No new upgrade operations detected` |
| `python scripts/smoke_test.py` | 主栈与本轮全新空卷栈均通过；覆盖上传/索引、幂等、Hybrid/Rerank、SSE citation、trace 重建、评测 target、metrics 和删除 |
| `python scripts/recovery_test.py --project enterprise-rag --api-url http://127.0.0.1:18000` | 首次因审计 shell 未注入主机侧 `REDIS_URL`/`MINIO_ENDPOINT` 而在故障注入前硬失败；补齐必需环境后通过，真实 Redis 中断与 worker restart 后任务成功且恰好一套 chunk |
| `pytest -q 'tests/unit/test_generation_core.py::test_openai_compatible_adapter_recovers_from_retriable_pre_output_status[429]'` | 通过；明确标记的 OpenAI-compatible HTTP stub 覆盖 provider 429 恢复 |
| `pytest -q tests/integration/test_generation.py::test_answer_trace_reconstructs_retrieval_rerank_provider_and_usage` | 通过；可还原 retrieval/rerank/provider/generation/usage/cost/citation/终态与版本 |
| `python scripts/load_test.py --chunks 50000 --concurrency 20 --requests 200 --max-p95-ms 500 ...` | 通过；client p50/p95/p99 = 133.269/167.056/171.517 ms，server p95 = 70.284 ms，`llm_included=false` |
| `docker compose -p enterprise-rag-m5-reaudit3-20260716 up -d --build --wait` + fresh-stack smoke | 通过；新 project、新 volumes、空库 migration 后完成从上传到带授权引用问答的全链路；隔离栈留存供检查 |
| `evaluate_retrieval.py` / `evaluate_generation.py` | 通过；Recall@5 = 0.865，citation 20/20，abstention 20/20；Rerank MRR +4.29% 仍为阈值下 non-finding |
| `gitleaks dir` 逐个扫描 103 个项目文件 / CI YAML 与 JSON(L) parse | 通过；0 secret findings，workflow、数据集和报告均可解析 |
| 主栈与 fresh-stack readiness/日志终态 | 通过；两栈 database/redis/minio 均 `ok`，fresh-stack 五个服务无 error/traceback/panic/fatal 标记 |

M5 退出条件第三次复审结论：

1. **20 并发检索性能：verified。** 50,000 chunks、20 并发、200 个 Hybrid/Rerank HTTP
   请求的 client p95 为 167.056 ms，低于 500 ms，报告明确不包含 LLM 时间。
2. **恢复行为：verified。** 真实 Redis 中断、worker restart、parse/embedding/database-write
   三阶段 kill、provider 429/503、timeout 与流已开始后不重试路径均有执行证据，无重复 chunk。
3. **问答 trace：verified。** integration、主栈 smoke 和全新栈 smoke 均可按 trace ID
   还原 retrieval、rerank、generation/provider、usage/cost、citation、终态和版本。
4. **全新环境 smoke：verified。** 新 Compose project 从空 volumes 完成迁移与依赖初始化，
   五个服务 healthy，并走完上传、索引、检索、带引用回答和删除。

本轮必需退出条件没有 **failed** 或 **unverified** 项。仍为 **unverified** 的非必需
外部路径是真实付费 OpenAI/DeepSeek、本地 Ollama、外部 OTLP collector、独立
`llm-eval-platform` hosted evaluator 和 GitHub-hosted CI；原因为未提供凭据/模型/collector、
外部评测项目保持独立，且当前仓库没有 commit/push。PyMuPDF 的 5 条第三方
warning 与 Rerank MRR 未达 5% 改善阈值继续记录为 **non-finding**。

#### GitHub 发布前总体验收（2026-07-16 22:57 CST）

本轮从当前工作树重新审计 M1--M5 的实现、测试、依赖、交付配置和既有验证记录。
仓库仍无 `HEAD` 和 remote，103 个项目文件全部为 untracked；因此空的 `git diff` 不是
“无变更”证据。本轮没有发现需要修改的功能缺口，只刷新评测报告并记录以下发布前证据。

| 命令 | 结果 |
|---|---|
| `mise exec -- uv lock --check` / `mise exec -- uv sync --frozen` | 通过；锁文件解析 82 个包，检查 80 个已安装包 |
| `mise exec -- uv run ruff check .` / `mise exec -- uv run pyright` | 通过；lint 无错误，类型检查 0 errors、0 warnings、0 informations |
| `mise exec -- uv run pytest --collect-only -q` | 通过；收集 85 tests，非 0 tests |
| `pytest -q -ra tests/unit` / `tests/integration` / `tests/security` / `tests/fault` | 通过；分别 37/32/13/3 passed、0 skipped；后三组连接真实 PostgreSQL/pgvector、Redis 和 MinIO |
| 移除全部 `TEST_*` 后运行集成测试 | 按预期 exit 1 并明确报告 `TEST_DATABASE_URL is required`；没有环境 gating 后静默 skip |
| `mise exec -- uv run pytest -q -ra` | 一轮在此前长测试会话被中断后出现 parse-stage 信号等待超时；该用例随后连续 5 次通过，两次独立全量复跑均为 85 passed、0 skipped；5 条 warning 均来自 PyMuPDF SWIG 导入 |
| `docker compose up -d --build --wait` | 通过；migrate/MinIO init exit 0，PostgreSQL、Redis、MinIO、API、worker healthy |
| `alembic upgrade head` / `current` / `check` | 通过；`20260716_0005 (head)`，没有待生成迁移 |
| `python scripts/smoke_test.py` | 通过；覆盖 readiness、鉴权、知识库、上传/索引、幂等重投、Hybrid/Rerank、SSE 引用、trace 重建、评测 target、metrics 和删除 |
| `evaluate_retrieval.py` / `evaluate_generation.py` | 通过；Recall@5 = 0.865，citation = 20/20，abstention = 20/20；Rerank MRR +4.29% 保持阈值下 non-finding |
| `python scripts/recovery_test.py --project enterprise-rag --api-url http://127.0.0.1:18000` | 通过；真实 Redis 中断和 worker restart 后任务恢复成功且恰好一套 chunk |
| `python scripts/load_test.py --chunks 50000 --concurrency 20 --requests 200 --max-p95-ms 500 ...` | 通过；client p50/p95/p99 = 106.783/200.356/249.849 ms，server p95 = 38.091 ms，`llm_included=false` |
| `docker compose -p enterprise-rag-release-20260716 ...` + fresh-stack smoke | 通过；新 project、新 volumes、空库迁移后完成上传到授权引用问答的全链路 |
| `gitleaks dir` 逐文件扫描 / CI YAML 与 JSON(L) parse | 通过；103 个项目文件 0 secret findings，workflow、数据集和报告均可解析 |

总体验收逐项结论：

1. **文档：verified。** PDF、TXT、Markdown 的上传、索引、版本更新、删除和重建均由单元、
   集成与 smoke 路径覆盖。
2. **幂等：verified。** 重复提交和 worker 重试不会产生重复 object、chunk 或 embedding；
   数据库约束与恢复测试共同验证。
3. **隔离：verified。** API、数据库检索条件、ACL 和对象键均带显式 tenant 边界；13 个
   security tests（含 50 组跨租户样本）全部通过。
4. **检索：verified。** 固定 200 条评测集 Recall@5 = 0.865，高于 0.85 门槛。
5. **引用：verified。** 20 个有答案样本引用正确率 100%，且引用只能映射到本次授权检索集。
6. **拒答：verified。** 20 个无证据样本拒答率 100%。
7. **性能：verified。** 50,000 chunks、20 并发、200 请求的 client p95 = 200.356 ms，
   低于 500 ms，且报告明确不包含 LLM 时间。
8. **恢复：verified。** parse、embedding、database-write 阶段终止以及真实 Redis/worker
   中断后均可恢复，无重复数据。
9. **观测：verified。** 日志、metrics 和持久 trace 可通过 request/run/trace ID 关联并重建
   retrieval、rerank、provider、usage/cost、citation、终态和配置版本。
10. **部署：verified。** Docker Compose 已从全新 volumes 启动完整依赖并通过全链路 smoke。

M1--M5 的全部必需退出条件均为 **verified**，没有 **failed** 或 **unverified** 项。
真实付费 OpenAI/DeepSeek、本地 Ollama、外部 OTLP collector、独立 `llm-eval-platform`
hosted evaluator 和 GitHub-hosted CI 仍为非门禁的 **unverified** 外部路径，原因分别是未提供
凭据/模型/collector、外部评测项目保持独立，以及尚未 push。PyMuPDF 5 条第三方 warning
和 Rerank MRR 未达到 5% 改善阈值为 **non-finding**。

## 6. 总体验收标准

| 类别 | 验收标准 |
|---|---|
| 文档 | PDF、TXT、Markdown 上传、索引、更新、删除、重建均可用 |
| 幂等 | 同一任务重复提交不会产生重复 chunk 或向量 |
| 隔离 | API、数据库查询、检索和对象存储均通过跨租户测试 |
| 检索 | 200 条固定评测集 Recall@5 >= 0.85 |
| 引用 | 有答案样本引用正确率 >= 90% |
| 拒答 | 无证据问题正确拒答率 >= 90% |
| 性能 | 5 万 chunk、20 并发时检索 p95 <= 500ms |
| 恢复 | 任务各阶段终止后可恢复且无重复数据 |
| 观测 | 日志、metrics、trace 可通过 request/run ID 关联 |
| 部署 | Docker Compose 可从空环境启动完整依赖 |

## 7. 计划验收命令

命令在对应工具真正加入项目后才能视为有效；不得在项目尚未实现时声称通过。

```bash
mise exec -- uv sync
mise exec -- uv run ruff check .
mise exec -- uv run pyright
mise exec -- uv run pytest -q
docker compose up -d --build
mise exec -- uv run alembic upgrade head
mise exec -- uv run alembic check
mise exec -- uv run pytest -q tests/integration
mise exec -- uv run pytest -q tests/security
mise exec -- uv run python scripts/smoke_test.py
mise exec -- uv run python scripts/evaluate_retrieval.py --dataset data/eval/retrieval.jsonl
mise exec -- uv run python scripts/evaluate_generation.py --dataset data/eval/generation.jsonl
mise exec -- uv run pytest -q tests/fault
mise exec -- uv run python scripts/load_test.py --chunks 50000 --concurrency 20
```

## 8. 测试矩阵

| 层级 | 必测内容 |
|---|---|
| 单元 | chunk、RRF、ACL predicate、引用校验、状态迁移 |
| 数据库 | migration、事务回滚、唯一约束、租户过滤、HNSW 查询 |
| 集成 | MinIO、Redis、PostgreSQL、Worker、Provider stub |
| 安全 | 越权、伪造 tenant ID、恶意文件名、超大文件、提示注入 |
| 故障 | Worker kill、Redis 中断、Provider 429/500/timeout |
| 质量 | Dense/Hybrid/Rerank 消融、引用、拒答 |
| 性能 | 索引吞吐、检索 p50/p95/p99、20 并发 |
| E2E | 登录、上传、等待索引、提问、打开引用、删除文档 |

## 9. 主要风险

- **评测数据不足**：先手工制作小而可信的数据集，再扩充，不用 LLM 自动生成数据冒充真值。
- **范围膨胀**：MVP 只支持三类文档和一种权限模型。
- **过滤后召回下降**：记录 filtered recall，并调整 HNSW 参数或租户分区。
- **LLM-as-judge 偏差**：保留人工标注集并报告分歧。
- **Provider 成本失控**：默认缓存、限流、预算上限和本地 stub。
- **解析器不稳定**：解析器做适配层，原始文件和错误页码可追溯。

## 10. 工具安装记录

没有安装 Homebrew/global package 或系统服务。Python 与 uv 由 mise 项目配置固定，Python 依赖
由 `uv.lock` 固定。

| 时间 | 工具 | 安装命令 | 原因 | 卸载命令 |
|---|---|---|---|---|
| 2026-07-13 17:13 CST | M1 运行依赖（FastAPI、SQLAlchemy、Alembic、psycopg、Redis、MinIO、JWT 等） | `mise exec -- uv sync` | API、持久化、认证和依赖健康检查 | `mise exec -- uv remove fastapi uvicorn sqlalchemy alembic psycopg pydantic-settings pyjwt pwdlib redis minio python-multipart email-validator greenlet` |
| 2026-07-13 17:13 CST | M1 开发依赖（pytest、pytest-asyncio、HTTPX、ruff、pyright 等） | `mise exec -- uv sync` | 单元、集成、安全、lint 和类型验证 | `mise exec -- uv remove --dev pytest pytest-asyncio httpx asgi-lifespan ruff pyright` |
| 2026-07-13 18:22 CST | M2 项目依赖（Dramatiq Redis broker、pgvector Python 类型、PyMuPDF） | `mise exec -- uv add 'dramatiq[redis]>=2.2,<3' 'pgvector>=0.4,<1' 'pymupdf>=1.26,<2'` | 异步任务投递、向量列映射和 PDF 解析 | `mise exec -- uv remove dramatiq pgvector pymupdf` |
| 2026-07-13 19:27 CST | M3 项目依赖（FlashRank 及锁定的 ONNX runtime/tokenizer 依赖） | `mise exec -- uv add 'flashrank>=0.2,<1'` | 本地 Cross-Encoder Rerank 与固定检索消融 | `mise exec -- uv remove flashrank` |
| 2026-07-15 23:15 CST | M4 项目依赖（HTTPX，从 dev-only 提升为 runtime） | `mise exec -- uv add 'httpx>=0.28,<1'`，随后 `mise exec -- uv remove --dev httpx` 去除重复声明 | OpenAI-compatible async HTTP/SSE、timeout 与资源关闭 | `mise exec -- uv remove httpx` |
| 2026-07-16 09:45 CST | M5 项目依赖（Prometheus client、OpenTelemetry API/SDK、OTLP/HTTP exporter） | `mise exec -- uv add 'prometheus-client>=0.22,<1' 'opentelemetry-api>=1.37,<2' 'opentelemetry-sdk>=1.37,<2' 'opentelemetry-exporter-otlp-proto-http>=1.37,<2'` | 低 cardinality metrics、跨阶段 trace 与可选 OTLP export | `mise exec -- uv remove prometheus-client opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http` |
| 2026-07-16 10:36 CST | uv 0.11.28（mise 项目工具） | 在 `.mise.toml` 固定版本后执行 `mise install` | 使 README fresh-machine 路径只依赖已声明的 Git、Docker、mise，并与 Dockerfile 版本一致 | `mise uninstall uv@0.11.28` |
