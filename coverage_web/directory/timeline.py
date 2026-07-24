"""Shared vocabulary for firm_dates event kinds.

Once home to the heat-mapped cycle-calendar builders; the calendar page was
retired in favour of the Opportunities urgency feed, so all that remains is
the single label map, imported by the firm-detail timeline table
(directory.views) and the Today week-ahead strip (crm.views).
"""

from __future__ import annotations

EVENT_LABELS = {
    "app_open": "Applications Open",
    "app_close": "Applications Close",
    "app_deadline": "Application Deadline",
    "insight_open": "Insight Programme Opens",
    "insight_close": "Insight Programme Closes",
    "interview": "Interviews",
    "offer": "Offers Out",
}
