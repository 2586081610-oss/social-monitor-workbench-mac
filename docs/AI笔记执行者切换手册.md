# AI 笔记执行者切换手册（2026-11-01）

> 场景：某个执行者（ZCode/Codex/Trae/WorkBuddy）没额度或想换工具时，怎么把
> AI 笔记消费无缝切到另一个执行者。核心概念先分清：
>
> - **队列（投递目标）**：`workbuddy/pending/` 或 `summary_targets/<名字>/pending/`，
>   前端「总结交付目标」切换（`GET/PUT /api/v1/summary-targets`）决定**新包**投到哪个队列；
> - **执行者（消费者）**：真正读包写笔记的一方。**切队列 ≠ 切执行者**，两边要配套。

## 当前绑定关系

| 队列 | 执行者 | 状态 |
|---|---|---|
| `workbuddy/pending/` + `summary_targets/zcode/pending/` | **ZCode 桌面自动化**（每30分钟，≤2包，id automation-16bf9a82） | ✅ 运行中 |
| `summary_targets/codex/pending/` | Codex（待配：桌面自动化或 `codex exec`） | 未配 |
| `summary_targets/trae/pending/` | trae-agent CLI（`trae-cli run`，需装 bytedance/trae-agent）或手动 | 未配 |
| `workbuddy/pending/`（WorkBuddy App 本义） | WorkBuddy App 定时自动化 | 未配（与 ZCode 共用队列，App 配置后二选一） |

## 标准切换流程（以 ZCode 没额度 → 切 Codex 为例）

1. **前端切默认目标**：作品本地页「总结交付目标」切到 codex（或 `PUT /api/v1/summary-targets`）。
   此后**新**素材包投到 `summary_targets/codex/pending/`。
2. **停旧执行者**：让 AI 助手停掉 ZCode 自动化（或 Automations 页暂停）。
3. **搬积压**（可选，把还没消费的包跟过去）：
   ```bash
   ./.venv/bin/python -m monitor.note_dispatcher --reassign codex --max 50 --dry-run   # 先看计划
   ./.venv/bin/python -m monitor.note_dispatcher --reassign codex --max 50             # 实际搬
   ```
   搬运会按新目标重算 expected_output 并删除旧包文件；同名目标自动跳过。
4. **启新执行者**：Codex 侧建同样契约的自动化（读 codex 队列→写 expected_output→
   调 `POST /works/:id/complete-note`，executor 记 "codex"），或让 AI 助手代建。
5. **验证**：`python -m monitor.note_dispatcher --dry-run` 看队列归属；笔记落到
   `summary_targets/codex/output/` 后 complete-note 会把 work 置 DONE。

## 铁律（任何执行者都必须遵守）

- 完成 = 笔记文件真实存在并回填 complete-note；**绝不把没写文件的包标 done**
- 失败写 `<包名>.error.json` 留痕；额度耗尽要标 `quota_exhausted` 方便人工切换
- archive 档类目（情感/剧情/颜值/朋友支持/集赞/未分类）只归档不写笔记
- 只动自己队列的包；其他目标的队列不碰

## 相关代码

- 队列/目标定义：`backend/app/services/summary_target_service.py`
- 任务包生成（含 expected_output 重算）：`backend/app/services/note_service.py`
- 消费/优先级/搬队列：`monitor/note_dispatcher.py`（`--dry-run/--reassign/--self-test`）
- 分诊（note/archive 档）：`monitor/triage.py`
- ZCode 桌面自动化：Automations 页「每30分钟消费 socialmonitor 待笔记任务包」
