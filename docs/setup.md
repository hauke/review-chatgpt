# ChatGPT Work PR review — setup

This is a **dry-run implementation** of the batched reviewer in
`prompts/pr-review.md`. It discovers public PRs,
prepares Git worktrees, and validates structured findings into manual publishing
previews. It makes no model or OpenAI API calls and contains no GitHub publisher, review
schedule, or automatic reply operation. Existing Claude workflows are unchanged.

## Requirements and environment

Use your existing personal ChatGPT Pro account with Work and GitHub available.
Work/Codex consume the subscription's shared usage allowance; this workflow
does not need an OpenAI API key. Subscription access does not guarantee enough
allowance to review eight or sixteen large PRs in one run. Check actual usage
and reduce the batch when necessary.

The scripts need Python 3.11+, Git, a POSIX shell, disk space, and HTTPS access
to `api.github.com`, `github.com`, and explicitly configured public reference
hosts. They use the Python standard library only. Discovery and validation
use either fresh `openwrt-ai` connector observations (`--github-snapshot`) or a
user token in the protected runtime environment variable
`CHATGPT_REVIEW_GITHUB_TOKEN`. There is no anonymous API fallback. Git source
fetches remain anonymous and never receive that token.

In token mode, the API client verifies the token using `GET /user` before any repository reads,
requiring login `openwrt-ai` and numeric ID `276137552`. It caches that verification
for the current client invocation. Missing/invalid tokens and other identities
are failures, not reasons to retry anonymously or use `hauke`.

