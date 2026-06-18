from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

from beemon_scoring.data_loader import DEFAULT_TIMEZONE, load_hive_config


DEFAULT_TABLE_NAME = "beemon-dev-telemetry-readings"
CSV_FIELDS = ["device_uid", "timestamp", "event_type", "sensor_data", "user_id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch recent BeeMon telemetry rows from DynamoDB.")
    parser.add_argument("--table", default=DEFAULT_TABLE_NAME, help="DynamoDB table name.")
    parser.add_argument("--days", type=int, default=7, help="Number of recent days to fetch.")
    parser.add_argument("--end", default=None, help="End time as ISO-8601. Defaults to now in America/New_York.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="Timezone for --end and default now.")
    parser.add_argument("--output-dir", type=Path, default=Path("local_data/dynamodb"), help="Directory for cached DynamoDB sensor CSVs.")
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    hives, _, _ = load_hive_config(project_root / "hive_config.py")
    timezone = ZoneInfo(args.timezone)
    end_at = datetime.fromisoformat(args.end).astimezone(timezone) if args.end else datetime.now(timezone)
    start_at = end_at - timedelta(days=args.days)

    start_ts = int(start_at.timestamp())
    end_ts = int(end_at.timestamp())
    client = boto3.client("dynamodb")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for hive in hives.values():
        rows = query_device_rows(client, args.table, hive.device_uid, start_ts, end_ts)
        output_path = args.output_dir / f"{hive.hive_id}_SENS.csv"
        write_rows(output_path, rows)
        total += len(rows)
        print(
            f"{hive.hive_id}: wrote {len(rows)} rows from {start_at.isoformat()} "
            f"to {end_at.isoformat()} -> {output_path}"
        )

    print(f"Done. Wrote {total} DynamoDB rows across {len(hives)} hives.")


def query_device_rows(client, table_name: str, device_uid: str, start_ts: int, end_ts: int) -> list[dict]:
    rows: list[dict] = []
    params = {
        "TableName": table_name,
        "KeyConditionExpression": "#device_uid = :device_uid AND #timestamp BETWEEN :start_ts AND :end_ts",
        "ExpressionAttributeNames": {
            "#device_uid": "device_uid",
            "#timestamp": "timestamp",
        },
        "ExpressionAttributeValues": {
            ":device_uid": {"S": str(device_uid)},
            ":start_ts": {"N": str(start_ts)},
            ":end_ts": {"N": str(end_ts)},
        },
        "ScanIndexForward": True,
    }

    while True:
        response = client.query(**params)
        rows.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        params["ExclusiveStartKey"] = last_key

    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "device_uid": _attr_to_csv_value(row.get("device_uid")),
                    "timestamp": _attr_to_csv_value(row.get("timestamp")),
                    "event_type": _attr_to_csv_value(row.get("event_type")),
                    "sensor_data": _attribute_json(row.get("sensor_data")),
                    "user_id": _attr_to_csv_value(row.get("user_id")),
                }
            )


def _attr_to_csv_value(attribute: dict | None) -> str:
    if not attribute:
        return ""
    for key in ("S", "N", "BOOL"):
        if key in attribute:
            return str(attribute[key])
    return ""


def _attribute_json(attribute: dict | None) -> str:
    if not attribute:
        return "{}"
    if "M" in attribute:
        return json.dumps(attribute["M"], separators=(",", ":"))
    return "{}"


if __name__ == "__main__":
    main()
