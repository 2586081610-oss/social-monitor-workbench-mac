# 健身训练笔记 v3

开始前阅读[通用规范](00-通用规范.md)、[分类路由](01-分类路由.md)及[证据核验](02-证据核验.md)。交付物是同一份笔记的 `note.md` 与 `note.json`，使用[数据契约](schemas/note-v3.schema.json)。Markdown 的 [YAML 头](schemas/frontmatter.yaml)由 JSON 元数据生成。原片位置用毫秒，图文位置用原始序号。结构化主张、训练表和专题模块引用同一套证据编号。

## 从现有任务包生成

从仓库根目录运行。将现有任务包作为只读输入，输出根选一个单独目录。

```sh
python -m pip install -r requirements-note-v3.txt
python -m monitor.note_v3 /path/to/package.json \
  --media-root /path/to/media \
  --output-root /path/to/v3-review \
  --level standard
```

默认使用本机 Codex。可用 `--executor` 与 `--executor-workspace` 读取已有的目标配置。只在显式调用此命令时生成 v3，原自动队列和完成回填仍走既有流程。

- 输入沿用 `work_id`、`platform`、`blogger`、`title`、`url`、`transcript_path`、`frames_dir` 及 `provenance`。可附上已核实的 `duration_seconds`、`published_at`、`media_id`、`source_hash`。图文读取相邻的抖音/X 图片清单，也可以在包副本中提供按原顺序排列的 `image_paths` 和原帖 `caption`。素材路径相对于 media 根。
- 抽帧选择沿用现有消费者的来源优先级与时间覆盖。仅复制正文实际引用的已有图片，使交付目录可独立阅读。不触发下载、转录、OCR 或抽帧。OCR 和听录纠正随笔记另存。
- 同一作品与深度使用稳定笔记 ID；输入、模板或处理版本变化产生新的快照，前次输出仍可阅读。完全相同的输入重用已有结果。每个快照修订号初始为 1。检查通过才一次落盘，执行失败可重跑。
- 研究核验使用所选执行器可访问的检索能力。找不到、无法访问、没有运行检索分别记录。需要人工复查研究真实可查及主张与原片是否相符。校验通过的输出仍为 draft。
- JSON 的 `knowledge_links` 和 `relations` 可携带已知的跨作品关系。可在任务包副本的 `related_notes` 提供候选，每项至少含 `note_id` 和用于对照的经核摘要，空数组代表当前未提供。此入口不扫描历史知识库，也不调用 EverOS 或工作台完成回填接口。

[罗马尼亚硬拉节选](demos/健身专用-罗马尼亚硬拉节选.md)演示原片脚标和对应的帧记录。按[真实素材验收计划](03-质量验收.md)记录运行结果。自动检查覆盖结构、脚标、时间和图片引用、训练顺序及模块一致性，不能代替看片、听音和逐篇核验研究。
