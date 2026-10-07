# Contributing to `whip-it`

Thank you for helping make `whip-it` faster, safer, and more reliable. Bug
reports, design questions, documentation corrections, tests, and code are all
valuable contributions.

This project uses an issue-first workflow. It gives contributors an early
design signal and prevents two people from solving the same problem in
different directions.

## Before writing a change

Search the [open issues] and [closed issues] for related work. If no accepted
issue covers the change, open one and describe:

- the problem or limitation;
- the behavior you propose;
- a small example when client hook contracts or configuration are involved; and
- any compatibility, latency, or multi-client implications you see.

Wait for the maintainer to acknowledge the issue before starting an
implementation. An issue is ready for community implementation only when the
maintainer adds the `status: accepted` label or explicitly comments that it is
ready. A request for more information, a general acknowledgment, or continued
design discussion is not approval to implement.

This gate applies to pull requests opened by anyone other than
[`@awill1988`]. The maintainer's own changes are exempt because their
authorship supplies the prioritization and design decision directly. The same
technical, test, and review standards apply to every pull request.

An early pull request is not treated as an implementation candidate. The
maintainer will point it to this guide and may close it without a technical
review. That response is about sequencing, not the merit of the person, idea,
or code. After the related issue is accepted, the contributor is welcome to
reopen the pull request or submit a focused replacement.

Small typo fixes still benefit from an issue, even if the issue and acceptance
happen in the same exchange. Issues labeled `good first issue` or
`help wanted` are already accepted unless a maintainer comment says otherwise.

## Maintainer commitments

The issue-first gate creates a matching obligation for the maintainer. The
maintainer will:

- acknowledge a new issue within seven calendar days;
- state whether it is accepted, needs information, needs design discussion,
  duplicates existing work, or is outside the project's direction;
- give a reason when declining or closing a proposal;
- provide an initial review of an eligible pull request within seven calendar
  days;
- communicate when a review is blocked or delayed instead of leaving the
  contributor without status; and
- keep feedback specific, respectful, and focused on the change.

Acknowledgment does not guarantee acceptance or a merge. It guarantees a
clear response and a visible next state. If seven days pass without a response,
one polite follow-up on the issue or pull request is welcome.

## Design constraints

Changes must preserve the project's core contract unless an accepted issue
explicitly changes it:

- **Sub-15ms hook execution latency**: Agent lifecycle hooks execute on every
  tool call and invocation event; latency must stay negligible.
- **Zero external runtime dependencies**: The engine runtime requires only
  Python 3.10+ standard library modules (`sys`, `os`, `re`, `json`, `hashlib`,
  `pathlib`, `fcntl`).
- **Fail-open resilience**: Hook errors, malformed payloads, or unexpected
  exceptions must never crash or deadlock the parent agent session.
- **Atomic state persistence**: Session quota state must be written atomically
  and protected with file locking (`fcntl`) against concurrent hook runs.
- **Multi-client parity**: Protocol adapters must maintain equivalent safety
  and simplification semantics across Anthropic Claude Code, Google
  Antigravity CLI, and OpenAI Codex CLI.
- **Strict override prevention**: Explicit user prompt constraints take absolute
  precedence over autonomous model planning or recursive delegation.

Keep proposals narrow. Changes to hook protocols, supported agent schemas,
runtime dependencies, release behavior, and licensing require design discussion
before implementation.

## Development setup

Activate the repository git hooks:

```sh
git config core.hooksPath .githooks
```

Create a focused branch in your fork.

## Commit standards

All commit messages MUST follow Conventional Commits (`feat:`, `fix:`,
`refactor:`, `chore:`, `docs:`, `test:`, `ci:`).

- **Strict lowercase subject**: The subject line must be lowercase (e.g.
  `feat: add feature`, not `feat: Add feature`).
  - Capitalization is permitted only inside an optional leading ticket prefix
    (e.g. `[WHIP-123] feat: add feature`).
- **Character limits**:
  - Header line: recommended 50 characters, hard cap at 72 characters.
  - Body lines: wrapped at 72 characters maximum.
  - No trailing period in the subject line.
- **Zero AI attribution**:
  - Do not include AI attribution footers or signatures (e.g. `Co-Authored-By`,
    `Generated-By`, `Assisted-By`, `Reviewed with Claude Code`, or robot
    emojis `🤖`).

The repository includes a commit message validator in `tools/commit_check`
enforced locally via `.githooks/commit-msg` and verified in CI.

## Validate the change

Run the checks that match CI before requesting review:

```sh
poetry run ruff check .
poetry run ruff format --check .
poetry run python -m unittest discover -s tests -p "test_*.py" -v
python3 tools/commit_check/test_commit_check.py
python3 tools/adversarial_reviewer/test_adversarial_review.py
poetry build
```

Add focused tests for behavior changes. Update documentation when the accepted
change alters interfaces, hook schemas, configuration options, or
contributor workflows.

## Open the pull request

Link the accepted issue and explain the resulting behavior. State the affected
clients, modules, and any manual validation. Keep one concern per pull request
and keep the branch current with `main` without rewriting shared history.

Review is a conversation. Answer questions, explain tradeoffs, and make review
updates visible in new commits until the change is approved. A green CI run is
required but does not replace maintainer review.

## Licensing

By contributing, you agree that your contribution is licensed under the
[`MIT`](LICENSE) terms of the project and that you have the right to submit
it under those terms.

[open issues]: https://github.com/awill1988/whip-it/issues
[closed issues]: https://github.com/awill1988/whip-it/issues?q=is%3Aissue%20state%3Aclosed
[`@awill1988`]: https://github.com/awill1988
