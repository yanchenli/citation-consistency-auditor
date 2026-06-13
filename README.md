# Citation Consistency Auditor — 学术论文引用一致性审计工具

> Version 1.0

自动检测学术论文中 **「有引无文」**（正文引用了但参考文献缺失）和 **「有文无引」**（参考文献存在但正文未引用）两类引用一致性问题。

## 功能特性

- 支持中英文混合论文
- 自动分割正文/参考文献区域
- 6 种引用模式（英文括号/叙述、中文括号/叙述、方括号、分号分隔）
- 多词姓氏支持（Van Doren, De Waal 等）
- 中文姓后缀匹配（「陽丹」→「歐陽丹」）
- 模糊匹配（Levenshtein 编辑距离，英文 0.80 / 中文 0.65）
- 跨年作者预警（自动发现引用年份笔误）
- 疑似重复文献检测
- AI 辅助分析（可选，需 LM Studio）
- 标准化引用导出
- HTML / 文本双格式报告

## 环境要求

- Python 3.8+
- PyMuPDF（`pip install PyMuPDF`）

## 快速开始

```bash
pip install PyMuPDF
python citation_audit_v1.0.py 论文.pdf
```

运行后自动在同目录生成 `论文.html` 报告。

## 使用文档

详细使用手册请参阅 `citation_audit_v1.0_使用手册.docx`。

## 命令行选项

| 选项 | 说明 |
|------|------|
| `论文.pdf` | 待检查的 PDF 文件（必填） |
| `-o FILE` | 额外保存文本报告 |
| `-v` | 详细模式（打印引用/参考文献清单） |
| `--fuzzy` | 启用模糊匹配 |
| `--std-cite` | 导出标准化引用对比文件 |
| `--ai` | AI 辅助分析（需 LM Studio） |
| `--ai-full` | AI 全量模式（逐项验证） |

## 文件说明

- `citation_audit_v1.0.py` — 主程序
- `citation_ai_v1_0.py` — AI 辅助分析模块（可选）
- `citation_audit_v1.0_使用手册.docx` — 使用手册

## 许可

仅供学术用途。
