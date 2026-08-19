HIVES = {
    "DR_WLKS": {
        "hive_id": "DR_WLKS",
        "device_uid": "351077454554331",
        "latitude": 36.247479,
        "longitude": -81.90097,
        "display_name": "DR Walks",
        "county": "Watauga County",
        "state": "NC",
        "region_label": "Watauga County",
    },
    "6LR": {
        "hive_id": "6LR",
        "device_uid": "868032061578211",
        "latitude": 36.212598,
        "longitude": -81.679678,
        "display_name": "6LR",
        "county": "Watauga County",
        "state": "NC",
        "region_label": "Watauga County",
    },
    "PRT_1": {
        "hive_id": "PRT_1",
        "device_uid": "868032061432054",
        "latitude": 36.203096,
        "longitude": -81.625792,
        "display_name": "PRT 1",
        "county": "Watauga County",
        "state": "NC",
        "region_label": "Watauga County",
    },
    "WTG_HSCHL": {
        "hive_id": "WTG_HSCHL",
        "device_uid": "868032061545061",
        "latitude": 36.214155,
        "longitude": -81.649756,
        "display_name": "Watauga High School",
        "county": "Watauga County",
        "state": "NC",
        "region_label": "Watauga County",
    },
}




REGION_RADIUS_MILES = 10

# Minimum number of distinct sites a region must contain. Regions smaller than
# this are merged into their nearest neighboring region so peer comparisons
# never degrade to "a colony compared only against its own L/R sister."
MIN_REGION_SITE_COUNT = 2

# How long a colony may go without sending a reading before it is flagged as not
# reporting and surfaced alongside the underperforming colonies. Measured against
# the newest reading in the cache (never the system clock), so a whole-apiary
# outage is invisible here -- it shows up as a stale window end instead.
MAX_REPORTING_GAP_DAYS = 1.0

# Share of the scoring window that data-quality issues must span before they put
# a colony on watch. Isolated bad readings happen everywhere and say nothing
# about the colony; a fault recurring across more than this share of the window
# does. At 0.30 a 7-day window needs issues on more than 2 distinct days.
QUALITY_ISSUE_DAY_SHARE_THRESHOLD = 0.30
