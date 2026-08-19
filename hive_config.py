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




# This file is the site inventory: which hives exist, where they are, and how
# they are labelled. Nothing tunable lives here any more -- every threshold and
# global value is in thresholds.toml at the repo root (region radius, window
# size, status cuts, event floors, quality bounds, weather cutoffs, metric
# weights). Adding a setting back here will not take effect; data_loader raises
# if it finds one of the moved names.
