# anydoc 的 pptx 输出形态

anydoc（当前最新发布版 `firecrawl-anydoc==0.1.8`，对应源码 tag `v0.1.8`，commit `4e3089b1ed43404241a303109f81e2c7933040b2`）把一份 pptx 的所有 slide 拼接进**同一个扁平的 `Vec<Block>`**，document model 里没有任何 slide/page 级结构节点；唯一可能标出页边界的信号——标题占位符生成的二级标题、演讲者备注生成的引用块、被别的 slide 链接时插入的锚点——全部是"某页恰好具备某个条件才出现"的偶然产物，没有一个能保证覆盖所有页。这个结论不是我们独家发现：anydoc 维护者自己在 2026-08 提交了 issue [#31](https://github.com/firecrawl/anydoc/issues/31)（标题就是「未加标题的 slide 会和上一页粘在一起」）并开了修复 PR [#32](https://github.com/firecrawl/anydoc/pull/32)（拟在 slide 之间插入 `---` 分隔符），但截至我们测试所用的 v0.1.8（也是目前最新发布版），**该 PR 尚未合并，问题依然存在**——我们用自造样本亲自复现了同样的现象。图片与所在页的关联进一步依赖这个不可靠的边界，本身也没有独立的"这张图属于第几页"的字段。

**因此，「页是原子单位」（ADR-0002）这个架构前提，在 anydoc 当前发布版本的输出上不能直接成立。** 想要维持这个前提，PPTExtract 不能指望从 `to_document()` 的扁平 block 序列里"事后"切出页边界，而需要在 anydoc 之外自己掌握切分（例如：绕开 anydoc 的整份转换，自己按 `sldIdLst` 拆出每页对应的最小 pptx 包再逐页调用 anydoc；或等待/推动上游合并 #31/#32 及配套的图片归属方案；或 fork/patch）。具体走哪条路径是后续 ADR 要做的决策，本笔记只负责把"现状是什么、可信到什么程度"说清楚。

## 结论速览

| # | 问题 | 结论 | 信心程度 |
|---|------|------|----------|
| 1 | 页边界 | 无显式 slide/page 节点；只能靠标题 Heading / 备注 BlockQuote / 内部链接 Anchor 三种偶然信号拼凑，**不保证覆盖所有页**，已用源码分析 + anydoc 自己的 issue #31 + 自造样本三重验证 | 高 |
| 2 | 页内图片归属 | 图片以 `Inline::Image` 出现在扁平 block 序列里，字节存放在 `Document.assets`（可拿到原始字节，已验证），但 `Asset` 结构体本身**没有"属于哪页"的字段**，归属完全靠 block 位置反推，因此和问题 1 一样不可靠 | 高 |
| 3a | 演讲者备注 | 提取，固定策略渲染为该页内容末尾的 `Block::BlockQuote`；在 pptx frontend 里 BlockQuote 只用于备注这一种用途，结构上可辨认，但"是哪一页的备注"仍靠位置紧邻，不是显式字段 | 高（结构可辨认性）／中（页归属仍需位置推断） |
| 3b | 隐藏页 | **完全不处理**，`show="0"` 被无条件忽略，隐藏页和普通页在输出里无任何区别，已实机验证 | 高 |
| 3c | 母版/页脚噪声 | 部分抑制：`sldNum`/`dt`/`ftr` 三种占位符类型被硬编码跳过（已验证），但如果作者把页脚/水印做成普通文本框而非版式占位符，会原样混入正文；母版/版式上的非占位符装饰内容从源码看不会被渲染，但**未做实机验证** | 高（占位符过滤）／中（未验证部分） |
| 3d | 表格 | 结构化 `Table` 节点（`grid: Vec<Vec<CellSlot>>`），不是 Markdown/HTML 文本，支持合并单元格与表头行，已验证 | 高 |
| 3e | 图片 alt text | 保留，来自 `descr` 属性，已验证；`to_markdown()` 输出里没有嵌入图片语法时会退化为纯 alt 文本段落 | 高 |
| 4 | Python API 面 | `to_document`/`to_markdown`/`to_markdown_bytes` 签名明确，document model 类型定义齐全（PyO3 摊平成 `kind` 字段 + 可选字段的 tagged-union 风格类）；但项目 2026-08-03 才建仓、6 天内发 8 个版本，仍是 0.x 阶段，**没有任何"稳定契约"声明**，短期内已出现过触及 model 语义的行为变更 | 高 |

## 1. 页边界

### 1.1 Document model 里没有 slide 节点

`Document` 的定义只有三个字段，`blocks` 是"reading order"上的扁平序列，没有任何按 slide 分组的容器：

```rust
pub struct Document {
    pub blocks: Vec<Block>,
    pub notes: Vec<Note>,
    pub assets: Vec<Asset>,
}
```
来源：[`src/model/mod.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/mod.rs)（29-38 行）。

`Block` 枚举本身也只有七种通用块级类型，没有 `Slide`/`Page`/`Section` 变体：`Heading`、`Paragraph`、`List`、`Table`、`BlockQuote`、`CodeBlock`、`Rule`。来源：[`src/model/block.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/block.rs)（全文件）。这一点在 Python 侧的 `_anydoc.pyi` 里逐字对应（`Block.kind: Literal["heading","paragraph","list","table","block_quote","code_block","rule"]`），确认绑定没有额外补上页节点。

### 1.2 pptx frontend 如何把 slide 拼进这个扁平序列

`src/formats/pptx/mod.rs` 的模块注释直接写明策略：

> "OOXML PresentationML (.pptx / .pptm / .ppsx): slides in `sldIdLst` order, with the full text cascade ... Speaker notes are included (fixed policy), rendered as a quote after each slide's content."

来源：[`src/formats/pptx/mod.rs:1-4`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L1-L4)。

`parse()` 主循环（119-209 行）对每个 slide 做的事情是：

1. 如果这个 slide 是**某个内部链接的目标**，才 push 一个 `Inline::Anchor` 段落（166-170 行）；否则什么都不 push。
2. 调用 `parse_shapes(sp_tree, &ctx, &mut blocks)` 把这一页的形状**直接 append 进传进来的同一个 `blocks: Vec<Block>`**（171 行）——没有"开始新 slide"的标记，也没有把这一页的内容先收集到局部 vec 再整体 push。
3. 如果这个 slide 有 notes part，把备注渲染为 `Block::BlockQuote` 并 push 到 `blocks` 末尾（173-207 行）。
4. 循环进入下一个 slide，continue 直接往同一个 `blocks` 里继续 append。

最终 `Document { blocks, notes: Vec::new(), assets }`（215 行）——注意 `notes` 字段在 pptx frontend 里**始终是空的**，演讲者备注不走这个字段（它是给 docx 脚注/尾注用的），这一点下面第 3a 节和实测都会印证。

来源：[`src/formats/pptx/mod.rs:119-216`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L119-L216)。

### 1.3 三种"偶然"边界信号，没有一种覆盖所有页

- **标题 Heading**：只有该 slide 有非空的 title 占位符时才产生，且固定 `level: 2`（`push_title_heading`，[`mod.rs:407-441`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L407-L441)，438 行硬编码 `level: 2`）。更微妙的是，这个 Heading **不保证是该页第一个 block**——代码注释明确写着"Titles get heading semantics but keep their shape-order position"（398 行注释），也就是说如果作者把标题形状放在其他形状之后（不常见但合法），标题 Heading 在 block 序列里也会排在后面。测试夹具 `handmade-order.pptx` 就是专门为验证这一点造的，其快照输出是：

  ```
  Kicker before the title

  ## Title placed second

  Body after the title

  Quarterly numbers
  ```
  来源：[`tests/snapshots/snapshots__pptx__handmade-order.pptx.snap`](https://github.com/firecrawl/anydoc/blob/v0.1.8/tests/snapshots/snapshots__pptx__handmade-order.pptx.snap)。

  更关键的反例来自 anydoc 自己"真实世界"的测试样本 `pres.pptx`：其快照里 "Deck Title Slide" 和 "Numbers Slide" 两行**完全没有 `##` 前缀**，说明这份样本里视觉上的"标题"用的是普通文本框而非标题占位符，因而根本不会被识别成 Heading：

  ```
  Deck Title Slide

  - Top level point
    - Nested detail
  - Second point with emphasis

  > Speaker note for the intro slide.

  Numbers Slide

  | Region | Total |
  | --- | --- |
  | North | 42 |
  ...
  > Second slide notes mention the table.
  ```
  来源：[`tests/snapshots/snapshots__pptx__pres.pptx.snap`](https://github.com/firecrawl/anydoc/blob/v0.1.8/tests/snapshots/snapshots__pptx__pres.pptx.snap)。在这个样本里，两页之间唯一能看出"换页"的信号是 slide 1 末尾的 `> Speaker note for the intro slide.`——而这只是因为这一页恰好带了备注；如果不带，"Deck Title Slide" 段落会和 "Numbers Slide" 段落无缝相连，肉眼和程序都分不出这是两页。

- **备注 BlockQuote**：只在该 slide 确实挂了 notes part 时才出现（[`mod.rs:176-207`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L176-L207)），大多数页不会有备注。

- **内部链接 Anchor**：只在**其他** slide 有超链接指向这一页时才插入（`targeted` 集合的构造见 [`mod.rs:108-117`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L108-L117)，插入逻辑见 166-170 行），绝大多数 pptx 里 slide 之间没有超链接跳转，这个信号几乎不会出现。

三者取并集，仍然存在大量"既无标题占位符、又无备注、又不是任何链接目标"的页——典型场景是章节分隔页、通栏大图页、延续上一页话题的无标题正文页——它们在 block 序列里和"上一页内容的自然延续"完全没有区别。

### 1.4 anydoc 自己已经确认这是个缺陷，修复尚未发布

Issue [#31](https://github.com/firecrawl/anydoc/issues/31)（"Presentation slides are concatenated with no boundary, so untitled slides merge into the previous one"）用几乎和我们一致的复现代码描述了同一问题，并给出结论：

> "A slide's title becomes a `Heading`, so decks that title every slide read correctly by accident. A slide with no title contributes no structural block at all ... Nothing marks where slide 2 ends and slide 3 begins."

对应修复 PR [#32](https://github.com/firecrawl/anydoc/pull/32) 提议在三个 presentation frontend（pptx / ppt / odp）里，在每页非空内容前插入 `Block::Rule`（渲染成 `---`），并给出验证：`cargo test` 194 passed，快照 diff 只新增分隔符、零内容改动。有独立用户 `H0rowitz` 在评论里用真实 Pandoc 生成的 pptx 复核了这个修复。**但该 PR 在我们调研时（对应最新 tag v0.1.8，2026-08-10 发布）仍处于 open 未合并状态**——也就是说我们通过 pip 装到的最新发布版本仍然带有这个缺陷，下面"验证方法"一节的实测结果就是证据。

另有一个已关闭的 issue [#49](https://github.com/firecrawl/anydoc/issues/49)"feat: preserve presentation slide identity in the document model"，标题看起来是想在 model 层面（而不是靠插入 Rule）彻底解决页归属问题，但其 body 为空、无评论、关闭事件也没有留下 `state_reason`，**无法确认它是被 #31/#32 的轻量方案取代、还是被维护者判定为不值得做而搁置**——这属于"查不到"的部分，见"未能确认的部分"。

即便 #32 未来合并，它解决的只是"用 `---` 分隔正文段落"，并不会给 `Asset` 结构体添加"属于第几页"的字段（PR 描述里明确写"no binding source or type surface changes, no new Block variant"）——也就是说图片归属问题（问题 2）**不会**被这个 PR 自动解决，需要额外的改动。

## 2. 页内图片归属

图片在 model 里是一个 inline 节点而不是独立的资产列表：

```rust
pub enum Inline {
    ...
    Image { alt: String, source: ImageSource },
    ...
}
pub enum ImageSource {
    External(String),
    Asset(AssetId),
    Unavailable,
}
```
来源：[`src/model/link.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/link.rs)。pptx frontend 在遇到 `<p:pic>` 时，把它包成 `Block::Paragraph(vec![Inline::Image{...}])` 并 push 进和其他内容**同一个** `blocks` 序列（[`mod.rs:351-378`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L351-L378)）——即图片在扁平序列里的位置，就是它在原 slide 里被遍历到的那个时间点，**除此之外没有任何显式字段说明它属于哪一页**。

字节存放在 `Document.assets`：

```rust
pub struct Asset {
    pub id: AssetId,
    pub media_type: String,
    pub origin_part: String,
    pub bytes: Vec<u8>,
}
```
来源：[`src/model/asset.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/asset.rs)。`origin_part` 是这张图在 OPC 包里的物理路径（例如 `ppt/media/image1.png`），**不是页面/slide 的标识**。更关键的是 `AssetSink::add()` 按 `origin_part` 去重共享：如果同一张媒体图片被多个 slide 引用（比如背景图/复用素材），只会在 `assets` 里存一份字节，多个 `Inline::Image` 节点各自指向同一个 `AssetId`：

```rust
if let Some(&id) = self.by_part.get(&origin_part) {
    return Ok(id);   // 复用已有 asset，不重复计入字节总量
}
```
来源：[`src/shared/assets.rs:29-46`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/shared/assets.rs#L29-L46)，配套单测 `repeated_origin_parts_share_one_asset` 印证了这个去重行为。

**结论**：图片原始字节可以拿到（`Asset.bytes` / Python 侧 `Asset.data`，已实机验证——见下节，测试图片的 PNG 字节被完整取出，`data[:8]` 正是 PNG 魔数）。但"这张图属于哪一页"这件事，`Asset` 结构体本身完全不携带，只能通过"这个 `Inline::Image` 在扁平 `blocks` 里排在哪两个页边界信号之间"来反推——而页边界信号本身就是第 1 节里论证过的不可靠信号。也就是说，**要计算 ADR-0002 要求的「页文本 + 页内图片字节」联合指纹，在 anydoc 当前输出上做不到可靠归属**，除非页边界问题先被解决（无论是等上游合并 #32/更完整的方案，还是自己在 anydoc 之外先做 slide 级切分）。

Python 绑定侧对应地把 `Inline.source.asset_id` 暴露为一个整数索引（见第 4 节），语义和 Rust 侧完全一致，没有额外补充页归属信息。

## 3. 元素可辨认性

### 3a. 演讲者备注（speaker notes）

**提取**，固定策略。模块注释写明"Speaker notes are included (fixed policy)"（[`mod.rs:3`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L3)）。实现上，每页处理完自己的形状后，会去找 `notesSlide` part，把其中除 `sldImg`/`sldNum`/`hdr`/`ftr`/`dt` 占位符之外的文本框内容解析成 `notes_blocks`，非空时整体包成一个 `Block::BlockQuote` push 到当前页内容之后（[`mod.rs:173-207`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L173-L207)）。

**能否与正文区分**：结构上可以——在 pptx frontend 里，`Block::BlockQuote` 唯一的产生位置就是这里，没有其他代码路径会为 pptx 产生 BlockQuote，因此下游只要检查 `block.kind == "block_quote"` 就能可靠识别出"这是一段备注"，不需要靠 Markdown 里的 `>` 做字符串猜测。但"这段备注属于哪一页"依然只能靠它在扁平序列里紧跟在哪一页内容后面来判断，本质上还是第 1 节的问题——如果该页在备注之前又没有标题 Heading，就无法确定备注对应的是哪一页。

**容易踩的坑**：Python 侧 `Document.notes` 字段（对应 Rust `Document.notes: Vec<Note>`）是给 docx 脚注/尾注用的，pptx frontend 里这个字段**始终为空**（`Ok(Document { blocks, notes: Vec::new(), assets })`，[`mod.rs:215`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L215)）。已实机验证：我们的样本里有一页带备注，`len(doc.notes) == 0`，备注其实以 `block_quote` 的形式出现在 `doc.blocks` 里。

### 3b. 隐藏页（hidden slides）

**完全不处理**。OOXML 里隐藏 slide 靠 `<p:sld show="0">` 标记，我们在整个 `src/formats/pptx/` 目录里搜索 `show`/`hidden` 两个关键词，**零命中**——代码从未读取、过滤或标记这个属性；`sldIdLst` 里列出的每一个 slide 都被无条件解析并拼进 `blocks`。README 全文也未出现"hidden"字样。已用 `show="0"` 的自造隐藏页实机验证：其标题、正文原样出现在 markdown 和 document model 里，没有任何字段、注释或标记能区分它是隐藏页——下游如果不希望隐藏页混入语料，必须在喂给 anydoc 之前，自己用 OOXML/python-pptx 读一遍 `show` 属性先过滤掉。

### 3c. 母版/页脚等每页重复的噪声

**部分抑制**。`parse_shape` 对 `ph_type` 为 `"sldNum"`（页码）、`"dt"`（日期）、`"ftr"`（页脚）的占位符直接跳过，不解析其文本内容（[`mod.rs:390-394`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L390-L394)）；备注解析那边也有同样的过滤（外加 `sldImg`/`hdr`，见 [`mod.rs:193-200`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L193-L200)）。已实机验证（见下节）：给页脚/页码/日期占位符塞入可见文本后，三者均未出现在输出的任何地方。

但这个过滤只认**占位符类型**，不认"视觉上像页脚"。如果作者没有用版式自带的页脚占位符，而是自己拖一个普通文本框/图片贴在每页同一位置当水印或页脚，`parse_shapes` 会把它当成普通形状处理（[`mod.rs:346-348`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L346-L348)），原样进入正文，和真正的页面内容完全不可区分。

母版（`slideMaster`）/版式（`slideLayout`）上直接画的非占位符装饰内容（例如 logo 图片直接摆在母版上，不通过占位符继承）——从源码看**不会**被渲染：`load_layout`/`load_master` 只对 layout/master 的 `spTree` 调用 `cascade::collect_placeholders`（用于收集占位符的样式，供后续级联到 slide 上的同名占位符），从未对它们调用 `parse_shapes`（[`mod.rs:226-257`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L226-L257)）。这一条**我们只做了源码推断，没有实机验证**（构造母版级装饰内容需要绕开 python-pptx 的高层 API，直接操作母版 XML，超出本次调研的时间预算），已列入"未能确认的部分"。

### 3d. 表格

**结构化 `Table` 节点**，不是 Markdown 表格文本，也不是 HTML：

```rust
pub struct Table {
    pub grid: Vec<Vec<CellSlot>>,
    pub header_rows: usize,
    pub kind: TableKind,   // Data | Layout
}
```
来源：[`src/model/table.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/table.rs)；pptx 侧的构建逻辑在 [`parse_table`，`mod.rs:605-638`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L605-L638)，用 `GridBuilder` 处理 `gridSpan`/`rowSpan`/`hMerge`/`vMerge`，"每个逻辑网格位置恰好出现一次"（`CellSlot::Origin` 携带内容，`CellSlot::Covered` 回指被合并到的 origin 坐标）。`header_rows` 从 `<a:tblPr firstRow="1">` 解析（`resolve_header_rows`）。已实机验证：Python 侧 `block.kind == "table"`，`block.table.grid` 可编程访问单元格，`to_markdown()` 渲染成 GFM 表格语法。

### 3e. 图片 alt text

**保留**。来自 `<p:cNvPr descr="...">` 属性，经 `clean_text` 清洗后放进 `Inline::Image.alt`（[`mod.rs:352-356`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L352-L356)）。已实机验证：中文 alt text 原样保留在 `document.blocks[i].content[0].alt` 里。`to_markdown()` 渲染时，因为 Markdown 语法本身无法内嵌字节，图片会退化成纯 alt 文本段落（Python README 明确说明："an embedded image renders as its alt text while the bytes stay on `document.assets`"）——这意味着**仅用 `to_markdown()` 拿不到任何图片存在过的结构化标记**（没有 `![]()`），必须用 `to_document()` 才能同时拿到 alt text 和字节。这一点也是 anydoc 自己 issue [#63](https://github.com/firecrawl/anydoc/issues/63)（"Expose embedded image assets in markdown output"）在跨格式讨论的通用问题，不是 pptx 独有。

## 4. Python 绑定 API 面

`python/anydoc/_anydoc.pyi`（来源：[`python/anydoc/_anydoc.pyi`](https://github.com/firecrawl/anydoc/blob/v0.1.8/python/anydoc/_anydoc.pyi)）是手写的类型存根，配套单测 `test_the_stubs_cover_the_module` 会检查存根和编译模块实际导出的符号集合一致（[`python/tests/test_anydoc.py:118-126`](https://github.com/firecrawl/anydoc/blob/v0.1.8/python/tests/test_anydoc.py#L118-L126)），所以这份签名可信度较高。

```python
def to_markdown(path: str | os.PathLike[str]) -> str: ...
def to_markdown_bytes(data: bytes | bytearray, format: Format | None = None) -> str: ...
def to_document(data: bytes | bytearray, format: Format | None = None) -> Document:
    """... Unsupported for `pdf`: PDF conversion produces Markdown directly and
    has no document-model form; use `to_markdown_bytes`."""

@final
class Document:
    blocks: list[Block]
    notes: list[Note]      # 脚注/尾注专用，pptx 恒为空，见 3a
    assets: list[Asset]
```

`Block`/`Inline`/`Table`/`ImageSource`/`LinkTarget` 等都不是 Python 原生的 `dataclass` 或每个变体一个子类的写法，而是 PyO3 常见的"摊平 tagged union"：**一个 `@final class`，一个 `kind: Literal[...]` 字段标出变体，其余字段全部是 `Optional`，按 `kind` 决定哪些有效**。例如 `Block`：

```python
@final
class Block:
    kind: Literal["heading", "paragraph", "list", "table", "block_quote", "code_block", "rule"]
    level: int | None       # heading: 1-6
    anchor: str | None      # heading
    content: list[Inline] | None   # heading, paragraph
    list: List | None
    table: Table | None
    blocks: list[Block] | None     # block_quote
    lang: str | None        # code_block
    text: str | None        # code_block
```

这种设计对下游代码不算特别友好（要先判 `kind` 再按需读其他字段，字段命名还和 Rust 侧的枚举 payload 直接对应），但类型存根写得很详细，逐字段注明了"哪个 kind 下这个字段才有效"。`Asset`/`ImageSource` 与第 1、2 节引用的 Rust 定义完全对应，绑定没有引入额外语义，也没有裁剪掉任何字段。

**稳定性评估**：

- 仓库 `firecrawl/anydoc` 创建于 **2026-08-03**（`created_at`，通过 `gh api repos/firecrawl/anydoc` 查得），首个发布版 `v0.1.1` 在 2026-08-04，到我们测试所用的 `v0.1.8`（2026-08-10）**6 天内发了 8 个版本**——非常年轻、非常活跃，仍处于 `0.x` 阶段。
- 没有 `CHANGELOG.md` 文件，GitHub Releases 的说明大多只有自动生成的 "Full Changelog: compare 链接"，信息量很低；少数几条有摘要的（`v0.1.5` "fix heading emphasis and header rows"，`v0.1.6` "binding error codes"）直接触及了我们依赖的 document model 语义（标题渲染、错误分类），说明短期内已经发生过影响输出/绑定行为的改动。
- README、python/README、类型存根里都没有出现"stable API"/"public contract"/"breaking change policy"之类的稳定性承诺字样。
- 我们查到的、仍处于 open 状态且会改变 pptx 输出结构的 PR（#32）本身就是一个"下个版本很可能变"的具体例子；#63（图片进 Markdown）如果被采纳，也会改变 `to_markdown()` 的输出形态。
- 结论：**document model 目前更接近"经过认真设计但仍在快速演进的内部实现细节"，而不是可以长期锚定的公开契约**。如果 PPTExtract 决定依赖 `to_document()` 的具体结构做页指纹/归属计算，建议锁定 anydoc 的具体版本号并对输出结构做契约测试，而不是假设未来版本行为不变。

## 验证方法与实际输出

环境：`Python 3` venv + `pip install firecrawl-anydoc python-pptx pillow`，实测版本 `firecrawl-anydoc==0.1.8`（与源码 tag `v0.1.8` / commit `4e3089b` 一致）。样本构造脚本、运行脚本、原始输出均保存在本次调研用的临时目录（未提交进仓库），关键片段摘录如下。

### 样本构造（5 页，覆盖 issue 要求的所有场景 + 边界案例）

```python
prs = Presentation()
# 第1页：标准标题页（有 title 占位符）
s1 = prs.slides.add_slide(prs.slide_layouts[0])
s1.shapes.title.text = "标题页：产品季度回顾"
s1.placeholders[1].text = "副标题占位符文本"

# 第2页：只有图片，无标题占位符（blank layout）
s2 = prs.slides.add_slide(prs.slide_layouts[6])
pic = s2.shapes.add_picture(img_path, Inches(1), Inches(1), width=Inches(3))
pic._element.find(qn('p:nvPicPr')).find(qn('p:cNvPr')).set(
    'descr', '一张测试用红色方块图片，用于验证 alt text 保留')

# 第3页：标题占位符 + 表格 + 演讲者备注
s3 = prs.slides.add_slide(prs.slide_layouts[1])
s3.shapes.title.text = "数据页：区域销售表"
table = s3.shapes.add_table(3, 2, Inches(2), Inches(2), Inches(4), Inches(1.2)).table
# ... 填充表格 ...
s3.notes_slide.notes_text_frame.text = "这是第三页的演讲者备注：向听众强调华东区域增长最快。"

# 第4页：隐藏页（show="0"），标题占位符里放独有文本
s4 = prs.slides.add_slide(prs.slide_layouts[1])
s4.shapes.title.text = "隐藏页：内部草稿数据，不应外泄"
s4.placeholders[1].text_frame.text = "这段文字只应该出现在隐藏页里：机密数字 42.195"
s4.element.set('show', '0')

# 第5页：纯文本框充当"标题"，不用标题占位符（模拟真实世界常见做法）
s5 = prs.slides.add_slide(prs.slide_layouts[6])
s5.shapes.add_textbox(...).text_frame.text = "看起来像标题但其实是文本框：不会被识别为 heading"
s5.shapes.add_textbox(...).text_frame.text = "第五页正文内容，紧跟在"伪标题"文本框之后，且本页无演讲者备注、无到本页的内部链接。"
```

### 实际输出：`to_markdown()`

```
## 标题页：产品季度回顾

副标题占位符文本

一张测试用红色方块图片，用于验证 alt text 保留

## 数据页：区域销售表

| 区域 | 总额 |
| --- | --- |
| 华东 | 128 |
| 华南 | 97 |

> 这是第三页的演讲者备注：向听众强调华东区域增长最快。

## 隐藏页：内部草稿数据，不应外泄

- 这段文字只应该出现在隐藏页里：机密数字 42.195

看起来像标题但其实是文本框：不会被识别为 heading

第五页正文内容，紧跟在"伪标题"文本框之后，且本页无演讲者备注、无到本页的内部链接。
```

**关键观察**：

- 第 4 页是隐藏页（`show="0"`），**照常出现**，标题变成 `##` 标题，正文变成列表项，和普通页毫无区别——证实 3b。
- 第 2 页（图片页，无标题占位符）和第 5 页（伪标题文本框，无标题占位符）在输出里**完全没有分隔标记**，第 2 页的图片 alt 文本紧跟在第 1 页副标题之后，第 5 页的"伪标题"段落紧跟在第 4 页列表项之后——证实 1.3/1.4 里论证的"未标题页边界丢失"，且这是当前最新发布版（v0.1.8）里真实存在的行为，不是理论推测。

### 实际输出：`to_document()` 结构（逐 block 打印）

```
len(blocks) = 10
len(notes) = 0
len(assets) = 1

[0] kind=heading level=2 anchor='标题页：产品季度回顾' text='标题页：产品季度回顾'
[1] kind=paragraph Text('副标题占位符文本')
[2] kind=paragraph Image(alt='一张测试用红色方块图片，用于验证 alt text 保留', source_kind=asset, asset_id=0)
[3] kind=heading level=2 anchor='数据页：区域销售表' text='数据页：区域销售表'
[4] kind=table grid 3x2 header_rows=1 kind=data
[5] kind=block_quote 1 inner blocks: 这是第三页的演讲者备注：向听众强调华东区域增长最快。
[6] kind=heading level=2 anchor='隐藏页：内部草稿数据，不应外泄' text='隐藏页：内部草稿数据，不应外泄'
[7] kind=list
[8] kind=paragraph Text('看起来像标题但其实是文本框：不会被识别为 heading')
[9] kind=paragraph Text('第五页正文内容，紧跟在"伪标题"文本框之后，且本页无演讲者备注、无到本页的内部链接。')

=== assets ===
id=0 media_type=image/png origin_part=ppt/media/image1.png len(data)=155 data[:8]=b'\x89PNG\r\n\x1a\n'
```

逐页手工标注（block 索引 ↔ 实际所属页，这个标注本身在输出里是**不存在**的，是我们根据造样本时的知识手工反推的）：

| 页 | 应属 block 索引 | 该页有无独立边界信号 |
|---|---|---|
| 1（标准标题页） | 0, 1 | 有（Heading） |
| 2（图片页，无标题） | 2 | **无**——与第 1 页无缝相接 |
| 3（表格+备注） | 3, 4, 5 | 有（Heading + BlockQuote） |
| 4（隐藏页，有标题） | 6, 7 | 有 Heading，但**无隐藏标记** |
| 5（伪标题文本框） | 8, 9 | **无**——与第 4 页无缝相接 |

`len(doc.notes) == 0` 直接证实：pptx 的演讲者备注**不会**出现在 `Document.notes`（那是脚注/尾注专用），而是以 `block_quote` 形式混在 `blocks` 里，验证了 3a 节的结论。

`assets` 只有 1 项，`data[:8]` 是 PNG 文件头魔数 `\x89PNG\r\n\x1a\n`，证实图片原始字节可以完整取出。`origin_part` 是媒体文件的包内路径，不含任何"页"的信息。

### 补充实验：母版/页脚占位符噪声抑制

```python
add_placeholder("ftr", "10", "版权所有 2026 某公司 - 机密")
add_placeholder("sldNum", "11", "3")
add_placeholder("dt", "12", "2026-08-13")
```

输出：

```
## 带页脚的页面

- 正文内容
```

三个占位符的文本（"版权所有..."、页码"3"、日期"2026-08-13"）**全部未出现**在输出的任何地方，证实 3c 节里"`sldNum`/`dt`/`ftr` 占位符类型被硬编码跳过"的判断。

## 未能确认的部分

- **母版/版式上非占位符装饰内容（如直接画在母版上的 logo 图片）是否真的完全不出现在输出里**：源码显示 `load_layout`/`load_master` 从不对 master/layout 的 `spTree` 调用 `parse_shapes`，只用于收集占位符样式（[`mod.rs:226-257`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs#L226-L257)），逻辑上应该被完全忽略；但我们没有做实机验证——python-pptx 的高层 API 不方便直接编辑 slide master，需要更底层的 lxml 操作，超出本次调研的时间预算。这条结论目前只是"源码里明确写了"，不是"验证过"。
- **issue #49"preserve presentation slide identity in the document model"被关闭的具体原因**：该 issue 的 `body` 字段为 `null`、没有任何评论，`closed` 事件里 `state_reason` 也是 `null`，GitHub API 暴露的信息到此为止，无法判断它是被 #31/#32 的轻量方案取代，还是被维护者认定不值得做而搁置。
- **真实语料下"有标题占位符的 slide 占比"到底有多高**，即页边界丢失问题在实践中的影响面：anydoc 自己在 #31 里也明确说"I have not scanned a corpus, so I will not claim a frequency"，我们同样没有条件跑大规模真实语料，只验证了"结构上不可靠"这个定性结论，给不出"多少比例的 pptx 会中招"这类量化数字。
- **Node.js / WebAssembly 绑定的 document model 是否和 Python 绑定完全一致**：理论上应该一致（core model 是单一来源，PR #32 里明确写"no binding source or type surface changes"这种模式），但我们没有逐字审阅 `node/src/document.rs`、`wasm/src/typescript.rs` 的完整内容，只读到了 issue/PR 里引用的具体行号，没有做交叉核对。任务范围限定在 Python 绑定，这里做了取舍，未展开。

## 来源

一手来源（均锁定在 tag `v0.1.8` / commit `4e3089b1ed43404241a303109f81e2c7933040b2`，2026-08-10 发布，与本次实测所用的 `firecrawl-anydoc==0.1.8` 一致）：

- 仓库根：https://github.com/firecrawl/anydoc
- Document model 定义：
  - [`src/model/mod.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/mod.rs)
  - [`src/model/block.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/block.rs)
  - [`src/model/asset.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/asset.rs)
  - [`src/model/link.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/link.rs)
  - [`src/model/table.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/model/table.rs)
- pptx frontend：
  - [`src/formats/pptx/mod.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/formats/pptx/mod.rs)（全文件通读，重点行号见正文）
  - [`src/shared/assets.rs`](https://github.com/firecrawl/anydoc/blob/v0.1.8/src/shared/assets.rs)
- 测试与快照（真实/构造样本的实际输出）：
  - [`tests/snapshots/snapshots__pptx__pres.pptx.snap`](https://github.com/firecrawl/anydoc/blob/v0.1.8/tests/snapshots/snapshots__pptx__pres.pptx.snap)
  - [`tests/snapshots/snapshots__pptx__handmade-order.pptx.snap`](https://github.com/firecrawl/anydoc/blob/v0.1.8/tests/snapshots/snapshots__pptx__handmade-order.pptx.snap)
  - [`tests/snapshots/snapshots__pptx__handmade-links.pptx.snap`](https://github.com/firecrawl/anydoc/blob/v0.1.8/tests/snapshots/snapshots__pptx__handmade-links.pptx.snap)
  - [`tests/snapshots/snapshots__pptx__handmade-inherit.pptx.snap`](https://github.com/firecrawl/anydoc/blob/v0.1.8/tests/snapshots/snapshots__pptx__handmade-inherit.pptx.snap)
- Python 绑定：
  - [`python/anydoc/_anydoc.pyi`](https://github.com/firecrawl/anydoc/blob/v0.1.8/python/anydoc/_anydoc.pyi)
  - [`python/anydoc/__init__.py`](https://github.com/firecrawl/anydoc/blob/v0.1.8/python/anydoc/__init__.py)
  - [`python/README.md`](https://github.com/firecrawl/anydoc/blob/v0.1.8/python/README.md)
  - [`python/tests/test_anydoc.py`](https://github.com/firecrawl/anydoc/blob/v0.1.8/python/tests/test_anydoc.py)
- 主 README（功能清单，无 hidden slide 相关描述）：[`README.md`](https://github.com/firecrawl/anydoc/blob/v0.1.8/README.md)
- 上游 issue / PR（GitHub API 读取，均为一手信息）：
  - [#31 Presentation slides are concatenated with no boundary](https://github.com/firecrawl/anydoc/issues/31)
  - [#32 fix(presentations): separate slides with a thematic break](https://github.com/firecrawl/anydoc/pull/32)（未合并）
  - [#49 feat: preserve presentation slide identity in the document model](https://github.com/firecrawl/anydoc/issues/49)（已关闭，无说明）
  - [#63 Expose embedded image assets in markdown output](https://github.com/firecrawl/anydoc/issues/63)
- 版本/发布节奏（GitHub API `repos/firecrawl/anydoc` 与 `repos/firecrawl/anydoc/releases`）：仓库创建于 2026-08-03T16:36:14Z，`v0.1.1`（2026-08-04）至 `v0.1.8`（2026-08-10）。

实测证据：本地 venv 安装 `firecrawl-anydoc==0.1.8` + `python-pptx` + `pillow`，用 `python-pptx` 构造 5 页样本 pptx（覆盖标题页、图片页、表格+备注页、隐藏页、伪标题页）及一份补充的页脚/页码/日期占位符样本，分别调用 `anydoc.to_markdown()` 与 `anydoc.to_document()` 并逐字段打印，输出摘录见"验证方法与实际输出"一节。样本构造脚本、运行脚本与原始输出保存在本机临时目录，未提交进仓库。