Authenticated requests use GitHub's user quota instead of the shared anonymous
IP quota; rate limits still apply and failures report the reset time. See
[GitHub authentication](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api)
and [rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

Hosted runtimes may route permitted HTTPS requests through an environment proxy.
The API client honors the runtime's standard proxy settings; Git retains only
the standard proxy/no-proxy variables and explicitly supported CA-bundle settings
alongside its restricted environment. TLS verification remains enabled.
Proxy routing/authentication is separate from the bot token and connector.
Do not copy proxy
credentials or environment values into the repository or execution logs.

If preliminary HTTPS probes succeed but discovery fails with `Temporary failure
in name resolution`, compare the probe and script execution environments and
report which transport failed; do not enable broader access or substitute tokens.

Start with a manually invoked Work task using a trusted local checkout of this
repository and an output directory outside every reviewed checkout. Test that
the actual environment can execute Python/Git and make the required GETs/fetches
before creating a schedule. Do not grant unrestricted shell/network access
solely to make background execution succeed. Trust and protect the script and
policy files separately from the PR worktrees.

The execution OS account must have no `.netrc` or `_netrc` file. Git/libcurl can
consult these independently of credential helpers, so preparation refuses to
run when either exists; it never reads their contents. Use a dedicated execution
account rather than relocating personal credentials into a checkout.

Scheduled web tasks can use connected tools and uploaded context, but do not
retain a local folder/worktree between runs. Desktop tasks needing local files
require the host to stay online with the app running. If using a cloud environment,
explicitly test shell/Git/network support, bootstrap the trusted scripts from an
operator-reviewed commit, and arrange durable output storage. Do not assume a
personal Work task inherits a Codex Cloud repository or its setup scripts.

See the official [scheduled-task documentation](https://learn.chatgpt.com/docs/automations),
[Work environment guidance](https://learn.chatgpt.com/docs/use-chatgpt), and
[subscription usage documentation](https://learn.chatgpt.com/docs/pricing).
Availability, approval behavior, model limits, and UI labels may vary by surface.

## Account and permission setup

1. In ChatGPT's GitHub connections, keep only the intended `openwrt-ai` identity
   available to the reviewer. Disconnect the personal `hauke` connection before
   automatic operation, as agreed for this setup. Review other enabled plugins
   and any signed-in browser sessions separately; disconnecting one GitHub app
   connection does not revoke other credentials or browser sessions.
2. Run the GitHub connector's authenticated-user read operation. Verify both
   `login: openwrt-ai` and `id: 276137552`. Repeat in the actual task environment,
   and again immediately before preparing a publishing preview. Never fall back
   to another account. Removing/reconnecting accounts can change tool parameters;
   use the actual tool schema, not a stale saved connection ID.
3. Keep connector credentials in its protected authorization mechanism. For the
   scripts in token mode, create a separate user token while signed in to GitHub as `openwrt-ai`,
   and inject it as the protected runtime secret `CHATGPT_REVIEW_GITHUB_TOKEN`.
   Prefer a fine-grained token with public-repository read access and no write
   permissions. Verify access to each intended public endpoint; do not grant
   private-repository access or expand permissions to solve an unrelated error.
   An installation token that cannot identify a user through `/user` is unsupported.
4. Never put a PAT into chat, shell arguments, scripts, policy JSON, this repository,
   logs, or worktrees. Never print environment values. The scripts deliberately
   ignore generic `GH_TOKEN`/`GITHUB_TOKEN` to avoid selecting personal credentials.
   Git subprocesses do not inherit the dedicated API token.

Configure the named secret through the execution environment's supported secret
mechanism, or a trusted local secret manager, before launching the scripts. Do
not assume a personal Work web task offers arbitrary environment-secret injection.
If that runtime cannot provide this secret securely, use connector-observation
mode below. The GitHub connector's OAuth token is not extracted by these
scripts: being connected in ChatGPT does not populate the environment variable.
Model-driven PR/discussion/CI reads should use the verified bot connector.
The snapshot adapter lets scripts consume these connector reads without making
HTTP calls or needing any token. It supports dry-run previews only.

## Connector mode in ChatGPT Work (no additional secret)

If the connector already verifies as `openwrt-ai`, use this mode in a Work runtime
without environment-secret injection. Work performs the authenticated tool reads,
normalizes the results to a local JSON snapshot, and the scripts consume that
snapshot. There is no local HTTP request or token extraction in this mode.

Capture format:

```json
{
  "version": 1,
  "source": "connector-snapshot",
  "observed_at": "2026-11-15T12:00:00+00:00",
  "identity": {"login": "openwrt-ai", "id": 276137552},
  "resources": {
    "/repos/openwrt/openwrt": {
      "complete": true,
      "data": {"full_name": "openwrt/openwrt", "private": false, "visibility": "public"}
    }
  }
}
```

This illustrates the schema, not actual fresh observations. Replace the time
and populate every required resource from real connector read results. Paths
are canonical GitHub REST-style resource identifiers with no query string. Each
collection contains the combined results across all pages, rather than a single
page. The adapter does not call REST for missing resources. Normalize fields as
follows (do not infer missing values):

| Resource | Data required |
| --- | --- |
| `/repos/OWNER/REPO` | `full_name`, `private: false`, public `visibility` |
| `/repos/OWNER/REPO/pulls/N` | `number`, `state`, `merged_at`, `updated_at`, `head.sha`, `head.repo.full_name/private`, `base.sha/ref/repo.full_name/private`, `changed_files`, `commits` |
| `/repos/OWNER/REPO/pulls/N/reviews` | Array of reviews with numeric `id`, `user.login/id`, `commit_id`, REST-style `state` (`COMMENTED`, `PENDING`, etc.), `submitted_at`, `body` |
| `/repos/OWNER/REPO/pulls/N/files` | Array of `filename` and full textual `patch` for each changed file; no fabricated patches for binary/unavailable files |
| `/repos/OWNER/REPO/pulls/N/commits` | Array with each commit's `sha`, if commit-message findings require membership validation |
| `/repos/OWNER/REPO/pulls/N/comments` | Complete inline comments with `id`, `body`, `user.login/id`, `pull_request_review_id`, `in_reply_to_id` where present; required for Claude detection |
| `/repos/OWNER/REPO/issues/N/comments` | Complete top-level conversation comments with numeric `id`, `body`, `user.login/id`, `created_at`, `updated_at`; required for exclusions and updates |
| `/repos/OWNER/REPO/pulls` | Optional candidate PR array with the PR fields above, for batches; it may be partial |

Tool responses use different field names; Work must preserve the actual values
when mapping them. For example, a review's `author` becomes `user`, `commit.oid`
becomes `commit_id`, and GraphQL `databaseId` is the numeric REST `id`. Never use
a GraphQL node string as a numeric ID. Mark `complete: true` only when all data
for that resource has been retrieved and the tool's pagination/completeness is
known. Mark truncated/unavailable collections false; required incomplete records
fail validation. Only the optional candidate listing may be partial. Its
remaining count is reported as unknown, even if all supplied candidates were read.

For one PR, collect repository metadata, PR metadata, complete reviews,
conversation comments and inline comments into
`discovery-github.json`, then run:

```sh
python3 -E -s -m openwrt_ai.review.discover \
  --config /path/to/policy.json --repository openwrt/openwrt --pr 14895 \
  --github-snapshot /path/to/discovery-github.json
```

Preparation uses that manifest normally and still fetches source through anonymous
HTTPS Git. After analysis, perform NEW connector reads of the current repository,
PR metadata, reviews, conversation and inline comments, all changed-file patches,
and any needed commits.
Recheck the authenticated identity and write `validation-github.json` and the
usual identity observation file. Append
`--github-snapshot /path/to/validation-github.json` to the normal validation command.

Snapshots expire after five minutes; a validation capture must be newer than
the discovery manifest. Refresh the connector reads if either condition fails.
Record only public, allowlisted resources and keep observations outside PR
worktrees. Missing fields, stale captures, changed SHAs, or incomplete collections
must be reported, not filled from memory or changed to make validation pass.

This mode trusts the captured connector evidence, just as the existing identity
file trusts the recorded tool observation. It cannot cryptographically prove
who captured JSON or detect a change after capture. Previews explicitly include
`github_read_source: connector-snapshot` and `live_revalidation: false`, with
`publishing_enabled: false`. It is for the dry run; a future publisher must perform
its own live authenticated identity, head/base, diff and thread checks. Do not
treat repeated reads from one snapshot as independent live GitHub checks.

## Publishing boundary

The scripts enforce their own allowlist, bot-token identity, GET-only access, and validation. They
cannot restrict what the Work model can do through another enabled tool. Prompt
instructions, identity JSON, a filesystem sandbox, and disconnecting `hauke` do
not provide a `COMMENT`-only permission boundary for the general GitHub connector.
There is no verified personal-Pro task-level connection lock implemented here.
Managed Team Tasks have a different service-account/connection model; do not
assume those controls exist for this personal task. See
[workspace connections](https://learn.chatgpt.com/docs/enterprise/shared-connections).

**Automatic publishing remains disabled.** A future publisher must have only
bot credentials, enforce the exact policy and payload itself, and exclude direct
unrestricted write tools from the reviewer. It must verify the live authenticated
identity, PR openness, head/base snapshots, and thread eligibility at submission,
use `COMMENT` with the explicit reviewed commit, and implement per-PR locking
and reconciliation after timeouts before retrying. Rechecking an SHA cannot
eliminate a concurrent push; submissions must remain pinned to the analyzed SHA.
The preview's payload hash identifies the reviewed payload; it is not a signature
or authorization token. Do not connect an automated publisher to these previews
without implementing that boundary and separately authorizing publication.

## Trusted policy

Copy the appropriate profile from `configs/` to a protected location outside
the reviewed worktrees. `configs/example.json` and `configs/procd.json` allowlist
only `openwrt/procd`; `configs/netifd.json` targets netifd. Use
`configs/openwrt.json` for the firmware repository or `configs/multi-repo.json`
for OpenWrt, procd, netifd, ubus and libubox together. Replace or extend
`repositories` explicitly for other public targets. Never infer an allowlist from PR
titles, repository files at PR head, model output, or comments.

Run module commands from the trusted tooling repository root. A new independent
Git repository has its own commit history: pin a commit from this repository
after publishing it, rather than a commit from `actions-shared-workflows`.

`max_prs` defaults to 8 and must be 1–16. `--max-prs` on discovery can override
the batch limit up to 16. The policy also bounds inline/reply count, per-comment
characters, and commit-check count; hard upper limits still apply. Reference
repositories are optional entries like:

```json
{"url": "https://github.com/u-boot/u-boot.git", "ref": "master"}
```

Only public HTTPS remotes belong in this trusted list. Select kernel tags from
version metadata, rather than blindly tracking current Linux.
Repeat a URL with different refs to make multiple versions available. Reference
fetches are shallow, use separate detached worktrees, and share one bare clone
per URL. Source repositories retain full commit history for incremental review.
Neither submodules nor custom filters are followed. Private/internal reference
hosts must be excluded using the environment's network policy as well.

## Deeper review using upstream source

For OpenWrt reviews, start the trusted policy from
`configs/openwrt.json`. Keep `max_prs` at one for
the first run. This example maps kernel patches, mac80211, dnsmasq and
uboot-envtools to their public source repositories. The generic
`configs/example.json` still leaves references empty. An earlier task instruction
that explicitly sets `reference_repositories: []` must be removed to enable these
checks. Extend trusted mappings for other packages such as hostap, Dropbear or
BusyBox using their actual source/version metadata.

A mapping has `url` and either an explicit `ref` or a `ref_from` object. Optional
`match_paths` is a list of globs matched against changed OpenWrt paths; matching
entries are selected automatically during preparation. Entries without matchers
remain manual via `--reference <policy-index>`. Remotes always come from trusted
policy, never from PR-controlled `PKG_SOURCE_URL` or patch text.

Optional `repositories` restricts a mapping to a subset of the main allowlist,
including manual selections. The supplied firmware mappings specify
`["openwrt/openwrt"]`, so they do not run against procd or netifd in a mixed batch.
Direct source repositories are reviewed from their own PR worktrees; no firmware
package paths or upstream mapping is required. Add references only when useful
for that project's dependencies or related implementations.

Example version-matched package mapping:

```json
{
  "url": "https://thekelleys.org.uk/git/dnsmasq.git",
  "match_paths": ["package/network/services/dnsmasq/**"],
  "ref_from": {
    "file": "package/network/services/dnsmasq/Makefile",
    "variable": ["PKG_UPSTREAM_VERSION", "PKG_VERSION"],
    "prefix": "v"
  }
}
```

`ref_from` reads a literal assignment from a regular Git blob at the discovered
base and PR head. `file` and `variable` may be ordered fallback lists. The first
existing file is used, then the first unambiguous literal variable. Make functions,
shell expressions and multi-line expressions are never evaluated. If a version
changes, both base and proposed versions are fetched. The proposed version is
data, not executable configuration or permission to change the remote.

For kernels, `{series}` in these fields is replaced only by numeric series found
in changed `patches-X.Y`, `backport-X.Y`, `pending-X.Y` or `hack-X.Y` paths. The
example checks both current `target/linux/generic/kernel-X.Y` and older
`include/kernel-X.Y` layouts, combining `LINUX_VERSION-X.Y` with a `vX.Y` prefix.
Thus a literal `.24` for series `6.1` selects `v6.1.24`. mac80211 has a separate
mapping based on its backports package version, not the running target kernel.

Missing/ambiguous versions, tags and failed fetches are recorded in `references`.
There is no fallback to master. Check these errors even when preparation's PR
worktree phase succeeds. Review manually configured naming when a project's tags
do not correspond directly to the package version. Kernel context is not a
substitute for inspecting backports/OpenWrt compatibility code and patch ordering.

Each successful prepared reference reports `url`, `ref`, pinned `sha`, `clone`,
`worktree`, `policy_indices` and `for_prs`. Bindings identify the OpenWrt PR,
base/head metadata revision and SHA. Identical URL/ref requests share a checkout;
multiple versions of a repository share one bare clone. Only selected snapshots
are fetched shallowly; a large Linux history is not cloned for reference use.

Collect full source context and cross-site search results for a prepared reference:

```sh
python3 -E -s -m openwrt_ai.review.references \
  /path/to/prepared.json --repository openwrt/openwrt --pr 14895 --reference 0
```

Here `0` indexes **prepared references**, not the policy. Run only for successful
references assigned to this PR. The helper reads changed `.patch` files through
Git blobs, extracts upstream paths and changed function-call names, saves whole
upstream files as text, and searches the pinned source tree for related sites.
It records which matched files are outside the patch. Output is under the PR's
trusted `context/upstream-<index>/` directory, outside source worktrees. Repeated
inspection needs a fresh output directory (the script refuses to overwrite one).

`git apply --check` reports whether the patch fits the raw reference without
changing it. It does not apply OpenWrt prerequisites, evaluate Makefiles, run
tests, or compile/execute code. Its failure alone is not a defect: the patch may
depend on an earlier OpenWrt patch or a different strip convention.

The helper bounds collection to 20 patches/files, 24 function symbols and 500
search matches, with a 1-million-character per-file limit. It reports truncation,
missing files and errors. It is a context collector, not a semantic correctness
checker. The Work prompt requires following callers/callees, shared invariants,
parallel driver/backend paths, error exits and lifecycle pairs, and checking the
OpenWrt patch stack before reporting a missed site. Broaden/refine searches when
the collected context is insufficient. Source grep hits alone are not findings.

Work should save `coverage-<PR>.json` with the examined source SHAs, functions,
related sites, patch-stack checks and limitations. Keep coverage in artifacts
and logs; maintainer comments stay concise and cite pinned source evidence.
These checks improve review context but cannot guarantee finding every missed
site or replace builds/runtime tests.

## Manual dry run

Use absolute paths in a Work task. The shell examples below assume this trusted
repository is the current directory and a trusted policy at `/tmp/review-policy.json`.
Put the task's output in a new private directory:

```sh
review_run=$(mktemp -d /tmp/chatgpt-review-run.XXXXXXXX)
python3 -E -s -m openwrt_ai.review.discover \
  --config /tmp/review-policy.json --max-prs 1 > "$review_run/discovery.json"
```

Check the exit status and JSON before continuing. A per-PR failure is included
in `failed` with exit status 1; successfully discovered PRs can still be reviewed
manually. API/listing failures return a manifest with `failed` and exit status 1,
then stop further scanning. Pending work is fetched oldest-update-first from
GitHub and merged across repositories. Discovery stops as soon as the batch is
full instead of checking every open PR's review history. Review pagination is
still complete for each inspected PR. Complete submitted ChatGPT reviews or marked
conversation comments count as checkpoints; unchanged heads are skipped. PRs
already commented on by Claude are skipped entirely, even after head changes.
Other new/changed heads are selected.

For the first dry run, select one known open PR directly (replace `12345` with
the actual PR number and ensure its repository is in the trusted policy):

```sh
python3 -E -s -m openwrt_ai.review.discover \
  --config /tmp/review-policy.json \
  --repository openwrt/openwrt --pr 12345 > "$review_run/discovery.json"
```

This reads only repository metadata, that PR, and its review history; it never
lists the repository's other open PRs. Use `--repository` and `--pr` together.
An ordinary one-PR batch also stops early, but may inspect unchanged PRs first.
When discovery stops early, `scan_complete` is false and `deferred` is null:
the remainder is unknown, not zero. Skipped/failed counts cover only inspected
PRs. Do not enumerate the rest just to calculate a log count.

The tooling uses authenticated API reads or connector observations and stops
when the batch is full. Wait for any authenticated quota reset reported
by GitHub before retrying. Never switch accounts automatically.

```sh
python3 -E -s -m openwrt_ai.review.prepare \
  --config /tmp/review-policy.json "$review_run/discovery.json" \
  > "$review_run/prepared.json"
```

Use `--reference 0` (repeatable, zero-based) only for relevant configured public
references. An empty batch creates no clone directory. `prepared.json` reports
its private temporary root, per-PR worktrees, trusted rule files, review diff,
full PR diff, commit metadata, and failures. Source PRs are fetched from the
**base repository's** `refs/pull/<number>/head`, including fork PRs. A missing or
changed ref is a failure, not a different checkout to review.

If the last reviewed SHA is an ancestor, review incrementally. If the SHA is
unavailable or history was rewritten, inspect the full PR from its merge base.
Rewritten commit messages are included in commit inspection even when the file
tree is unchanged. Shallow source history is rejected. Rules are extracted from
the discovered base SHA as a regular Git blob; a symlink cannot supply rules.
PR checkout symlinks are materialized as text to avoid following them off-tree.

Ask Work to follow the task prompt and produce one findings file per PR, based
on `examples/review-findings.json`. It must inspect code and
discussion; scripts do not perform AI analysis. Keep the task rooted in the
trusted project, not the PR worktree, to avoid loading PR-controlled instructions.

Immediately after a real connector identity read, save its actual result with
the observation time in an identity file:

```json
{
  "login": "openwrt-ai",
  "id": 276137552,
  "observed_at": "2026-11-14T12:00:00+00:00"
}
```

Replace the example time with the current UTC observation time. Never fill in
identity from memory. Evidence must be at most five minutes old, with a timezone.
For proposed `fixed_threads`, capture the connector's thread state as:

```json
{
  "observed_at": "2026-11-14T12:00:00+00:00",
  "repository": "openwrt/procd",
  "number": 1,
  "head_sha": "0000000000000000000000000000000000000000",
  "threads": [{"comment_id": 7, "is_resolved": false, "comment_count": 1}]
}
```

Replace all example identifiers with actual observations from the target PR.
This is a normalized observation format, not the raw output of any particular
connector version. Public REST replies are rechecked, but unresolved state
comes from this recent connector observation because the public REST comment
list does not expose thread resolution. The code cannot prove that a finding
is semantically fixed or that a model-supplied observation is authentic.

```sh
python3 -E -s -m openwrt_ai.review.validate \
  --config /tmp/review-policy.json \
  --manifest "$review_run/discovery.json" \
  --identity "$review_run/identity.json" \
  "$review_run/findings-1.json" > "$review_run/preview-1.json"
```

Add `--threads "$review_run/threads-1.json"` only when proposing replies. The
validator checks policy, bot identity observation, snapshot, open/public state,
head and base against current REST reads or the fresh connector capture, full file pagination, LEFT/RIGHT
line locations, same-hunk ranges, action, sizes/counts, duplicate payload entries,
commit membership, and eligible prior ChatGPT threads. Missing/truncated textual
patches are rejected for inline anchors. GitHub's 3,000-file listing limit is
detected using `changed_files`; commit-check validation rejects PRs above the
250-commit endpoint cap. Binary files have no textual inline anchors. Sentence
count, relevance, semantic duplicate findings, and correctness need model/human
review; size/location validation is not a proof of review quality.

**Stop after inspecting the previews.** No script accepts a publish/enable flag.
Each preview explicitly says `publishing_enabled: false`. Validation failure
returns nonzero and emits no preview. Identity evidence is not an authentication
mechanism, and a preview can become stale before someone submits it.

## Completion and retries

A first future/manual authorized `COMMENT` review uses the exact analyzed
`commit_id`, line-anchored findings, and generated body. Every section contains
the full visible `Reviewed commit: <SHA>` and a hidden marker of the form
`<!-- chatgpt-work-v1:complete:<40-character SHA> -->`, including clean reviews.

If a marked ChatGPT review or conversation comment already exists, the preview
extends the latest existing one instead of proposing another review. It preserves
the old body exactly and appends a section with the checked SHA, commit checks,
and new findings with validated path/line references. Follow-up findings go into
that body rather than a second review with new inline comments. Do not repeat
unresolved findings from earlier rounds.

Such a preview has `review_preview: null` and a `comment_update_preview` containing
`comment_kind`, `comment_id`, `reviewed_commit`, the full replacement `body`, and
`expected_body_sha256` for the old body. A future publisher must check the live
body hash, ownership, PR membership and head before updating. If the body changed,
regenerate the preview rather than overwriting newer content. `comment_kind: issue`
means a top-level issue comment; `comment_kind: review` means a submitted review
body. They have different GitHub endpoints and IDs. If the connector cannot edit
a review body, keep the update as a preview; do not create another review instead.

GitHub keeps the original `commit_id` when a review body is edited. Discovery
reads the latest marked SHA from its appended body when the visible SHA agrees,
then uses that SHA for incremental review. It also recognizes bot-owned marked
conversation comments. Pending/dismissed ChatGPT reviews are not checkpoints.

Claude exclusions are checked in discovery and again during validation. Recognized
authors are `claude[bot]`, `claude-code[bot]`, and unmarked legacy `openwrt-ai`
output (requiring both the bot login and numeric ID). This legacy rule is needed
because both reviewers used that account; it deliberately treats other unmarked
output by that bot as already covered. Human mentions of Claude cannot trigger
the exclusion. Inline ChatGPT comments need no separate marker when their parent
review is marked. Never edit Claude's or another author's comments.

Dry-run previews do not advance published completion. Repeated dry runs will
review the same unpublished heads intentionally. Archive findings/previews and
logs in durable trusted storage if you want to reuse analysis manually. Do not
pretend an unpublished preview or an incomplete review was posted. Once a marked
review is actually submitted, unchanged heads will be skipped by discovery.

Never automatically retry a write after a timeout: verify whether the marked
review already exists first. This implementation checks existing completion
during preview validation but has no cross-run publisher lock and cannot prevent
two simultaneous users from later submitting the same preview. This is one
reason automatic publication is not part of this implementation.

## Fork test and future schedules

1. Open a small public fork PR against an explicitly allowlisted test repository.
   Include a known issue and optionally an untrusted instruction in its title or
   comment. Verify it remains inert data. Do not run its code.
2. Run discovery with one PR, prepare its worktree, analyze it, and validate the
   preview. Verify the checkout SHA matches the PR head and that trusted base
   rules were used. Inspect every proposed inline anchor and suggestion.
3. Push a fix and repeat. The stale old findings must fail validation. To test
   published-history skipping without posting, use the offline tests below.
   Live completion/reply tests require separate explicit authorization to post
   on a disposable test PR; none is granted by following this setup guide.
4. Test actual task tool/approval behavior before relying on unattended execution.
   Offline tests cannot establish that publishing or scheduling works in Work.

After the manual dry run is satisfactory, the operator may separately create
one standalone scheduled task using the version-controlled prompt, explicit
trusted paths, and UTC timezone. The intended times are **03:00 and 15:00 UTC**:

```text
RRULE:FREQ=DAILY;BYHOUR=3,15;BYMINUTE=0;BYSECOND=0
```

If the UI requires two schedules, use two triggers with identical task instructions
and prevent overlapping runs at the execution host. Check the saved timezone;
Berlin local time changes with daylight saving. Start with a one-PR batch and
inspect the first runs before increasing to eight (or sixteen). Scheduling a
dry-run task does not enable publishing. Before any future live activation,
disable the Claude reviewer to avoid two systems using `openwrt-ai` completion
history. This repository does not disable Claude for you.

To pause, disable the task in ChatGPT's Scheduled view. Also stop any running
task; disabling future triggers need not stop an already running invocation.
Revoking bot authorization is a separate emergency stop for connector access.

## Operations and security limits

Retain stdout JSON, stderr, exit codes, and the Work execution log outside the
reviewed worktrees. Distinguish skipped, deferred, failed, validated, and actually
published PRs. This implementation always publishes zero. On failure, repair
policy/access or retry discovery; do not edit the expected SHA to force acceptance.
Retain the temp root while investigating; afterward remove only that exact
reported temporary directory. Each preparation invocation uses a fresh root and
does not reuse previous or attacker-created clones.

The Git subprocess environment drops ambient tokens, askpass helpers, global
configuration, templates, hooks, custom attributes, and non-HTTPS protocols.
Arguments are arrays without a shell. PR numbers, repository names, SHAs and
diff anchors are validated. Push URLs in temporary clones are deliberately
unusable. These controls constrain the supplied scripts, not arbitrary future
commands issued by the model. Resource exhaustion from very large repositories
or diffs still requires host limits; run in a dedicated restricted environment.

Work versus Codex Cloud and Claude Routines:

| Capability | This Work implementation | Existing Claude routines / Codex Cloud |
| --- | --- | --- |
| Trigger | Operator-configured Work schedule, not activated here | Claude HTTP routine trigger; Codex Cloud has its own task/environment model |
| Model billing | Existing ChatGPT subscription allowance | Separate product-specific allowance/configuration |
| PR contexts | One worktree per PR; separate model contexts must be tested | Claude prompt delegates; no assumption of identical Work orchestration |
| Source setup | Trusted scripts and explicit runtime/network prerequisites | Existing consumer/routine or Cloud environment setup |
| Publishing | Preview only; future enforced bot-only publisher required | Existing Claude connector posts; not reused by these scripts |

Manual configuration still required: trusted policy and script commit, runtime
and network restrictions, durable output location, bot-only connector access,
manual fork test, then any separately requested schedule. Live posting also
requires a separately reviewed publisher/permission boundary and authorization.

## Offline checks

```sh
python3 -B -m unittest discover -s tests -v
ruff check openwrt_ai tests
ruff format --check openwrt_ai tests
```

Tests mock public GitHub responses and use local Git fixtures only. They never
call a model or post a review. Local-file Git transport exists only in the test
fixture runner; production Git access allows HTTPS only.

`.github/workflows/checks.yml` runs these offline tests for tooling
PRs or manual dispatch. It has no review trigger, schedule, secrets, or write
permissions and does not run the discovery scripts against live GitHub.

Issue triage is a separate, disabled-by-default task. See
[issue-triage.md](issue-triage.md) for its read-only foundation and future work.
