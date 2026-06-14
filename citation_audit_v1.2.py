#!/usr/bin/env python3
"""
citation_audit_v1.2.py - 学术论文引用一致性审计工具 (Version 1.2)
Academic Paper Citation Consistency Auditor

检测两种问题:
  1. 有引无文 (Cited but Missing): 正文引用了但参考文献列表中不存在
  2. 有文无引 (Uncited Reference): 参考文献列表中存在但正文未引用

使用说明:
  python citation_audit.py <pdf文件路径> [-o 输出文件] [-v]

依赖: PyMuPDF (pip install PyMuPDF)
"""

import re
import sys
import time
import json
import math
import unicodedata
import argparse
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional

try:
    import fitz
except ImportError:
    print("错误: 需要安装 PyMuPDF。请运行: pip install PyMuPDF")
    sys.exit(1)


# ============================================================
#  数据模型 (Data Models per TDS §4)
# ============================================================

@dataclass
class CitationRecord:
    """一条正文引用记录"""
    raw_text: str
    author: str
    year: str
    citation_type: str      # A_eng_paren / B_eng_multi / C_eng_narr / D_zh_paren / E_zh_narr
    page: int
    confidence: float = 1.0

@dataclass
class ReferenceRecord:
    """一条参考文献记录"""
    raw_text: str
    first_author: str
    year: str
    all_authors: list = field(default_factory=list)
    title: str = ''
    language: str = ''       # 'en' / 'zh'
    confidence: float = 1.0
    entry_id: Optional[int] = None

@dataclass
class IssueRecord:
    """一条审计问题记录"""
    issue_type: str          # MISSING_REFERENCE / UNUSED_REFERENCE / YEAR_MISMATCH / AUTHOR_MISMATCH / DUPLICATE
    severity: str            # HIGH / MEDIUM / LOW
    description: str
    citation: Optional['CitationRecord'] = None
    reference: Optional['ReferenceRecord'] = None


# ============================================================
#  配置
# ============================================================

REFERENCE_HEADINGS = [
    "参考文献", "參考文獻", "References", "BIBLIOGRAPHY",
    "Bibliography", "WORKS CITED", "Works Cited", "REFERENCES",
    "参考文獻", "Reference", "reference"
]

NON_BODY_HEADINGS = [
    "附录", "附錄", "Appendix", "Appendices",
    "致谢", "致謝", "Acknowledgement", "Acknowledgements",
    "致謝詞", "后记", "後記", "Postscript",
    "个人简历", "個人簡歷", "作者简介", "作者簡介",
    "Resume", "Curriculum Vitae"
]


# ============================================================
#  引用风格检测 (Citation Style Detector  per TDS §7)
# ============================================================

def detect_citation_style(text: str) -> str:
    """
    自动检测论文主要引用风格。
    返回: 'APA' / 'HARVARD' / 'CHINESE' / 'NUMERIC' / 'MIXED'
    """
    scores = {'APA': 0, 'HARVARD': 0, 'CHINESE': 0, 'NUMERIC': 0}

    # Numeric: [1], [2,3], [1-3]
    numeric = len(re.findall(r'\[\d+(?:[,\-\s]\d+)*\]', text))
    if numeric > 3:
        scores['NUMERIC'] += numeric

    # APA: (Author, Year)  — 注意逗号
    apa = len(re.findall(r'[（\(][A-Z][a-z]+[^）\)]*[，,]\s*\d{4}[）\)]', text))
    scores['APA'] += apa

    # Harvard: (Author Year) — 无逗号
    harvard = len(re.findall(r'[（\(][A-Z][a-z]+(?:\s+et\s+al\.?)?\s+\d{4}[a-z]?[）\)]', text))
    # 排除 APA 已经算过的（APA 也有 Harvard 特征）
    harvard_only = harvard - apa
    if harvard_only > 0:
        scores['HARVARD'] += harvard_only

    # Chinese: 中文作者（年份）
    chinese = len(re.findall(r'[\u4e00-\u9fff]{2,4}\s*[（\(]\d{4}[）\)]', text))
    scores['CHINESE'] += chinese

    # Chinese paren: （作者，年份）
    chinese_paren = len(re.findall(r'[（\(][\u4e00-\u9fff][^）\)]*[，,]\s*\d{4}[）\)]', text))
    scores['CHINESE'] += chinese_paren

    best_style = max(scores, key=scores.get)
    best_score = scores[best_style]

    if best_score == 0:
        return 'UNKNOWN'

    # 如果两种风格都很显著，返回 MIXED
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) > 1 and sorted_scores[1] > best_score * 0.5:
        return 'MIXED'

    return best_style


# ============================================================
#  1. PDF文本提取
# ============================================================

def extract_pdf_text(pdf_path):
    """提取PDF所有页面的文本，返回 [(页码, 文本), ...] 和合并文本"""
    doc = fitz.open(str(pdf_path))
    pages = []
    raw_parts = []

    for i in range(len(doc)):
        text = doc[i].get_text()
        pages.append((i + 1, text))
        raw_parts.append(text)

    full_text = "\n".join(raw_parts)
    return pages, full_text


# ============================================================
#  2. 正文/参考文献段落分割
# ============================================================

def normalize_text(text):
    """标准化文本：统一换行"""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\u2019', "'").replace('\u2018', "'")
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def find_ref_section(full_text):
    """
    查找参考文献章节的起止位置。
    核心锚点: 寻找 [1]  开头的一行，向上回溯找到章节标题。

    返回 (body_text, ref_text)
    """
    text = normalize_text(full_text)

    # 策略A: 以 [1] 开头的行作为主要锚点
    m = re.search(r'\n\[1\]\s+', text)
    if m:
        ref_start = m.start() + 1  # 包括 [1] 前面的换行
        heading_pos = None
        # 向上回溯最多 500 字符找章节标题
        before = text[max(0, ref_start - 500):ref_start]
        for h in REFERENCE_HEADINGS:
            p = before.rfind(h)
            if p != -1:
                heading_pos = max(0, ref_start - 500) + p
                break
        if heading_pos is not None and heading_pos < ref_start:
            ref_start = heading_pos
    else:
        # 策略B: 通过章节标题查找
        ref_start = None
        for heading in REFERENCE_HEADINGS:
            pos = text.find('\n' + heading + '\n')
            if pos != -1:
                after = text[pos + len(heading) + 1:pos + len(heading) + 500]
                if re.search(r'\[\d+\]', after) or re.search(r'\(\d{4}\)', after) or re.search(r'（\d{4}）', after):
                    ref_start = pos
                    break
            # 也试试行尾匹配
            pos = text.find(heading + '\n')
            if pos != -1 and ref_start is None:
                after = text[pos + len(heading) + 1:pos + len(heading) + 500]
                if re.search(r'\[\d+\]', after):
                    ref_start = pos
                    break

    if ref_start is None:
        # 策略C: 搜索 References 等英文标题
        for heading in ["\nReferences\n", "\nreferences\n", "\nReference\n", "\nBIBLIOGRAPHY\n"]:
            pos = text.find(heading)
            if pos != -1:
                after = text[pos + len(heading):pos + len(heading) + 500]
                if re.search(r'[A-Z][a-z]+.*\d{4}', after):
                    ref_start = pos
                    break

    if ref_start is None:
        return text, ""

    body = text[:ref_start]
    ref_raw = text[ref_start:]

    # 去除附录等后续章节
    appendix_pos = len(ref_raw)
    for heading in NON_BODY_HEADINGS:
        for p in [ref_raw.find('\n' + heading + '\n'), ref_raw.find('\n' + heading)]:
            if p != -1 and p < appendix_pos and p > 5:
                appendix_pos = p
    ref = ref_raw[:appendix_pos]

    # 过滤参考文献区域的页眉
    ref = filter_ref_headers(ref)

    return body, ref


def filter_ref_headers(ref_text):
    """
    过滤参考文献区域的页眉/页码行。
    保留所有 [N] 条目和作者年份行，移除孤立标题/行号。
    """
    lines = ref_text.split('\n')
    result = []
    for line in lines:
        s = line.strip()
        # 跳过纯数字行（页码）
        if re.match(r'^\d+$', s) and len(s) < 5:
            continue
        # 跳过仅含标题的行（保留 [N] 行）
        if s in REFERENCE_HEADINGS:
            continue
        # 跳过过短的无意义行
        if len(s) < 3:
            continue
        # 跳过句子片段（如期刊名延续行）但不跳过以 [ 开头或含年份的行
        if s.startswith('['):
            result.append(line)
        elif re.search(r'[（\(]\d{4}[）\)]', s):
            result.append(line)
        else:
            # 可能是延续行，保留
            result.append(line)
    return '\n'.join(result)


# ============================================================
#  3. 正文引用提取
# ============================================================

def preprocess_body_text(text):
    """预处理正文文本，为引用提取做准备"""
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    text = text.replace('\u2019', "'").replace('\u2018', "'")
    text = re.sub(r'(\w)-\n(\w)', r'\1-\2', text)
    text = re.sub(r'(\w)\n(\w)', r'\1 \2', text)
    text = re.sub(r'\n', ' ', text)
    text = re.sub(r' +', ' ', text)
    return text


def _EN_PREFIX_RE():
    return re.compile(r'^(?:e\.g\.|i\.e\.|see|cf\.)\s*,?\s*', re.I)


def _extract_semicolon_citations(text, page_num=0):
    """从文本中提取分号分隔的多引用块（如 (Author1 2000; Author2, 2001)）。
    
    v1.2 提取为独立函数，便于跨页处理时复用。
    """
    citations = []
    _PREFIX_RE = _EN_PREFIX_RE()
    for m in re.finditer(r'[（\(]([^（）\(\)]+)[）\)]', text):
        inner = m.group(1).strip()
        if ';' not in inner and '；' not in inner:
            continue
        for part in re.split(r'[;；]', inner):
            part = _PREFIX_RE.sub('', part).strip()
            if not part:
                continue
            # v1.2 归一化 "et al.,2009" / "等,2023" → 加空格让年份正则可匹配
            part = re.sub(r'(et\s+al\.?|等)\s*[,，](\d{4})', r'\1 \2', part, flags=re.I)
            # v1.2: 泛化逗号归一化 — 分号块内 "Author, 2003" / "et al., 2009" → 空格+年份
            part = re.sub(r',\s*(\d{4})', r' \1', part)
            # v1.2: 修复中文姓名PDF断行空格 "謝 榮慧" → "謝榮慧"
            part = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', part)
            # 优先匹配 "Author and Author Year"（v1.1允许年份前一个逗号）
            m2 = re.match(
                r"([A-Z][A-Za-z\u00C0-\u024F'\-]+)\s+(?:and|&)\s+([A-Z][A-Za-z\u00C0-\u024F'\-]+)\s*[，,]?\s*(\d{4})\s*$", part
            )
            if m2:
                citations.append({
                    'raw': part, 'author': m2.group(1),
                    'year': m2.group(3), 'type': 'semicolon', 'page': page_num,
                    'author_count': 'dual', 'author2': m2.group(2)
                })
                continue
            # v1.2: 小写前缀双作者 "Preston & de Waal 2002"
            m2 = re.match(
                r"([A-Z][A-Za-z\u00C0-\u024F'\-]+)\s+(?:and|&)\s+([a-z\u00E0-\u024F][A-Za-z\u00C0-\u024F'\-]+\s+[A-Z][A-Za-z\u00C0-\u024F'\-]+)\s*[，,]?\s*(\d{4})\s*$", part
            )
            if m2:
                citations.append({
                    'raw': part, 'author': m2.group(1),
                    'year': m2.group(3), 'type': 'semicolon', 'page': page_num,
                    'author_count': 'dual', 'author2': m2.group(2)
                })
                continue
            # 再匹配 "Author Year" / "Author et al. Year"（支持2词姓如 "Du Toit"）
            m2 = re.match(
                r"([A-Z][A-Za-z\u00C0-\u024F'\-]+(?:\s+[A-Z][A-Za-z\u00C0-\u024F'\-]+)?(?:\s+et\s+al\.?)?)\s*(\d{4})\s*$", part
            )
            if m2:
                author = re.sub(r'\s+(et\s+al\.?|等)$', '', m2.group(1).strip(), flags=re.I).strip()
                # 过滤以 "The" 开头的非作者名
                if len(author.split()) >= 2 and author.split()[0].lower() == 'the':
                    continue
                has_etal = bool(re.search(r'\bet\s+al\.?', part, re.I)) or '等' in part
                citations.append({
                    'raw': part, 'author': author,
                    'year': m2.group(2), 'type': 'semicolon', 'page': page_num,
                    'author_count': 'multi' if has_etal else 'single',
                    'author2': None
                })
                continue
            # v1.2: 中文姓名匹配 — 处理分号块中的中引用如 "李艾霖等，2025"
            # 优先匹配有等号: "李艾霖等，2025" → multi
            m2 = re.match(r"([\u4e00-\u9fff]{2,4})等\s*[，,]?\s*(\d{4})\s*$", part)
            if m2:
                citations.append({
                    'raw': part, 'author': m2.group(1),
                    'year': m2.group(2), 'type': 'semicolon', 'page': page_num,
                    'author_count': 'multi', 'author2': None
                })
                continue
            # 匹配双作者(顿号分隔): "李艾霖、劉廣增，2025" → dual/multi
            m2 = re.match(r"([\u4e00-\u9fff]{2,4})、([\u4e00-\u9fff]{2,4})(?:、([\u4e00-\u9fff]{2,4}))?\s*[，,]\s*(\d{4})\s*$", part)
            if m2:
                has_third = m2.group(3) is not None
                citations.append({
                    'raw': part, 'author': m2.group(1),
                    'year': m2.group(4), 'type': 'semicolon', 'page': page_num,
                    'author_count': 'multi' if has_third else 'dual',
                    'author2': m2.group(2)
                })
                continue
            # 匹配单作者: "李艾霖，2025" → single
            m2 = re.match(r"([\u4e00-\u9fff]{2,4})\s*[，,]\s*(\d{4})\s*$", part)
            if m2:
                citations.append({
                    'raw': part, 'author': m2.group(1),
                    'year': m2.group(2), 'type': 'semicolon', 'page': page_num,
                    'author_count': 'single', 'author2': None
                })
                continue
    return citations


