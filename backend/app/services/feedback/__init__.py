"""反馈聚合（ENG-09）。

`service.py`：任务归属与终态校验、每用户每任务一条、持久幂等。
"""

from app.services.feedback.service import FEEDBACK_TERMINAL_STATUSES, FeedbackService

__all__ = ["FEEDBACK_TERMINAL_STATUSES", "FeedbackService"]
