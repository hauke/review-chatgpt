"""Read authenticated connector observations without extracting connector credentials.

Snapshots are evidence for dry-run previews, not independent authentication or
live revalidation. A future publisher must query GitHub itself at submission.
"""

import copy
import re
from datetime import UTC, datetime

from openwrt_ai.common import BOT, BOT_ID, GitHub, pr_created_at, read_json, require

RESOURCE = re.compile(
    r"/repos/([^/]+/[^/]+)(?:/pulls(?:/[1-9][0-9]*(?:/(?:reviews|files|commits|comments))?)?|/issues(?:/[1-9][0-9]*(?:/comments)?)?)?\Z"
)


class ConnectorSnapshot(GitHub):
    source = "connector-snapshot"
    listing_complete = False

    def __init__(self, data, policy, clock=None):
        require(
            isinstance(data, dict)
            and data.get("version") == 1
            and data.get("source") == self.source,
            "invalid connector snapshot",
        )
        identity = data.get("identity", {})
        require(
            identity.get("login") == BOT and identity.get("id") == BOT_ID,
            "connector snapshot identity must be openwrt-ai",
        )
        self.observed_at = datetime.fromisoformat(data["observed_at"])
        self.clock = clock or (lambda: datetime.now(UTC))
        self.resources = data.get("resources")
        self.policy = policy
        require(isinstance(self.resources, dict), "snapshot resources must be an object")
        self.check_freshness()
        for path in self.resources:
            self.check_path(path)

    def check_path(self, path):
        match = RESOURCE.fullmatch(path)
        require(match is not None, "unsupported connector resource path")
        self.policy.allow(match[1])

    def check_freshness(self):
        require(
            self.observed_at.tzinfo is not None
            and 0 <= (self.clock() - self.observed_at).total_seconds() <= 300,
            "connector snapshot is stale or from the future; refresh connector observations",
        )

    def get(self, path):
        self.check_freshness()
        self.check_path(path)
        require(path in self.resources, f"missing connector observation: {path}")
        record = self.resources[path]
        require(
            isinstance(record, dict) and "data" in record and type(record.get("complete")) is bool,
            "connector observation must contain data and completeness",
        )
        require(
            record.get("complete") is True or path.endswith(("/pulls", "/issues")),
            f"connector observation is incomplete: {path}",
        )
        return copy.deepcopy(record["data"])

    def pages(self, path, parameters=None):
        data = self.get(path)
        require(isinstance(data, list), "connector collection must be an array")
        parameters = parameters or {}
        if path.endswith("/pulls") and parameters.get("sort") == "created":
            data.sort(key=pr_created_at, reverse=parameters.get("direction", "desc") == "desc")
        elif path.endswith(("/pulls", "/issues")):
            data.sort(key=lambda pr: pr.get("updated_at", ""))
        yield from data


def client_argument(parser):
    parser.add_argument(
        "--github-snapshot",
        help="fresh authenticated connector observations JSON; no token needed",
    )


def github_client(args, policy):
    if args.github_snapshot:
        return ConnectorSnapshot(read_json(args.github_snapshot), policy)
    return GitHub()
