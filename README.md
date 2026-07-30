# blare

blare analyzes a codebase, documents its failure modes — including the upstream links in a
failure chain, not just the user-visible end — and recommends the metrics and alerts needed so
every one of them can be alerted on. **The alert arriving is the point, not the failure mode
documentation.**

It writes structured YAML and derived markdown views under `.blare/` in the target repo. You
review and commit them yourself; blare never runs a git write operation.

## When to use it

- You operate a service in production, its code lives in git, and you want its observability
  gaps found systematically rather than accumulated ad hoc after incidents.
- You want failure modes traced upstream — the root cause a symptom traces back to — not just
  the symptom itself, so each link can be caught as early as possible.
- You're willing to review an agent's findings at a few checkpoints per run and commit the
  result yourself.

## When not to use it

- **Monorepos or git submodules** — out of scope for now.
- **CI/CD or any non-interactive use** — blare is a terminal tool driven by a human at a
  checkpoint; it has no unattended mode.
- **Implementing the recommendations** — blare recommends metric and alert changes; it doesn't
  write them into your codebase or deploy them.
- **Anything beyond Prometheus/PromQL** — the only shipped metrics/alerting stack in the MVP.
- **Non-Linux hosts, or codebases not managed in git.**

## Install

Requirements: Linux, git, [Bazel](https://bazel.build), and a Claude Code subscription (blare
authenticates through the Claude Agent SDK's subscription login — it never stores an API key).

```
$ git clone <this-repo-url> blare
$ cd blare
$ bazel build //src/blare:blare
```

Run it via `bazel run //src/blare:blare -- <args>` from anywhere, or copy
`bazel-bin/src/blare/blare` onto your `PATH`.

## Quick start

From the root of the git repo you want analyzed:

```
$ blare analyze
```

This runs four phases — system map, failure modes, metric coverage, alert recommendations —
pausing at each to show you what it found and take chat if you want to redirect it. Approving
the last phase writes the artifact set to `.blare/` and records the commit you analyzed at.
Review the diff under `.blare/` and commit it — committing is how you accept the run.

Once a codebase has been analyzed, keep it current as the code changes:

```
$ blare update
```

`update` computes the delta since the last recorded analysis and only re-runs the phases that
delta actually affects — checkpointing the same way, or telling you outright that nothing
changed if the delta touches nothing blare tracks.

Every failure mode blare finds ends up with a **coverage status** — `alertable` (metrics exist,
only the alert rule is missing), `metric-gap` (the metric itself needs to change first), or
`excluded` (deliberately not covered, with the reason recorded) — so `2 gaps` always means
something concrete and actionable, never a vague health score.
