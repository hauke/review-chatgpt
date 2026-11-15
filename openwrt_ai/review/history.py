"""Identify bot-owned history and Claude exclusions without trusting human text."""

from openwrt_ai.common import BOT, BOT_ID, completion_sha, last_review, positive_int

CLAUDE_USERS = {"claude[bot]", "claude-code[bot]"}


def own_author(item):
    return item.get("user", {}).get("login") == BOT and item.get("user", {}).get("id") == BOT_ID


def claude_comment(item, is_review=False):
    if is_review and (item.get("state") == "PENDING" or not item.get("submitted_at")):
        return False
    if item.get("user", {}).get("login", "").lower() in CLAUDE_USERS:
        return True
    # Claude and ChatGPT share the established bot identity. Preserve legacy
    # Claude coverage by excluding its unmarked output, even after head changes.
    return own_author(item) and completion_sha(item.get("body")) is None


def read_history(api, repository, number):
    reviews = list(api.pages(f"/repos/{repository}/pulls/{number}/reviews"))
    if any(claude_comment(review, is_review=True) for review in reviews):
        return reviews, [], [], True, None
    comments = list(api.pages(f"/repos/{repository}/issues/{number}/comments"))
    if any(claude_comment(comment) for comment in comments):
        return reviews, comments, [], True, None
    inline = list(api.pages(f"/repos/{repository}/pulls/{number}/comments"))
    own_reviews = {review["id"] for review in reviews if last_review([review])}
    if any(
        comment.get("user", {}).get("login", "").lower() in CLAUDE_USERS
        or (own_author(comment) and comment.get("pull_request_review_id") not in own_reviews)
        for comment in inline
    ):
        return reviews, comments, inline, True, None
    candidates = []
    for review in reviews:
        completed = last_review([review])
        if completed:
            candidates.append({**completed, "comment_kind": "review"})
    for comment in comments:
        sha = completion_sha(comment.get("body"))
        if own_author(comment) and sha and f"Reviewed commit: `{sha}`" in comment["body"]:
            candidates.append({**comment, "commit_id": sha, "comment_kind": "issue"})
    for candidate in candidates:
        positive_int(candidate["id"], "existing bot comment ID")
    previous = max(
        candidates,
        key=lambda item: (
            item.get("updated_at") or item.get("submitted_at") or item.get("created_at", ""),
            item["id"],
        ),
        default=None,
    )
    return reviews, comments, inline, False, previous
