"""Persistent memory on Hugging Face (optional; off unless HF_TOKEN is set).

GitHub refuses files over 100 MB and slows to a crawl well before that, which
is what broke saves on 2026-10-27 (an 83 MB bot.sqlite.gz timed out). A private
Hugging Face *dataset* repo stores large files (LFS) for free, so with
HF_TOKEN + HF_MEMORY_REPO set the database lives there instead; the small files
the phone app reads (summary.json, REPORT, the dashboard) stay on bot-data.

Same safety rules as the bot-data path:
- load fails closed: a reachable repo without the file falls back to bot-data
  (first-run migration); any other error stops the run.
- save refuses to overwrite newer memory: every upload names the commit this
  run restored as its parent, and Hugging Face rejects it if the repo moved.

CLI (used by .github/load.sh / save.sh):
    python -m bot.hf_memory enabled          exit 0 if configured
    python -m bot.hf_memory load  <dest.gz>  prints the restored commit, exit 3 if empty
    python -m bot.hf_memory save  <src.gz> <base>
"""
import os
import sys

FILE = "bot.sqlite.gz"


def config():
    token = os.environ.get("HF_TOKEN") or ""
    repo = os.environ.get("HF_MEMORY_REPO") or ""
    return (token, repo) if token and repo else (None, None)


def enabled():
    return config()[0] is not None


def _api(api=None):
    if api is not None:
        return api
    from huggingface_hub import HfApi
    return HfApi(token=config()[0])


def load(dest, api=None):
    """Download the memory file. Returns the repo commit it came from, or None
    when the repo exists but holds no memory yet (caller falls back)."""
    token, repo = config()
    api = _api(api)
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
    info = api.repo_info(repo, repo_type="dataset", files_metadata=False)
    names = {s.rfilename for s in (info.siblings or [])}
    if FILE not in names:
        return None
    path = api.hf_hub_download(repo_id=repo, filename=FILE, repo_type="dataset",
                               revision=info.sha, local_dir=os.path.dirname(dest) or ".")
    if os.path.abspath(path) != os.path.abspath(dest):
        os.replace(path, dest)
    return info.sha


def save(src, base, api=None):
    """Upload the memory file on top of commit `base` (None = repo had none yet).
    Raises if the repo moved since this run restored it."""
    from huggingface_hub import CommitOperationAdd
    token, repo = config()
    api = _api(api)
    info = api.repo_info(repo, repo_type="dataset", files_metadata=False)
    if base and info.sha != base:
        raise RuntimeError(f"HF memory moved since this run restored it "
                           f"(restored {base[:12]}, now {info.sha[:12]}); not overwriting")
    commit = api.create_commit(
        repo_id=repo, repo_type="dataset",
        operations=[CommitOperationAdd(path_in_repo=FILE, path_or_fileobj=src)],
        commit_message="bot memory", parent_commit=info.sha)
    return getattr(commit, "oid", None) or getattr(commit, "commit_url", "")


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "enabled":
        return 0 if enabled() else 1
    if not enabled():
        print("HF memory not configured (HF_TOKEN / HF_MEMORY_REPO)", file=sys.stderr)
        return 2
    if cmd == "load":
        sha = load(argv[1])
        if sha is None:
            return 3
        print(sha)
        return 0
    if cmd == "save":
        base = argv[2] if len(argv) > 2 and argv[2] not in ("", "none") else None
        print(save(argv[1], base))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