def extract_citations_from_text(text, page_num=0):
    """
    从文本中提取所有引用信息。
    返回 [{'raw':str, 'author':str, 'year':str, 'type':str, 'page':int}]
    已去重(同一页)。
    """
    text = preprocess_body_text(text)
    citations = []

    def clean_author(name):
        """去除 et al. / 等 后缀"""
        return re.sub(r'\s+(et\s+al\.?|等)$', '', name.strip(), flags=re.I).strip()

    def _has_etal(raw):
        return bool(re.search(r'\bet\s+al\.?', raw, re.I)) or '等' in raw

    # --- 模式0: 分号分隔多引用展开 (Author1 Year1; Author2 Year2; ...) ---
    # 处理 (Erdem 1996; Novak 1993; Shugan 1987) 及 (e.g., Erdem 1996; ...)
    citations.extend(_extract_semicolon_citations(text, page_num))

    # --- 模式A: 英文夹注 (Author, Year) / (Author Year) / (Author et al., Year) ---
    # 逗号已改为可选；支持2词姓如 "Du Toit"
    for m in re.finditer(
        r"[（\(]\s*([A-Z][A-Za-z\u00C0-\u024F'\-]+(?:\s+[A-Z][A-Za-z\u00C0-\u024F'\-]+)?(?:\s+et\s+al\.?)?)\s*[，,]?\s*(\d{4})\s*[）\)]",
        text
    ):
        raw = m.group()
        author = clean_author(m.group(1))
        # 过滤以 "The" 开头的非作者名（如 "The Style Journal"）
        if len(author.split()) >= 2 and author.split()[0].lower() == 'the':
            continue
        is_multi = _has_etal(raw)
        citations.append({
            'raw': raw, 'author': author,
            'year': m.group(2), 'type': 'A_eng_paren', 'page': page_num,
            'author_count': 'multi' if is_multi else 'single', 'author2': None
        })

    # 模式A-宽松 (v1.2): 年份后紧跟中文正文再反括号
    # 处理 "（Etkin et al., 2011 的共情相關區域）" — 年份不贴后括号
    for m in re.finditer(
        r"[（\(]\s*([A-Z][A-Za-z\u00C0-\u024F'\-]+(?:\s+[A-Z][A-Za-z\u00C0-\u024F'\-]+)?(?:\s+et\s+al\.?)?)\s*[，,]?\s*(\d{4})[\u4e00-\u9fff][^）\)]{0,50}[）\)]",
        text
    ):
        raw = m.group()
        author = clean_author(m.group(1))
        if len(author.split()) >= 2 and author.split()[0].lower() == 'the':
            continue
        is_multi = _has_etal(raw)
        citations.append({
            'raw': raw, 'author': author,
            'year': m.group(2), 'type': 'A_eng_paren', 'page': page_num,
            'author_count': 'multi' if is_multi else 'single', 'author2': None
        })

    # 模式A-小写: 处理 (de Waal, 2002) — 小写前缀+大写姓
    for m in re.finditer(
        r"[（\(]\s*([a-z\u00E0-\u024F][A-Za-z\u00C0-\u024F'\-]+\s+[A-Z][A-Za-z\u00C0-\u024F'\-]+(?:\s+[A-Z][A-Za-z\u00C0-\u024F'\-]+)?(?:\s+et\s+al\.?)?)\s*[，,]?\s*(\d{4})\s*[）\)]",
        text
    ):
        raw = m.group()
        author = clean_author(m.group(1))
        # 过滤以 "The" 开头的非作者名
        if len(author.split()) >= 2 and author.split()[0].lower() == 'the':
            continue
        is_multi = _has_etal(raw)
        citations.append({
            'raw': raw, 'author': author,
            'year': m.group(2), 'type': 'A_eng_paren', 'page': page_num,
            'author_count': 'multi' if is_multi else 'single', 'author2': None
        })

    # --- 模式B: 英文夹注 (Author & Author, Year) / (Author & Author Year) ---
    # 支持2词姓如 "Du Toit & Bernal"
    for m in re.finditer(
        r"[（\(]\s*([A-Z][A-Za-z\u00C0-\u024F'\-]+(?:\s+[A-Z][A-Za-z\u00C0-\u024F'\-]+)?(?:\s*[,&]\s*[A-Z][A-Za-z\u00C0-\u024F'\-]+(?:\s+[A-Z][A-Za-z\u00C0-\u024F'\-]+)?)*)\s*[，,]?\s*(\d{4})\s*[）\)]",
        text
    ):
        raw = m.group()
        parts = [p.strip() for p in re.split(r'[,&]\s*', m.group(1)) if p.strip()]
        first = clean_author(parts[0])
        count = 'multi' if len(parts) >= 3 else ('dual' if len(parts) == 2 else 'single')
        author2 = clean_author(parts[1]) if len(parts) >= 2 else None
        citations.append({
            'raw': raw, 'author': first,
            'year': m.group(2), 'type': 'B_eng_multi', 'page': page_num,
            'author_count': count, 'author2': author2
        })

    # --- 模式C1: 英文叙述双作者 Author1 and Author2 (Year) / ... & ... (Year) ---
    # 注意: 叙事模式没有括号锚定，多词姓会误捕获机构名，仅支持单词姓
    for m in re.finditer(
        r"(?<![A-Za-z])([A-Z][A-Za-z\u00C0-\u024F'\-]+)\s+(?:and|&)\s+([A-Z][A-Za-z\u00C0-\u024F'\-]+)\s*[（\(]\s*(\d{4})\s*[）\)]",
        text
    ):
        citations.append({
            'raw': m.group(), 'author': m.group(1),
            'year': m.group(3), 'type': 'C1_eng_narr_dual', 'page': page_num,
            'author_count': 'dual', 'author2': m.group(2)
        })

    # --- 模式C: 英文叙述 Author (Year) / Author et al. (Year)  ---
    # 注意: 叙事模式没有括号锚定，多词姓会误捕获机构名，仅支持单词姓
    for m in re.finditer(
        r"(?<![A-Za-z])([A-Z][A-Za-z\u00C0-\u024F'\-]+(?:\s+et\s+al\.?)?)\s*[（\(]\s*(\d{4})\s*[）\)]",
        text
    ):
        raw = m.group()
        author = clean_author(m.group(1))
        if author.lower() in ('figure', 'table', 'equation', 'chapter', 'section', 'see'):
            continue
        citations.append({
            'raw': raw, 'author': author,
            'year': m.group(2), 'type': 'C_eng_narr', 'page': page_num,
            'author_count': 'multi' if _has_etal(raw) else 'single', 'author2': None
        })

    # --- 模式F: 英文方括号引用 Author [Year] ---
    # 处理 (see the review by Shugan [2014]) 中的 Shugan [2014]
    for m in re.finditer(
        r"(?<![A-Za-z])([A-Z][A-Za-z\u00C0-\u024F'\-]+(?:\s+et\s+al\.?)?)\s*[\[【]\s*(\d{4})\s*[\]】]",
        text
    ):
        raw = m.group()
        author = clean_author(m.group(1))
        if author.lower() in ('figure', 'table', 'equation', 'chapter', 'section', 'see'):
            continue
        citations.append({
            'raw': raw, 'author': author,
            'year': m.group(2), 'type': 'F_eng_bracket', 'page': page_num,
            'author_count': 'multi' if _has_etal(raw) else 'single', 'author2': None
        })

    CONJ_FIRST = '与和及或同跟把被将与與將而'          # 首字过滤（连接词）
    CONJ_INNER = '与與和及或'                          # 内部分割用
    SUFFIX_BLOCK = ['學者', '学者', '等人', '等']
    PREFIX_BLOCK = sorted(['针对', '針對', '根據', '根据', '通過', '通过', '為了', '为了',
                            '關於', '关于', '對於', '对于', '按照', '依照',
                            '比如', '例如',
                            '參照', '参照',
                            '文中', '本文', '上文', '借鉴', '借鑒', '借鑑',
                            '參考', '参考', '採用', '采用', '如', '以'],
                           key=len, reverse=True)

    def _strip_prefix(name):
        while True:
            stripped = False
            for prefix in PREFIX_BLOCK:
                if name.startswith(prefix):
                    name = name[len(prefix):].strip()
                    stripped = True
                    break
            if not stripped:
                break
        return name

    def _strip_suffix(name):
        for suffix in SUFFIX_BLOCK:
            if name.endswith(suffix):
                return name[:-len(suffix)].strip()
        return name

    # --- 模式D: 中文夹注 （Author等，Year）/（Author等, Year）/（Author、Author，Year）---
    # 注意: 逗号在中文中必须保留，以避免非引用文本误匹配如（详见2021）
    for m in re.finditer(
        r'[（\(]\s*((?:[\u4e00-\u9fff]|[A-Za-z])+(?:[、，,](?:[\u4e00-\u9fff]|[A-Za-z])+)*(?:等)?)\s*[，,]\s*(\d{4})\s*[）\)]',
        text
    ):
        raw = m.group()
        author_text = m.group(1).strip()
        has_等 = '等' in author_text
        # 按等/、/, 分割
        parts = [p.strip() for p in re.split(r'[等、，,]', author_text) if p.strip()]
        # 去除前缀/后缀
        parts = [_strip_prefix(_strip_suffix(p)) for p in parts]
        first = parts[0] if parts else ''
        # 内部分割（与/和）
        for sep in CONJ_INNER:
            if sep in first:
                before = first.split(sep)[0].strip()
                after = first.split(sep, 1)[1].strip() if sep in first else ''
                first = before
                if after and len(parts) == 1:
                    parts = [before, after]
                break
        if not first or len(first) < 2:
            continue
        if has_等:
            count = 'multi'
            author2 = None
        elif len(parts) >= 3:
            count = 'multi'
            author2 = None
        elif len(parts) >= 2:
            count = 'dual'
            author2 = parts[1]
        else:
            count = 'single'
            author2 = None
        citations.append({
            'raw': raw, 'author': first,
            'year': m.group(2), 'type': 'D_zh_paren', 'page': page_num,
            'author_count': count, 'author2': author2
        })

    # --- 模式E: 中文叙述 "作者（年份）" / "作者等（年份）" / "作者与作者（年份）" ---
    # 允许作者与（之间有空格（常见于PDF提取）
    for m in re.finditer(
        r'(?:^|[\u3002\uff0c\uff1b\uff01\uff1f\u3001\uff1a\uff61\s,.;:!?])([\u4e00-\u9fff]{2,8})\s*[（\(]\s*(\d{4})\s*[）\)]',
        text
    ):
        raw = m.group()
        author = m.group(1).strip()
        year = m.group(2)
        author_clean = re.sub(r'[等]', '', author).strip()
        if not author_clean:
            continue
        # 首字过滤（纯连接词）
        if author_clean[0] in CONJ_FIRST:
            continue
        author_clean = _strip_prefix(author_clean)
        author_clean = _strip_suffix(author_clean)
        if not author_clean or len(author_clean) < 2:
            continue
        # 检测是否 etc. (等) → multi
        is_multi = '等' in author
        # 检测内部分割：处理"A与B（Year）" → 取A, 保存B
        author2 = None
        count = 'single'
        for sep in CONJ_INNER:
            if sep in author_clean:
                parts_split = author_clean.split(sep, 1)
                author_clean = parts_split[0].strip()
                if len(parts_split) > 1:
                    author2 = parts_split[1].strip()
                break
        if is_multi:
            count = 'multi'
            author2 = None
        elif author2:
            count = 'dual'
        if not author_clean or len(author_clean) < 2:
            continue
        citations.append({
            'raw': raw, 'author': author_clean,
            'year': year, 'type': 'E_zh_narr', 'page': page_num,
            'author_count': count, 'author2': author2
        })

    # 页内去重
    seen = set()
    unique = []
    for c in citations:
        key = (c['author'].lower().replace(' ', ''), c['year'])
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


