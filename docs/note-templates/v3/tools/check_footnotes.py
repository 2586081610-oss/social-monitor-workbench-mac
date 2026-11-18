#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3 脚标校验工具。

用法：
    python3 check_footnotes.py <note.md>            # 人读报告
    python3 check_footnotes.py <note.md> --json     # 机器可读
    python3 check_footnotes.py <note.md> --strict   # warning 也算失败
    python3 check_footnotes.py --self-test          # 内置用例自检

只读本地 Markdown，不联网、不写文件（自检也不落盘）。
退出码：0 通过；1 有 error（或 --strict 下有 warning）；2 参数或读取失败。
"""

import argparse
import json
import re
import sys

# 合法脚标前缀：v 原片、s 支持、q 质疑、d 权威数据、u 未找到
VALID_PREFIXES = {
    "v": "原片",
    "s": "支持",
    "q": "质疑",
    "d": "权威数据",
    "u": "未找到",
}

# frontmatter 字段 -> 对应的脚标前缀
FRONTMATTER_FIELDS = {
    "evidence_support": "s",
    "evidence_question": "q",
    "evidence_data": "d",
    "evidence_unfound": "u",
}

REF_RE = re.compile(r"\[\^([A-Za-z]+)(\d+)\]")
DEF_RE = re.compile(r"^ {0,3}\[\^([A-Za-z]+)(\d+)\]:")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s*(.+?)\s*#*\s*$")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

DENSITY_MIN = 0.2
DENSITY_MAX = 2.0
MIN_CHARS_FOR_DENSITY = 100


def _entry(code, message, line=None):
    """构造一条 error/warning 记录。"""
    return {"code": code, "message": message, "line": line}


def _format_entry(item):
    """把一条记录格式化成人类可读的一行。"""
    prefix = "第 {} 行：".format(item["line"]) if item.get("line") else ""
    return "[{}] {}{}".format(item["code"], prefix, item["message"])


def _parse_frontmatter(lines):
    """解析 frontmatter 里的简单 key: value，忽略注释与空行。"""
    data = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            data[key] = value
    return data


def _split_frontmatter(lines):
    """返回 (frontmatter 字典, 正文起始行号)；没有 frontmatter 时返回 ({}, 0)。"""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return _parse_frontmatter(lines[1:index]), index + 1
    return {}, 0


def analyze(text):
    """核心校验：返回 errors / warnings / stats。"""
    errors = []
    warnings = []

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    frontmatter, body_start = _split_frontmatter(lines)

    refs = []          # [(编号, 行号)]，同一编号可重复出现
    definitions = []   # [(编号, 行号)]
    body_chars = 0     # 正文中文字符数
    has_ledger = False
    in_fence = False
    fence_marker = ""

    for index in range(body_start, len(lines)):
        raw = lines[index]
        line_no = index + 1
        stripped = raw.strip()

        # 围栏代码块整体跳过，避免把示例代码当成真实脚标
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if in_fence:
                if marker == fence_marker:
                    in_fence = False
                    fence_marker = ""
            else:
                in_fence = True
                fence_marker = marker
            continue
        if in_fence:
            continue

        heading = HEADING_RE.match(raw)
        if heading and "证据台账" in heading.group(1):
            has_ledger = True

        definition = DEF_RE.match(raw)
        if definition:
            prefix, number = definition.group(1), definition.group(2)
            if prefix not in VALID_PREFIXES:
                errors.append(_entry(
                    "E003",
                    "定义 [^{}{}]: 使用了非法前缀「{}」，合法前缀为 v/s/q/d/u".format(prefix, number, prefix),
                    line_no,
                ))
            else:
                definitions.append((prefix + number, line_no))
            # 定义行属于参考信息，不计入正文字符数；行内若还有引用则继续扫描
            scan_text = raw[definition.end():]
        else:
            body_chars += len(CJK_RE.findall(raw))
            scan_text = raw

        # 行内代码 `...` 里的内容属于示例，不当成真实引用
        scan_text = re.sub(r"`[^`]*`", "", scan_text)

        for match in REF_RE.finditer(scan_text):
            prefix, number = match.group(1), match.group(2)
            if prefix not in VALID_PREFIXES:
                errors.append(_entry(
                    "E003",
                    "引用 [^{}{}] 使用了非法前缀「{}」，合法前缀为 v/s/q/d/u".format(prefix, number, prefix),
                    line_no,
                ))
            else:
                refs.append((prefix + number, line_no))

    # 重复定义
    first_seen = {}
    unique_defs = {}
    for key, line_no in definitions:
        if key in first_seen:
            errors.append(_entry(
                "E002",
                "编号 [^{}] 重复定义（首次定义在第 {} 行）".format(key, first_seen[key]),
                line_no,
            ))
        else:
            first_seen[key] = line_no
            unique_defs[key] = line_no

    # 未定义引用：同一编号只报一次
    missing = {}
    for key, line_no in refs:
        if key not in unique_defs and key not in missing:
            missing[key] = line_no
    for key, line_no in missing.items():
        errors.append(_entry(
            "E001",
            "引用 [^{}] 没有对应的定义 [^{}]:".format(key, key),
            line_no,
        ))

    # 未使用定义
    referenced_keys = {key for key, _ in refs}
    for key, line_no in unique_defs.items():
        if key not in referenced_keys:
            warnings.append(_entry(
                "W001",
                "定义 [^{}]: 未被正文引用".format(key),
                line_no,
            ))

    # 证据台账标题
    if not has_ledger:
        errors.append(_entry("E004", "缺少「证据台账」标题（例如 `## 证据台账`）", None))

    # 按类型统计：数量以去重后的定义为准
    def_counts = {prefix: 0 for prefix in VALID_PREFIXES}
    for key in unique_defs:
        def_counts[key[0]] += 1
    ref_counts = {prefix: 0 for prefix in VALID_PREFIXES}
    for key, _ in refs:
        ref_counts[key[0]] += 1

    # 支持/质疑平衡
    external_total = sum(def_counts[p] for p in ("s", "q", "d", "u"))
    if external_total and def_counts["q"] == 0 and def_counts["u"] == 0:
        if def_counts["s"]:
            message = "只有支持证据（s），至少需要 1 条质疑（q）或未找到（u）"
        else:
            message = "只有权威数据（d），至少需要 1 条质疑（q）或未找到（u）"
        warnings.append(_entry("W002", message, None))

    # 引用密度：每 100 个中文字符的脚标引用数
    refs_total = len(refs)
    density = None
    density_checked = body_chars >= MIN_CHARS_FOR_DENSITY
    if density_checked:
        density = round(refs_total * 100.0 / body_chars, 3)
        if density < DENSITY_MIN:
            warnings.append(_entry(
                "W003",
                "引用密度 {:.3f}/100 字，低于下限 {}".format(density, DENSITY_MIN),
                None,
            ))
        elif density > DENSITY_MAX:
            warnings.append(_entry(
                "W004",
                "引用密度 {:.3f}/100 字，高于上限 {}".format(density, DENSITY_MAX),
                None,
            ))

    # frontmatter 自报数量必须与实际统计一致
    for field, prefix in FRONTMATTER_FIELDS.items():
        if field not in frontmatter:
            continue
        raw_value = frontmatter[field]
        try:
            declared = int(raw_value)
        except (TypeError, ValueError):
            errors.append(_entry(
                "E006",
                "frontmatter 字段 {} 的值「{}」不是整数".format(field, raw_value),
                None,
            ))
            continue
        actual = def_counts[prefix]
        if declared != actual:
            errors.append(_entry(
                "E005",
                "frontmatter {}={}，实际 {} 类型定义为 {} 条".format(field, declared, prefix, actual),
                None,
            ))

    stats = {
        "total_chinese_chars": body_chars,
        "total_footnotes": refs_total,
        "unique_footnotes": len(unique_defs),
        "citation_density": density,
        "density_checked": density_checked,
        "by_type": {
            prefix: {
                "label": label,
                "references": ref_counts[prefix],
                "definitions": def_counts[prefix],
            }
            for prefix, label in VALID_PREFIXES.items()
        },
    }

    return {"errors": errors, "warnings": warnings, "stats": stats}


def exit_code_for(result, strict):
    """有 error 返回 1；--strict 下有 warning 也返回 1；否则 0。"""
    if result["errors"]:
        return 1
    if strict and result["warnings"]:
        return 1
    return 0


def format_report(path, result, strict, code):
    """生成人读报告。"""
    errors = result["errors"]
    warnings = result["warnings"]
    stats = result["stats"]

    lines = ["脚标校验：{}".format(path)]

    lines.append("[错误] {} 条".format(len(errors)))
    for item in errors:
        lines.append("  " + _format_entry(item))

    lines.append("[警告] {} 条".format(len(warnings)))
    for item in warnings:
        lines.append("  " + _format_entry(item))

    if stats["density_checked"]:
        density_text = "{:.3f} / 100 字".format(stats["citation_density"])
    else:
        density_text = "未校验（中文字符不足 {}）".format(MIN_CHARS_FOR_DENSITY)
    lines.append("[统计] 中文字符 {}｜脚标引用 {} 个（唯一定义 {} 个）｜密度 {}".format(
        stats["total_chinese_chars"],
        stats["total_footnotes"],
        stats["unique_footnotes"],
        density_text,
    ))
    lines.append("[统计] 定义：v {}｜s {}｜q {}｜d {}｜u {}".format(
        stats["by_type"]["v"]["definitions"],
        stats["by_type"]["s"]["definitions"],
        stats["by_type"]["q"]["definitions"],
        stats["by_type"]["d"]["definitions"],
        stats["by_type"]["u"]["definitions"],
    ))

    if code == 0:
        lines.append("[结果] PASS")
    elif errors:
        lines.append("[结果] FAIL（存在 error）")
    else:
        lines.append("[结果] FAIL（--strict 下的 warning）")

    return "\n".join(lines)


def build_payload(path, result, strict, code):
    """生成 --json 的机器可读结果。"""
    return {
        "file": path,
        "strict": strict,
        "exit_code": code,
        "ok": code == 0,
        "errors": result["errors"],
        "warnings": result["warnings"],
        "stats": result["stats"],
    }


# ---------------------------------------------------------------------------
# 自检用例：全部在内存中运行，不读写任何文件
# ---------------------------------------------------------------------------

_CASE_OK = """---
title: 睡眠与恢复
evidence_support: 1
evidence_question: 1
evidence_data: 1
evidence_unfound: 1
---

