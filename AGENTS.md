# 仓库维护约定

- `books/` 下的 17 个“`书名 - 数理化自学丛书编委会.md`”文件是规范正文源；`raw/` 只保存17册原始扫描 PDF 及哈希，根目录不放置单册电子书文件。
- 图片统一存放于 `books/assets/`；移动或重命名资源时必须同步更新全部 Markdown 引用。
- 疑似习题标题、练习标题、页眉或纯文字卡片的图片不得直接删除；必须遵循 `docs/IMAGE_TEXT_CARD_WORKFLOW.md`，先做只读清单、长上下文和原PDF版面核对。
- 书名是每册唯一 H1；章节从 H2 开始。
- 不凭空补写原扫描缺失内容；证据不足时保留透明校注。
- 公式内容不得经过会破坏 LaTeX 的全角标点替换。
- 批量修改前先在仓库外建立备份；不得执行破坏工作区状态的 Git 操作。
- 修改 `raw/` 后必须更新 `raw/SHA256SUMS.txt` 并运行 `make pdf-audit`。
- 提交前运行 `make audit` 和 `make privacy`；推送前运行 `make pre-push`。
- 修改构建链后运行 `make all` 和 `make verify`。
- `dist/`、`.build/` 和动态 JSON 报告属于生成物，不纳入版本控制。
