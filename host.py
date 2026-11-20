import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


HOST_NAME = "github_inbox_reviews"
ADDON_ID = "github-inbox-reviews@local"
LOGIN_PATTERN = re.compile(r"[a-zA-Z0-9-]{1,39}")
PULL_PATTERN = re.compile(r"([a-zA-Z0-9-]{1,39})/([a-zA-Z0-9_.-]{1,100})/([1-9][0-9]{0,9})")
MAX_BATCH = 25
INVALID_REQUEST = "Invalid review lookup."
READ_ERROR = "Couldn’t read review history. Check gh auth status and repository access."


def group_pull_requests(pull_requests):
    repositories = defaultdict(dict)
    for key in pull_requests:
        match = PULL_PATTERN.fullmatch(key) if isinstance(key, str) else None
        if not match:
            return None
        owner, name, number = match.groups()
        number = int(number)
        if number > 2_147_483_647:
            return None
        repositories[owner, name][key] = number
    return repositories


def build_query(repositories):
    fields = ["viewer { login }"]
    paths = {}
    for index, ((owner, name), pulls) in enumerate(repositories.items()):
        repo_alias = f"r{index}"
        parts = []
        for pr_index, (key, number) in enumerate(pulls.items()):
            pr_alias = f"p{pr_index}"
            paths[key] = (repo_alias, pr_alias)
            parts.append(f"{pr_alias}: pullRequest(number: {number}) {{ ...ReviewStatus }}")
        fields.append(
            f'{repo_alias}: repository(owner: "{owner}", name: "{name}")'
            f' {{ {" ".join(parts)} }}'
        )

    query = f"query($login: String!) {{ {' '.join(fields)} }} " + """
        fragment ReviewStatus on PullRequest {
          headRefOid
          viewerLatestReviewRequest { id }
          decision: reviews(author: $login, last: 1, states: [APPROVED, CHANGES_REQUESTED, DISMISSED]) {
            nodes { ...Review }
          }
          comment: reviews(author: $login, last: 1, states: [COMMENTED]) {
            nodes { ...Review }
          }
          timelineItems(last: 20, itemTypes: [REVIEW_DISMISSED_EVENT]) {
            nodes {
              ... on ReviewDismissedEvent { review { id } previousReviewState }
            }
          }
        }
        fragment Review on PullRequestReview { id state commit { oid } }
    """
    return query, paths


def review_status(pull_request):
    decisions = pull_request["decision"]["nodes"]
    comments = pull_request["comment"]["nodes"]
    reviews = decisions or comments
    if not reviews:
        return {"state": "UNREVIEWED"}

    review = reviews[0]
    state = review["state"]
    dismissed = state == "DISMISSED"
    if dismissed:
        for event in reversed(pull_request["timelineItems"]["nodes"]):
            if event and (event.get("review") or {}).get("id") == review["id"]:
                state = event["previousReviewState"]
                break

    reviewed_oid = (review.get("commit") or {}).get("oid")
    current_oid = pull_request["headRefOid"]
    head = "unknown"
    if reviewed_oid and current_oid:
        head = "same" if reviewed_oid == current_oid else "changed"

    return {
        "state": state,
        "head": head,
        "dismissed": dismissed,
        "requestedAgain": pull_request["viewerLatestReviewRequest"] is not None,
    }


def lookup(request, gh):
    if not isinstance(request, dict):
        return {"error": INVALID_REQUEST}

    viewer = request.get("viewer")
    pull_requests = request.get("pullRequests")
    if (
        not isinstance(viewer, str)
        or not LOGIN_PATTERN.fullmatch(viewer)
        or not isinstance(pull_requests, list)
        or not 1 <= len(pull_requests) <= MAX_BATCH
    ):
        return {"error": INVALID_REQUEST}

    repositories = group_pull_requests(pull_requests)
    if repositories is None:
        return {"error": INVALID_REQUEST}

    query, paths = build_query(repositories)
    try:
        response = subprocess.run(
            [gh, "api", "--hostname", "github.com", "graphql", "--input", "-"],
            input=json.dumps({"query": query, "variables": {"login": viewer}}),
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
        payload = json.loads(response.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"error": READ_ERROR}

    data = payload.get("data") or {}
    cli_viewer = (data.get("viewer") or {}).get("login")
    if not cli_viewer:
        return {"error": READ_ERROR}
    if cli_viewer.lower() != viewer.lower():
        return {"error": "Your Firefox and GitHub CLI accounts differ. Switch gh to your inbox account."}

    errors = {tuple(error.get("path") or ())[:2] for error in payload.get("errors", [])}
    results = {}
    for key, (repo, pr) in paths.items():
        pull_request = (data.get(repo) or {}).get(pr)
        if pull_request is None or errors.intersection({(), (repo,), (repo, pr)}):
            results[key] = {"state": "UNAVAILABLE"}
        else:
            results[key] = review_status(pull_request)
    return {"reviews": results}


def serve(gh):
    length = sys.stdin.buffer.read(4)
    if len(length) != 4:
        return
    size, = struct.unpack("=I", length)
    if not 0 < size <= 16_384:
        return

    try:
        request = json.loads(sys.stdin.buffer.read(size))
        result = lookup(request, gh)
    except ValueError:
        result = {"error": INVALID_REQUEST}
    payload = json.dumps(result).encode()
    sys.stdout.buffer.write(struct.pack("=I", len(payload)) + payload)
    sys.stdout.buffer.flush()


def install():
    gh = shutil.which("gh")
    if not gh:
        raise SystemExit("Install the GitHub CLI (gh), then run this command again.")
    if sys.platform == "darwin":
        directory = Path.home() / "Library/Application Support/Mozilla/NativeMessagingHosts"
    elif sys.platform.startswith("linux"):
        directory = Path.home() / ".mozilla/native-messaging-hosts"
    else:
        raise SystemExit("The helper installer supports macOS and Linux.")

    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / HOST_NAME
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "--serve", gh])
    launcher.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8")
    launcher.chmod(0o700)
    manifest = directory / f"{HOST_NAME}.json"
    manifest.write_text(
        json.dumps(
            {
                "name": HOST_NAME,
                "description": "Read your GitHub PR review status with gh.",
                "path": str(launcher),
                "type": "stdio",
                "allowed_extensions": [ADDON_ID],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Registered Firefox helper at {manifest}")


if __name__ == "__main__":
    if sys.argv[1:] == ["--install"]:
        install()
    elif len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(sys.argv[2])
    else:
        raise SystemExit("Usage: python3 host.py --install")
