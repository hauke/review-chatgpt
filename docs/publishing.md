# Publish PR reviews

Run the publisher after the review task has produced a discovery manifest,
findings, and a validated preview for the PR. Use the same pinned tooling
checkout and trusted policy for both phases. For firmware-utils, use
`configs/firmware-utils.json`. Keep `fixed_threads` empty in findings for runs
intended to publish.

## GitHub and runtime setup

1. Use a GitHub **user credential for `openwrt-ai`** with **Pull requests: Read
   and write** on `openwrt/firmware-utils`. The publisher verifies
   `GET /user` against the bot's login and ID. For a fine-grained token, select
   the OpenWrt organization as resource owner and scope access to firmware-utils
   if the bot and organization are eligible. Ask an organization owner to
   approve the token if required. GitHub limits fine-grained tokens for
   public repositories where the account is outside the organization.
   [GitHub token restrictions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
   · [Review permissions](https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request)
2. Configure `CHATGPT_REVIEW_PUBLISH_TOKEN` as a protected runtime secret for
   the publisher process. Keep the review phase on the existing read connection
   or read token. The connector does not supply a shell credential. If the
   Work runtime cannot inject the publishing secret securely, run the publisher
   on a trusted host that receives the run artifacts and holds the secret.
3. Use one publisher host with a private, persistent state directory for
   every run against this repository. The directory must be owned by the
   publisher account and have mode `0700`. Keep it outside PR worktrees.
4. Disable any active Claude reviewer for the target repository before enabling
   the publishing step. Existing Claude comments still cause their PRs to be
   skipped.

## Configure the task

Retain the regular Work review prompt for discovery, analysis, and preview
validation. Add the following instruction to the saved task once the publisher
runtime is configured:

> For each successful preview, hand its discovery manifest, findings with
> `fixed_threads: []`, and preview to the configured publisher. Use the same
> pinned checkout and policy. Report the publisher's status and GitHub comment
> ID in the execution log. Use the publish command documented below if this
> runtime provides the protected publisher secret and persistent state directory.

Run the command from the trusted tooling checkout. Set `review_run` to that
run's artifact directory, `pr_number` to the reviewed PR number, and
`publisher_state` to the configured persistent state directory.

```sh
python3 -B -E -s -m openwrt_ai.review.publish \
  --config configs/firmware-utils.json \
  --manifest "$review_run/discovery.json" \
  --preview "$review_run/preview-$pr_number.json" \
  --state-dir "$publisher_state" \
  "$review_run/findings-$pr_number.json"
```

The publisher reads the live PR, diff, reviews, and discussion using the bot
credential. It checks the allowlist, PR state, base and head SHAs, Claude
exclusions, and the exact preview payload. It submits the first result as a
`COMMENT` review at the reviewed commit with inline findings. For a later
commit, it extends the bot's existing marked review or conversation comment,
preserving its body and recording the newly checked SHA.

Each PR is locked while publishing. The lock applies to runs sharing the state
directory. An in-flight marker survives a timeout or process interruption.
The next attempt checks GitHub for the matching published review before
allowing another write. If the result remains uncertain, inspect GitHub and
clear the reported `.pending` file only after confirming the write failed.
Keep the lock file in place. GitHub can still receive a concurrent PR change
between the final read and the write, so first reviews carry the exact commit
ID and updates record the exact SHA in their body.
