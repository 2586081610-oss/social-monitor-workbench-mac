"""personal_source_marks — 本人来源事实（点赞/收藏，C3 融合）。

最小事实单元 = (platform, external_id, source_mark)；同一作品可有 like+favorite 两条事实。
folder 字段如实导入 DouyinDataReview 的单夹语义（一作一夹、未分区为 NULL），
不推断"取消收藏"，快照差异不做删除。
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.utils.timeutil import now_iso


class PersonalSourceMark(Base):
    __tablename__ = "personal_source_marks"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", "source_mark", name="uq_personal_marks_key"),
        Index("ix_personal_marks_work_id", "work_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_mark: Mapped[str] = mapped_column(String(20), nullable=False)  # like | favorite
    # DDR 单夹语义如实导入：有夹记 id/name，未分区记 NULL；重跑以后一次快照为准。
    folder_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    folder_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_channel: Mapped[str] = mapped_column(String(20), nullable=False)  # file | api
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 动作时间仅在来源可信（platform_action）时落库，否则保持 NULL（未知）。
    occurred_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), nullable=True
    )
    first_seen_at: Mapped[str] = mapped_column(Text, nullable=False, default=now_iso)
    last_seen_at: Mapped[str] = mapped_column(Text, nullable=False, default=now_iso)
