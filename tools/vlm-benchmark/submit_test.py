#!/usr/bin/env python3
"""git_submit_test -- commit a web-UI-created VLM benchmark task to the repo (issue #127).

Takes a task **name** (or a path to an exported ``tasks-<name>.jsonl`` file),
validates it with the runner's own ``vlm_bench._load_tasks()`` -- so a
committed task is guaranteed to load with ``vlm_bench.py --tasks-file`` --
copies it to the canonical location ``tools/vlm-benchmark/tasks/<name>.jsonl``,
then runs ``git add`` + ``git commit`` (optionally ``git push``).

Every command that is (or would be) run is printed, so a human can copy-paste
or verify. This is the ``git_submit_test`` helper the benchmark web UI
displays per saved task (the UI itself only shows + copies the command -- a
``file://`` page cannot launch a local process).

Usage:
  python3 tools/vlm-benchmark/submit_test.py red-circle-detect
  python3 tools/vlm-benchmark/submit_test.py tasks-red-circle-detect.jsonl
  python3 tools/vlm-benchmark/submit_test.py red-circle-detect --message "my custom message"
  python3 tools/vlm-benchmark/submit_test.py red-circle-detect --push
  python3 tools/vlm-benchmark/submit_test.py red-circle-detect --dry-run

Exit codes: 0 = committed (or nothing to do), 1 = task file not usable,
2 = usage error (task not found / bad arguments).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # tools/vlm-benchmark
REPO = os.path.dirname(os.path.dirname(HERE))              # repo root
TASKS_DIR = os.path.join(HERE, "tasks")


def _safe_name(name: str) -> str:
    """Sanitize a task name the same way the web UI's safeName() does."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or ""))
    s = s.strip("-")
    return s or "task"


def _name_from_path(path: str) -> str:
    """Derive the canonical task name from an exported file name.

    ``tasks-red-circle-detect.jsonl`` -> ``red-circle-detect``; a bare
    ``my-task.jsonl`` -> ``my-task``.
    """
    base = os.path.basename(path)
    if base.startswith("tasks-"):
        base = base[len("tasks-"):]
    if base.endswith(".jsonl"):
        base = base[: -len(".jsonl")]
    return _safe_name(base)


def resolve(name_or_path: str) -> tuple[str, str, str]:
    """Resolve the user's argument to (source_path, task_name, dest_path).

    Accepts either an existing file path (an exported task file) or a task
    name. Name lookup order: ``<cwd>/<name>``, ``<cwd>/tasks-<name>.jsonl``,
    ``tools/vlm-benchmark/tasks/<name>.jsonl``,
    ``tools/vlm-benchmark/tasks-<name>.jsonl``.
    """
    arg = name_or_path.strip()
    cwd = os.getcwd()
    if os.path.isfile(arg):
        name = _name_from_path(arg)
        return arg, name, os.path.join(TASKS_DIR, name + ".jsonl")
    name = _safe_name(arg)
    for cand in (
        os.path.join(cwd, name),
        os.path.join(cwd, "tasks-" + name + ".jsonl"),
        os.path.join(TASKS_DIR, name + ".jsonl"),
        os.path.join(HERE, "tasks-" + name + ".jsonl"),
    ):
        if os.path.isfile(cand):
            return cand, name, os.path.join(TASKS_DIR, name + ".jsonl")
    tried = [
        os.path.join(cwd, name),
        os.path.join(cwd, "tasks-" + name + ".jsonl"),
        os.path.join(TASKS_DIR, name + ".jsonl"),
        os.path.join(HERE, "tasks-" + name + ".jsonl"),
    ]
    print(f"error: no task file found for '{arg}'. Tried:", file=sys.stderr)
    for t in tried:
        print(f"  {t}", file=sys.stderr)
    print("hint: export the task from the web UI first (Saved tests -> export),", file=sys.stderr)
    print("      or pass the path to the exported tasks-<name>.jsonl file.", file=sys.stderr)
    raise SystemExit(2)


def validate(path: str) -> int:
    """Validate with the runner's own loader (A5). Returns usable task count."""
    sys.path.insert(0, HERE)
    import vlm_bench  # noqa: E402  (stdlib-only; same dir)
    tasks, malformed = vlm_bench._load_tasks(path)
    if malformed:
        print(f"# warning: {malformed} malformed task line(s) skipped in {path}")
    if not tasks:
        print(f"error: {path} has no usable tasks (each line needs a non-empty prompt).", file=sys.stderr)
        raise SystemExit(1)
    print(f"# validated: {len(tasks)} usable task(s) via vlm_bench._load_tasks()")
    return len(tasks)


def git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Commit a web-UI-created VLM benchmark task to the repo (issue #127).",
    )
    ap.add_argument("task", help="task name, or a path to an exported tasks-<name>.jsonl file")
    ap.add_argument("--message", default=None, help="custom commit message (default: 'vlm-bench: add/update task <name>')")
    ap.add_argument("--push", action="store_true", help="also git push after committing")
    ap.add_argument("--dry-run", action="store_true", help="validate + print the commands; change nothing")
    args = ap.parse_args()

    src, name, dest = resolve(args.task)
    validate(src)

    rel_dest = os.path.relpath(dest, REPO)
    add_cmd = f"git add {rel_dest}"
    commit_cmd = f"git commit -m \"vlm-bench: add task {name}\" -- {rel_dest}"
    push_cmd = "git push"
    rel_src = os.path.relpath(src, REPO)
    copy_cmd = f"cp {rel_src} {rel_dest}"

    if args.dry_run:
        print("# dry-run: the following would run (from the repo root):")
        print(f"#   {copy_cmd}")
        print(f"#   {add_cmd}")
        print(f"#   {commit_cmd}")
        if args.push:
            print(f"#   {push_cmd}")
        print(f"# dry-run OK: task '{name}' is valid and would be committed to {rel_dest}")
        return 0

    # Idempotent: same content already in place -> nothing to do.
    if os.path.isfile(dest) and open(src, "rb").read() == open(dest, "rb").read():
        print(f"# nothing to do: {rel_dest} already matches {os.path.basename(src)}")
        return 0

    os.makedirs(TASKS_DIR, exist_ok=True)
    print(f"# $ {copy_cmd}")
    shutil.copyfile(src, dest)

    # add vs update: is the file already tracked?
    tracked = git(REPO, "ls-files", "--error-unmatch", "--", rel_dest).returncode == 0
    verb = "update" if tracked else "add"
    message = args.message or f"vlm-bench: {verb} task {name}"

    print(f"# $ {add_cmd}")
    r = git(REPO, "add", "--", rel_dest)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(1)

    print(f"# $ git commit -m \"{message}\" -- {rel_dest}")
    r = git(REPO, "commit", "-m", message, "--", rel_dest)
    if r.returncode != 0:
        print(r.stdout, file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(1)
    print(r.stdout.strip())

    if args.push:
        print(f"# $ {push_cmd}")
        r = git(REPO, "push")
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            raise SystemExit(1)
        print(r.stdout.strip())

    print(f"# committed task '{name}' -> {rel_dest}")
    print(f"# re-run it with: python3 tools/vlm-benchmark/vlm_bench.py --models <model> --tasks-file {rel_dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
