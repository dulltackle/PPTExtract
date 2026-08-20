---
name: PPTExtract
description: 面向内部策展人员的高密度逐页证据审看与决策系统
colors:
  monitor-black: "#171b20"
  monitor-raised: "#20262c"
  monitor-active: "#293139"
  slate-paper: "#e8ebec"
  bright-log-paper: "#f6f7f7"
  log-ink: "#172026"
  muted-ink: "#657078"
  dark-hairline: "#3a444d"
  light-hairline: "#c8ced1"
  selection-teal: "#64bbb8"
  selection-teal-deep: "#276f70"
  pending-amber: "#f2b84b"
  pending-amber-deep: "#895e12"
  exclusion-red: "#e05d4d"
  exclusion-red-deep: "#8d3027"
  approved-green: "#5ca47d"
  focus-cyan: "#9ee9e5"
typography:
  headline:
    fontFamily: '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.45
    letterSpacing: "normal"
  title:
    fontFamily: '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "17px"
    fontWeight: 760
    lineHeight: 1.45
    letterSpacing: "-0.02em"
  body:
    fontFamily: '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: '"Noto Sans SC", "Source Han Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif'
    fontSize: "12px"
    fontWeight: 650
    lineHeight: 1.45
    letterSpacing: "normal"
  data:
    fontFamily: '"JetBrains Mono", "SFMono-Regular", Consolas, monospace'
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
rounded:
  control-sm: "5px"
  field: "6px"
  control: "7px"
  container: "8px"
  raised-container: "9px"
  dialog: "10px"
  system: "12px"
  pill: "999px"
spacing:
  hair: "4px"
  tight: "6px"
  compact: "8px"
  control: "10px"
  cluster: "12px"
  section: "14px"
  panel: "16px"
  wide: "18px"
  dialog: "24px"
  viewport: "32px"
components:
  button-primary:
    backgroundColor: "{colors.selection-teal-deep}"
    textColor: "white"
    rounded: "{rounded.control}"
    padding: "6px 10px"
    height: "34px"
  button-primary-hover:
    backgroundColor: "#328184"
    textColor: "white"
    rounded: "{rounded.control}"
  button-danger:
    backgroundColor: "{colors.exclusion-red-deep}"
    textColor: "white"
    rounded: "{rounded.control}"
    padding: "6px 10px"
    height: "34px"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "#f4f6f7"
    rounded: "{rounded.control}"
    padding: "6px 10px"
    height: "34px"
  field-light:
    backgroundColor: "#fbfcfc"
    textColor: "{colors.log-ink}"
    rounded: "{rounded.field}"
    padding: "8px 9px"
  status-pending:
    backgroundColor: "#fff0c9"
    textColor: "#5c4000"
    rounded: "{rounded.pill}"
    padding: "3px 8px"
---

# Design System: PPTExtract

## Overview

**Creative North Star: “样片审看室”**

PPTExtract 像一间为连续判断而布置的样片审看室：中性监看器黑让来源证据保持视觉主权，明亮场记纸让策展日志、继承关系与审核结论可被快速书写和复核。界面不追求展示性，而以克制、精确、可追溯的操作氛围支撑内部专家长时间工作。

系统以高密度、细分隔和明确状态建立秩序。证据区与决策区应始终并置，编号、焦点和状态在两者之间保持对应；桌面布局优先，并在 1280px 级宽度仍保住来源内容的可读性。具体页面可以采用不同构图，不应把某一张工作台的单页比例提升为全局模板。

**Key Characteristics:**

- 监看器黑与场记纸形成证据、决策双表面。
- teal 表示当前选择，amber 表示待确认，red 表示排除；状态同时使用文字、图形或线型。
- 发丝线、紧凑控件和可见传输键支持高频桌面审看。
- 来源证据、当前编辑值与继承来源保持可对照。
- 视觉表达克制，不使用无依据装饰争夺注意力。

## Colors

色彩是工作语义而非装饰：深色中性面承载观察，明亮冷灰纸面承载记录，三种高辨识色只标记选择、待确认与排除。

### Primary

- **选择青绿：**用于当前页、当前来源文件或人工截图、主确认动作与交互选中态；深色版本用于纸面上的文字、边框和实底动作。

### Secondary