# 繁→简对照（用于引用匹配时的归一化）
_TC2SC = str.maketrans({
    '張': '张', '劉': '刘', '陳': '陈', '楊': '杨', '趙': '赵',
    '黃': '黄', '吳': '吴', '李': '李', '王': '王', '馬': '马',
    '孫': '孙', '羅': '罗', '謝': '谢', '馮': '冯', '蕭': '萧',
    '韓': '韩', '關': '关', '嚴': '严', '鄭': '郑', '魏': '魏',
    '蔣': '蒋', '薑': '姜', '魯': '鲁', '萬': '万', '盧': '卢',
    '葉': '叶', '蘇': '苏', '潘': '潘', '範': '范', '譚': '谭',
    '賀': '贺', '湯': '汤', '溫': '温', '閆': '闫', '閻': '阎',
    '景': '景', '熊': '熊', '盛': '盛', '程': '程',
    '鳳': '凤', '軍': '军', '寧': '宁', '寶': '宝', '劍': '剑',
    '鋒': '锋', '強': '强', '勝': '胜', '計': '计', '劃': '划',
    '瑩': '莹', '穎': '颖', '潔': '洁', '暉': '晖', '麗': '丽',
    '龍': '龙', '淵': '渊', '偉': '伟', '傑': '杰', '華': '华',
    '銳': '锐', '琳': '琳', '濤': '涛', '斌': '斌',
})


def normalize_cn(name):
    """归一化中文名：繁转简 + lowercase + strip + 去除重音符号"""
    n = name.strip().lower()
    n = n.translate(_TC2SC)
    # 去除重音符号（如 é→e, ó→o）
    n = ''.join(c for c in unicodedata.normalize('NFD', n) if not unicodedata.combining(c))
    return n


def extract_all_citations(pages):
    """从所有页面提取引用（全局去重，含繁简归一）。
    
    v1.2 添加跨页分号引用提取——将全文拼接后重新运行分号模式，
    解决 (Author1 2000; Author2, 2001) 跨页丢失问题。
    """
    all_citations = []
    seen = set()
    for page_num, text in pages:
        for c in extract_citations_from_text(text, page_num):
            count = c.get('author_count', 'single')
            key = (normalize_cn(c['author']), c['year'], count)
            if key not in seen:
                seen.add(key)
                all_citations.append(c)
    
    # 跨页分号引用提取 (v1.2
    # 对每对相邻页面，拼接前一页末尾与后一页正文，捕获跨页分号块
    for i in range(len(pages) - 1):
        prev_text = pages[i][1]
        curr_text = pages[i + 1][1]
        overlap = prev_text[-500:] if len(prev_text) > 500 else prev_text
        stitched = preprocess_body_text(overlap + '\n' + curr_text)
        for c in _extract_semicolon_citations(stitched, page_num=pages[i+1][0]):
            count = c.get('author_count', 'single')
            key = (normalize_cn(c['author']), c['year'], count)
            if key not in seen:
                seen.add(key)
                all_citations.append(c)
    
    # v1.2 跨页中文叙述引用提取 (Pattern E)
    # 解决 "。李薇等（2022）" 跨页导致 `。` 在上一页丢失的问题
    CONJ_FIRST_E = '与和及或同跟把被将与與將而'
    CONJ_INNER_E = '与與和及或'
    PREFIX_BLOCK_E = sorted(['针对', '針對', '根據', '根据', '通過', '通过', '為了', '为了',
                              '關於', '关于', '對於', '对于', '按照', '依照',
                              '比如', '例如', '參照', '参照',
                              '文中', '本文', '上文', '借鉴', '借鑒', '借鑑',
                              '參考', '参考', '採用', '采用', '如', '以'],
                             key=len, reverse=True)
    SUFFIX_BLOCK_E = ['學者', '学者', '等人', '等']
    for i in range(len(pages) - 1):
        prev_text = pages[i][1]
        curr_text = pages[i + 1][1]
        overlap = prev_text[-500:] if len(prev_text) > 500 else prev_text
        stitched = preprocess_body_text(overlap + '\n' + curr_text)
        for m in re.finditer(
            r'(?:^|[\u3002\uff0c\uff1b\uff01\uff1f\u3001\uff1a\uff61\s,.;:!?])([\u4e00-\u9fff]{2,8})\s*[（\(]\s*(\d{4})\s*[）\)]',
            stitched
        ):
            author = m.group(1).strip()
            year = m.group(2)
            author_clean = re.sub(r'[等]', '', author).strip()
            if not author_clean or author_clean[0] in CONJ_FIRST_E:
                continue
            # strip prefix
            for prefix in PREFIX_BLOCK_E:
                if author_clean.startswith(prefix):
                    author_clean = author_clean[len(prefix):].strip()
                    break
            # strip suffix
            for suffix in SUFFIX_BLOCK_E:
                if author_clean.endswith(suffix):
                    author_clean = author_clean[:-len(suffix)].strip()
                    break
            if not author_clean or len(author_clean) < 2:
                continue
            is_multi = '等' in author
            author2 = None
            count = 'single'
            for sep in CONJ_INNER_E:
                if sep in author_clean:
                    parts = author_clean.split(sep, 1)
                    author_clean = parts[0].strip()
                    if len(parts) > 1:
                        author2 = parts[1].strip()
                    break
            if is_multi:
                count = 'multi'
            elif author2:
                count = 'dual'
            if not author_clean or len(author_clean) < 2:
                continue
            key = (normalize_cn(author_clean), year, count)
            if key not in seen:
                seen.add(key)
                all_citations.append({
                    'raw': m.group(), 'author': author_clean,
                    'year': year, 'type': 'E_zh_narr', 'page': pages[i+1][0],
                    'author_count': count, 'author2': author2
                })
    
    # v1.2 过滤单作者幽灵引用——当某单作者引用同时是另一条C1叙事双作者引用的
    # 第二作者（同年、同页或邻页），判定为叙事模式中误提取的第二作者，予以删除
    dual_c1_pages = {}  # (author2_norm, year) -> set of pages
    for c in all_citations:
        if c.get('type') == 'C1_eng_narr_dual' and c.get('author2'):
            key = (normalize_cn(c['author2']), c['year'])
            if key not in dual_c1_pages:
                dual_c1_pages[key] = set()
            dual_c1_pages[key].add(c.get('page', 0))
    
    filtered = []
    for c in all_citations:
        if c.get('author_count') == 'single':
            key = (normalize_cn(c['author']), c['year'])
            c_page = c.get('page', 0)
            if key in dual_c1_pages:
                # 检查是否同页或邻页
                dual_pages = dual_c1_pages[key]
                if any(abs(c_page - dp) <= 1 for dp in dual_pages):
                    continue  # 弃用——此单作者是C1叙事双作者中的第二作者
        filtered.append(c)
    all_citations = filtered
    
    return all_citations


# ============================================================
#  4. 参考文献解析
# ============================================================

def parse_reference_entries(ref_text, unparsed_out=None):
    """
    解析参考文献列表。

    参数:
      ref_text: 参考文献章节文本
      unparsed_out: 可选 list，用于收集规则引擎无法解析的原始文本块

    返回 [{'id':int/None, 'raw':str, 'first_author':str, 'year':str, 'all_authors':[str]}]
    """
    if not ref_text or len(ref_text) < 20:
        return []

    # 清理控制字符（防止 \x13 等破坏 APA 作者名检测正则）
    ref_text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', ref_text)

    # 按编号 [N] 拆分
    # 先统一格式: 确保 [N 后面有 ]
    ref_text = re.sub(r'\[(\d+)\s', r'[\1] ', ref_text)

    entries = []
    parts = re.split(r'\n\s*(?=\[\d+\])', ref_text)
    if len(parts) <= 1:
        parts = re.split(r'(?=\[\d+\])', ref_text)

    for part in parts:
        part = part.strip()
        if not part or len(part) < 10:
            continue

        m = re.match(r'\[(\d+)\]\s*(.*)', part, re.DOTALL)
        if m:
            entry_id = int(m.group(1))
            content = m.group(2).strip()
            parsed = parse_single_reference(content, entry_id)
            if parsed:
                entries.append(parsed)
            elif unparsed_out is not None:
                unparsed_out.append(part[:300])
        else:
            parsed = parse_single_reference(part, None)
            if parsed:
                entries.append(parsed)
            elif unparsed_out is not None:
                unparsed_out.append(part[:300])

    # 如果按编号拆分结果太少，尝试按段拆分（无编号格式）
    if len(entries) < 3:
        entries = []
        blocks = re.split(r'\n\n+', ref_text)
        for block in blocks:
            block = block.strip()
            if len(block) < 20:
                continue
            parsed = parse_single_reference(block, None)
            if parsed:
                entries.append(parsed)
            elif unparsed_out is not None:
                unparsed_out.append(block[:300])

    # 仍然太少 → 尝试 APA 无编号格式（检测作者行开头）
    if len(entries) < 3:
        entries = []
        lines = ref_text.split('\n')
        # 预合并：PDF断裂的单姓行（如 "Kalwani,\nManohar"）→ "Kalwani, Manohar"
        _SHORT_WORDS = {'and', 'in', 'of', 'for', 'the', 'to', 'a', 'an',
                        'on', 'at', 'by', 'or', 'is', 'as', 'its', 'it',
                        'with', 'from', 'into', 'upon', 'not', 'but'}
        merged = []
        was_merged = set()
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if not s:
                merged.append('')
                i += 1
                continue
            if s in REFERENCE_HEADINGS or re.match(r'^\d+$', s):
                merged.append(s)
                i += 1
                continue
            # 若本行为 "Surname," 且为行尾（< 20 字符，PDF断行），则与下一行合并
            # 支持重音字母 (Gómez, Grühn) 及下一行为 (Year) 的情形
            _LATIN = r'A-Za-z\u00C0-\u024F'
            if len(s) < 20 and re.match(r'^[' + _LATIN + r'][' + _LATIN + r'\-]+,\s*$', s) and i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and (re.match(r'^[' + _LATIN + r']', nxt) or re.match(r'^[（(]\d{4}', nxt)):
                    merged.append(s + ' ' + nxt)
                    was_merged.add(len(merged) - 1)
                    i += 2
                    continue
            merged.append(s)
            i += 1

        ref_blocks = []
        cur = []
        for idx, line in enumerate(merged):
            s = line.strip()
            if not s:
                continue
            if s in REFERENCE_HEADINGS or re.match(r'^\d+$', s):
                continue
            if re.match(r'^[一二三四五六七八九十]、', s):
                continue
            ref_start = False
            # 被预合并的行：仅当前一个block已含年份时才作为新条目起始
            # （避免将同一参考文献的后续行误判为新条目）
            # v1.2 若前一行以句号结尾，也视为条目边界（PDF连续文本中常见）
            if idx in was_merged:
                cur_text = '\n'.join(cur)
                # cur 为空（当前条目的首行）→ 直接作为新条目；否则仅 cur 含年份时才为新条目
                if not cur or re.search(r'[（(]\s*\d{4}\s*[）)]', cur_text):
                    ref_start = True
                elif cur:
                    prev_line = cur[-1].strip()
                    # 仅当前一行以数字+句号结尾（如页码 "20, 35-50."）才视为条目边界
                    # 避免正文句结尾 "conclusion." 误分裂参考文献块
                    if re.search(r'\d\s*[.。]\s*$', prev_line):
                        ref_start = True
            elif len(s) >= 20 and not re.match(r'^(?:Journal|International|Proceedings|Advances|Annual|British|American)\b', s):
                # 英文：Surname, I. 或 Surname, FullName（含 "Van der Maaten" 等多词姓）
                # 支持重音字母（Gómez, Grühn）
                _LATIN = r'A-Za-z\u00C0-\u024F'
                if re.match(r'^[' + _LATIN + r'][' + _LATIN + r'\-]*(?:\s+[' + _LATIN + r'a-z][' + _LATIN + r'\-]*)*,\s', s):
                    after_first_comma = s.split(',', 1)[1].strip() if ',' in s else ''
                    # 逗号后以小写开头（如 "and Daniel..."）→ 续行，非新条目
                    if not re.match(r'^\d+', after_first_comma) and not re.match(r'^[a-z]', after_first_comma):
                        ref_start = True
                # 中文：2-4汉字开头，允许中英混排（如 "朱振中、房曉芸、Chen,H.A.(2024)"）
                elif re.match(r'^[\u4e00-\u9fff]{2,4}(?:[、，,][\u4e00-\u9fff]{2,4})*.*?[（(]\d{4}', s):
                    ref_start = True
                # 英文机构名（无逗号）：Brand Vision Insights (2024), The Style Journal (2019)
                # 仅在上一行以年份/页码结尾时才匹配（避免将续行的作者名误判）
                elif re.match(r'^[A-Za-z][A-Za-z\s]+\s*[（(]\d{4}', s):
                    if idx > 0:
                        prev = merged[idx - 1].strip()
                        if re.search(r'(?:\b\d{4}|[\d][–-]\d+)\.?\s*$', prev):
                            ref_start = True
            if ref_start and cur:
                ref_blocks.append('\n'.join(cur))
                cur = [s]
            else:
                cur.append(s)
        if cur:
            ref_blocks.append('\n'.join(cur))
        for block in ref_blocks:
            if len(block) < 20:
                continue
            parsed = parse_single_reference(block, None)
            if parsed:
                entries.append(parsed)
            elif unparsed_out is not None:
                unparsed_out.append(block[:300])

    return entries


