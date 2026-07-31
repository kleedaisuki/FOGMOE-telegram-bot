"""@brief Admin bounded context 领域公开 API / Public domain API for the Admin bounded context."""

from .announcement import AnnouncementId
from .recipient import (
    AnnouncementRecipientClaim,
    AnnouncementRecipientKind,
)

__all__ = [
    "AnnouncementId",
    "AnnouncementRecipientClaim",
    "AnnouncementRecipientKind",
]
