"""Delete all batches from a project. Use before re-uploading with deterministic UUIDs."""
import argparse
import time
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WORKERS = 20


def fetch_all_uuids(api_url, token, project_id):
    url = f"{api_url}/v1/data"
    headers = {"Authorization": f"Bearer {token}"}
    uuids = []
    offset = 0
    limit = 10000
    while True:
        params = {"limit": limit, "offset": offset, "metadata_only": "true", "project_id": project_id}
        resp = requests.get(url, headers=headers, params=params, verify=False)
        resp.raise_for_status()
        page = resp.json().get("batches", [])
        uuids.extend(b["uuid"] for b in page)
        print(f"  Fetched {len(uuids)} batches so far...")
        if len(page) < limit:
            break
        offset += limit
    return uuids


def delete_one(api_url, token, uuid):
    resp = requests.delete(
        f"{api_url}/v1/data/{uuid}",
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=30,
    )
    return uuid, resp.ok, resp.status_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="https://localhost")
    parser.add_argument("--token", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=WORKERS,
                        help=f"Parallel delete threads (default: {WORKERS})")
    args = parser.parse_args()

    print(f"Fetching all batches for: {args.project_id}")
    uuids = fetch_all_uuids(args.api_url, args.token, args.project_id)
    print(f"Total: {len(uuids)} batches")

    if not uuids:
        print("Nothing to delete.")
        return

    if args.dry_run:
        print(f"[DRY RUN] Would delete {len(uuids)} batches.")
        return

    confirm = input(f"Delete ALL {len(uuids)} batches from {args.project_id}? (yes/no): ")
    if confirm.strip().lower() != "yes":
        print("Cancelled.")
        return

    deleted = failed = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(delete_one, args.api_url, args.token, u): u for u in uuids}
        for i, fut in enumerate(as_completed(futures), 1):
            uuid, ok, status = fut.result()
            if ok:
                deleted += 1
            else:
                failed += 1
                print(f"  ✗ {uuid}: {status}")
            if i % 500 == 0:
                elapsed = time.time() - start
                rate = i / elapsed
                remaining = (len(uuids) - i) / rate
                print(f"  {i}/{len(uuids)} done — {rate:.0f}/s — ~{remaining:.0f}s left")

    print(f"\nDone: {deleted} deleted, {failed} failed in {time.time()-start:.0f}s.")


if __name__ == "__main__":
    main()
