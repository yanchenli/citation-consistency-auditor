# Citation Consistency Auditor — 学术论文引用一致性审计工具

> Version 1.2

自动检测学术论文中 **「有引无文」**（正文引用了但参考文献缺失）和 **「有文无引」**（参考文献存在但正文未引用）两类引用一致性问题。

## 版本演进 / Version History

| 版本 | 有文无引 | 主要修复 |
|------|---------|---------|
| v1.0 | 86 | 基线 / Baseline |
| v1.1 | 83 | 幽灵单作者过滤、作者数量匹配、跨年预警清理 |
| **v1.2** | **56** | 分号逗号归一化、Latin 扩展字符集、中文分号/叙述匹配、HTML 报告增强 |

*(以上数据基于同一篇中文硕士论文的测试结果 / Tested on the same Chinese master's thesis)*

---

## 中文文档

### 功能特性

- 支持中英文混合论文
- 自动分割正文/参考文献区域
- 7 种引用模式（英文括号/叙述、中文括号/叙述、方括号、分号分隔、中文叙事、中文分号、小写前缀）
- Latin 扩展字符集支持（Bošnjaković, Grühn, Jöhnk, Yalçın 等带附加符姓名）
- 多词姓氏支持（Van Doren, De Waal, de Obesso 等）
- 中文姓后缀匹配（「陽丹」→「歐陽丹」）
- 模糊匹配（Levenshtein 编辑距离，英文 0.80 / 中文 0.65）
- 作者数量严格匹配（单↔单、双↔双、multi↔3+）
- 跨年作者预警（自动发现引用年份笔误）
- 年份不匹配检测（含详细 HTML 说明）
- 疑似重复文献检测
- AI 辅助分析（可选，需 LM Studio）
- 标准化引用导出
- HTML / 文本双格式报告
- 有文无引条目展示参考文献原文 + APA 引用建议

### 环境要求

- Python 3.8+
- PyMuPDF（`pip install PyMuPDF`）

### 快速开始

```bash
pip install PyMuPDF
python citation_audit_v1.2.py 论文.pdf
```

运行后自动在同目录生成 `论文.html` 报告。

### 命令行选项

| 选项 | 说明 |
|------|------|
| `论文.pdf` | 待检查的 PDF 文件（必填） |
| `-o FILE` | 额外保存文本报告 |
| `-v` | 详细模式（打印引用/参考文献清单） |
| `--fuzzy` | 启用模糊匹配 |
| `--std-cite` | 导出标准化引用对比文件 |
| `--ai` | AI 辅助分析（需 LM Studio） |
| `--ai-full` | AI 全量模式（逐项验证） |

### 文件说明

- `citation_audit_v1.2.py` — 主程序（最新版）
- `citation_audit_v1.1.py` — 主程序 v1.1
- `citation_audit_v1.0.py` — 主程序 v1.0（稳定版）
- `citation_ai_v1_2.py` — AI 辅助分析模块 v1.2
- `citation_audit_v1.0_使用手册.docx` — 使用手册

---

## English Documentation

### Features

- Supports Chinese-English mixed papers
- Auto-separates body text and reference section
- 7 citation patterns (English parenthetical/narrative, Chinese parenthetical/narrative, brackets, semicolon, Chinese narrative, Chinese semicolon, lowercase prefix)
- Latin extended character support (Bošnjaković, Grühn, Jöhnk, Yalçın, etc.)
- Multi-word surname support (Van Doren, De Waal, de Obesso, etc.)
- Chinese surname suffix matching
- Fuzzy matching (Levenshtein distance, English 0.80 / Chinese 0.65)
- Strict author count matching (single↔single, dual↔dual, multi↔3+)
- Cross-year author warnings
- Year mismatch detection (with detailed HTML explanation)
- Duplicate reference detection
- AI-assisted analysis (optional, requires LM Studio)
- Standard citation export
- HTML / text dual-format report
- Uncited reference entries show raw reference text + APA citation suggestion

### Requirements

- Python 3.8+
- PyMuPDF (`pip install PyMuPDF`)

### Quick Start

```bash
pip install PyMuPDF
python citation_audit_v1.2.py paper.pdf
```

An HTML report is auto-generated in the same directory.

### Command-Line Options

| Option | Description |
|------|------|
| `paper.pdf` | PDF file to audit (required) |
| `-o FILE` | Save additional text report |
| `-v` | Verbose mode (print citation/reference lists) |
| `--fuzzy` | Enable fuzzy matching |
| `--std-cite` | Export standardized citation comparison files |
| `--ai` | AI-assisted analysis (requires LM Studio) |
| `--ai-full` | AI full mode (verify each mismatch item-by-item) |

### File Description

- `citation_audit_v1.2.py` — Main program (latest)
- `citation_audit_v1.1.py` — Main program v1.1
- `citation_audit_v1.0.py` — Main program v1.0 (stable)
- `citation_ai_v1_2.py` — AI analysis module v1.2
- `citation_audit_v1.0_使用手册.docx` — User manual (Chinese)

---

## 许可 / License

仅供学术用途 / For academic use only.
