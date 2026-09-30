# DevDeck 网页标签图标

由 Stitch MCP 生成，采用深蓝圆角底座、薄荷绿字母 D 和终端提示符。

- [Stitch 设计项目](https://stitch.withgoogle.com/projects/14559378969948006471)
- 原始设计稿：`stitch.html`
- 原始预览：`stitch-preview.png`
- 矢量图标：`../../devdeck/favicon.svg`，viewBox 为 64 × 64

图标沿用 DevDeck 的 `#0c1018` 和 `#a9e4b3`，展示稿包含
16、32、64 像素及浅色、深色标签栏预览。

网页通过内嵌 SVG data URI 加载图标，适用于现有 HTTP 服务。
页面左上角与空项目提示复用同一标签页图标，保持视觉一致。
更新图标时应同步更新 SVG 文件和 `devdeck/index.html` 中的图标链接。