def parse_single_reference(text, entry_id=None):
    """
    解析单条参考文献，提取作者和年份。
    返回 dict 或 None。
    """
    if not text or len(text) < 10:
        return None

    # 跳过标题行
    if text in REFERENCE_HEADINGS:
        return None

    # 提取年份（支持中英文标点混用，及括号内外空格）
    year_match = re.search(r'[（\(]\s*(\d{4})\s*[）\)]', text)
    if not year_match:
        year_match = re.search(r'[.,，；;]\s*(\d{4})\s*[.,，：:;\s]', text)
    if not year_match:
        year_match = re.search(r'(\d{4})\s*[.，。：:；;]', text)

    if not year_match:
        return None

    year = year_match.group(1)
    if int(year) < 1900 or int(year) > 2030:
        return None

    # 提取作者部分：年份之前的文本
    author_text = text[:year_match.start()].strip()
    author_text = re.sub(r'^\[\d+\]\s*', '', author_text).strip()
    author_text = re.sub(r'^(?:[A-Z]\.\s*)+', '', author_text).strip()

    first_author = extract_first_author(author_text)
    all_authors = extract_all_authors(author_text)
    has_et_al = bool(re.search(r'\bet\s+al\.?', author_text))

    if not first_author:
        return None

    # 过滤明显不是作者名的英文单词
    _NON_AUTHORS = {'Education', 'Science', 'Management', 'Research', 'Journal',
                    'International', 'Proceedings', 'Conference', 'Review',
                    'Psychology', 'Computers', 'Information', 'Decision',
                    'Emotion', 'Personality', 'Advances', 'Behaviour',
                    'Contemporary', 'Current', 'Frontiers', 'Future',
                    'Learning', 'New', 'Online', 'Social', 'British',
                    'American', 'European', 'Asian', 'African', 'Australian'}
    if first_author in _NON_AUTHORS or first_author.capitalize() in _NON_AUTHORS:
        return None

    return {
        'id': entry_id,
        'raw': text[:200],
        'first_author': first_author,
        'year': year,
        'all_authors': all_authors,
        'has_et_al': has_et_al
    }


def extract_first_author(author_text):
    """从作者文本中提取第一作者姓氏"""
    text = author_text.strip().rstrip('. ').strip()
    if not text:
        return None

    # 中文作者（包含汉字）
    if re.search(r'[\u4e00-\u9fff]', text):
        text = re.sub(r'[,，;；]\s*$', '', text)
        author = text.split('、')[0].split('；')[0].split(';')[0].split('，')[0].split(',')[0].strip()
        author = re.sub(r'\s*等\s*$', '', author).strip()
        return author if author and len(author) >= 2 else None

    # 英文作者
    text = re.sub(r'[,，]\s*$', '', text)
    first_segment = re.split(r'[,，&]\s*', text)[0].strip()
    first_segment = re.sub(r'\s+et\s+al\.?\s*$', '', first_segment).strip()
    if not first_segment:
        return None

    if ',' in first_segment:
        surname = first_segment.split(',')[0].strip()
    else:
        parts = first_segment.split()
        # 无逗号 + 多词 → 机构名，返回完整名称
        if len(parts) >= 2:
            return first_segment
        # 从后往前取第一个非缩写词作为姓氏
        surname = None
        for p in reversed(parts):
            p_clean = p.replace('.', '').strip()
            if not re.match(r'^[A-Z]+$', p_clean):
                surname = p
                break
        if surname is None:
            return None

    surname = surname.strip().strip(',.').strip()
    return surname if surname else None