# 睡眠与恢复

## 一句话

睡眠不足会明显拖慢训练后的恢复速度，这一点博主用自己连续两周的记录来说明[^v1]。慢呼吸能够提高迷走神经介导的心率变异性，这一点有系统综述和元分析支持[^s1]；但「四七八」这种固定比例并没有直接随机对照试验[^u1]。官方指南给成年人的建议是每晚七小时以上[^d1]。不过，把训练表现下降全部归因于睡眠并不成立，疲劳、热量缺口和训练量都需要一起看[^q1]。

## 一、具体做法

他把睡前流程拆成三步：先关掉所有强光，再做十分钟慢呼吸，最后把手机放到卧室外面。这三步里只有慢呼吸有比较明确的生理机制，其余两步属于个人经验，本条不额外引用外部研究。

## 二、边界条件

他特别强调，这套流程只在训练日执行，休息日不必照做；这个区分来自他个人的体感，而不是来自对照实验，因此只能当作经验参考，不能当成普遍适用的结论。

## 证据台账

| 编号 | 类型 | 来源 | 年份 | 支持/质疑的对象 | 强度 |
|---|---|---|---|---|---|
| v1 | 原片 | 原片 00:10 | — | 睡眠不足拖慢恢复 | — |
| s1 | 系统综述 | 某综述 | 2022 | 慢呼吸提高 HRV | 强 |
| q1 | 队列研究 | 某研究 | 2021 | 睡眠与训练表现 | 中 |
| d1 | 权威数据 | 睡眠指南 | 2020 | 成人睡眠时长 | 强 |
| u1 | 未找到 | — | — | 四七八固定比例 | — |

