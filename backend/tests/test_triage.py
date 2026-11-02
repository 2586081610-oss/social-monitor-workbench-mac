"""兴趣分诊测试：类目词库命中、三层降级、优先级排序。

背景（2026-11-01 用户确认）：个人来源存量 1.6 万+，全量直通会撑爆 AI 笔记
消费端且淹没知识库。类目 = 用户 6 类 + 实弹 10 条样本补充；实测教训直接写成
回归用例：隐喻标题必须转写仲裁、标题与内容错位要靠 tier 2 纠偏。
"""

from __future__ import annotations

from monitor import triage


class TestTitleClassification:
    """tier 1：标题+标签粗分（实弹样本回归）。"""

    def test_beauty_title_high(self):
        # 实弹 16471：纯 BGM 颜值视频（转写为空反证）
        result = triage.classify("是真的蟀！！！#iphone原相机")
        assert result.category == "beauty"
        assert result.confidence == "high"
        assert result.action == "archive"

    def test_drama_title_high(self):
        # 实弹 16567：短剧，转写全是对白噪声，标题反而可靠
        result = triage.classify('第十九集"蜀道难"#剧情 #麻婆嘿不黑')
        assert result.category == "drama"
        assert result.confidence == "high"

    def test_ai_tools_title_high(self):
        result = triage.classify("本周GitHub最实用的10个开源神器")
        assert result.category == "ai_tools"
        assert result.action == "note"

    def test_digital_tools_title(self):
        result = triage.classify("第010期｜不要再给网盘充会员了！直链下载不限速")
        assert result.category == "digital_tools"
        assert result.action == "note"

    def test_money_title(self):
        result = triage.classify("赚钱的第一步是停止厌蠢 #大学生#赚钱#干货分享")
        assert result.category == "money"

    def test_travel_title(self):
        result = triage.classify("江南赏枫天花板！苏州，即将迎来最佳赏味期！攻略都给你做好了")
        # "天花板"命中 good_works、"赏枫/攻略"命中 travel——travel 词更多应胜出
        assert result.category == "travel"

    def test_support_campaign(self):
        result = triage.classify("家人们求赞，集赞换奖品")
        assert result.category == "support_campaign"

    def test_no_match_falls_to_other_archive(self):
        result = triage.classify("今天天气不错")
        assert result.category == "other"
        assert result.confidence == "low"
        assert result.action == "archive"
        assert result.tier == 1


class TestTranscriptArbitration:
    """tier 2：低置信时转写仲裁（实测教训回归）。"""

    def test_metaphor_title_rescued_by_transcript(self):
        # 实弹 16465："闲鱼捡漏最高的山"=开源 AI 监控项目——纯标题判不了
        result = triage.classify("闲鱼捡漏最高的山它来了 #神奇的闲鱼 #闲鱼捡漏王")
        assert result.category == "other"
        arbitrated = triage.classify(
            "闲鱼捡漏最高的山它来了 #神奇的闲鱼",
            transcript_text="国内大佬开源的AI闲鱼智能监控系统，多模态大模型分析商品实拍图，github拿下十五k star",
        )
        assert arbitrated.category == "ai_tools"
        assert arbitrated.tier == 2

    def test_title_content_mismatch_corrected(self):
        # 实弹 16572：标题是涨粉分析，内容实为情感成长——标题必分错
        title_only = triage.classify("大麦哥说 7天涨粉近百万，大麦哥的爆火不是偶然")
        assert title_only.category == "money"
        arbitrated = triage.classify(
            "大麦哥说 7天涨粉近百万",
            transcript_text="我们想让一个还没有得到的人看到更好的自己，真正有吸引力的人还在生活，保持自己的兴趣爱好",
        )
        assert arbitrated.category == "emotion"
        assert arbitrated.tier == 2

    def test_transcript_with_no_signal_stays_other(self):
        result = triage.classify("随便拍着玩", transcript_text="哈哈哈哈哈")
        assert result.category == "other"
        assert result.tier == 1  # 仲裁无信号，维持 tier 1 的 other


class TestFitnessCategory:
    """健身类目（2026-11-01 用户确认新增）：动作词强信号 + 周边维度弱信号。"""

    def test_fitness_movements_high(self):
        # 实弹 16580：引体/划船对比科普
        result = triage.classify("《Vibe知识大赏》引体和划船，练的并不是同一块背 引体向上和划船")
        assert result.category == "fitness"
        assert result.confidence == "high"
        assert result.action == "note"

    def test_fitness_weak_signals_alone_stay_medium(self):
        # 睡眠/饮食/恢复/拉伸是弱信号：单独出现不应判 high
        result = triage.classify("最近睡眠不太好，拉伸了一下")
        assert result.category == "fitness"
        assert result.confidence in ("medium",)  # 弱信号不给 high

    def test_fitness_facets_mapping(self):
        facets = triage.facets_for("fitness", ("引体", "划船", "背部"))
        assert "背" in facets
        assert triage.facets_for("emotion", ("引体",)) == ()

    def test_non_fitness_with_generic_words_not_misfired(self):
        # "恢复"出现在非健身内容里（手机数据恢复）不应判健身
        result = triage.classify("手机数据恢复教程，删除的照片找回来了")
        assert result.category != "fitness"


class TestPriority:
    """消费优先级：博主监控 > 个人来源 note 类目 > archive 类目。"""

    def test_monitor_source_beats_personal(self):
        assert triage.priority_rank("monitor", "beauty") == 0
        assert triage.priority_rank("manual", "other") == 0

    def test_note_categories_rank_before_archive(self):
        assert triage.priority_rank("personal_source", "ai_tools") < triage.priority_rank(
            "personal_source", "emotion"
        )
        assert triage.priority_rank("personal_source", "money") < triage.priority_rank(
            "personal_source", "drama"
        )

    def test_unknown_category_gets_lowest(self):
        assert triage.priority_rank("personal_source", "nonexistent") == triage.priority_rank(
            "personal_source", "other"
        )
