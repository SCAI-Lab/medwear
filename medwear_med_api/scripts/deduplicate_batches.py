"""
Deduplication script for MedWear batches.

Finds and deletes duplicate batches (same patient_id, signal_type,
start_timestamp, end_timestamp, sample_count) keeping the oldest one.

Usage:
  python deduplicate_batches.py \
      --api-url https://localhost \
      --token <JWT> \
      --project-id medwear_ncgg \
      [--dry-run]
"""

import argparse
import time
from collections import defaultdict

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def fetch_all_batches(api_url, token, project_id):
    url = f"{api_url}/v1/data"
    headers = {"Authorization": f"Bearer {token}"}
    batches = []
    offset = 0
    limit = 10000
    while True:
        params = {"limit": limit, "offset": offset, "metadata_only": "true"}
        if project_id:
            params["project_id"] = project_id
        resp = requests.get(url, headers=headers, params=params, verify=False)
        resp.raise_for_status()
        data = resp.json()
        page = data.get("batches", [])
        batches.extend(page)
        print(f"  Fetched {len(batches)} batches so far...")
        if len(page) < limit:
            break
        offset += limit
    return batches


def find_duplicates(batches):
    groups = defaultdict(list)
    for b in batches:
        key = (
            b.get("patient_id", ""),
            b.get("signal_type", ""),
            b.get("start_timestamp", ""),
            b.get("end_timestamp", ""),
            b.get("sample_count", 0),
        )
        groups[key].append(b)

    duplicates = []
    for key, group in groups.items():
        if len(group) > 1:
            # Sort by created_at, keep oldest
            group.sort(key=lambda x: x.get("created_at", ""))
            duplicates.extend(group[1:])  # delete all but the first

    return duplicates


def delete_batch(api_url, token, uuid):
    url = f"{api_url}/v1/data/{uuid}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.delete(url, headers=headers, verify=False)
    return resp.ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="https://localhost")
    parser.add_argument("--token", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted without actually deleting")
    args = parser.parse_args()

    print(f"Fetching all batches for project: {args.project_id}")
    batches = fetch_all_batches(args.api_url, args.token, args.project_id)
    print(f"Total batches: {len(batches)}")

    duplicates = find_duplicates(batches)
    print(f"Duplicate batches to delete: {len(duplicates)}")

    if not duplicates:
        print("No duplicates found!")
        return

    if args.dry_run:
        print("\n[DRY RUN] Would delete:")
        for b in duplicates[:20]:
            print(f"  {b['uuid']} | {b['patient_id']} | {b['signal_type']} | {b.get('start_timestamp','')[:19]}")
        if len(duplicates) > 20:
            print(f"  ... and {len(duplicates) - 20} more")
        return

    print(f"\nDeleting {len(duplicates)} duplicate batches...")
    deleted = 0
    failed = 0
    for b in duplicates:
        ok = delete_batch(args.api_url, args.token, b["uuid"])
        if ok:
            deleted += 1
            print(f"  ✓ deleted {b['uuid']} | {b['patient_id']} | {b['signal_type']}")
        else:
            failed += 1
            print(f"  ✗ failed  {b['uuid']}")
        time.sleep(0.1)

    print(f"\nDone: {deleted} deleted, {failed} failed.")


if __name__ == "__main__":
    main()
