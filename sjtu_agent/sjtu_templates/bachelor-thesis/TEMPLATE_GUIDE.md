# SJTU 本科毕业论文模板

来源: https://github.com/sjtug/SJTUThesis

## 模板结构

- `main.tex` — 主文件，定义文档类型、章节顺序
- `setup.tex` — 格式设置（字体、页边距等）
- `texmf/` — 模板 class 文件 (`sjtuthesis.cls`)
- `contents/` — 各章节内容（摘要、正文、结论等）
- `refs.bib` — 参考文献 BibTeX 文件
- `figures/` — 图片目录

## AI 套用指引

Claude Code 读取用户文档后，应按以下规则填入模板：

1. **标题**: 从用户文档第一行或文件名提取，写入 `main.tex` 的 `\title{}` 命令
2. **摘要**: 用户文档的开头段落 → `contents/abstract.tex`
3. **关键词**: 从用户文档提取 → `setup.tex` 的 `\keywords{}`
4. **正文**: 用户文档的主体内容 → 按章节拆分为 `contents/chapter_*.tex`
5. **参考文献**: 提取用户文档的引用 → `refs.bib`