def extract_all_authors(author_text):
    """提取所有作者姓氏"""
    text = author_text.strip().rstrip('. ').strip()
    if not text:
        return []

    authors = []

    if re.search(r'[\u4e00-\u9fff]', text):
        parts = re.split(r'[、，,；;]', text)
        for p in parts:
            p = p.strip()
            p = re.sub(r'\s*等\s*$', '', p).strip()
            if p and len(p) >= 2:
                authors.append(p)
        return authors

    text = re.sub(r',?\s*et\s+al\.?\s*', '', text)
    # 支持 and / & / ; 作为作者分隔符
    parts = re.split(r'\s+and\s+|\s+&\s+|；|;', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if ',' in part or '，' in part:
            # APA 格式 "Surname, FirstName, Surname2, FirstName2"
            # 取偶数位段（0, 2, 4...）作为姓氏
            segs = [s.strip() for s in re.split(r'[，,]', part) if s.strip()]
            for i, seg in enumerate(segs):
                if i % 2 != 0:
                    continue
                seg_clean = seg.strip().strip(',.，；;').strip()
                if not seg_clean:
                    continue
                # 第一个姓氏段（i==0）保持完整（如 "Du Toit" 是多词姓）
                # 后续姓氏段（i>=2）取末词（处理 "Joshua Gans" 等 PDF 断裂格式）
                words = seg_clean.split()
                if i == 0 and len(words) >= 2:
                    authors.append(seg_clean)
                elif len(words) >= 2:
                    for p in reversed(words):
                        p_clean = p.replace('.', '').strip()
                        if not re.match(r'^[A-Z]+$', p_clean):
                            authors.append(p)
                            break
                else:
                    authors.append(seg_clean)
        else:
            # "FirstName LastName" → 取最后一个非缩写词
            words = part.split()
            surname = None
            s_idx = None
            for i, p in enumerate(reversed(words)):
                p_clean = p.replace('.', '').strip()
                if not re.match(r'^[A-Z]+$', p_clean):
                    surname = p
                    s_idx = len(words) - 1 - i
                    break
            if surname is None:
                continue
            surname = surname.strip().strip(',.，；;').strip()
            if surname:
                authors.append(surname)
                # v1.2: 若姓氏前有小写前缀 (de, van 等)，额外补全前缀版本
                if s_idx is not None and s_idx > 0:
                    prev_word = words[s_idx - 1]
                    if re.match(r'^[a-z]+$', prev_word):
                        authors.append(prev_word + ' ' + surname)

    return authors


# ============================================================
#  5. 双向匹配 (含繁简归一化 + 多作者规则)
# ============================================================

def citation_matches_ref(citation, ref):
    """
    根据作者数量判断正文引用是否匹配参考文献。

    规则:
      single:  引用1位作者 → 匹配参考文献第一作者
      dual:    引用2位作者 → 匹配参考文献前两位作者（次序一致）
      multi:   引用 N 位作者(et al.) → 匹配参考文献第一作者 + 参考文献有 3+ 位作者
    """

    def _name_matches(cite_name, ref_name):
        """作者名匹配：精确匹配或连字号前缀匹配（Rodriguez → Rodriguez-Marciel）"""
        if cite_name == ref_name:
            return True
        # 连字号前缀匹配：cite_name 是 ref_name 的连字号前段
        parts = ref_name.split('-')
        return len(parts) > 1 and cite_name == parts[0]

    def _name_in_list(cite_name, name_list):
        """检查 cite_name 是否匹配 name_list 中的任一名字"""
        return any(_name_matches(cite_name, n) for n in name_list)

    count = citation.get('author_count', 'single')
    cite_a1 = normalize_cn(citation['author'])
    ref_a1 = normalize_cn(ref['first_author'])
    ref_all = [normalize_cn(a) for a in ref.get('all_authors', [])]

    if count == 'dual':
        cite_a2 = normalize_cn(citation.get('author2', ''))
        if not cite_a2 or not ref_all:
            return False
        return (_name_matches(cite_a1, ref_a1) and
                len(ref_all) >= 2 and
                _name_matches(cite_a2, ref_all[1]))
    if count == 'multi':
        return _name_matches(cite_a1, ref_a1) and (len(ref_all) >= 3 or ref.get('has_et_al'))
    else:
        return _name_matches(cite_a1, ref_a1) or _name_in_list(cite_a1, ref_all)


def build_ref_index(ref_entries):
    """构建参考文献索引 {(author_norm, year): [ref, ...]}"""
    index = defaultdict(list)
    for ref in ref_entries:
        key = (normalize_cn(ref['first_author']), ref['year'])
        index[key].append(ref)
        for author in ref['all_authors']:
            alt = (normalize_cn(author), ref['year'])
            if alt != key:
                index[alt].append(ref)
    return index


def match_citations(citations, ref_entries, ref_index):
    """
    双向匹配（多作者感知），找出有引无文和有文无引。

    对每条引用:
      - single:  key = (author, year) → 查 ref_index
      - dual:    (author1, author2, year) → 遍历 ref_entries
      - multi:   (author, year) → 查 ref_index，然后验证 3+ 作者
    """
    seen_missing = set()
    missing = []
    for c in citations:
        count = c.get('author_count', 'single')
        if count == 'dual':
            # 双作者：遍历所有 ref 逐条判断
            found = any(citation_matches_ref(c, r) for r in ref_entries)
            if not found:
                dedup_key = (normalize_cn(c['author']),
                             normalize_cn(c.get('author2', '')),
                             c['year'])
                if dedup_key not in seen_missing:
                    seen_missing.add(dedup_key)
                    missing.append(c)
        else:
            # single / multi: 先查索引再验证
            key = (normalize_cn(c['author']), c['year'])
            matched = False
            if key in ref_index:
                for r in ref_index[key]:
                    if citation_matches_ref(c, r):
                        matched = True
                        break
            if not matched and key not in seen_missing:
                seen_missing.add(key)
                missing.append(c)

    # 有文无引：对每条 ref，在所有 cite 中找匹配
    seen_uncited = set()
    uncited = []
    for ref in ref_entries:
        found = False
        for c in citations:
            if citation_matches_ref(c, ref):
                found = True
                break
        key = (normalize_cn(ref['first_author']), ref['year'])
        if not found and key not in seen_uncited:
            seen_uncited.add(key)
            uncited.append(ref)

    return missing, uncited


# ============================================================
#  5b. 多级匹配引擎 (Matching Engine per TDS §11)
# ============================================================

def _levenshtein_ratio(s1: str, s2: str) -> float:
    """计算两个字符串的编辑距离相似度 (0~1)"""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    # 只用前 min(len, 20) 字符加速
    a, b = s1[:20], s2[:20]
    n, m = len(a), len(b)
    if n > m:
        a, b = b, a
        n, m = m, n
    cur = list(range(n + 1))
    for i in range(1, m + 1):
        prev = cur[:]
        cur[0] = i
        for j in range(1, n + 1):
            cost = 0 if a[j - 1] == b[i - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
    return 1.0 - cur[n] / max(len(a), len(b))


def fuzzy_match_citations(citations, ref_entries, ref_index, threshold=0.8):
    """
    多级匹配（TDS §11，多作者感知）:
      Level 1: 严格匹配 (使用 citation_matches_ref)
      Level 2: 模糊作者 + 严格年份
      Level 3: 仅年份 + 模糊作者（低保底）

    返回 (missing, uncited, year_mismatches, author_mismatches)
    """

    def _strict_match(c, r):
        """严格匹配（含多作者规则）"""
        return citation_matches_ref(c, r)

    def _fuzzy_match(c, r):
        """模糊作者匹配（仅第一作者）"""
        count = c.get('author_count', 'single')
        a_norm = normalize_cn(c['author'])
        if count == 'dual':
            a2 = normalize_cn(c.get('author2', ''))
            if not a2:
                return False
            ref_all = [normalize_cn(a) for a in r.get('all_authors', [])]
            return (len(ref_all) >= 2 and
                    _levenshtein_ratio(a_norm, ref_all[0]) >= threshold and
                    _levenshtein_ratio(a2, ref_all[1]) >= threshold)
        elif count == 'multi':
            ref_all = [normalize_cn(a) for a in r.get('all_authors', [])]
            return (_levenshtein_ratio(a_norm, ref_all[0]) >= threshold and
                    (len(ref_all) >= 3 or r.get('has_et_al')))
        else:
            for au in [r['first_author']] + r.get('all_authors', []):
                if _levenshtein_ratio(a_norm, normalize_cn(au)) >= threshold:
                    return True
            return False

    # Level 1: strict match
    strict_matched_cites = set()
    strict_matched_refs = set()
    for c in citations:
        for r in ref_entries:
            if _strict_match(c, r):
                count = c.get('author_count', 'single')
                key = (normalize_cn(c['author']), c['year'], count)
                strict_matched_cites.add(key)
                strict_matched_refs.add(id(r))

    # Level 2: fuzzy author match
    fuzzy_matched_cites = set()
    fuzzy_matched_refs = set()
    for c in citations:
        count = c.get('author_count', 'single')
        key = (normalize_cn(c['author']), c['year'], count)
        if key in strict_matched_cites:
            continue
        for r in ref_entries:
            if id(r) in strict_matched_refs:
                continue
            if r['year'] != c['year']:
                continue
            if _fuzzy_match(c, r):
                fuzzy_matched_cites.add(key)
                fuzzy_matched_refs.add(id(r))
                break

    # Build results
    seen_missing = set()
    missing = []
    for c in citations:
        count = c.get('author_count', 'single')
        key = (normalize_cn(c['author']), c['year'], count)
        if key not in strict_matched_cites and key not in fuzzy_matched_cites and key not in seen_missing:
            seen_missing.add(key)
            missing.append(c)

    seen_uncited = set()
    uncited = []
    for r in ref_entries:
        rid = id(r)
        if rid in strict_matched_refs or rid in fuzzy_matched_refs:
            continue
        found = any(_strict_match(c, r) or _fuzzy_match(c, r) for c in citations)
        key = (normalize_cn(r['first_author']), r['year'])
        if not found and key not in seen_uncited:
            seen_uncited.add(key)
            uncited.append(r)

    # 年份不匹配检测
    year_mismatches = []
    for c in citations:
        a_norm = normalize_cn(c['author'])
        y = c['year']
        for r in ref_entries:
            ref_fn = normalize_cn(r['first_author'])
            if _levenshtein_ratio(a_norm, ref_fn) < 0.7:
                continue
            if r['year'] != y:
                year_mismatches.append({
                    'citation': c, 'reference': r,
                    'cite_year': y, 'ref_year': r['year']
                })
                break

    # 作者不匹配检测
    author_mismatches = []
    for c in citations:
        a_norm = normalize_cn(c['author'])
        y = c['year']
        for r in ref_entries:
            if r['year'] != y:
                continue
            ref_fn = normalize_cn(r['first_author'])
            if 0.3 < _levenshtein_ratio(a_norm, ref_fn) < 0.7:
                author_mismatches.append({
                    'citation': c, 'reference': r,
                    'cite_author': c['author'], 'ref_author': r['first_author']
                })
                break

    return missing, uncited, year_mismatches, author_mismatches


def detect_year_mismatches(citations, ref_entries):
    """
    检测引用年份与参考文献年份不一致的情况。
    对每条文内引用，若作者名可在参考文献中找到（精确+模糊），
    但没有任何一条同名文献的年份与引用年份匹配，则标记为年份不匹配。
    """
    # 构建 (normalized_author -> set of years) 索引
    author_years = defaultdict(set)
    author_refs = defaultdict(list)     # (norm_author) -> [ref, ...]
    for r in ref_entries:
        fn = normalize_cn(r['first_author'])
        author_years[fn].add(r['year'])
        author_refs[fn].append(r)
        for a in r.get('all_authors', []):
            an = normalize_cn(a)
            if an != fn:
                author_years[an].add(r['year'])
                author_refs[an].append(r)

    mismatches = []
    seen = set()
    for c in citations:
        a_norm = normalize_cn(c['author'])
        y = c['year']
        count = c.get('author_count', 'single')

        # 收集此引用的所有候选匹配作者名
        candidates = {a_norm}
        # 对 dual，也检查第二作者
        if count == 'dual' and c.get('author2'):
            candidates.add(normalize_cn(c['author2']))

        matched_same_year = False
        found_different_year = False
        best_ref = None

        for cand in candidates:
            if cand in author_years:
                years = author_years[cand]
                if y in years:
                    matched_same_year = True
                    break
                # 有同名作者但年份不同 → 记录供展示
                for r in author_refs[cand]:
                    if r['year'] != y:
                        found_different_year = True
                        best_ref = r
                        break

        if not matched_same_year and found_different_year and best_ref:
            key = (a_norm, y)
            if key not in seen:
                seen.add(key)
                mismatches.append({
                    'citation': c, 'reference': best_ref,
                    'cite_year': y, 'ref_year': best_ref['year']
                })

    return mismatches


def detect_author_mismatches(citations, ref_entries):
    """
    检测引用作者与参考文献作者不一致（年份相同但作者名近似不匹配）。
    """
    mismatches = []
    seen = set()
    for c in citations:
        a_norm = normalize_cn(c['author'])
        y = c['year']
        for r in ref_entries:
            if r['year'] != y:
                continue
            ref_fn = normalize_cn(r['first_author'])
            if 0.3 < _levenshtein_ratio(a_norm, ref_fn) < 0.7:
                key = (a_norm, y, id(r))
                if key not in seen:
                    seen.add(key)
                    mismatches.append({
                        'citation': c, 'reference': r,
                        'cite_author': c['author'], 'ref_author': r['first_author']
                    })
                    break
    return mismatches


# ============================================================
#  5c. 规范格式匹配引擎 (Canonical Form Matching)
# ============================================================

def author_normalize(name):
    """归一化作者名：lowercase + 去重音 + 繁转简 + hyphen前缀 + 去末尾标点 + 去撇号"""
    n = normalize_cn(name)
    n = n.split('-')[0].strip()
    n = re.sub(r"['\u2019\u2018]", '', n)
    n = n.rstrip(',.，。;；:：')
    return n


def canonical_citation(citation):
    """将正文引用转为规范格式字符串。

    single → 'author year'
    dual   → 'a1 and a2 year'
    multi  → 'a1 et al. year'
    """
    a1 = author_normalize(citation['author'])
    year = citation['year']
    count = citation.get('author_count', 'single')
    if count == 'dual':
        a2 = author_normalize(citation.get('author2', ''))
        return f"{a1} and {a2} {year}"
    elif count == 'multi':
        return f"{a1} et al. {year}"
    else:
        return f"{a1} {year}"


def canonical_forms_for_ref(ref):
    """从一条参考文献生成所有可能的规范引用格式。

    示例：
      single author  → {'schroeder 2021'}
      dual           → {'schroeder 2021', 'sagarin 2021', 'schroeder and sagarin 2021'}
      multi (3+)     → 同上 + {'schroeder et al. 2021'}
    """
    year = ref['year']
    all_authors = [author_normalize(a) for a in ref.get('all_authors', [])]
    if not all_authors:
        return []
    forms = set()
    a1 = all_authors[0]
    for a in all_authors:
        forms.add(f"{a} {year}")
    if len(all_authors) >= 2:
        forms.add(f"{a1} and {all_authors[1]} {year}")
        # v1.2: 若名单中有第二作者的小写前缀补全版本，额外生成对应双作者形式
        for extra_a in all_authors[2:]:
            parts = extra_a.split()
            if len(parts) >= 2 and parts[-1] == all_authors[1]:
                forms.add(f"{a1} and {extra_a} {year}")
    if len(all_authors) >= 3 or ref.get('has_et_al'):
        forms.add(f"{a1} et al. {year}")
    return list(forms)


def match_via_canonical(citations, ref_entries, fuzzy=False):
    """基于规范格式字符串的双向匹配引擎（替代所有旧匹配函数）。

    返回 (missing, uncited, year_mismatches)
    """
    # 反向索引：规范格式 → ref 下标列表
    index = defaultdict(list)
    for ri, ref in enumerate(ref_entries):
        for cform in canonical_forms_for_ref(ref):
            index[cform].append(ri)

    # 年份不匹配索引（仅主规范格式，不含个体合作者形式）
    # single:  "a"         ← 来自任意 ref 的 first_author
    # dual:    "a1 and a2" ← 来自任意 ref 的前两位作者
    # multi:   "a1 et al." ← 来自任意 ref 且 3+ 作者 或 has_et_al
    ym_index = defaultdict(list)
    for ri, ref in enumerate(ref_entries):
        year = ref['year']
        all_a = [author_normalize(a) for a in ref.get('all_authors', [])]
        if not all_a:
            continue
        a1 = all_a[0]
        ym_index[a1].append((ri, year))
        if len(all_a) >= 2:
            ym_index[f"{a1} and {all_a[1]}"].append((ri, year))
        if len(all_a) >= 3 or ref.get('has_et_al'):
            ym_index[f"{a1} et al."].append((ri, year))

    matched_refs = set()
    missing = []
    year_mismatches = []

    for c in citations:
        cform = canonical_citation(c)
        cy = c['year']
        a1 = author_normalize(c['author'])
        count = c.get('author_count', 'single')
        # 精确匹配
        matched = index.get(cform, [])
        if matched:
            for ri in matched:
                matched_refs.add(ri)
            continue

        # 年份不匹配：主规范格式作者集匹配但年份不同
        # v1.2 按作者数量匹配，防止 single→dual / dual→multi 误关联
        author_part = cform.rsplit(' ', 1)[0]
        found_ym = False
        if author_part in ym_index:
            for ri, ref_year in ym_index[author_part]:
                if ref_year == cy:
                    continue
                ref = ref_entries[ri]
                ref_aa = ref.get('all_authors', [])
                # 验证作者数量一致性
                if count == 'single' and len(ref_aa) != 1:
                    continue
                if count == 'dual' and len(ref_aa) != 2:
                    continue
                if count == 'multi' and not (len(ref_aa) >= 3 or ref.get('has_et_al')):
                    continue
                year_mismatches.append({
                    'citation': c, 'reference': ref,
                    'cite_year': cy, 'ref_year': ref_year
                })
                matched_refs.add(ri)
                found_ym = True
                break
        if found_ym:
            continue

        # 模糊兜底匹配 — 检查 ref 的所有作者（始终生效）
        def _cjk_threshold(s1, s2):
            """中文名使用更宽松阈值(0.65)，英文明使用0.80"""
            if re.search(r'[\u4e00-\u9fff]', s1) and re.search(r'[\u4e00-\u9fff]', s2):
                return 0.65
            return 0.80

        def _fuzzy_match_citation(c, ref_authors, ri):
            if count == 'dual':
                a2 = author_normalize(c.get('author2', ''))
                if not a2 or len(ref_authors) < 2:
                    return False
                return (_levenshtein_ratio(a1, ref_authors[0]) >= _cjk_threshold(a1, ref_authors[0]) and
                        _levenshtein_ratio(a2, ref_authors[1]) >= _cjk_threshold(a2, ref_authors[1]))
            elif count == 'multi':
                if not ref_authors:
                    return False
                return (_levenshtein_ratio(a1, ref_authors[0]) >= _cjk_threshold(a1, ref_authors[0]) and
                        (len(ref_authors) >= 3 or ref_entries[ri].get('has_et_al')))
            else:
                return any(_levenshtein_ratio(a1, au) >= _cjk_threshold(a1, au) for au in ref_authors)

        ref_all_norm = [(ri, [author_normalize(a) for a in ref.get('all_authors', [])])
                        for ri, ref in enumerate(ref_entries) if ref['year'] == cy]
        matched_fuzzy = False
        for ri, ref_authors in ref_all_norm:
            if _fuzzy_match_citation(c, ref_authors, ri):
                matched_refs.add(ri)
                matched_fuzzy = True
                break
        if matched_fuzzy:
            continue

        # 中文姓后缀匹配：PDF提取可能丢失中文姓的第一个字符
        # 如 "陽丹" → "歐陽丹"，检测 2字中文引用名是否匹配 3+字参考文献名的后缀
        if re.match(r'^[\u4e00-\u9fff]{2}$', a1):
            for ri, ref in enumerate(ref_entries):
                if ref['year'] != cy:
                    continue
                ref_fa_norm = author_normalize(ref['first_author'])
                if (re.match(r'^[\u4e00-\u9fff]{3,}$', ref_fa_norm)
                        and ref_fa_norm.endswith(a1)):
                    matched_refs.add(ri)
                    break
            else:
                missing.append(c)
        else:
            missing.append(c)

    # 有文无引
    uncited = []
    for ri, ref in enumerate(ref_entries):
        if ri not in matched_refs:
            uncited.append(ref)

    return missing, uncited, year_mismatches


def find_cross_year_warnings(missing, ref_entries):
    """对有引无文的引用，检查是否存在同作者但不同年份的参考文献。

    返回 [(citation, matched_ref, ref_year, ratio)]
    v1.2 匹配规则与主引擎一致——按作者数量/顺序匹配，不跨作者位匹配。
    """
    def _cross_year_threshold(s1, s2):
        """跨年预警使用更严格阈值，避免短英文名误匹配"""
        if re.search(r'[\u4e00-\u9fff]', s1) and re.search(r'[\u4e00-\u9fff]', s2):
            return 0.80
        return 0.90

    warnings = []
    seen = set()
    for c in missing:
        a1 = author_normalize(c['author'])
        cy = c['year']
        count = c.get('author_count', 'single')
        a2 = author_normalize(c.get('author2', '')) if count == 'dual' else None

        best = None
        best_ratio = 0
        for r in ref_entries:
            if r['year'] == cy:
                continue
            ref_authors = [author_normalize(a) for a in r.get('all_authors', [])]
            if not ref_authors:
                continue

            matched = False
            ratio = 0.0
            if count == 'single':
                if len(ref_authors) == 1:
                    r1 = _levenshtein_ratio(a1, ref_authors[0])
                    if r1 >= _cross_year_threshold(a1, ref_authors[0]):
                        matched = True
                        ratio = r1
            elif count == 'dual':
                if len(ref_authors) == 2 and a2:
                    r1 = _levenshtein_ratio(a1, ref_authors[0])
                    r2 = _levenshtein_ratio(a2, ref_authors[1])
                    if r1 >= _cross_year_threshold(a1, ref_authors[0]) and r2 >= _cross_year_threshold(a2, ref_authors[1]):
                        matched = True
                        ratio = (r1 + r2) / 2
            elif count == 'multi':
                if len(ref_authors) >= 3 or r.get('has_et_al'):
                    r1 = _levenshtein_ratio(a1, ref_authors[0])
                    if r1 >= _cross_year_threshold(a1, ref_authors[0]):
                        matched = True
                        ratio = r1

            if matched and ratio > best_ratio:
                best_ratio = ratio
                best = (c, r, r['year'], ratio)

        if best:
            key = (a1, cy, id(best[1]))
            if key not in seen:
                seen.add(key)
                warnings.append(best)
    return warnings


# ============================================================
#  5d. 标准引用格式导出
# ============================================================

def export_standard_citation_files(pdf_path, citations, ref_entries):
    """导出两个标准化引用 TXT 文件（按第一作者首字母排序）。

    * {stem}_citations_std.txt  — 正文引用的标准格式
    * {stem}_references_std.txt — 从参考文献生成的标准引用格式
    """
    stem = pdf_path.with_suffix('').name

    def _sort_key(item):
        return item[0].lower()

    # 引用 canonical 字符串用于比较（统一小写规范格式）
    cite_lines_display = []     # (display_line, sort_key)  正文原文作者+年 用于展示
    cite_canon_set = set()      # canonical 字符串集合 用于匹配比较
    for c in citations:
        disp = f"{format_citation_author(c)} {c['year']}"
        canon = canonical_citation(c)  # lowercase, normalized
        cite_lines_display.append((disp, canon))
        cite_canon_set.add(canon)
    cite_lines_display.sort(key=_sort_key)

    cite_path = pdf_path.with_suffix('.citations_std.txt')
    with open(cite_path, 'w', encoding='utf-8') as f:
        f.write(f"# 正文引用标准格式 — {stem}\n")
        f.write(f"# 共 {len(cite_lines_display)} 条，按第一作者首字母排序\n\n")
        for disp, _ in cite_lines_display:
            f.write(disp + '\n')
    print(f'  正文引用标准格式已保存至: {cite_path}')

    # 参考文献
    ref_lines_display = []      # (canon_display, canon)  小写 canonical 用于展示
    ref_canon_set = set()
    for ref in ref_entries:
        for cform in canonical_forms_for_ref(ref):
            ref_lines_display.append((cform, cform))
            ref_canon_set.add(cform)
    ref_lines_display = sorted(set(ref_lines_display), key=_sort_key)

    ref_path = pdf_path.with_suffix('.references_std.txt')
    with open(ref_path, 'w', encoding='utf-8') as f:
        f.write(f"# 参考文献标准引用格式 — {stem}\n")
        f.write(f"# 共 {len(ref_lines_display)} 条，按第一作者首字母排序\n\n")
        for disp, _ in ref_lines_display:
            f.write(disp + '\n')
    print(f'  参考文献标准格式已保存至: {ref_path}')

    # 合并对比文件（基于 canonical string 比较）
    both_path = pdf_path.with_suffix('.comparison_std.txt')
    with open(both_path, 'w', encoding='utf-8') as f:
        f.write(f"# 引用标准格式对比 — {stem}\n")
        f.write(f"# 正文引用 {len(cite_canon_set)} 条 | 参考文献 {len(ref_canon_set)} 条\n\n")
        f.write("=" * 60 + "\n")
        f.write("【正文引用标准格式】\n")
        f.write("=" * 60 + "\n")
        for disp, canon in cite_lines_display:
            marker = " ✓" if canon in ref_canon_set else ""
            f.write(f"{disp}{marker}\n")
        f.write("\n")
        f.write("=" * 60 + "\n")
        f.write("【参考文献标准引用格式】\n")
        f.write("=" * 60 + "\n")
        for disp, canon in ref_lines_display:
            marker = " ✓" if canon in cite_canon_set else ""
            f.write(f"{disp}{marker}\n")
    print(f'  对比文件已保存至: {both_path}')


def format_citation_author(citation):
    """格式化引用作者显示：dual → 'A and B' / 'A和B'，multi → 'A et al.' / 'A等'"""
    count = citation.get('author_count', 'single')
    a1 = citation['author']
    a2 = citation.get('author2')
    is_cjk = bool(re.search(r'[\u4e00-\u9fff]', a1))
    if count == 'dual' and a2:
        return f'{a1} and {a2}' if not is_cjk else f'{a1}和{a2}'
    elif count == 'multi':
        return f'{a1} et al.' if not is_cjk else f'{a1}等'
    else:
        return a1


def format_ref_author(ref):
    """格式化参考文献作者显示"""
    all_a = ref.get('all_authors', [])
    a1 = ref['first_author']
    if len(all_a) >= 2:
        a2 = all_a[1]
        return f'{a1}, {a2}' if not re.search(r'[\u4e00-\u9fff]', a1) else f'{a1}、{a2}'
    return a1


# ============================================================
#  6. 报告生成
# ============================================================

def generate_report(pdf_path, citations, ref_entries, missing, uncited,
                    year_mismatches, elapsed, cross_year_warnings=None):
    """生成审计报告文本"""
    def _cite_key(c):
        return (normalize_cn(c['author']), c['year'], c.get('author_count', 'single'))
    total_cites = len(set(_cite_key(c) for c in citations))
    total_refs = len(ref_entries)

    lines = []
    sep = '=' * 78

    lines.append(sep)
    lines.append('  引用一致性审计报告')
    lines.append('  Citation Consistency Audit Report')
    lines.append(sep)
    lines.append(f'  文件: {pdf_path.name}')
    lines.append(f'  时间: {time.strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'  耗时: {elapsed:.2f} 秒')
    lines.append(sep)
    lines.append('')

    # 有引无文
    lines.append('  [有引无文] 正文有引用但参考文献缺失')
    lines.append('  (Cited in text but NOT found in reference list)')
    lines.append('  ' + '-' * 74)
    if missing:
        lines.append(f'  共 {len(missing)} 处，请逐条人工复核：')
        lines.append('')
        for i, c in enumerate(missing, 1):
            pg = f' (p{c["page"]})' if c.get('page') else ''
            auth = format_citation_author(c)
            lines.append(f'    {i:>3}.  {auth:30s}  {c["year"]}{pg}')
        lines.append('')
        lines.append('  ! 注意: PDF提取可能导致中文字丢失首位，建议对照原文核实')
    else:
        lines.append('  ✓ 未发现异常')
    lines.append('')

    # 跨年作者预警
    if cross_year_warnings:
        lines.append('  [跨年作者预警] 有引无文条目存在同作者/不同年份的参考文献')
        lines.append('  (Missing citation has same author with different year in ref list)')
        lines.append('  ' + '-' * 74)
        lines.append(f'  共 {len(cross_year_warnings)} 项，引用年份可能笔误：')
        lines.append('')
        for i, (c, r, ref_yr, ratio) in enumerate(cross_year_warnings, 1):
            cite_auth = format_citation_author(c)
            ref_auth = format_ref_author(r)
            lines.append(f'    {i:>3}.  正文引用: {cite_auth:30s}  {c["year"]}')
            lines.append(f'         → 参考文献: {ref_auth:30s}  {ref_yr}')
        lines.append('')
        lines.append('  ! 提醒: 人工确认引用年份是笔误还是两篇不同文献')
        lines.append('')

    # 有文无引
    lines.append('  [有文无引] 参考文献存在但正文未引用')
    lines.append('  (In reference list but NOT cited in text)')
    lines.append('  ' + '-' * 74)
    if uncited:
        lines.append(f'  共 {len(uncited)} 处，请逐条人工复核：')
        lines.append('')
        for i, ref in enumerate(uncited, 1):
            id_s = f'[R{ref["id"]}] ' if ref['id'] else ''
            auth = format_ref_author(ref)
            lines.append(f'    {i:>3}.  {id_s}{auth:30s}  {ref["year"]}')
    else:
        lines.append('  ✓ 未发现异常')
    lines.append('')

    # 年份不匹配
    lines.append('  [年份不匹配] 引用年份与参考文献年份不一致')
    lines.append('  (Author matches reference, but year differs)')
    lines.append('  ' + '-' * 74)
    if year_mismatches:
        lines.append(f'  共 {len(year_mismatches)} 处，请逐条人工复核：')
        lines.append('')
        for i, ym in enumerate(year_mismatches, 1):
            cite_auth = format_citation_author(ym['citation'])
            ref_auth = format_ref_author(ym['reference'])
            lines.append(f'    {i:>3}.  正文引用: {cite_auth:30s}  {ym["cite_year"]}')
            lines.append(f'         参考文献: {ref_auth:30s}  {ym["ref_year"]}')
        lines.append('')
    else:
        lines.append('  ✓ 未发现异常')
    lines.append('')

    # 统计
    lines.append('  [统计摘要] Summary')
    lines.append('  ' + '-' * 74)
    match_rate = ((total_cites - len(missing)) / total_cites * 100) if total_cites > 0 else 0
    lines.append(f'    正文引用数 (去重, 第一作者+年)       {total_cites}')
    lines.append(f'    参考文献数                          {total_refs}')
    lines.append(f'    有引无文                            {len(missing)}')
    lines.append(f'    有文无引                            {len(uncited)}')
    lines.append(f'    年份不匹配                          {len(year_mismatches)}')
    if total_cites > 0:
        lines.append(f'    匹配通过率                          {match_rate:.1f}%')
    lines.append('')

    # 已知限制
    lines.append('  [已知限制] Limitations')
    lines.append('  ' + '-' * 74)
    lines.append('  1. PDF文本提取可能导致部分文字丢失，影响中文姓名匹配')
    lines.append('  2. 表格/图表中的引用（如文献综述汇总表）无法被提取')
    lines.append('  3. 正文引用仅含(作者, 年份)信息时，无法区分同作者同年份的多篇文献（如 Author 2020a / 2020b）')
    lines.append('  4. 越南/匈牙利等非英美姓名格式可能提取不准确')
    lines.append('  5. 脚注/尾注中的引用不在检测范围内')
    lines.append('  6. 繁简汉字转换基于有限映射表，可能遗漏部分字形差异')
    lines.append('')
    lines.append('  建议: 将本工具结果作为初审参考，与原文对照确认')
    lines.append('')

    return '\n'.join(lines)


# ============================================================
#  6b. HTML 报告生成 (per TDS §14)
# ============================================================

def generate_html_report(pdf_path, citations, ref_entries, missing, uncited,
                         year_mismatches, duplicates, elapsed,
                         cross_year_warnings=None):
    """生成带颜色编码的 HTML 审计报告"""
    def _cite_key(c):
        return (normalize_cn(c['author']), c['year'], c.get('author_count', 'single'))
    total_cites = len(set(_cite_key(c) for c in citations))
    total_refs = len(ref_entries)
    match_rate = ((total_cites - len(missing)) / total_cites * 100) if total_cites > 0 else 0

    def _sev_color(sev):
        return {'HIGH': '#dc3545', 'MEDIUM': '#fd7e14', 'LOW': '#0d6efd'}.get(sev, '#6c757d')

    def _issue_rows(items, label, severity, type_key):
        rows = ''
        for i, item in enumerate(items, 1):
            color = _sev_color(severity)
            if type_key == 'missing':
                pg = f' (p{item["page"]})' if item.get('page') else ''
                auth = format_citation_author(item)
                rows += f'<tr style="background:{color}10"><td>{i}</td><td>{auth}</td><td>{item["year"]}</td><td>{pg}</td><td><span class="badge" style="background:{color}">{severity}</span></td></tr>\n'
            elif type_key == 'uncited':
                raw = item.get('raw', '').replace('\n', ' ').strip()
                if len(raw) > 120:
                    raw = raw[:120] + '…'
                authors = item.get('all_authors', [])
                year = item['year']
                if len(authors) == 1:
                    suggest = f"({authors[0]}, {year})"
                elif len(authors) == 2:
                    suggest = f"({authors[0]} &amp; {authors[1]}, {year})"
                elif len(authors) >= 3:
                    suggest = f"({authors[0]} et al., {year})"
                else:
                    suggest = f"(?, {year})"
                rows += f'<tr style="background:{color}10"><td>{i}</td><td style="max-width:400px;word-break:break-word">{raw}</td><td style="white-space:nowrap">{suggest}</td><td><span class="badge" style="background:{color}">{severity}</span></td></tr>\n'
            elif type_key == 'year_mismatch':
                cite_auth = format_citation_author(item["citation"])
                ref_auth = format_ref_author(item["reference"])
                rows += f'<tr style="background:{_sev_color("HIGH")}10"><td>{i}</td><td>{cite_auth}</td><td>{item["cite_year"]} → {item["ref_year"]}</td><td>{ref_auth}</td><td><span class="badge" style="background:{_sev_color("HIGH")}">HIGH</span></td></tr>\n'
            elif type_key == 'duplicate':
                auth = format_ref_author(item)
                rows += f'<tr style="background:{_sev_color("MEDIUM")}10"><td>{i}</td><td>{auth}</td><td>{item["year"]}</td><td>R{item.get("id","?")}</td><td><span class="badge" style="background:{_sev_color("MEDIUM")}">MEDIUM</span></td></tr>\n'
        return rows

    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>引用一致性审计报告 - {pdf_path.name}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#f8f9fa; color:#333; padding:20px; }}
.container {{ max-width:1200px; margin:0 auto; }}
.header {{ background:linear-gradient(135deg,#1a237e,#283593); color:#fff; padding:30px; border-radius:12px; margin-bottom:24px; }}
.header h1 {{ font-size:24px; margin-bottom:8px; }}
.header .meta {{ font-size:14px; opacity:.85; }}
.summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; margin-bottom:24px; }}
.summary-card {{ background:#fff; border-radius:10px; padding:20px; text-align:center; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
.summary-card .num {{ font-size:32px; font-weight:700; }}
.summary-card .label {{ font-size:13px; color:#666; margin-top:4px; }}
.card {{ background:#fff; border-radius:10px; padding:20px; margin-bottom:20px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
.card h2 {{ font-size:18px; margin-bottom:12px; padding-bottom:8px; border-bottom:2px solid #eee; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ padding:10px 12px; text-align:left; border-bottom:1px solid #eee; }}
th {{ font-weight:600; color:#555; font-size:13px; text-transform:uppercase; }}
.badge {{ display:inline-block; padding:2px 10px; border-radius:20px; color:#fff; font-size:12px; font-weight:600; }}
.mt-0 {{ margin-top:0; }}
.text-muted {{ color:#999; font-size:13px; }}
.limitations {{ background:#fff; border-radius:10px; padding:20px; margin-bottom:20px; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
.limitations h2 {{ font-size:18px; margin-bottom:12px; }}
.limitations ul {{ padding-left:20px; }}
.limitations li {{ margin-bottom:6px; font-size:14px; color:#555; }}
footer {{ text-align:center; font-size:13px; color:#999; margin-top:24px; padding:20px; }}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>📋 引用一致性审计报告</h1>
<p class="meta">文件: {pdf_path.name} | 时间: {time.strftime("%Y-%m-%d %H:%M:%S")} | 耗时: {elapsed:.2f}s</p>
</div>

<div class="summary-grid">
<div class="summary-card"><div class="num" style="color:#1a237e">{total_cites}</div><div class="label">正文引用数</div></div>
<div class="summary-card"><div class="num" style="color:#1a237e">{total_refs}</div><div class="label">参考文献数</div></div>
<div class="summary-card"><div class="num" style="color:#dc3545">{len(missing)}</div><div class="label">有引无文</div></div>
<div class="summary-card"><div class="num" style="color:#fd7e14">{len(uncited)}</div><div class="label">有文无引</div></div>
<div class="summary-card"><div class="num" style="color:#dc3545">{len(year_mismatches)}</div><div class="label">年份不匹配</div></div>
<div class="summary-card"><div class="num" style="color:#0d6efd">{len(duplicates)}</div><div class="label">疑似重复</div></div>
<div class="summary-card"><div class="num" style="color:{'#28a745' if match_rate >= 90 else '#fd7e14' if match_rate >= 70 else '#dc3545'}">{match_rate:.1f}%</div><div class="label">匹配通过率</div></div>
</div>'''

    if missing:
        html += f'''<div class="card">
<h2 style="color:#dc3545">🔴 有引无文 ({len(missing)})</h2>
<p class="text-muted">正文引用了但参考文献列表中缺失</p>
<table><thead><tr><th>#</th><th>作者</th><th>年份</th><th>页码</th><th>严重度</th></tr></thead><tbody>
{_issue_rows(missing,'missing','HIGH','missing')}
</tbody></table></div>'''

    if cross_year_warnings:
        cy_rows = ''
        for i, (c, r, ref_yr, ratio) in enumerate(cross_year_warnings, 1):
            cite_auth = format_citation_author(c)
            ref_auth = format_ref_author(r)
            cy_rows += f'<tr><td>{i}</td><td>{cite_auth}</td><td>{c["year"]} → {ref_yr}</td><td>{ref_auth}</td></tr>\n'
        html += f'''<div class="card">
<h2 style="color:#fd7e14">🟡 跨年作者预警 ({len(cross_year_warnings)})</h2>
<p class="text-muted">有引无文条目存在同作者但不同年份的参考文献 — 引用年份可能笔误</p>
<table><thead><tr><th>#</th><th>正文引用</th><th>年份差异</th><th>参考文献</th></tr></thead><tbody>
{cy_rows}
</tbody></table></div>'''

    if uncited:
        html += f'''<div class="card">
<h2 style="color:#fd7e14">🟠 有文无引 ({len(uncited)})</h2>
<p class="text-muted">参考文献列表中存在但正文未引用</p>
<table><thead><tr><th>#</th><th>参考文献原文</th><th>建议正文引用 (APA)</th><th>严重度</th></tr></thead><tbody>
{_issue_rows(uncited,'uncited','MEDIUM','uncited')}
</tbody></table></div>'''

    if year_mismatches:
        html += f'''<div class="card">
<h2 style="color:#dc3545">🔴 年份不匹配 ({len(year_mismatches)})</h2>
<p class="text-muted" style="line-height:1.8">
正文引用的作者与参考文献中的作者<b>按数量/位序一致</b>，
但年份不同。可能原因：<br>
&nbsp;&nbsp;• 引用年份是笔误（应更正为参考文献中的年份）<br>
&nbsp;&nbsp;• 作者有多篇同年份论文，引用时混淆了<br>
&nbsp;&nbsp;• 参考文献确实缺漏该年份的文献，但同一作者有其他年份的文献<br>
<br>
<b>匹配规则：</b>单作者↔单作者 | 双作者↔双作者 | et al.↔3+作者<br>
此板块不计入"有引无文"或"有文无引"统计。
</p>
<table><thead><tr><th>#</th><th>正文作者</th><th>年份</th><th>参考文献</th><th>严重度</th></tr></thead><tbody>
{_issue_rows(year_mismatches,'year','HIGH','year_mismatch')}
</tbody></table></div>'''

    if duplicates:
        html += f'''<div class="card">
<h2 style="color:#0d6efd">🔵 疑似重复文献 ({len(duplicates)})</h2>
<p class="text-muted">参考文献列表中存在疑似重复条目</p>
<table><thead><tr><th>#</th><th>作者</th><th>年份</th><th>编号</th><th>严重度</th></tr></thead><tbody>
{_issue_rows(duplicates,'dup','MEDIUM','duplicate')}
</tbody></table></div>'''

    html += f'''<div class="limitations">
<h2>⚠️ 已知限制</h2>
<ul>
<li>PDF文本提取可能导致部分文字丢失，影响中文姓名匹配</li>
<li>表格/图表中的引用（如文献综述汇总表）无法被提取</li>
<li>正文引用仅含(作者, 年份)信息时，无法区分同作者同年份的多篇文献（如 Author 2020a / 2020b）</li>
<li>越南/匈牙利等非英美姓名格式可能提取不准确</li>
<li>脚注/尾注中的引用不在检测范围内</li>
<li>繁简汉字转换基于有限映射表，可能遗漏部分字形差异</li>
<li>无法检测参考文献列表中是否存在虚假文献（杜撰的、不存在的文献）</li>
</ul>
<p style="margin-top:12px; font-size:14px; color:#666;">建议: 将本工具结果作为初审参考，与原文对照确认</p>
</div>

<footer>Generated by Reference Integrity Auditor (RIA) v1.0</footer>
</div>
</body>
</html>'''
    return html


# ============================================================
#  6c. 重复文献检测 (per TDS §12 RULE_005/006)
# ============================================================

def detect_duplicate_references(ref_entries, threshold=0.85):
    """检测参考文献列表中的疑似重复条目"""
    duplicates = []
    seen = []
    for ref in ref_entries:
        fn = normalize_cn(ref['first_author'])
        y = ref['year']
        ref_authors = ref.get('all_authors', [])
        for existing in seen:
            if existing['year'] != y:
                continue
            ef = normalize_cn(existing['first_author'])
            ratio = _levenshtein_ratio(fn, ef)
            if ratio >= threshold:
                # 检查 all_authors 是否一致（不同合作者说明是不同作品）
                exist_authors = existing.get('all_authors', [])
                if ref_authors != exist_authors:
                    continue
                duplicates.append(ref)
                break
        seen.append(ref)
    return duplicates


# ============================================================
#  6d. 引用质量统计 (Citation Quality per ChatGPT建议)
# ============================================================

def compute_quality_stats(citations, ref_entries, missing, uncited,
                           year_mismatches, duplicates,
                           style=""):
    """
    计算引用质量量化指标。

    返回 dict:
    {
        'style': str,
        'total_cites': int,       # 去重引用数
        'total_refs': int,        # 参考文献数
        'missing_count': int,
        'uncited_count': int,
        'year_mismatch_count': int,
        'duplicate_count': int,
        'match_rate': float,      # 匹配通过率 %
        'recent_ratio': float,    # 近5年文献占比 %
        'en_ratio': float,        # 英文文献占比 %
        'zh_ratio': float,        # 中文文献占比 %
        'avg_year': float,        # 参考文献平均年份
        'year_distribution': dict,  # 年份→篇数
        'ai_false_positive': 0,   # AI 后续填入
        'ai_confirmed_match': 0,
    }
    """
    def _cite_key(c):
        return (normalize_cn(c['author']), c['year'], c.get('author_count', 'single'))
    total_cites = len(set(_cite_key(c) for c in citations))
    total_refs = len(ref_entries)
    match_rate = ((total_cites - len(missing)) / total_cites * 100) if total_cites > 0 else 0

    # 年份分布
    years = []
    for r in ref_entries:
        try:
            y = int(r['year'])
            if 1900 <= y <= 2030:
                years.append(y)
        except (ValueError, TypeError):
            pass

    avg_year = sum(years) / len(years) if years else 0

    from collections import Counter
    year_dist = dict(Counter(years))

    # 近5年
    current = 2026
    recent = sum(1 for y in years if y >= current - 5)
    recent_ratio = (recent / len(years) * 100) if years else 0

    # 语言比例
    en_count = sum(1 for r in ref_entries if r.get('first_author') and re.match(r'^[A-Za-z]', r['first_author']))
    zh_count = sum(1 for r in ref_entries if r.get('first_author') and re.search(r'[\u4e00-\u9fff]', r['first_author']))
    en_ratio = (en_count / total_refs * 100) if total_refs else 0
    zh_ratio = (zh_count / total_refs * 100) if total_refs else 0

    return {
        'style': style,
        'total_cites': total_cites,
        'total_refs': total_refs,
        'missing_count': len(missing),
        'uncited_count': len(uncited),
        'year_mismatch_count': len(year_mismatches),
        'duplicate_count': len(duplicates),
        'match_rate': match_rate,
        'recent_ratio': recent_ratio,
        'en_ratio': en_ratio,
        'zh_ratio': zh_ratio,
        'avg_year': avg_year,
        'year_distribution': year_dist,
        'ai_false_positive': 0,
        'ai_confirmed_match': 0,
    }


# ============================================================
#  主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='学术论文引用一致性审计工具 (Citation Consistency Auditor)',
        epilog=(
            '示例:\n'
            '  python citation_audit.py thesis.pdf\n'
            '  python citation_audit.py thesis.pdf -o report.txt\n'
            '  python citation_audit.py thesis.pdf -v\n'
        )
    )
    parser.add_argument('pdf_path', help='PDF论文文件路径')
    parser.add_argument('-o', '--output', help='输出报告文件路径')
    parser.add_argument('--html', action='store_true', help='（已废弃 - HTML 报告默认自动生成）')
    parser.add_argument('--fuzzy', action='store_true', help='启用模糊作者匹配（Level 2）')
    parser.add_argument('--ai', action='store_true', help='启用本地 AI 辅助分析（需 LM Studio）')
    parser.add_argument('--ai-full', action='store_true',
                        help='AI 全量模式：逐项验证缺失/不匹配引用（较慢，仅建议 GPU）')
    parser.add_argument('--lm-studio-url', default='http://localhost:1234/v1',
                        help='LM Studio API 地址（默认 http://localhost:1234/v1）')
    parser.add_argument('--ai-model', help='AI 模型名称（可选，默认使用 LM Studio 已加载模型）')
    parser.add_argument('--std-cite', action='store_true',
                        help='导出标准化引用 TXT 文件（正文引用 + 参考文献引用 + 对比）')
    parser.add_argument('-v', '--verbose', action='store_true', help='显示详细信息')

    args = parser.parse_args()
    pdf_path = Path(args.pdf_path)

    if not pdf_path.exists():
        print(f'错误: 文件不存在: {pdf_path}')
        sys.exit(1)
    if pdf_path.suffix.lower() != '.pdf':
        print(f'错误: 文件必须是PDF格式: {pdf_path}')
        sys.exit(1)

    start = time.time()
    print(f'正在处理: {pdf_path} ...')

    # 1
    print('  [1/5] 提取PDF文本...')
    pages, full_text = extract_pdf_text(pdf_path)
    print(f'    共 {len(pages)} 页')

    # 2
    print('  [2/5] 分割正文/参考文献区域...')
    body_text, ref_text = find_ref_section(full_text)
    print(f'    正文 {len(body_text):,} 字符 | 参考文献 {len(ref_text):,} 字符')

    if not ref_text:
        print('  ! 未检测到参考文献章节，尝试在全文搜索引用...')
        body_text = full_text

    # 引用风格检测
    style = detect_citation_style(body_text)
    print(f'    检测到引用风格: {style}')

    # 3
    print('  [3/5] 提取正文引用...')
    citations = extract_all_citations(pages)

    # 过滤: 移除参考文献区域内被误提取的引用
    if ref_text and citations:
        before = len(citations)
        body_check = preprocess_body_text(body_text)
        filtered = []
        for c in citations:
            raw = c.get('raw', '')
            # 用 preprocess_body_text 处理 raw 使其格式与 body_check 一致后检查
            raw_check = raw.replace('\n', ' ')
            raw_check = re.sub(r' +', ' ', raw_check)
            if raw_check in body_check:
                filtered.append(c)
            else:
                # 兜底：检查 author 与 year 是否在 body_check 中共现
                pattern = rf'{re.escape(c["author"])}.{{0,80}}{c["year"]}'
                if re.search(pattern, body_check):
                    filtered.append(c)
        citations = filtered
        n_removed = before - len(citations)
        if n_removed > 0:
            print(f'    过滤 {n_removed} 个误提取，剩余 {len(citations)} 个')
        else:
            print(f'    提取到 {len(citations)} 个不重复引用')
    else:
        print(f'    提取到 {len(citations)} 个不重复引用')

    # 4
    print('  [4/5] 解析参考文献条目...')
    unparsed_refs = []
    ref_entries = parse_reference_entries(ref_text, unparsed_out=unparsed_refs)
    print(f'    解析到 {len(ref_entries)} 条', end='')
    if unparsed_refs:
        print(f'（{len(unparsed_refs)} 条未解析）')
    else:
        print()

    if args.verbose:
        print()
        print('--- 引用列表 ---')
        cite_sorted = sorted(citations, key=lambda x: (x['year'], x['author']))
        for i, c in enumerate(cite_sorted, 1):
            cnt = c.get('author_count', 'single')
            tag = {'single': '1', 'dual': '2', 'multi': 'N'}.get(cnt, '?')
            auth_disp = format_citation_author(c)
            print(f'  {i:>3}. {auth_disp:35s} {c["year"]}  [{c["type"]}:{tag}] (p{c["page"]})')
        print()
        print('--- 参考文献列表 ---')
        for i, ref in enumerate(ref_entries, 1):
            id_s = f'[{ref["id"]}]' if ref['id'] else '[?]'
            auth_disp = format_ref_author(ref)
            print(f'  {i:>3}. {id_s:5s} {auth_disp:35s} {ref["year"]}')
        print()

    # 5 — 匹配
    print('  [5/5] 执行双向匹配...')
    missing, uncited, year_mismatches = match_via_canonical(
        citations, ref_entries, fuzzy=args.fuzzy
    )

    # 跨年作者预警：对有引无文条目检查是否存在同作者不同年份的参考文献
    cross_year_warnings = find_cross_year_warnings(missing, ref_entries)
    if cross_year_warnings and args.verbose:
        print(f'    跨年作者预警: {len(cross_year_warnings)} 项（引用年份可能笔误）')

    duplicates = detect_duplicate_references(ref_entries)

    # 引用质量统计
    stats = compute_quality_stats(
        citations, ref_entries, missing, uncited,
        year_mismatches, duplicates, style
    )

    elapsed = time.time() - start

    # 6 — 报告
    text_report = generate_report(pdf_path, citations, ref_entries, missing, uncited,
                                  year_mismatches, elapsed, cross_year_warnings)
    html_report = generate_html_report(
        pdf_path, citations, ref_entries, missing, uncited,
        year_mismatches, duplicates, elapsed,
        cross_year_warnings
    )

    # 保存 HTML 报告（默认）
    html_out = pdf_path.with_suffix('.html')
    with open(html_out, 'w', encoding='utf-8') as f:
        f.write(html_report)
    print(f'\nHTML 报告已保存至: {html_out}')

    # 如果指定了 -o，同时保存一份文本版
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(text_report)
        print(f'文本报告已保存至: {args.output}')
    else:
        print()
        print(text_report)

    # 标准化引用导出
    if args.std_cite:
        print()
        print('  [导出] 生成标准化引用文件...')
        export_standard_citation_files(pdf_path, citations, ref_entries)

    # ========================================
    #  AI 辅助分析（可选，需 LM Studio）
    # ========================================
    if args.ai:
        print()
        print('  [AI] 加载 AI 分析引擎...')
        try:
            from citation_ai_v1_2 import LMStudioClient, ai_do_full_review, ai_generate_review_summary

            client = LMStudioClient(args.lm_studio_url, args.ai_model)
            if client.is_available():
                print(f'  [AI] LM Studio 已连接: {args.lm_studio_url}')

                # 快速模式：只做 Reference Recovery + 审稿评语
                # 全量模式：增加逐项验证缺失/不匹配引用
                if args.ai_full:
                    ai_result = ai_do_full_review(
                        client, citations, ref_entries,
                        missing, uncited,
                        year_mismatches,
                        [],  # author_mismatches removed in v1.2
                        duplicates, unparsed_refs, pages, stats
                    )
                else:
                    print('  [AI] 快速模式——仅 Recovery + 审稿评语（加 --ai-full 启用逐项验证）')
                    from citation_ai_v1_2 import ai_recover_references_batch
                    ai_result = {'missing_verdicts': [], 'author_verdicts': [], 'year_verdicts': []}
                    if unparsed_refs:
                        print(f'  [AI] 正在尝试恢复 {len(unparsed_refs)} 条未解析的参考文献...')
                        ai_result['recovered_refs'] = ai_recover_references_batch(client, unparsed_refs)
                    else:
                        ai_result['recovered_refs'] = []

                # -- 1. Reference Recovery 结果 --
                recovered = ai_result.get('recovered_refs', [])
                recovered_ok = [r for r in recovered if r]
                if recovered_ok:
                    print(f'\n  [AI] ✅ Reference Recovery: {len(recovered_ok)} 条未解析参考文献被恢复')
                    for r in recovered_ok[:5]:
                        print(f'    → {r.get("first_author","?"):20s} {r.get("year","?")}  [{r.get("title","")[:50]}]')
                    if len(recovered_ok) > 5:
                        print(f'    ... 还有 {len(recovered_ok)-5} 条')

                # -- 2. 有引无文验证（仅全量模式） --
                mv = ai_result.get('missing_verdicts', [])
                fp_count = sum(1 for _, v in mv if not v.get('is_missing', True))
                if fp_count:
                    print(f'\n  [AI] ⚠ 疑似误报（AI 认为引用存在于上下文）: {fp_count} 项')
                    for idx, verdict in mv:
                        if not verdict.get('is_missing', True):
                            c = missing[idx]
                            auth_disp = format_citation_author(c)
                            print(f'    {auth_disp:30s} {c["year"]}  → {verdict.get("reason", "")[:80]}')

                # -- 3. 作者/年份不匹配（仅全量模式） --
                av = ai_result.get('author_verdicts', [])
                same_count = sum(1 for _, v in av if v.get('is_same_person', False))
                if same_count:
                    print(f'\n  [AI] 🔍 作者可能为同一人: {same_count} 项')

                yv = ai_result.get('year_verdicts', [])
                same_work = sum(1 for _, v in yv if v.get('is_same_work', False))
                if same_work:
                    print(f'\n  [AI] 🔍 年份差异可能为同一文献: {same_work} 项')

                # -- 4. 审稿评语 --
                stats['ai_false_positive'] = fp_count
                stats['ai_confirmed_match'] = same_count + same_work
                print('  [AI] 正在生成审稿评语...')
                review = ai_generate_review_summary(client, stats)
                if review:
                    print(f'\n  [AI] 📝 审稿评语:\n{"="*60}')
                    print(f'    {review}')
                    print(f'{"="*60}')
            else:
                print(f'  [AI] 无法连接 LM Studio ({args.lm_studio_url})')
                print('  请确保: 1) LM Studio 已启动  2) 已加载模型  3) 已开启本地服务器')
        except ImportError:
            print('  [AI] 未找到 citation_ai.py，AI 分析不可用。')
            print('  确保 citation_ai.py 与此文件在同一目录。')
        except Exception as e:
            print(f'  [AI] AI 分析出错: {e}')
            import traceback
            traceback.print_exc()


if __name__ == '__main__':
    main()
