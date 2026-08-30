# PPTExtract

PPTExtract 的初版单机产品脊柱：FastAPI、React 文档入口、一个持久任务 worker、SQLite 与本地 SHA-256 内容寻址对象目录。

## 开发启动

```bash
uv sync --dev
cd web && npm install && npx --no-install playwright install chromium && cd ..
uv run python scripts/dev.py
```

打开 `http://127.0.0.1:5173/documents`。开发脚本同时启动：

- API：`http://127.0.0.1:8000`
- React/Vite：`http://127.0.0.1:5173`
- 单 worker：与 API 共用 `var/data/pptextract.sqlite3` 和 `var/data/objects`

健康检查位于 `GET /api/v1/health`。worker 尚未产生新鲜心跳时返回 `503 degraded`；API、SQLite、对象目录与 worker 全部就绪后返回 `200 ready`。这些基础设施状态不会出现在产品 UI。

## 生产式单机启动

```bash
cd web && npm run build && cd ..
# 终端 1
uv run python -m pptextract.worker
# 终端 2
uv run uvicorn pptextract.api:app --host 127.0.0.1 --port 8000
```

构建后的 React 壳层由 FastAPI 同源提供，默认路由为 `http://127.0.0.1:8000/documents`。SQLite 与对象目录必须位于本机持久存储，配置会拒绝临时目录与已知网络文件系统。

## 验证

```bash
uv run pytest
uv run mypy src/pptextract
uv run ruff check src tests
cd web && npm test && npm run typecheck && npm run build
```

产品级门禁把部署后的服务视为黑盒，使用公开合成 PPTX、真实 SQLite、本地持久对象目录、单 worker、锁定文档工具链和 Chrome，贯通上传、页对应、审核继承、四类策展路径、发布、Range 下载与下游 generation 原子切换。事务、文件发布和租约恢复使用独立故障标记：

```bash
uv run pytest -m product_acceptance
uv run pytest -m product_fault
```

公开 CI 不读取 `fixtures/` 中的真实本地样本；产品门禁生成的文件名、内容、渲染和产物均来自仓库内的虚构夹具。

### 文档工具链契约门禁

AnyDoc 作为 Python 依赖精确锁定；LibreOffice、Poppler 与字体包封装在一次性发布的公共 GHCR 镜像中。CI、生产和本地验证都使用 `src/pptextract/document_toolchain.json` 内按 registry digest 锁定的同一镜像，不在常规门禁中重新构建：

```bash
RENDER_IMAGE="ghcr.io/dulltackle/pptextract-document-toolchain@sha256:96333270f446993a9f228606ca83d42dc5f082b402f8e674916f248cbc8f9501"
docker pull "$RENDER_IMAGE"

uv run python -m pptextract.toolchain \
  --render-image "$RENDER_IMAGE"
uv run pytest tests/test_rendering_contract.py tests/test_toolchain_gate.py
```

门禁会验证完整的不可变镜像引用，并探测实际加载的 `firecrawl-anydoc`、LibreOffice、Poppler、fontconfig、字体包及 144 DPI/PDF 导出配置，与 `src/pptextract/document_toolchain.json` 逐项比较。任一版本、镜像 digest 或配置变化都会失败。

升级工具链时，使用一个尚未发布的新整数标签构建候选镜像；先用公开合成夹具和获授权的本地样本完成人工视觉门禁，再将候选镜像发布到 `ghcr.io/dulltackle/pptextract-document-toolchain`。从 `docker push` 的结果取得 registry digest，把合同中的 `rendering_image` 更新为完整的 `image@sha256:…` 引用，并重新运行上述门禁。标签只用于发布导航，运行时不得使用标签；发布后执行 `docker logout ghcr.io`，不要在仓库中保存 registry 凭据。

公开契约测试只在内存中生成虚构 `.pptx`，覆盖纯文字、重复与跨页复用图片、图表、组合形状、复杂合并表格、演讲者备注、隐藏页、重复页、页序变更和缺失字体。真实样本仍按 `fixtures/README.md` 的规则只在本地使用。

## 存储维护、备份与恢复

对象回收统一使用两阶段可达性扫描。第一次扫描只标记候选；同一对象在长于任务租约和正常重试窗口的宽限期后仍不可达，第二次扫描才删除字节：

```bash
uv run python -m pptextract.storage_maintenance gc
```

协调备份会用 SQLite backup API 捕获一致快照，再复制该快照同期引用的全部内容寻址对象并执行引用审计。目标目录必须尚不存在：

```bash
uv run python -m pptextract.storage_maintenance backup /srv/pptextract-backups/2026-08-30
```

恢复必须在 API 与 worker 停止时进行，并写入尚不存在的新数据根目录；不要预先创建或覆盖目标。命令先在同一文件系统的暂存根目录恢复并同步 SQLite 与对象，审计完成后一次原子切换整个数据根目录，随后校验原始 PPTX、不可变版本来源、正式视觉资产、冻结成员和当前/保留期内产物的存在性、字节数及 SHA-256。只有审计通过，恢复库的写入与发布门禁才会变为 `ready`：

```bash
uv run python -m pptextract.storage_maintenance restore \
  /srv/pptextract-backups/2026-08-30 \
  /srv/pptextract-restored
```

确认命令退出码为 0 后，再把 `PPTEXTRACT_DATA_ROOT` 切换到新目录并启动 API 与 worker。审计失败时命令退出码为 1，健康检查显示 `recovery: blocked`，所有写请求和 worker 任务保持关闭。可在停机状态下再次执行全量审计：

```bash
uv run python -m pptextract.storage_maintenance audit
```

恢复演练在隔离目录中恢复同一协调备份，通过真实 worker 从检查点续跑一个非终态摄取任务，再读取和校验当前产物；结果写入生产库的 `recovery_drills`。用于演练的备份必须包含一个可控的 `queued` 或 `running` 摄取任务和一个当前产物，演练工作目录也必须尚不存在。初版只记录结果，不验证量化 RPO/RTO：

```bash
uv run python -m pptextract.storage_maintenance drill \
  /srv/pptextract-backups/2026-08-30 \
  /srv/pptextract-drills/2026-08-30
```
