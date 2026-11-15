# Issue triage foundation

PR review and issue triage share the trusted repository allowlist, verified
`openwrt-ai` identity, read-only API client and connector snapshot adapter.
Task modules and prompts remain separate so a PR completion marker never marks
an issue as triaged.

All supplied profiles contain:

```json
"issue_triage": {
  "enabled": false,
  "max_issues": 8,
  "allowed_labels": []
}
```

To test read-only discovery later, change `enabled` to `true` in a trusted policy
copy, configure the same bot-token or connector transport as for review, and run:

```sh
python3 -B -E -s -m openwrt_ai.triage.discover \
  --config /path/to/trusted-policy.json --repository openwrt/procd --max-issues 1
```

Discovery excludes pull requests returned by GitHub's issues endpoint, verifies
public repositories, and merges issue streams by oldest update. It stops at the
batch limit. `scan_complete: false` and `deferred: null` mean the remainder is
unknown. A connector candidate listing may be partial and never establishes a
complete scan. Records include title, body and existing labels as untrusted data.
Long text is truncated explicitly; comments are not collected. This is candidate
discovery, not sufficient evidence for a triage decision.

Connector captures use the same envelope documented in [setup.md](setup.md),
with records for `/repos/OWNER/REPO` and `/repos/OWNER/REPO/issues`. Each record
contains `data` and `complete`. Only candidate listings may be incomplete. Use
actual authenticated connector observations, never invented API responses.

The next implementation stage should introduce a versioned proposal schema,
complete issue/comment evidence, repository-specific triage rules from a trusted
base commit, duplicate-search evidence, and a validator. Proposed labels must be
in both the trusted `allowed_labels` list and the repository's current label list.
An empty list authorizes no labels. This field is reserved policy: the discovery
module does not interpret it or propose changes.

A later validator should bind proposals to an observed issue update time and
discussion/label snapshot. An eventual publisher would need fresh identity and
issue revalidation, idempotency and independent authorization. There is no
publisher, proposal validator, persistent triage checkpoint, or triage schedule
in this repository yet.
