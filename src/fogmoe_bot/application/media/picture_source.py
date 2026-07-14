"""@brief 免费图片图库读取端口 / Free picture-gallery read port.

图片能力现在只负责免费预览；该端口刻意不暴露报价、扣费、高清下载或回执操作。
/ The picture capability now serves free previews only.  This port deliberately exposes no
offers, charges, HD downloads, or receipts.
"""

from __future__ import annotations

from typing import Protocol

from fogmoe_bot.domain.media.picture import PictureCandidate, PictureRating


class PictureSource(Protocol):
    """@brief 有界图库读取端口 / Bounded picture-gallery read port."""

    async def fetch(
        self,
        rating: PictureRating,
        *,
        limit: int,
    ) -> tuple[PictureCandidate, ...]:
        """@brief 获取一个有界候选批次 / Fetch one bounded candidate batch.

        @param rating 内容分级 / Content rating.
        @param limit 最大候选数 / Maximum candidate count.
        @return 规范化图片候选 / Canonical picture candidates.
        """

        ...
