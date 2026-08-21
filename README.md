# PPTExtract

PPTExtract 的初版单机产品脊柱：FastAPI、React 文档入口、一个持久任务 worker、SQLite 与本地 SHA-256 内容寻址对象目录。

## 开发启动

```bash
uv sync --dev
cd web && npm install && cd ..
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

### 文档工具链契约门禁

AnyDoc 作为 Python 依赖精确锁定；LibreOffice、Poppler 与字体包由仓库内的容器构建文件固定。构建并验证实际工具链：

```bash
docker build \
  --provenance=false \
  --platform=linux/amd64 \
  --file docker/document-toolchain.Dockerfile \
  --tag pptextract/document-toolchain:1 \
  .

uv run python -m pptextract.toolchain \
  --render-image pptextract/document-toolchain:1
uv run pytest tests/test_rendering_contract.py tests/test_toolchain_gate.py
```

门禁会探测实际加载的 `firecrawl-anydoc`、LibreOffice、Poppler、fontconfig、字体包、容器内容地址及 144 DPI/PDF 导出配置，并与 `src/pptextract/document_toolchain.json` 逐项比较。任一版本或配置变化都会失败；升级时必须先用公开合成夹具和获授权的本地样本完成人工视觉验证，再显式更新契约。

公开契约测试只在内存中生成虚构 `.pptx`，覆盖纯文字、重复与跨页复用图片、图表、组合形状、复杂合并表格、演讲者备注、隐藏页、重复页、页序变更和缺失字体。真实样本仍按 `fixtures/README.md` 的规则只在本地使用。
