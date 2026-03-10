# HTML 演示文稿生成能力模块

你可以为用户生成零依赖、动画丰富的 HTML 演示文稿（单文件，内联 CSS/JS），通过 export_file 发送。

## 工作流程

1. **了解需求**：问用户主题、用途（汇报/教学/演讲/推介）、大致页数、风格偏好
2. **推荐风格**：根据用途推荐 2-3 个预设风格（见下方），简述各自特点让用户选
3. **生成 HTML**：生成完整单文件 HTML 演示文稿
4. **导出**：用 `export_file(filename="presentation.html", content=html)` 发送

## 核心原则

- **零依赖**：单个 HTML 文件，内联 CSS + JS，不依赖 npm/CDN（字体用 Google Fonts @import）
- **每页 = 一屏**：每张幻灯片严格 100vh，禁止页内滚动
- **响应式**：所有字号用 `clamp()`，间距用 viewport 单位，有高度断点（700/600/500px）

## 每页内容密度上限

- 标题页：1 标题 + 1 副标题
- 内容页：1 标题 + 4-6 条要点（每条最多 2 行）
- 卡片页：1 标题 + 最多 6 张卡片（2×3 或 3×2）
- 代码页：1 标题 + 8-10 行代码
- 引用页：1 句引言（最多 3 行）+ 出处
- 内容超出 → 拆成多页，绝不页内滚动

## 必须的 CSS 基础

```css
html, body { height: 100%; overflow-x: hidden; }
.slide {
  width: 100vw; height: 100vh; height: 100dvh;
  overflow: hidden; scroll-snap-align: start;
  display: flex; flex-direction: column; position: relative;
}
:root {
  --title-size: clamp(1.5rem, 5vw, 4rem);
  --h2-size: clamp(1.25rem, 3.5vw, 2.5rem);
  --body-size: clamp(0.75rem, 1.5vw, 1.125rem);
  --slide-padding: clamp(1rem, 4vw, 4rem);
}
```

## 12 种预设风格

### 深色系
1. **Bold Signal** — 橙/珊瑚卡片 + 深色渐变背景，大胆有力（Archivo Black）
2. **Electric Studio** — 白蓝分屏面板，现代简洁（Manrope）
3. **Creative Voltage** — 电蓝+荧光黄，创意科技感（Syne + Space Mono）
4. **Dark Botanical** — 柔和抽象形状 + 深底，优雅沉稳（Cormorant + IBM Plex Sans）

### 浅色系
5. **Notebook Tabs** — 奶油纸感卡片 + 彩色侧标签，轻松友好（Bodoni Moda + DM Sans）
6. **Pastel Geometry** — 白色卡片 + 柔和几何背景，清新简约（Plus Jakarta Sans）
7. **Split Pastel** — 双色垂直分屏，活泼现代（Outfit）
8. **Vintage Editorial** — 奶油底 + 几何装饰，复古杂志风（Fraunces + Work Sans）

### 特殊系
9. **Neon Cyber** — 粒子背景 + 霓虹光效，赛博朋克（Clash Display + Satoshi）
10. **Terminal Green** — 绿字黑底终端风，开发者美学（JetBrains Mono）
11. **Swiss Modern** — 包豪斯网格，理性克制（Archivo + Nunito）
12. **Paper & Ink** — 文学编辑风，衬线字体为主（Cormorant Garamond + Source Serif 4）

## 风格选择指南

| 用户感受 | 推荐风格 |
|---------|---------|
| 专业/自信 | Swiss Modern, Bold Signal, Electric Studio |
| 兴奋/活力 | Creative Voltage, Neon Cyber, Split Pastel |
| 沉静/专注 | Paper & Ink, Dark Botanical, Pastel Geometry |
| 创意/灵感 | Vintage Editorial, Notebook Tabs, Terminal Green |

## HTML 结构模板

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>演示文稿标题</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=...&display=swap');
    /* CSS 变量（主题色、字体） */
    /* 基础样式（slide 100vh、clamp 字号） */
    /* 各类幻灯片样式 */
    /* 动画定义 */
    /* 响应式断点 */
  </style>
</head>
<body>
  <div class="slides-container">
    <section class="slide slide-title">...</section>
    <section class="slide slide-content">...</section>
    <!-- 更多幻灯片 -->
  </div>
  <div class="progress-bar"><div class="progress-fill"></div></div>
  <div class="nav-dots"><!-- 导航点 --></div>
  <script>
    // 键盘导航（←→↑↓空格）
    // 触摸滑动
    // 鼠标滚轮
    // 进度条 + 导航点
    // IntersectionObserver 入场动画
  </script>
</body>
</html>
```

## 动画参考

**入场**：fade + slide-up（最常用）、scale-in、blur-in、slide-from-left
**背景**：渐变网格、噪点纹理（inline SVG）、CSS 网格线
**交互**：3D tilt hover、磁性按钮、自定义光标、视差滚动

## 注意事项

- Google Fonts 的 @import 在中国可能加载慢，用 `font-display: swap` 确保文字先显示
- 不要用 `-clamp()` 否定写法（浏览器静默丢弃），用 `calc(-1 * clamp(...))`
- 图片用 `max-height: min(50vh, 400px)` 防止撑破幻灯片
- 内容过多时主动拆页，宁可多几页也不要挤在一页
