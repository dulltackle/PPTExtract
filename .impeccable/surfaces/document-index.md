---
version: 1
slug: "document-index"
primary_target: "web/src/App.tsx"
related_targets:
  - "web/src/styles.css"
approved_comp: ".impeccable/mocks/decision/issue-18-stage-runway.webp"
quality_bar: ".impeccable/mocks/decision/model-pick.webp"
---

# 默认文档入口

- **范围 / 模式**：真实 React 桌面产品壳层；Operate。1440px 为主，1280px 必须无页面级横向溢出；移动端不在本票范围。
- **用户与任务**：内部操作者启动系统后确认稳定身份，按“待处理 / 处理中 / 可策展”三条阶段跑道判断当前工作位，并找到上传入口。
- **已选方向**：阶段跑道。继承“样片审看室”的监看器黑、冷灰场记纸、发丝分隔、紧角和等宽数据；三条跑道纵向堆叠，不做 Kanban 卡片墙。
- **真实性边界**：三条跑道必须由 bootstrap API 驱动并保持诚实空态；批准稿中的文档行只表达未来密度，不进入产品。上传入口只接收 `.pptx`，通过正式摄取 API 可靠提交；服务端接受后刷新真实跑道，不预演处理结果。
- **记忆点**：从顶栏到三条全宽跑道形成连续的“工作位轨道”，每条固定标签区与未来文档行共用一条水平基线。

## 批准稿实现清单

| 要素 | 构图承诺 | 实现媒介 |
| --- | --- | --- |
| 顶栏 | 58px 单行框架；产品、当前“文档”、稳定操作者和上传入口持续可见 | React + CSS + inline SVG |
| 三条阶段跑道 | 纵向堆叠的全宽横向跑道；固定标签区与连续内容区；每条允许 0 项 | 语义 section + CSS Grid |
| 空状态 | 保留未来列基线，明确当前为空与下一步来源，不虚构业务数据 | React 文本 + inline SVG 几何 |
| 状态语言 | amber / teal / green 同时搭配文字、计数、边框与位置 | CSS token + 语义文本 |
| 上传入口 | 顶栏入口可聚焦并打开单文件选择；提交中防止重复操作，失败可用同一幂等键重试，成功后刷新跑道 | React file input + button + inline status |
| 底部命令框架 | 48px 固定收口；只列出现有真实操作：焦点移动与刷新，并同步连接状态 | footer + kbd |

组件保持 1px 发丝线、2–8px 紧角、平面容器；只有焦点、浮动边界说明与恢复错误获得轻量阴影。标题 18px，正文 12–13px，机器状态与键帽使用等宽 10–11px。

## 方向契约

- **THESIS**：默认入口就是一组可续接的工作轨道；操作者无需先读仪表盘，只要扫描待处理、处理中、可策展三条连续跑道，就能判断工作位。
- **OWN-WORLD**：继承“样片审看室”的中性监看器黑、场记纸秩序、发丝分隔、紧角与传输键；视觉语言服务长时间桌面操作，不借用营销页、卡片墙或基础设施面板。
- **STORY**：操作者带着稳定身份进入“文档”区域；顶栏下立即出现三条跑道；每条以真实 0 项说明当前没有可续接工作；顶栏上传入口可靠提交首个 `.pptx` 并让新任务进入跑道；底部只保留当前真实可用的刷新与焦点命令。
- **FIRST VIEWPORT**：58px 顶栏直接进入三条纵向堆叠的全宽跑道，不插入标题 hero 或统计摘要。跑道左侧固定阶段标签，右侧沿未来文档行的列基线呈现空位；48px 底部命令条收口整张工作面。1440px 保持批准稿密度，1320px 起压缩标签区和列间距，1280px 不出现页面级横向溢出。
- **FORM**：`seed_key: brief-pinned/issue-18-stage-runway@fedd31e`。本表面不是开放概念轮：GitHub Issue #18 明确钉住“阶段跑道”，而 `fedd31e` 提交保存了对应批准稿；按 new-work 的 “user- or brief-pinned direction beats the roll” 规则，不重新掷 concept seed。表面形式为连续横向轨道、固定左标签、暗色工程表面和单一底部传输条。

## 落地记录

- **实现真相**：`web/src/App.tsx` 与 `web/src/styles.css` 已把批准的阶段跑道落为真实 React 表面；bootstrap 驱动 loading / ready / error，三条跑道读取真实数组并允许 0 项，不含批准稿中的示例文档行。
- **独有构图**：三条纵向堆叠跑道、240px 标签区、右侧未来列基线、每条空位槽，以及顶栏上传边界说明均为 `document-index` 的任务构图，不提升为全局模板。
- **上传入口**：入口可聚焦并打开单文件选择器；仅接受 `.pptx`，提交时显示可靠保存语义并防止重复提交，服务端失败时保留文件与幂等键供重试，接受后刷新 bootstrap 跑道。
- **可访问与恢复**：当前区域、跑道阶段和连接状态均有文字线索；`Tab` / `Shift + Tab` 与 `R` 命令持续可见；加载动画遵守 reduced motion；错误态提供“重新连接”。
- **评审结论**：批准稿为 `issue-18-stage-runway.webp`，质量基准为 `model-pick.webp`；finish reviewer 最终 disposition 为 **ship**，4 项修复均 **resolved**。
