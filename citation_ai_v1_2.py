#!/usr/bin/env python3
"""
citation_ai_v1_2.py - LM Studio AI 辅助分析模块 (Version 1.2)
=============================================================

通过本地 LM Studio 的 OpenAI 兼容 API 对审计结果进行智能验证。
完全独立于 citation_audit_v1.2.py，离线模式无需安装此模块。

用法:
  1. 启动 LM Studio，加载模型，开启本地服务器（默认 localhost:1234）
  2. 在 citation_audit_v1.2.py 中加 --ai 参数即可

依赖: 无 (使用标准库 urllib.request)
"""

import json
import sys
import urllib.request
from typing import Optional


# ============================================================
#  LM Studio API 客户端
# ============================================================

class LMStudioClient:
    """与本地 LM Studio 的 OpenAI 兼容 API 通信"""

    def __init__(self, base_url: str = "http://localhost:1234/v1",
                 model: Optional[str] = None,
                 timeout: int = 60):
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout

    def _chat(self, messages, temperature=0.0, max_tokens=512, timeout=None):
        """发送聊天补全请求，返回响应文本"""
        payload = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.model:
            payload["model"] = self.model

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode('utf-8'),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        actual_timeout = timeout if timeout is not None else self.timeout
        try:
            with urllib.request.urlopen(req, timeout=actual_timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            raise ConnectionError(f"LM Studio 通信失败: {e}")

    def is_available(self) -> bool:
        """测试 LM Studio 是否可达"""
        try:
            req = urllib.request.Request(f"{self.base_url}/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False


# ============================================================
#  AI 验证函数
# ============================================================

def ai_verify_missing(client: LMStudioClient,
                      author: str, year: str, context: str,
                      prev_page: str = "", next_page: str = "") -> dict:
    """
    验证『有引无文』项：该引用是否确实缺失，还是工具漏匹配。

    返回 {'is_missing': bool, 'reason': str, 'confidence': float}
    """
    system_msg = (
        "你是一个学术论文引用检查助手。用户会给出一条正文引用的信息 "
        "（作者，年份）及周围上下文。请判断这条引用是否确实在参考文献中不存在，"
        "还是说它其实存在于上下文某处（如括号内、脚注中、表格内）而工具漏掉了。"
        "只回答 JSON 格式，不要多余文字。"
    )
    context_trimmed = context[:800]
    user_msg = (
        f"引用: {author} ({year})\n"
        f"上下文: {context_trimmed}\n"
    )
    if prev_page:
        user_msg += f"上一页末尾: {prev_page[-300:]}\n"
    if next_page:
        user_msg += f"下一页开头: {next_page[:300]}\n"
    user_msg += (
        "请判断: 这个引用是否真正缺失？"
        "回答 JSON: {\"is_missing\": true/false, \"reason\": \"...\", \"confidence\": 0.0-1.0}"
    )

    try:
        reply = client._chat([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ], temperature=0.0)
        reply_clean = reply.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        return json.loads(reply_clean)
    except Exception as e:
        return {"is_missing": True, "reason": f"AI 分析失败: {e}", "confidence": 0.0}


def ai_verify_author_mismatch(client: LMStudioClient,
                               cite_author: str, ref_author: str, year: str) -> dict:
    """
    验证『作者不匹配』项：两个名字是否指向同一个人。

    返回 {'is_same_person': bool, 'reason': str, 'confidence': float}
    """
    system_msg = (
        "你是一个学术引用核查助手。用户给出正文引用中的作者姓名和参考文献中的作者姓名（同年份），"
        "请判断它们是否可能指向同一个人（如姓+名缩写差异、中西文名差异、拼写变体等）。"
        "只回答 JSON 格式。"
    )
    user_msg = (
        f"年份: {year}\n"
        f"正文作者: {cite_author}\n"
        f"文献作者: {ref_author}\n"
        "它们是同一个人吗？"
        "回答 JSON: {\"is_same_person\": true/false, \"reason\": \"...\", \"confidence\": 0.0-1.0}"
    )

    try:
        reply = client._chat([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ], temperature=0.0)
        reply_clean = reply.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        return json.loads(reply_clean)
    except Exception as e:
        return {"is_same_person": False, "reason": f"AI 分析失败: {e}", "confidence": 0.0}


def ai_verify_year_mismatch(client: LMStudioClient,
                             cite_author: str, ref_author: str,
                             cite_year: str, ref_year: str) -> dict:
    """
    验证『年份不匹配』项：同作者不同年份是否为同一文献的不同版本。

    返回 {'is_same_work': bool, 'reason': str, 'confidence': float}
    """
    system_msg = (
        "你是一个学术引用核查助手。用户给出一条引用的作者+年份和文献的作者+年份，"
        "请判断它们是否可能是同一篇文献（如重印版、翻译版、电子版与印刷版年份不同）。"
        "只回答 JSON 格式。"
    )
    user_msg = (
        f"正文引用: {cite_author} ({cite_year})\n"
        f"参考文献: {ref_author} ({ref_year})\n"
        "它们可能是同一篇文献吗？"
        "回答 JSON: {\"is_same_work\": true/false, \"reason\": \"...\", \"confidence\": 0.0-1.0}"
    )

    try:
        reply = client._chat([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ], temperature=0.0)
        reply_clean = reply.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        return json.loads(reply_clean)
    except Exception as e:
        return {"is_same_work": False, "reason": f"AI 分析失败: {e}", "confidence": 0.0}


def ai_generate_summary(client: LMStudioClient,
                         total_cites: int, total_refs: int,
                         missing: list, uncited: list,
                         year_mismatches: list, author_mismatches: list,
                         duplicates: list,
                         style: str = "") -> str:
    """生成审计总结的自然语言段落"""
    system_msg = (
        "你是一个学术写作助理。根据以下引用审计数据，生成一段简要的中文分析总结，"
        "指出主要问题和可能原因。语气专业、客观。控制在 200 字以内。"
    )
    user_msg = (
        f"引用风格: {style}\n"
        f"正文引用: {total_cites} 处 | 参考文献: {total_refs} 条\n"
        f"有引无文: {len(missing)} 项 | 有文无引: {len(uncited)} 项\n"
        f"年份不匹配: {len(year_mismatches)} 项 | 作者不匹配: {len(author_mismatches)} 项\n"
        f"疑似重复: {len(duplicates)} 项\n"
        "请给出简要的审计分析总结。"
    )

    try:
        reply = client._chat([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ], temperature=0.3, max_tokens=300)
        return reply
    except Exception as e:
        return f"(AI 分析不可用: {e})"


# ============================================================
#  Reference Recovery — AI 解析规则引擎无法处理的参考文献
# ============================================================

def ai_recover_reference(client: LMStudioClient, raw_text: str) -> dict:
    """
    让 LLM 尝试解析规则引擎无法处理的参考文献条目。

    返回 {'first_author': str, 'year': str, 'title': str, 'confidence': float}
    失败时返回 None。
    """
    system_msg = (
        "你是一个参考文献解析助手。用户会给你一条参考文献文本，"
        "请从中提取：第一作者姓氏、年份、论文标题。"
        "只回答 JSON 格式，不要多余文字。"
        "如果某字段无法提取，用空字符串代替。"
    )
    user_msg = (
        f"参考文献:\n{raw_text[:600]}\n\n"
        "请提取以下字段：\n"
        '{"first_author": "", "year": "", "title": "", "confidence": 0.0}'
    )
    try:
        reply = client._chat([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ], temperature=0.0, max_tokens=256)
        reply_clean = reply.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
        data = json.loads(reply_clean)
        # 验证必要字段
        if data.get("first_author") and len(data["first_author"]) >= 2:
            return data
        return None
    except Exception:
        return None


def ai_recover_references_batch(client: LMStudioClient, unparsed_texts: list) -> list:
    """批量恢复未解析的参考文献"""
    results = []
    for i, text in enumerate(unparsed_texts):
        recovered = ai_recover_reference(client, text)
        results.append(recovered)
        if (i + 1) % 10 == 0:
            print(f"    ...已恢复 {i+1}/{len(unparsed_texts)}")
    return results


# ============================================================
#  Review Assistant — 生成审稿质量评语
# ============================================================

def ai_generate_review_summary(client: LMStudioClient, stats: dict) -> str:
    """
    根据引用质量统计数据，生成一段可供审稿使用的参考文献质量评语。

    返回 200~300 字的中文评语。
    """
    system_msg = (
        "你是一位学术期刊的审稿人。请根据以下引用审计统计数据，"
        "写一段专业、客观的参考文献质量评语，语气适合在审稿意见中使用。"
        "控制在 200~300 字。"
    )
    user_msg = (
        f"【基本信息】\n"
        f"引用风格: {stats.get('style', '?')}\n"
        f"正文引用数: {stats['total_cites']} | 参考文献数: {stats['total_refs']}\n"
        f"匹配通过率: {stats['match_rate']:.1f}%\n\n"
        f"【一致性问题】\n"
        f"有引无文: {stats['missing_count']} | 有文无引: {stats['uncited_count']}\n"
        f"年份不匹配: {stats['year_mismatch_count']} | 作者不匹配: {stats['author_mismatch_count']}\n"
        f"疑似重复: {stats['duplicate_count']}\n\n"
        f"【引用质量指标】\n"
        f"近五年文献占比: {stats['recent_ratio']:.1f}%\n"
        f"英文文献占比: {stats['en_ratio']:.1f}%\n"
        f"中文文献占比: {stats['zh_ratio']:.1f}%\n"
        f"AI 确认误报: {stats.get('ai_false_positive', 0)} 项\n"
        f"AI 确认匹配: {stats.get('ai_confirmed_match', 0)} 项\n\n"
        "请写出审稿意见中关于参考文献质量的部分。"
    )
    try:
        reply = client._chat([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}

        ], temperature=0.3, max_tokens=500, timeout=300)
        return reply
    except Exception as e:
        return f"(AI 审稿摘要生成失败: {e})"


# ============================================================
#  AI 辅助评审聚合入口
# ============================================================

def ai_do_full_review(client: LMStudioClient,
                       citations: list, ref_entries: list,
                       missing: list, uncited: list,
                       year_mismatches: list, author_mismatches: list,
                       duplicates: list,
                       unparsed_refs: list,
                       pages: list,
                       stats: dict) -> dict:
    """
    执行完整的 AI 辅助评审流程：
      1. Reference Recovery — 解析规则引擎未解析的参考文献
      2. 异常项验证（有引无文/作者不匹配/年份不匹配）
      3. 审稿评语生成

    返回:
    {
        'recovered_refs': [dict or None, ...],
        'missing_verdicts': [(index, dict), ...],
        'author_verdicts': [(index, dict), ...],
        'year_verdicts': [(index, dict), ...],
        'review_summary': str,
    }
    """
    page_map = {p: t for p, t in pages}
    result = {
        "recovered_refs": [],
        "missing_verdicts": [],
        "author_verdicts": [],
        "year_verdicts": [],
        "review_summary": ""
    }

    # 1. Reference Recovery
    if unparsed_refs:
        print(f"  [AI] 正在尝试恢复 {len(unparsed_refs)} 条未解析的参考文献...")
        result["recovered_refs"] = ai_recover_references_batch(client, unparsed_refs)
        recovered = sum(1 for r in result["recovered_refs"] if r)
        print(f"    AI 恢复成功: {recovered}/{len(unparsed_refs)}")

    # 2. 验证有引无文
    if missing:
        print(f"  [AI] 正在验证 {len(missing)} 项有引无文...")
        for i, c in enumerate(missing):
            pg = c.get("page", 0)
            context = page_map.get(pg, "")
            prev_text = page_map.get(pg - 1, "") if pg > 1 else ""
            next_text = page_map.get(pg + 1, "") if pg < len(pages) else ""
            verdict = ai_verify_missing(client, c["author"], c["year"],
                                         context, prev_text, next_text)
            result["missing_verdicts"].append((i, verdict))
            if (i + 1) % 10 == 0:
                print(f"    ...已分析 {i+1}/{len(missing)}")
        print(f"    AI 验证完成: {len(missing)} 项")

    # 3. 验证作者不匹配
    if author_mismatches:
        print(f"  [AI] 正在分析 {len(author_mismatches)} 项作者不匹配...")
        for i, am in enumerate(author_mismatches):
            verdict = ai_verify_author_mismatch(
                client, am["cite_author"], am["ref_author"], am["citation"]["year"]
            )
            result["author_verdicts"].append((i, verdict))
        print(f"    AI 分析完成: {len(author_mismatches)} 项")

    # 4. 验证年份不匹配
    if year_mismatches:
        print(f"  [AI] 正在分析 {len(year_mismatches)} 项年份不匹配...")
        for i, ym in enumerate(year_mismatches):
            verdict = ai_verify_year_mismatch(
                client, ym["citation"]["author"], ym["reference"]["first_author"],
                ym["cite_year"], ym["ref_year"]
            )
            result["year_verdicts"].append((i, verdict))
        print(f"    AI 分析完成: {len(year_mismatches)} 项")

    # 5. 审稿评语
    print("  [AI] 正在生成审稿评语...")
    result["review_summary"] = ai_generate_review_summary(client, stats)

    return result


# ============================================================
#  便捷入口：一次验证所有缺失引用
# ============================================================

def ai_audit_all(client: LMStudioClient,
                 citations: list, ref_entries: list,
                 missing: list, uncited: list,
                 year_mismatches: list, author_mismatches: list,
                 pages: list,
                 batch_mode: bool = True) -> dict:
    """
    对审计发现的所有问题进行 AI 验证。

    返回:
    {
        'missing_verdicts': [(index, verdict_dict), ...],
        'author_verdicts':  [(index, verdict_dict), ...],
        'year_verdicts':    [(index, verdict_dict), ...],
        'summary': str
    }
    """
    # 构建页码→文本映射
    page_map = {p: t for p, t in pages}

    result = {
        "missing_verdicts": [],
        "author_verdicts": [],
        "year_verdicts": [],
        "summary": ""
    }

    if batch_mode:
        print("  [AI] 正在分析缺失引用...")
        for i, c in enumerate(missing):
            pg = c.get("page", 0)
            context = page_map.get(pg, "")
            prev_text = page_map.get(pg - 1, "") if pg > 1 else ""
            next_text = page_map.get(pg + 1, "") if pg < len(pages) else ""
            verdict = ai_verify_missing(client, c["author"], c["year"],
                                         context, prev_text, next_text)
            result["missing_verdicts"].append((i, verdict))
            if (i + 1) % 10 == 0:
                print(f"    ...已分析 {i+1}/{len(missing)}")
        print(f"    AI 分析完成: {len(missing)} 项")

        print("  [AI] 正在分析作者不匹配...")
        for i, am in enumerate(author_mismatches):
            verdict = ai_verify_author_mismatch(
                client, am["cite_author"], am["ref_author"], am["citation"]["year"]
            )
            result["author_verdicts"].append((i, verdict))
        print(f"    AI 分析完成: {len(author_mismatches)} 项")

        print("  [AI] 正在分析年份不匹配...")
        for i, ym in enumerate(year_mismatches):
            verdict = ai_verify_year_mismatch(
                client, ym["citation"]["author"], ym["reference"]["first_author"],
                ym["cite_year"], ym["ref_year"]
            )
            result["year_verdicts"].append((i, verdict))
        print(f"    AI 分析完成: {len(year_mismatches)} 项")

    return result


# ============================================================
#  命令行入口：测试 LM Studio 连接
# ============================================================

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='LM Studio AI 连接测试')
    parser.add_argument('--url', default='http://localhost:1234/v1', help='LM Studio API 地址')
    parser.add_argument('--model', help='模型名称（可选）')
    parser.add_argument('--test', action='store_true', help='发送测试对话')
    args = parser.parse_args()

    client = LMStudioClient(args.url, args.model)
    if client.is_available():
        print(f"✓ LM Studio 连接成功: {args.url}")
        if args.test:
            print("  发送测试消息...")
            reply = client._chat([
                {"role": "user", "content": "用一句话解释参考文献的作用。"}
            ])
            print(f"  回复: {reply}")
    else:
        print(f"✗ LM Studio 未响应: {args.url}")
        print("  请确保 LM Studio 已启动并已开启本地服务器。")
        sys.exit(1)
