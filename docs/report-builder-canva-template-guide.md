# Report Builder Canva 模板包规范

用于报告页“导入模板包”功能。目标：可变高度导出时背景不变形。

## 1. 模板包结构

压缩包（`.zip`）内至少包含 3 张图片：

- `top.png`：顶部装饰区（固定高度）
- `middle.png`：中间延展区（纵向平铺）
- `bottom.png`：底部装饰区（固定高度）

可选文件：

- `template.json`：模板元信息（推荐）

示例目录：

```text
my-template.zip
├─ top.png
├─ middle.png
├─ bottom.png
└─ template.json
```

## 2. template.json 示例

```json
{
  "name": "Canva-Blue-Clean",
  "top_height_px": 220,
  "bottom_height_px": 160,
  "content_padding_px": 72,
  "files": {
    "top": "top.png",
    "middle": "middle.png",
    "bottom": "bottom.png"
  }
}
```

## 3. Canva 设计建议

- 画布宽度建议与导出比例一致（A4 竖版视觉比例）
- `top` / `middle` / `bottom` 三图宽度保持一致
- `middle` 图上下边缘尽量无明显接缝，便于 `repeat-y` 平铺
- 避免在 `middle` 放置必须完整出现的 logo 或大标题
- 图片建议 `PNG`，单个模板包建议小于 `20MB`

## 4. 导入与验证

1. 报告页点击“导入模板包”
2. 图片模板切到“导入模板”
3. 选择一份长内容报告导出，检查：
- 顶部是否固定不拉伸
- 中段是否自然延展、无明显接缝
- 底部是否固定不拉伸
- 内容区边距是否合适（通过 `content_padding_px` 微调）