- **待确认琥珀：**用于 pending、继承预填、草稿和需要人工确认的信号；不得代替阻塞或错误。

### Tertiary

- **排除红：**用于明确排除、危险动作和硬阻塞；深色版本承载实底危险动作，避免大面积铺色。
- **批准绿：**只表达完成、批准或已保存，不承担主要导航职责。

### Neutral

- **监看器黑：**来源画面周边、应用框架与命令条的主背景；抬升与激活层只用于同一暗色环境内的分层。
- **场记纸：**策展日志、字段与审核闸门的主表面；亮纸用于可编辑内容与当前页签。
- **日志墨色：**纸面主文字；弱墨色用于次级说明和元数据。
- **发丝线：**暗、亮表面各自使用对应的细分隔色，不跨表面混用。
- **焦点青：**键盘焦点专用，必须与选择状态可区分。

### Named Rules

**The 状态不独唱 Rule.** teal、amber、red 和 green 从不单独传达结果；至少搭配文字、图标、边框样式或位置关系。

**The 稀释红色 Rule.** red 只用于排除、错误与硬阻塞，普通强调和当前选择不得借用它。

## Typography

**Display Font:** Noto Sans SC（回退至 Source Han Sans SC、PingFang SC、Microsoft YaHei 与 sans-serif）

**Body Font:** Noto Sans SC（同一中文无衬线回退栈）

**Label/Mono Font:** JetBrains Mono（回退至 SFMono-Regular、Consolas 与 monospace）

**Character:** 中文无衬线保持中性、紧凑和高识别度；等宽数据字体负责页码、计时、对象编号、快捷键和机器状态，让传输信息与叙述文本一眼分离。

### Hierarchy

- **Headline：**仅用于覆盖层标题与关键模式标题，不在日常工作台中制造营销式大标题。
- **Title：**用于品牌、栏标题和高层级区域名，字重明显但尺寸克制。
- **Body：**用于说明、字段内容和连续阅读，是高密度工作台的默认层级。
- **Label：**用于按钮、页签、状态、元数据和字段标签；通过字重而非全大写建立扫描层级。
- **Data：**用于页码、计时、编号、JSON、键帽和表格数值，启用等宽数字以稳定对齐。

### Named Rules

**The 数据有轨 Rule.** 会变化的数字和机器状态使用等宽数据字体；解释与判断仍使用 UI 字体。

## Layout

布局以“证据区与决策区并置”为耐久原则：来源渲染占据可用空间的主位，页队列、日志或审核控制围绕它建立连续路径。单个工作台采用三栏并不意味着所有页面都必须复用同一比例；新表面应从任务所需的证据—决策关系推导构图。

桌面是当前核心环境。应用框架使用固定顶部状态条与底部命令条，中间区域独立滚动；在较窄桌面宽度触发一次降密度调整，压缩页列、日志动作与来源画面边距，但保留来源区最小可读宽度。界面允许较高信息密度，并以紧凑、成组的间距节奏替代大块留白。

**The 证据不退场 Rule.** 编辑与结论界面必须让来源证据保持可见或可立即对照，不能把关键判断拆进失去上下文的通用卡片流程。

**The 构图非模板 Rule.** 复用证据与决策并置、桌面高密度及 1280px 级可用性，不复制某张已批准页面的固定栏宽。

## Elevation & Depth

系统以发丝线和明暗表面分层为主，阴影为辅。阴影只出现在悬浮工具条、当前页、当前日志条目、来源页画布、抽屉、提示和固定审核闸门等确有前后关系的元素；普通容器保持平面，避免把每组信息都做成漂浮卡片。

### Shadow Vocabulary

- **工具浮层：**用于来源画面上方的临时工具集合。
- **当前队列项：**轻量标明队列中的当前页，不替代 teal 边框。
- **来源画布：**把标准页渲染从监看器背景中抬起。
- **固定审核闸门：**向上投影，说明它固定于明亮日志表面的底部。
- **抽屉：**只向内容侧投影，表达从右边缘进入的覆盖层。

### Named Rules

**The 平面优先 Rule.** 容器默认依靠表面色与发丝线分组；只有真实叠层、固定层或当前焦点才获得阴影。

## Shapes

