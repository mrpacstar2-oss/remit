"""Live adapters for the coding workflow (trusted host code).

Live: the model (Claude CLI) and a real pytest run. The repository is a COPY of template/ under data/work/
(created on first use; delete data/work to reset). WARNING: tests.run executes repository code, including
model-written edits, on the host without an OS sandbox. Only use it on a disposable toy repository.
"""
import os
import shutil

from remit.rterrors import CapabilityFailed
from remit.stdadapters import RootedFiles, run_pytest


def make_adapters(app_dir):
    work = os.path.join(app_dir, "data", "work")
    if not os.path.exists(work):
        shutil.copytree(os.path.join(app_dir, "template"), work)
    fs = RootedFiles(work)

    def files(args, ctx):
        out = []
        for root, dirs, names in os.walk(work):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for n in names:
                out.append(os.path.relpath(os.path.join(root, n), work))
        return sorted(out)[:40]

    def edit(args, ctx):
        text = fs.read(args["path"])
        n = text.count(args["find"])
        if n != 1:
            raise CapabilityFailed(f"`find` must occur exactly once in {args['path']} (found {n})")
        fs.write(args["path"], text.replace(args["find"], args["replace"], 1))
        return True

    return {"repo.files": files, "repo.read": lambda a, c: fs.read(a["path"]),
            "tests.run": lambda a, c: run_pytest(work, timeout=c.timeout or 120), "repo.edit": edit}
