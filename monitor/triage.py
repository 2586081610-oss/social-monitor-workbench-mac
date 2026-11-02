"""兴趣分诊层：给作品定类目与处理档位（三层分诊，2026-11-01 用户确认）。

背景：流水线原设计全量直通（NEW → 下载 → 转写 → 抽帧 → AI 笔记），而个人来源
（点赞/收藏）存量 1.6 万+，末端 AI 笔记每条要跑一次完整代理会话——生产速度
远大于消费速度，积压会无限增长且低价值内容淹没知识库。用户确认：不消化全量，
按兴趣类目分诊：

  ① 标题+话题标签粗分（零成本，实测 10 条能判 7-8 条）
  ② 低置信 → 轻转写仲裁（FunASR 本地秒级免费；转写文本已存在则直接用）
  ③ 高价值类目才进全链路（抽帧 + AI 笔记），其余只归档

类目（用户 6 类 + 实弹 10 条样本补充，2026-11-01 确认）与默认档位：
  - ai_tools         AI·开源工具        → note（全链路）
  - digital_tools    数码工具·实用      → note
  - money            赚钱认知·商业      → note
  - travel           旅行攻略          → note
  - fitness          健身运动          → note（2026-11-01 用户确认新增）
  - good_works       好作品            → note
  - emotion          情感·自我成长      → archive（默认不进笔记，可配置）
  - drama            剧情娱乐·追剧      → archive
  - beauty           颜值欣赏          → archive
  - friends_support  朋友支持          → archive（靠作者关系识别，词库是兜底）
  - support_campaign 集赞支持          → archive
  - other            未分类            → archive

健身类目细分（用户口径，作笔记双层标签用，见 FITNESS_FACETS）：
  按部位（背/手臂/二头/三头/大腿/肩/胸/核心/臀/小腿）+ 睡眠/饮食/恢复/拉伸。

实测教训（写进词库设计）：
  - 隐喻标题（"闲鱼捡漏最高的山"=开源 AI 项目）只有转写能救 → tier 2 必要
  - 标题与内容可能错位（标题"涨粉分析"实为情感内容）→ 转写仲裁价值所在
  - 剧情类转写全是对白噪声，标题 #剧情 标签反而更可靠 → 不强求转写
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# 类目 → 关键词词库（命中按"去重关键词个数"计分；顺序即同分优先级）
CATEGORY_LEXICON: dict[str, tuple[str, ...]] = {
    "ai_tools": (
        "github", "开源", "大模型", "ai工具", "ai改造", "codex", "claude", "gpt",
        "agent", "智能体", "vibe coding", "vibecoding", "人工智能", "anthropic",
        "豆包", "deepseek", "通用人工智能", "ai ", "ai·", "ai！", "aigc", "prompt",
    ),
    "digital_tools": (
        "app", "软件", "mac", "windows", "安卓", "鸿蒙", "网盘", "下载", "效率",
        "清理", "去广告", "智能家居", "全屋智能", "数码", "改造", "测评", "装机",
        "显示器", "键盘", "浏览器", "插件", "快捷指令", "自动化神器", "不限速",
    ),
    "money": (
        "赚钱", "认知", "副业", "干货", "变现", "涨粉", "流量", "生意", "理财",
        "基金", "纳指", "纳斯达克", "qdii", "溢价", "投资", "止损", "爆仓", "仓",
        "存款", "搞钱", "生意经", "创业", "复盘",
    ),
    # 健身：动作/部位词为主信号（多字词，避免"背/肩/腿"单字误命中"背单词"等）。
    # 睡眠/饮食/拉伸等是弱信号（用户口径的健身周边维度）：单独出现不给 high；
    # "恢复"过于通用（手机数据恢复）不进分类词库，只留在 FITNESS_FACETS 标签细分。
    "fitness": (
        "健身", "引体", "划船", "深蹲", "硬拉", "卧推", "推举", "二头", "三头",
        "手臂", "背部", "练背", "背阔肌", "大腿", "腿部", "练腿", "肩部", "推肩",
        "斜方肌", "胸肌", "练胸", "核心", "腹肌", "臀桥", "臀部训练", "小腿",
        "有氧", "增肌", "减脂", "体脂", "力量训练", "抗阻", "组数", "重量训练",
        "睡眠", "饮食", "拉伸", "蛋白质", "热量缺口", "基础代谢",
    ),
    "emotion": (
        "情感", "恋爱", "脱单", "相亲", "分手", "失恋", "婚姻", "情绪", "成长",
        "自我", "人生思考", "聆听", "缘分", "暗恋", "心事", "治愈", "讨好",
        "吸引力", "展示面", "交友", "孤独", "焦虑", "冥想", "王阳明", "龙场悟道",
    ),
    "travel": (
        "攻略", "旅行", "徒步", "赏枫", "赏秋", "自驾", "景点", "机位", "打卡",
        "路线", "一日游", "自由行", "穷游", "露营", "爬山",
    ),
    "drama": ("剧情", "短剧", "第一集", "第二集", "第十九集", "全集", "影视", "电视剧", "番外"),
    "beauty": ("美女", "颜值", "帅哥", "原相机", "氛围感", "蟀", "漂亮", "绝美", "颜值天花板"),
    "friends_support": ("兄弟帮我", "朋友的作品", "支持一下我朋友", "我朋友的店"),
    "good_works": ("神作", "名场面", "高能", "绝了", "封神", "顶级", "天花板"),
    "support_campaign": ("求赞", "集赞", "帮我点赞", "助力", "投票", "砍一刀", "转发抽奖"),
}

# 类目 → 处理档位：note=全链路（抽帧+AI 笔记）；archive=只归档（转写可留，不进笔记）
CATEGORY_ACTIONS: dict[str, str] = {
    "ai_tools": "note",
    "digital_tools": "note",
    "money": "note",
    "travel": "note",
    "fitness": "note",
    "good_works": "note",
    "emotion": "archive",
    "drama": "archive",
    "beauty": "archive",
    "friends_support": "archive",
    "support_campaign": "archive",
    "other": "archive",
}

CATEGORY_LABELS: dict[str, str] = {
    "ai_tools": "AI·开源工具",
    "digital_tools": "数码工具·实用",
    "money": "赚钱认知·商业",
    "travel": "旅行攻略",
    "fitness": "健身运动",
    "good_works": "好作品",
    "emotion": "情感·自我成长",
    "drama": "剧情娱乐·追剧",
    "beauty": "颜值欣赏",
    "friends_support": "朋友支持",
    "support_campaign": "集赞支持",
    "other": "未分类",
}

# 健身笔记的双层标签细分（2026-11-01 用户口径：按部位 + 睡眠/饮食/恢复/拉伸）。
# 值为触发该细分的关键词；笔记执行者可用 matched 命中反查细分作 #小标签。
FITNESS_FACETS: dict[str, tuple[str, ...]] = {
    "背": ("背部", "练背", "背阔肌", "引体", "划船"),
    "手臂": ("手臂", "二头", "三头"),
    "大腿": ("大腿", "腿部", "练腿", "深蹲", "硬拉"),
    "肩": ("肩部", "推肩", "斜方肌"),
    "胸": ("胸肌", "练胸", "卧推"),
    "核心": ("核心", "腹肌"),
    "臀": ("臀桥", "臀部训练"),
    "小腿": ("小腿",),
    "睡眠": ("睡眠",),
    "饮食": ("饮食", "蛋白质", "热量缺口", "基础代谢"),
    "恢复": ("恢复",),
    "拉伸": ("拉伸",),
    "有氧": ("有氧",),
    "体态目标": ("增肌", "减脂", "体脂"),
}

# 消费优先级（越小越先）：博主监控/手动 > 个人来源 note 类目 > 个人来源 archive
_PRIORITY_RANK: dict[str, int] = {
    "ai_tools": 1,
    "digital_tools": 2,
    "money": 3,
    "travel": 4,
    "fitness": 5,
    "good_works": 6,
    "emotion": 8,
    "drama": 9,
    "beauty": 10,
    "friends_support": 11,
    "support_campaign": 12,
    "other": 13,
}


@dataclass(frozen=True)
class TriageResult:
    category: str
    label: str
    confidence: str  # high / medium / low
    action: str      # note / archive
    tier: int        # 1=标题粗分；2=转写仲裁
    matched: tuple[str, ...]


# 健身弱信号词：单独命中（哪怕多个）只给 medium，必须有动作/部位强信号才给 high
FITNESS_WEAK_KEYWORDS = frozenset({"睡眠", "饮食", "拉伸", "蛋白质", "热量缺口", "基础代谢"})


def _confidence_for(category: str, matched: tuple[str, ...], score: int) -> str:
    confidence = "high" if score >= 2 else ("medium" if score == 1 else "low")
    if (
        category == "fitness"
        and matched
        and all(kw in FITNESS_WEAK_KEYWORDS for kw in matched)
    ):
        confidence = "medium"
    return confidence


def _score(text: str, category: str) -> tuple[int, tuple[str, ...]]:
    lowered = text.lower()
    matched = tuple(kw for kw in CATEGORY_LEXICON[category] if kw in lowered)
    return len(matched), matched


def _pick(text: str) -> tuple[str, str, tuple[str, ...]]:
    """返回 (category, confidence, matched)；全零命中 → other/low。"""
    best_category, best_score, best_matched = "other", 0, ()
    for category in CATEGORY_LEXICON:
        score, matched = _score(text, category)
        if score > best_score:
            best_category, best_score, best_matched = category, score, matched
    confidence = _confidence_for(best_category, best_matched, best_score)
    return best_category, confidence, best_matched


def classify(title: str, tags: str = "", transcript_text: str = "") -> TriageResult:
    """三层分诊主入口：先标题+标签粗分，置信不足且有转写时用转写仲裁。

    仲裁规则（实测教训）：
    - 标题零命中（low）→ 转写非零即采纳（隐喻标题：16465"最高的山"）；
    - 标题与内容同为 medium 但类目冲突 → 内容优先（错位标题：16572
      "涨粉分析"实为情感内容）——标题关键词常常是流量钩子而非真实主题；
    - 转写 high 恒可覆盖 medium 标题。
    """
    title_text = f"{title or ''} {tags or ''}".strip()
    category, confidence, matched = _pick(title_text)
    tier = 1
    if (transcript_text or "").strip() and confidence != "high":
        category2, confidence2, matched2 = _pick(transcript_text)
        should_replace = (
            (confidence == "low" and confidence2 != "low")
            or (confidence == "medium" and confidence2 == "high")
            or (
                confidence == "medium"
                and confidence2 == "medium"
                and category2 != category
            )
        )
        if should_replace:
            category, confidence, matched = category2, confidence2, matched2
            tier = 2
    if confidence == "low":
        category = "other"
    return TriageResult(
        category=category,
        label=CATEGORY_LABELS[category],
        confidence=confidence,
        action=CATEGORY_ACTIONS[category],
        tier=tier,
        matched=matched,
    )


def priority_rank(source_type: str | None, category: str) -> int:
    """消费优先级：博主监控/手动来源最优先，个人来源按类目档位排序。"""
    if source_type and source_type != "personal_source":
        return 0
    return _PRIORITY_RANK.get(category, _PRIORITY_RANK["other"])


def facets_for(category: str, matched: tuple[str, ...]) -> tuple[str, ...]:
    """类目细分标签（双层标签里 #小标签 的候选）；目前只有健身有细分。

    用户口径（2026-11-01）：健身按部位（背/手臂/二头/三头/大腿…）+
    睡眠/饮食/恢复/拉伸 等维度细分。
    """
    if category != "fitness":
        return ()
    return tuple(
        facet for facet, keywords in FITNESS_FACETS.items() if any(kw in matched for kw in keywords)
    )


def _read_transcript_text(media_root: Path, transcript_path: str | None) -> str:
    """读转写文本用于仲裁；文件缺失/损坏如实返回空（绝不编造）。"""
    if not transcript_path:
        return ""
    path = media_root / transcript_path
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) else ""


def scan(limit: int = 0, use_transcript: bool = True, source: str = "all") -> list[dict]:
    """只读扫描 works 表并分诊，返回按入库倒序的行（CLI 展示用；不写库）。"""
    from monitor.store.shared_db import SessionLocal, settings

    from app.models import Work
    from sqlalchemy import select

    rows: list[dict] = []
    with SessionLocal() as db:
        stmt = select(Work).order_by(Work.id.desc())
        if source == "personal":
            stmt = stmt.where(Work.source_type == "personal_source")
        if limit > 0:
            stmt = stmt.limit(limit)
        for work in db.execute(stmt).scalars():
            transcript = ""
            if use_transcript:
                transcript = _read_transcript_text(settings.media_root, work.transcript_path)
            result = classify(work.title or "", transcript_text=transcript)
            rows.append(
                {
                    "work_id": work.id,
                    "title": (work.title or "")[:40],
                    "source_type": work.source_type,
                    "status": work.status,
                    "category": result.category,
                    "label": result.label,
                    "confidence": result.confidence,
                    "action": result.action,
                    "tier": result.tier,
                    "matched": result.matched,
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="兴趣分诊：类目判定 + 档位/优先级（只读）")
    parser.add_argument("--scan", action="store_true", help="扫描 works 表并输出分诊结果")
    parser.add_argument("--limit", type=int, default=0, help="最多扫描 N 条（0=全部）")
    parser.add_argument("--source", choices=["all", "personal"], default="all")
    parser.add_argument("--no-transcript", action="store_true", help="只用标题粗分，不读转写仲裁")
    args = parser.parse_args()
    if not args.scan:
        parser.print_help()
        return 1
    rows = scan(limit=args.limit, use_transcript=not args.no_transcript, source=args.source)
    for row in rows:
        print(
            f"{row['work_id']:>6} | {row['label']:<8} | {row['confidence']:<6} | "
            f"tier{row['tier']} | {row['action']:<7} | {row['source_type']:<15} | {row['title']}"
        )
    summary: dict[str, int] = {}
    for row in rows:
        summary[row["label"]] = summary.get(row["label"], 0) + 1
    print(f"--- 共 {len(rows)} 条 ---")
    for label, count in sorted(summary.items(), key=lambda kv: -kv[1]):
        print(f"{label}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
