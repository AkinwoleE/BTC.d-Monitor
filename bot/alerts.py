"""Open or comment on one GitHub issue named 'Hedge alerts'. Never prints tokens."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from redact import sanitize

ISSUE_TITLE = "Hedge alerts"
ISSUE_BODY = """Operational log for the Decibel/Hyperliquid funding hedge.

Comments are posted only when the hourly run has an entry, exit, risk action, attractive-spread change, or error. Quiet hours do not comment.

This repository may be public. Comments include pair, size, and liquidation distance. They do not include private keys. Account equity is not posted. Prefer a private repository before turning on live trading. See the README.
"""


def _api(method, path, token, payload=None):
    url = f"https://api.github.com{path}"
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hedge-bot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read()[:180].decode("utf-8", "replace")
        raise RuntimeError(f"github {method} {path} HTTP {e.code} {sanitize(detail)}") from None


def ensure_issue(repo, token):
    issues = _api("GET", f"/repos/{repo}/issues?state=open&per_page=100", token)
    for issue in issues:
        if issue.get("title") == ISSUE_TITLE and "pull_request" not in issue:
            _try_pin(issue, token)
            return issue["number"]
    created = _api(
        "POST",
        f"/repos/{repo}/issues",
        token,
        {"title": ISSUE_TITLE, "body": ISSUE_BODY},
    )
    _try_pin(created, token)
    return created["number"]


def _try_pin(issue, token):
    node = issue.get("node_id")
    if not node:
        return
    query = "mutation($id:ID!){ pinIssue(input:{issueId:$id}) { issue { number } } }"
    try:
        _api("POST", "/graphql", token, {"query": query, "variables": {"id": node}})
    except RuntimeError:
        # Pinning needs a permission the Actions token often does not have.
        return


def post_comment(repo, token, number, body):
    text = sanitize(body).strip()
    if not text:
        return
    _api(
        "POST",
        f"/repos/{repo}/issues/{number}/comments",
        token,
        {"body": text[:6000]},
    )


def comment_if_needed(body):
    """Ensure the Hedge alerts issue exists. Comment only when body is non-empty.

    Returns posted, ok (issue ensured, nothing to say), skipped (no token), or failed.
    """
    token = os.environ.get("GITHUB_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    if not token or not repo:
        if body:
            print("alerts: not posted (no GITHUB_TOKEN)")
        return "skipped"
    try:
        number = ensure_issue(repo, token)
        if body:
            post_comment(repo, token, number, body)
            print(f"alerts: commented on issue {number}")
            return "posted"
        return "ok"
    except RuntimeError as e:
        print(f"alerts: {sanitize(e)}")
        return "failed"


def main():
    ap = argparse.ArgumentParser(description="Post a manual hedge action to the Hedge alerts issue")
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--coin", required=True)
    ap.add_argument("--dry", required=True)
    ap.add_argument("--ok", required=True)
    ap.add_argument("--size", default="")
    ap.add_argument("--direction", default="")
    args = ap.parse_args()
    mode = "dry-run" if args.dry in ("1", "true", "True") else "LIVE"
    line = (
        f"**Manual {args.cmd}** ({mode}) coin={args.coin} "
        f"size={args.size or 'n/a'} direction={args.direction or 'n/a'} ok={args.ok}"
    )
    status = comment_if_needed(line)
    if status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
