"""personal_source_folder_items — 平台收藏夹归属事实（F2 多夹语义）。

最小事实单元 = (platform, external_id, folder_id)：同一作品可在多个收藏夹中
（一作多夹），同一夹可含多作品。与 personal_source_marks 的单夹字段
（folder_id，DDR 语义）并存不混用；快照差异不做删除（缺项不推断取消归属）。
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.utils.timeutil import now_iso


class PersonalSourceFolderItem(Base):
    __tablename__ = "personal_source_folder_items"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", "folder_id", name="uq_personal_folder_items_key"),
        Index("ix_personal_folder_items_work_id", "work_id"),
        Index("ix_personal_folder_items_folder", "platform", "folder_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    folder_id: Mapped[str] = mapped_column(Text, nullable=False)
    folder_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_channel: Mapped[str] = mapped_column(String(20), nullable=False)  # f2
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), nullable=True
    )
    first_seen_at: Mapped[str] = mapped_column(Text, nullable=False, default=now_iso)
    last_seen_at: Mapped[str] = mapped_column(Text, nullable=False, default=now_iso)
