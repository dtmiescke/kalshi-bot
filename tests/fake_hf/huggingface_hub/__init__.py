"""Test double for the few huggingface_hub calls bot/hf_memory.py makes, backed
by a local folder (FAKE_HF_DIR). Put tests/fake_hf first on PYTHONPATH."""
import hashlib
import os
import shutil
import types


def _root():
    if os.environ.get("FAKE_HF_FAIL"):
        raise ConnectionError("fake Hugging Face outage")
    return os.environ["FAKE_HF_DIR"]


class CommitOperationAdd:
    def __init__(self, path_in_repo, path_or_fileobj):
        self.path_in_repo, self.src = path_in_repo, path_or_fileobj


class HfApi:
    def __init__(self, token=None):
        self.token = token

    def _dir(self, repo):
        d = os.path.join(_root(), repo.replace("/", "__"))
        os.makedirs(os.path.join(d, "files"), exist_ok=True)
        if not os.path.exists(os.path.join(d, "HEAD")):
            open(os.path.join(d, "HEAD"), "w").write("0" * 40)
        return d

    def create_repo(self, repo, repo_type=None, private=None, exist_ok=False):
        self._dir(repo)

    def repo_info(self, repo, repo_type=None, files_metadata=False):
        d = self._dir(repo)
        files = sorted(os.listdir(os.path.join(d, "files")))
        return types.SimpleNamespace(sha=open(os.path.join(d, "HEAD")).read().strip(),
                                     siblings=[types.SimpleNamespace(rfilename=f) for f in files])

    def hf_hub_download(self, repo_id, filename, repo_type=None, revision=None, local_dir="."):
        d = self._dir(repo_id)
        dest = os.path.join(local_dir, filename)
        shutil.copy(os.path.join(d, "files", filename), dest)
        return dest

    def create_commit(self, repo_id, operations, commit_message="", repo_type=None,
                      parent_commit=None):
        d = self._dir(repo_id)
        head = open(os.path.join(d, "HEAD")).read().strip()
        if parent_commit and parent_commit != head:
            raise RuntimeError("A commit has happened since parent_commit")
        h = hashlib.sha1(head.encode())
        for op in operations:
            shutil.copy(op.src, os.path.join(d, "files", op.path_in_repo))
            h.update(open(op.src, "rb").read())
        new = h.hexdigest()
        open(os.path.join(d, "HEAD"), "w").write(new)
        return types.SimpleNamespace(oid=new)