形状语言是轻微圆角与工程化直线的组合。字段和控制使用小圆角，日志条目与浮层使用略大的圆角，状态 chip 使用胶囊形；源文件预览、人工截图框、编号标签和幻灯片本体保持更硬朗的几何轮廓。所有边界服务于扫描、命中或状态识别，不做装饰性轮廓。

**The 紧角 Rule.** 高频控件保持紧凑小圆角；大胶囊只留给短状态，不将面板和卡片做成松软的 SaaS 气泡。

## Components

### Buttons

- **Shape:** 紧凑小圆角、发丝描边和明确的最小高度。
- **Primary:** 深 teal 实底配白字，用于确认、批准等正向提交动作。
- **Hover / Focus:** hover 通过同色系明度变化或暗面抬色响应；键盘焦点使用独立的亮 cyan 外轮廓并保留偏移。
- **Secondary / Ghost / Tertiary:** quiet 按钮在暗面保持透明，仅 hover 抬高表面；danger 使用深 red；decision 按钮在纸面并排呈现纳入与排除，并用文字持续说明语义。

### Chips

- **Style:** 细边胶囊、短文本与紧凑内边距；纸面状态使用浅色底和深色字，暗面状态使用高亮文字与描边。
- **State:** pending、approved、excluded、hidden 使用不同文字与颜色组合；来源 chip 用于标注继承或证据来源，不伪装成可点击动作。

### Cards / Containers

- **Corner Style:** 队列项与日志条目使用克制圆角，来源画布和对象定位框更接近直角。
- **Background:** 队列项留在暗色监看环境，日志条目留在亮纸环境，不把两类表面混成统一卡片。
- **Shadow Strategy:** 默认平面；当前项可获得轻阴影，但仍以边框与编号同步作为主识别方式。
- **Border:** 单像素发丝线建立密集分组，选中时切换为 teal。
- **Internal Padding:** 使用紧凑的控制与分区间距，展开内容略大于标题行。

### Inputs / Fields

- **Style:** 亮纸字段使用接近白色的输入面、灰色描边和紧凑圆角；暗色筛选字段使用近黑输入面和亮灰文字。
- **Focus:** 使用全局 focus cyan 外轮廓；文本光标在纸面采用深 teal。
- **Error / Disabled:** 禁用字段降低对比并保持值可读；错误和阻塞使用 red 语义容器，同时给出文字原因。

### Navigation

顶部状态条承载文档与会话信息，左侧队列承载页级移动，底部命令条持续展示高频键盘动作。当前筛选、页项和页签均通过边框、底色与文字共同标记；较窄桌面只做降密度，不切换移动端导航。

### Source Review and Manual Capture

来源审核采用“原件先于截图”的签名顺序：右侧先逐项呈现 AnyDoc 文字与图片源文件，保留文件名、格式、尺寸、预览和人工处置；只有来源确认完成后才解锁人工截图。截图框与同编号日志条目保持同步焦点，拖动、裁剪和键盘微调都有明确可见反馈。界面不得用默认检测框暗示人必须从截图开始。

### Command Keys

键帽使用暗色实体、细描边和更厚的下边，模拟可按压的传输键。快捷键与动作文字始终成对出现，输入聚焦时不得劫持编辑按键。

## Do's and Don'ts

### Do:

- **Do** 让来源证据和当前决策同时可见，并用一致编号维持对应。
- **Do** 先呈现可追溯的原始提取文件，确认不足后再提供截图工具。
- **Do** 为状态提供颜色之外的文字、图形、边框或位置线索。
- **Do** 保持桌面高密度，在 1280px 级宽度优先保护来源内容可读性。
- **Do** 把键盘路径、焦点轮廓和当前操作状态持续呈现给专家用户。
- **Do** 使用发丝线、紧凑间距与有限阴影建立层级。

### Don't:

- **Don't** 把工作台退化成通用 SaaS 卡片后台，或让证据离开判断上下文。
- **Don't** 在进入页面时默认生成或展示检测框，迫使用户先处置截图对象。
- **Don't** 使用营销 hero、渐变、玻璃拟态或无依据装饰。
- **Don't** 把 teal、amber、red 或 green 当作唯一状态线索。
- **Don't** 把单个已批准页面的栏宽与比例提升为全局唯一布局。
- **Don't** 让大圆角、厚重阴影或宽松留白降低连续策展的信息密度。