## 参考文献

1. 某作者. 某标题. 某期刊, 2022.

---
[^v1]: 原片 00:10，博主原话「睡不够就别练」。
[^s1]: 支持 · 慢呼吸提高 HRV。证据强度：强。
[^q1]: 质疑 · 训练表现下降受多因素影响。证据强度：中。
[^d1]: 权威数据 · 成人每晚睡眠七小时以上。
[^u1]: 未找到直接研究——本条不对固定比例做外部背书。
"""

_CASE_MISSING_DEF = _CASE_OK.replace(
    "不额外引用外部研究。",
    "不额外引用外部研究。另外，他还提到睡前两小时不要进食[^v9]。",
)

_CASE_ONLY_SUPPORT = """# 慢呼吸与恢复

## 一句话

慢呼吸可以提升心率变异性，这一点在系统综述里有比较一致的结论[^s1]。博主在视频里也演示了同样的节奏[^v1]，并且要求每天做满十分钟。

## 一、具体做法

他要求用鼻子吸气、用嘴呼气，吸气和呼气的时间比例大约是一比一点五，整个过程保持肩膀放松。他还强调最好在固定的时间做，形成习惯之后更容易坚持。这些操作细节属于个人经验，没有额外的外部文献支持。

## 证据台账

| 编号 | 类型 |
|---|---|
| v1 | 原片 |
| s1 | 支持 |

