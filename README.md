# OpenWrt AI tools

Standalone tooling for ChatGPT-assisted pull request review across public OpenWrt
repositories. ChatGPT reads the code and writes structured findings; these Python
helpers discover PRs, prepare source context, and validate publishing previews.
They make no model API calls. Posting is available through a separate publisher.

This directory is an independent Git repository. Its original implementation was
extracted from `actions-shared-workflows` at commit
`0d788a1918168397d3c3774b9c2fa33baed2a420`. The original workflows are unchanged.

## Choose a repository profile

| Trusted policy | Target repositories | Upstream context |
| --- | --- | --- |
| `configs/openwrt.json` | `openwrt/openwrt` | Version-matched Linux, mac80211, dnsmasq and uboot-envtools |
| `configs/procd.json` | `openwrt/procd` | PR repository source |
| `configs/netifd.json` | `openwrt/netifd` | PR repository source |
| `configs/firmware-utils.json` | `openwrt/firmware-utils` | PR repository source |
| `configs/multi-repo.json` | OpenWrt, procd, netifd, ubus, libubox | Firmware mappings apply only to `openwrt/openwrt` |
| `configs/example.json` | procd example | Starting point for another public repository |

Edit a trusted copy of the policy to change the allowlist or add source mappings.
The review pipeline is shared: procd and other direct source repositories do not
need OpenWrt's package or kernel layout. Project review rules come from
`.github/llm-review-rules.md` at the target PR's base commit, when present.

## Run a review preview

Use Python 3.11+ and Git from this repository's root. Runtime dependencies are
Python standard library only; installation is unnecessary. Read
[setup instructions](docs/setup.md) for authentication, connector snapshots,
runtime requirements and the complete preview procedure. Supply
[the review prompt](prompts/pr-review.md) to ChatGPT with absolute trusted paths.

For a first test, select one actual PR and a new output directory:

```sh
review_run=$(mktemp -d /tmp/chatgpt-review-run.XXXXXXXX)
python3 -B -E -s -m openwrt_ai.review.discover \
  --config configs/procd.json --repository openwrt/procd --pr 123 \
  > "$review_run/discovery.json"
# Check exit status and failures before continuing.
python3 -B -E -s -m openwrt_ai.review.prepare \
  --config configs/procd.json "$review_run/discovery.json" \
  > "$review_run/prepared.json"
```

Replace `123` with an open PR number. Discovery requires either the protected
`CHATGPT_REVIEW_GITHUB_TOKEN` for `openwrt-ai` or fresh authenticated connector
observations supplied with `--github-snapshot`. There is no anonymous API fallback.
Git fetches public source over HTTPS without that token.

ChatGPT then examines the prepared context and creates findings in the
[example format](examples/review-findings.json). The validator rechecks the PR
and produces a preview; see the setup guide for its identity and snapshot inputs.
PRs with Claude comments are skipped. Existing marked ChatGPT comments are
extended in the preview with the checked commit SHA.

## Publish a review

Follow the [publishing setup](docs/publishing.md) to run the opt-in publisher
after preview validation. It revalidates against GitHub as `openwrt-ai` and
either posts a single comment review or extends the existing marked comment.

## Future ticket triage

Issue triage has a separate module and [design](docs/issue-triage.md). It is
disabled in every supplied profile. Its current implementation only discovers
issue candidates when explicitly enabled; it does not classify, label, comment,
close issues, or advance triage state.

```sh
python3 -B -E -s -m openwrt_ai.triage.discover --config configs/procd.json
```

This reports `status: disabled` without authentication or network calls. The
[triage prompt](prompts/issue-triage.md) records the intended future boundaries.

## Development

```sh
python3 -B -W error::ResourceWarning -m unittest discover -s tests -v
ruff check openwrt_ai tests
ruff format --check openwrt_ai tests
```

Tests use mocked GitHub data and local Git fixtures. CI runs these offline tests
with read-only repository permissions. Logs, credentials and run artifacts belong
outside the tracked source tree; ignore patterns cover common local output names.
