# Module design — gitrepo

## Decisions needed from you

This section contains only open items. **No open items** — everything below is implementation
of boundaries the architecture fixed. 

## Responsibility

All git access for the run (architecture: no other module invokes git). Pure queries — this
module never mutates the repository, matching the never-commits decision.

## Interface

```python
class GitRepo:
    @classmethod
    def discover(cls, path: Path, git_executable: str = "git") -> "GitRepo"
    @property
    def worktree_root(self) -> Path
    def repo_id(self) -> str
    def head_sha(self) -> str
    def resolves(self, sha: str) -> bool
    def is_ancestor(self, ancestor: str, descendant: str) -> bool
    def dirty_paths_outside(self, exclude: str) -> list[str]
    def effective_delta(self, base_sha: str, end_sha: str, exclude: str) -> Delta
    def tree_matches(self, sha: str, exclude: str) -> bool
```

- `discover` walks up from `path` to the worktree root; raises `NotARepositoryError` outside
  a repo. `git_executable` exists for failure injection and is never varied in production.
- `repo_id`: first 16 hex chars of SHA-256 of the resolved worktree root path — same checkout
  collides on the lock (R21), different checkouts do not.
- `head_sha` raises `NoCommitsError` on an unborn HEAD (R11).
- `dirty_paths_outside(".blare")`: every working-tree difference from HEAD — tracked files
  modified or deleted, plus untracked files; git-ignored files never counted (R11); paths
  under the excluded directory filtered out. Parsed from
  `git status --porcelain=v1 -z --no-renames`.
- `effective_delta(base_sha, end_sha, exclude)`: net range diff between the two named
  commits excluding the given directory, via
  `git diff --name-status -z --no-renames base end -- . ':(exclude).blare'` — the end commit
  is a parameter so the orchestrator can pass the SHA captured at run start (R6) rather
  than relying on HEAD-at-call-time. Empty for a
  change plus its revert (R7's net-diff semantics). `--no-renames` makes output independent
  of the user's `diff.renames` config: a rename is always added-plus-deleted. Status `T`
  (typechange) maps to `modified`; any other status letter is unparseable output
  (`GitCommandError`).
- `tree_matches`: true when the working tree outside `exclude` is byte-identical to the tree
  at `sha` and no untracked files exist outside `exclude` — the R20 write-time re-check.
  Git-ignored files never affect it, exactly as in `dirty_paths_outside`: a build output or
  editor cache appearing mid-run must not abort the write. Implemented as
  `git diff --quiet <sha> -- . ':(exclude)<exclude>'` plus the same porcelain untracked
  scan the dirty check uses.
- `is_ancestor`: `git merge-base --is-ancestor` (R15).

## Data structures

```python
@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: Literal["added", "modified", "deleted"]

@dataclass(frozen=True)
class Delta:
    files: tuple[ChangedFile, ...]
    @property
    def is_empty(self) -> bool
```

## Error handling

Three exception types, all carrying the failed command and stderr where applicable:

- `NotARepositoryError` — feeds R11's outside-a-repo refusal.
- `NoCommitsError` — feeds R11's no-commits refusal.
- `GitCommandError` — a git invocation whose exit code falls outside that command's answer
  set (below), the executable missing, or output the module cannot parse regardless of exit
  code; message includes the exact command line and stderr verbatim.

Some commands answer through their exit code; those codes are answers, not errors:

- `is_ancestor` — `git merge-base --is-ancestor`: exit 0 → true, 1 → false, ≥2 →
  `GitCommandError`.
- `resolves` — `git rev-parse --verify --quiet <sha>^{commit}`: exit 0 → true, 1 → false.
- `discover` — `git rev-parse --show-toplevel` failing → `NotARepositoryError`.
- `head_sha` — `git rev-parse HEAD` failing on an unborn branch → `NoCommitsError`.

All subprocess calls run with explicit `cwd`, never inherit the caller's, and never use
`shell=True`.

## Failure visibility

This module raises; it never prints or logs. `GitCommandError.message` carries command line
plus stderr so the orchestrator's R13 rendering shows the user exactly what git said. There
are no swallowed errors: every git exit outside its command's answer set, and every
unparseable output, becomes an exception.

## Test plan

Unit tests use real git against temporary repositories built per test — git is fast, local,
and deterministic. The only test doubles are the stub executables injected through
`git_executable` in the failure-mode tests, for outputs real git cannot produce.

Contract tests (`test_contract_*`), one per behaviour:

- discover inside a repo (and from a subdirectory) finds the root; outside raises.
- `head_sha` returns the commit; unborn HEAD raises `NoCommitsError`.
- `resolves` true for a real SHA, false for garbage and for a truncated unknown SHA.
- `is_ancestor` true/false/self cases.
- dirty detection: modified tracked file listed; deleted tracked file listed; untracked
  file listed; git-ignored file not listed; file under the excluded dir not listed; clean
  tree yields empty list.
- delta lists added/modified/deleted with correct statuses; same-SHA delta empty;
  change-plus-revert delta empty; files under the excluded dir absent from the delta;
  a rename surfaces as added plus deleted regardless of `diff.renames` config; a
  typechange (file replaced by symlink) maps to `modified`.
- `repo_id` stable across calls and invocation directories; differs between two checkouts of
  the same content.
- `tree_matches` true on clean tree at HEAD; false after a tracked edit, after an untracked
  file appears, and after a new commit; edits under the excluded dir and a git-ignored file
  appearing do not break it.

Failure-mode tests (`test_failure_<dependency>_<mode>`), dependency = the git subprocess:

- `test_failure_git_missing_executable` — `git_executable` pointed at a nonexistent path;
  `GitCommandError` naming the executable.
- `test_failure_git_command_error` — corrupted object store (`.git/objects` emptied),
  exercised through `effective_delta` (whose corrupt-store failure has no answer-set or
  typed-refusal mapping); `GitCommandError` carrying git's stderr.
- `test_failure_git_answer_set_exceeded` — stub git exiting 2 on
  `merge-base --is-ancestor`; `GitCommandError`, not `False`.
- `test_failure_git_unparseable_status` — injected executable (a stub script) exiting zero
  while emitting malformed `-z` output; `GitCommandError` raised rather than silent misparse.
- `test_failure_git_unknown_status_letter` — stub emitting a status letter outside the
  mapping; `GitCommandError`.

The injectable `git_executable` parameter is the only injection point; no environment or
global state is touched.