## 参考文献

1. 某综述. 慢呼吸与心率变异性, 2022.

---
[^v1]: 原片 01:20，博主演示四秒吸气、六秒呼气。
[^s1]: 支持 · 慢呼吸提高 HRV。证据强度：强。
"""

_CASE_STRUCTURE = """---
title: 结构错误示例
evidence_support: 3
---

# 结构错误示例

正文引用一个原片[^v1]，再引用一个支持[^s1]，还有一个前缀非法的[^x2]。

---
[^v1]: 原片 00:10。
[^v1]: 原片 00:20。
[^s1]: 支持 · 某研究。
[^u1]: 未找到直接研究。
"""


def run_self_test():
    """内置自检：断言退出码与关键输出。"""
    failures = []
    checks = 0

    def expect(condition, label):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(label)

    # 用例 1：完全正确
    result = analyze(_CASE_OK)
    code = exit_code_for(result, False)
    report = format_report("case-ok.md", result, False, code)
    expect(code == 0, "用例 1 完全正确：退出码应为 0，实际 {}".format(code))
    expect(not result["errors"], "用例 1 完全正确：不应有 error")
    expect(not result["warnings"], "用例 1 完全正确：不应有 warning")
    expect(result["stats"]["by_type"]["s"]["definitions"] == 1, "用例 1：s 定义应为 1 条")
    expect(result["stats"]["by_type"]["q"]["definitions"] == 1, "用例 1：q 定义应为 1 条")
    expect(result["stats"]["by_type"]["u"]["definitions"] == 1, "用例 1：u 定义应为 1 条")
    expect("PASS" in report, "用例 1 完全正确：报告应显示 PASS")

    # 用例 2：引用缺少定义
    result = analyze(_CASE_MISSING_DEF)
    code = exit_code_for(result, False)
    report = format_report("case-missing.md", result, False, code)
    expect(code == 1, "用例 2 缺定义：退出码应为 1，实际 {}".format(code))
    expect(any(item["code"] == "E001" for item in result["errors"]), "用例 2：应报 E001")
    expect("没有对应的定义" in report and "v9" in report, "用例 2：报告应指出 [^v9] 无定义")

    # 用例 3：只有支持，没有质疑/未找到
    result = analyze(_CASE_ONLY_SUPPORT)
    strict_code = exit_code_for(result, True)
    normal_code = exit_code_for(result, False)
    report = format_report("case-support.md", result, True, strict_code)
    expect(normal_code == 0, "用例 3 只有支持：非 strict 退出码应为 0，实际 {}".format(normal_code))
    expect(strict_code == 1, "用例 3 只有支持：strict 退出码应为 1，实际 {}".format(strict_code))
    expect(any(item["code"] == "W002" for item in result["warnings"]), "用例 3：应报 W002")
    expect("只有支持" in report, "用例 3：报告应指出只有支持证据")

    # 用例 4：结构错误综合（重复定义、非法前缀、缺台账、frontmatter 不一致、未使用定义）
    result = analyze(_CASE_STRUCTURE)
    codes = {item["code"] for item in result["errors"]}
    expect(exit_code_for(result, False) == 1, "用例 4 结构错误：退出码应为 1")
    expect({"E002", "E003", "E004", "E005"} <= codes, "用例 4：应同时报 E002/E003/E004/E005")
    expect(any(item["code"] == "W001" for item in result["warnings"]), "用例 4：应报未使用定义 W001")

    if failures:
        print("[自检] 失败 {} / {} 项断言".format(len(failures), checks))
        for item in failures:
            print("  - " + item)
        return 1

    print("[自检] 通过 {} 项断言（4 个用例）".format(checks))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="校验中文视频笔记里的脚标引用与定义（v3）。"
    )
    parser.add_argument("path", nargs="?", help="要校验的 Markdown 笔记路径")
    parser.add_argument("--strict", action="store_true", help="warning 也视为失败")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--self-test", action="store_true", help="运行内置自检用例")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    if not args.path:
        parser.print_usage(sys.stderr)
        print("[错误] 需要提供 Markdown 笔记路径（或使用 --self-test）", file=sys.stderr)
        return 2

    try:
        with open(args.path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        print("[错误] 无法读取文件：{}".format(exc), file=sys.stderr)
        return 2

    result = analyze(text)
    code = exit_code_for(result, args.strict)

    if args.json:
        payload = build_payload(args.path, result, args.strict, code)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_report(args.path, result, args.strict, code))

    return code


if __name__ == "__main__":
    sys.exit(main())
