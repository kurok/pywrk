#!/usr/bin/env python3
"""Post or update the performance report on a pull request.

Deliberately a thin shim: the decision of whether to edit or create lives in
:func:`pywrkr.ci.upsert_pr_comment`, where the test suite can reach it.
"""

from __future__ import annotations

import os
import sys

from pywrkr.ci import upsert_pr_comment


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    number = os.environ.get("PR_NUMBER", "")
    body_file = os.environ.get("SUMMARY_FILE", "")

    if not (token and repo and number.isdigit() and body_file):
        print("pr_comment: not enough context to comment; skipping", file=sys.stderr)
        return 0

    with open(body_file, "r", encoding="utf-8") as handle:
        body = handle.read()

    try:
        action = upsert_pr_comment(
            repo,
            int(number),
            body,
            token=token,
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
    except Exception as exc:  # noqa: BLE001 - a failed comment must not fail the run
        print(f"pr_comment: could not post the report: {exc}", file=sys.stderr)
        return 0
    print(f"pr_comment: {action} the performance report on #{number}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
