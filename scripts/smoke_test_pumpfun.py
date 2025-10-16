"""
Ad-hoc smoke test for the Pump.fun pipeline.

This script does *not* call external APIs automatically; instead it provides a
single entrypoint you can run after deployment to sanity-check the Cloud Run
services and Pub/Sub wiring.

Usage:
    python scripts/smoke_test_pumpfun.py --project just-skyline-474622-e1 \
        --publisher-url https://pumpfun-publisher-<hash>.run.app/trigger

It will:
  1. Send a POST to the publisher trigger endpoint (empty JSON body).
  2. Pull a few messages from the `pumpfun-clip`, `mp3-ready`, and
     `diarization-ready` topics (requires `gcloud` credentials).
  3. Print a consolidated summary so you can confirm the pipeline is flowing.

The script intentionally stays lightweight—perfect for local smoke testing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Sequence

import requests


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pump.fun pipeline smoke test.")
    parser.add_argument("--project", required=True, help="GCP project id.")
    parser.add_argument(
        "--publisher-url",
        required=True,
        help="Full URL to the pumpfun-publisher /trigger endpoint.",
    )
    parser.add_argument(
        "--topics",
        nargs="*",
        default=["pumpfun-clip", "mp3-ready", "diarization-ready"],
        help="Pub/Sub topics to peek (defaults to the Pump.fun pipeline topics).",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=5,
        help="Maximum messages to pull from each topic when peeking.",
    )
    return parser.parse_args(argv)


def _trigger_publisher(url: str) -> int:
    resp = requests.post(url, json={}, timeout=30)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text}
    print(f"[publisher] {resp.status_code} {payload}")
    return resp.status_code


def _peek_topic(project: str, topic: str, max_messages: int) -> None:
    subscription = f"{topic}-smoke-test"
    create_cmd = [
        "gcloud",
        "pubsub",
        "subscriptions",
        "create",
        subscription,
        f"--topic={topic}",
        f"--project={project}",
        "--ack-deadline=10",
    ]
    pull_cmd = [
        "gcloud",
        "pubsub",
        "subscriptions",
        "pull",
        "--auto-ack",
        f"--limit={max_messages}",
        subscription,
        f"--project={project}",
        "--format=json",
    ]
    delete_cmd = [
        "gcloud",
        "pubsub",
        "subscriptions",
        "delete",
        subscription,
        f"--project={project}",
        "--quiet",
    ]
    try:
        subprocess.run(create_cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pull = subprocess.run(pull_cmd, check=False, capture_output=True, text=True)
        if pull.returncode == 0 and pull.stdout.strip():
            print(f"[pubsub] {topic}:")
            try:
                messages = json.loads(pull.stdout)
            except json.JSONDecodeError:
                print(pull.stdout)
            else:
                for msg in messages:
                    data = msg.get("message", {}).get("data")
                    attrs = msg.get("message", {}).get("attributes")
                    print(f"  data={data} attrs={attrs}")
        else:
            print(f"[pubsub] {topic}: no messages ({pull.stderr.strip()})")
    finally:
        subprocess.run(delete_cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv: Sequence[str]) -> int:
    args = _parse_args(argv)
    status = _trigger_publisher(args.publisher_url)
    if status != 200:
        return 1
    for topic in args.topics:
        _peek_topic(args.project, topic, args.max_messages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
