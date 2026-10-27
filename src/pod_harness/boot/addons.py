"""addons: git repos pinned to commits (ComfyUI node packs and the like), installed into the
workload's python, linked to models, probed.

GET /v1/pod/addons answers the resolved set: {"set", "version", "items": [item, …]} with the
item schema of APP-API §2. The server validates it; this validates it AGAIN, because the
pod is the side that runs it:

    name     ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$, not "." or ".."
    repo     https://github.com/<owner>/<repo>(.git)? only
    commit   a full 40-hex SHA (a branch or a short pin could move under us)
    pip      requirement strings; anything starting with "-" is refused (an option such as
             --index-url would redirect every install)
    links    file: under $PODH_MODELS_ROOT; to: inside the addon's dir, or comfy:<path>
             inside $PODH_COMFY_ROOT. Anything resolving outside its root is refused.

Each item is cloned at its commit (a shallow fetch of exactly that SHA, falling back to a
full clone), its requirements.txt (unless `requirements: false`) and `pip` extras installed
with the workload's python ($PODH_COMFY_PY or --python), its links made, its `probe` run with
that python. Every git and pip command has a timeout. A required item that fails fails the
boot; an `optional` one is reported and skipped.

Addons live in $PODH_ADDONS_ROOT, else $PODH_COMFY_ROOT/custom_nodes, else <boot dir>/addons.
A set with a version is kept as kit `addons-<set>` keyed `v<version>` plus the python, torch,
CUDA and image facts: the first pod clones and publishes the trees, later ones unpack them
(pip still runs — the venv is not in the kit — but against an image that already has most of
it). An item already installed at its commit with the same definition is skipped (resume).

An image built for podh-boot may bake addons in ($PODH_BAKED_ROOT/addons.json): an item whose
checkout is already at its commit in the baked directory skips the clone AND its
requirements.txt (the build installed it, under the image's constraints); `pip` extras,
links and the probe still run. A set made only of baked items publishes no kit.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from .. import fetch, heartbeat, kits
from .core import NAME_RE, BootFailure, Context, baked_addons, digest, inside, safe_rel

REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+?(?:\.git)?$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
GIT_TIMEOUT = 600
PIP_TIMEOUT = 1200
PROBE_TIMEOUT = 300


def validate(item) -> list:
    """Every reason this item may not be installed; [] when it may."""
    if not isinstance(item, dict):
        return [f"not an object: {item!r}"]
    errs = []
    name = item.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name) or name in (".", ".."):
        errs.append(f"name {name!r} is not allowed")
    repo = item.get("repo")
    if not isinstance(repo, str) or not REPO_RE.match(repo) or "/../" in repo or repo.endswith("/.."):
        errs.append(f"repo {repo!r}: only https://github.com/<owner>/<repo> is allowed")
    commit = item.get("commit")
    if not isinstance(commit, str) or not COMMIT_RE.match(commit):
        errs.append(f"commit {commit!r}: a full 40-hex SHA is required")
    pip = item.get("pip") or []
    if not isinstance(pip, list) or not all(isinstance(p, str) and p.strip() for p in pip):
        errs.append("pip must be a list of requirement strings")
    else:
        for p in pip:
            if p.strip().startswith("-"):
                errs.append(f"pip {p!r}: options are refused, requirements only")
    links = item.get("links") or []
    if not isinstance(links, list):
        errs.append("links must be a list")
    else:
        for ln in links:
            if not isinstance(ln, dict) or not isinstance(ln.get("file"), str) \
                    or not isinstance(ln.get("to"), str):
                errs.append(f"link {ln!r}: needs file and to")
                continue
            try:
                safe_rel(ln["file"], "link file")
                to = ln["to"]
                safe_rel(to[len("comfy:"):] if to.startswith("comfy:") else to, "link to")
            except ValueError as e:
                errs.append(str(e))
    if item.get("probe") is not None and not isinstance(item.get("probe"), str):
        errs.append("probe must be a string")
    return errs


def addons_root(ctx: Context) -> Path:
    env = os.environ.get("PODH_ADDONS_ROOT")
    if env:
        return Path(env)
    if ctx.comfy_root:
        return ctx.comfy_root / "custom_nodes"
    return ctx.boot_dir / "addons"


def link_paths(ctx: Context, addon_dir: Path, ln: dict) -> tuple:
    """(source, destination) for a link, both checked to stay inside their roots."""
    src = inside(ctx.models_root, ln["file"], "link file")
    to = ln["to"]
    if to.startswith("comfy:"):
        if not ctx.comfy_root:
            raise ValueError(f"link to {to!r}: no ComfyUI root on this pod")
        dst = inside(ctx.comfy_root, to[len("comfy:"):], "link to")
    else:
        dst = inside(addon_dir, to, "link to")
    return src, dst


def make_link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        dst.unlink()
    elif dst.is_dir() and not any(dst.iterdir()):
        dst.rmdir()
    elif dst.exists():
        raise ValueError(f"{dst} exists and is not a link; not replacing it")
    os.symlink(str(src), str(dst))


def _git_env(ctx: Context) -> dict:
    env = ctx.child_env()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")
    return env


def _head(ctx: Context, d: Path) -> Optional[str]:
    if not (d / ".git").exists():
        return None
    rc, out = ctx.run(["git", "-C", str(d), "rev-parse", "HEAD"], timeout=30, env=_git_env(ctx))
    return (out or "").strip().splitlines()[-1] if rc == 0 and out else None


def clone(ctx: Context, item: dict, dest: Path) -> None:
    repo, commit = item["repo"], item["commit"]
    env = _git_env(ctx)
    log = ctx.boot_dir / "logs" / f"addon-{item['name']}.log"
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{item['name']}.podh-", dir=str(dest.parent)))
    try:
        tree = work / "tree"
        steps = [["git", "init", "-q", str(tree)],
                 ["git", "-C", str(tree), "remote", "add", "origin", repo],
                 ["git", "-C", str(tree), "fetch", "-q", "--depth", "1", "origin", commit],
                 ["git", "-C", str(tree), "checkout", "-q", "--detach", "FETCH_HEAD"]]
        ok = True
        for cmd in steps:
            rc, out = ctx.run(cmd, timeout=GIT_TIMEOUT, log=log, env=env)
            if rc != 0:
                ok = False
                break
        if not ok:                              # a server that will not serve a bare SHA
            shutil.rmtree(tree, ignore_errors=True)
            for cmd in (["git", "clone", "-q", "--no-checkout", repo, str(tree)],
                        ["git", "-C", str(tree), "checkout", "-q", "--detach", commit]):
                rc, out = ctx.run(cmd, timeout=GIT_TIMEOUT, log=log, env=env)
                if rc != 0:
                    raise BootFailure(f"git {'timed out' if rc is None else f'exit {rc}'} "
                                      f"fetching {commit[:12]}", log_tail=out)
        if (tree / ".gitmodules").exists():
            rc, out = ctx.run(["git", "-C", str(tree), "submodule", "update", "--init",
                               "--recursive", "--depth", "1"], timeout=GIT_TIMEOUT, log=log, env=env)
            if rc != 0:
                raise BootFailure("git submodules failed", log_tail=out)
        if _head(ctx, tree) != commit:
            raise BootFailure(f"checked out {(_head(ctx, tree) or '?')[:12]}, wanted {commit[:12]}")
        fetch.replace_dir(tree, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def pip_install(ctx: Context, item: dict, d: Path, requirements: bool = True) -> None:
    py = ctx.python
    log = ctx.boot_dir / "logs" / f"addon-{item['name']}.log"
    base = [py, "-m", "pip", "install", "--no-input", "--disable-pip-version-check"]
    timeout = float(os.environ.get("PODH_PIP_TIMEOUT") or PIP_TIMEOUT)
    if requirements and item.get("requirements", True) and (d / "requirements.txt").is_file():
        rc, out = ctx.run(base + ["-r", "requirements.txt"], timeout=timeout, log=log, cwd=str(d))
        if rc != 0:
            raise BootFailure(f"pip install -r requirements.txt "
                              f"{'timed out' if rc is None else f'exit {rc}'}", log_tail=out)
    extras = [p.strip() for p in (item.get("pip") or [])]
    if extras:
        rc, out = ctx.run(base + ["--"] + extras, timeout=timeout, log=log, cwd=str(d))
        if rc != 0:
            raise BootFailure(f"pip install {' '.join(extras)[:120]} "
                              f"{'timed out' if rc is None else f'exit {rc}'}", log_tail=out)


def probe(ctx: Context, item: dict, d: Path) -> None:
    code = item.get("probe")
    if not code:
        return
    env = ctx.child_env({"PYTHONPATH": f"{d.parent}:{d}:${{PYTHONPATH}}"})
    rc, out = ctx.run([ctx.python, "-c", code], timeout=PROBE_TIMEOUT, cwd=str(d), env=env)
    if rc != 0:
        raise BootFailure(f"probe {code[:80]!r} {'timed out' if rc is None else f'exit {rc}'}",
                          log_tail=out)


def baked(item: dict, d: Path) -> bool:
    """The image checked this addon out HERE at this commit and installed its requirements.

    Its requirements.txt is then not run again: it was, by the build, under the image's
    constraints — and some packs list things (an unpinned git URL, say) that a pod should
    not be fetching at boot. The checkout itself is still verified by its git HEAD."""
    b = baked_addons().get(item["name"])
    return bool(b) and b.get("commit") == item["commit"] and isinstance(b.get("dir"), str) \
        and os.path.realpath(b["dir"]) == os.path.realpath(d)


def install(ctx: Context, item: dict, root: Path, from_kit: bool) -> str:
    """'skipped' | 'baked' | 'kit' | 'built'. Raises BootFailure."""
    d = root / item["name"]
    key = f"addon:{item['name']}"
    want = digest({**{k: item.get(k) for k in ("repo", "commit", "pip", "requirements",
                                               "links", "probe")}, "python": ctx.python})
    head = _head(ctx, d)
    if ctx.marker(key) == want and head == item["commit"]:
        return "skipped"
    requirements = True
    how = "kit"
    if head != item["commit"]:
        clone(ctx, item, d)
        how = "built"
    elif baked(item, d):
        how, requirements = "baked", False
    elif not from_kit:
        how = "built"
    pip_install(ctx, item, d, requirements=requirements)
    for ln in item.get("links") or []:
        try:
            src, dst = link_paths(ctx, d, ln)
            make_link(src, dst)
        except (ValueError, OSError) as e:
            raise BootFailure(f"link {ln.get('file')} -> {ln.get('to')}: {e}")
    probe(ctx, item, d)
    ctx.set_marker(key, want)
    return how


def resolve(ctx: Context) -> dict:
    st, obj = heartbeat.request("GET", "/v1/pod/addons", timeout=30)
    if st != 200 or obj is None:
        raise BootFailure(f"addon set: control plane answered {st or 'nothing'} "
                          f"({heartbeat.last_error() or obj})")
    if isinstance(obj, list):
        obj = {"items": obj}
    if not isinstance(obj.get("items"), list):
        raise BootFailure("addon set: `items` is not a list")
    return obj


def _kit_name(set_name) -> Optional[str]:
    if not set_name:
        return None
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(set_name)).strip("-.")
    return f"addons-{slug}"[:64].rstrip("-.") if slug else None


def run(ctx: Context) -> None:
    if not ctx.boot.get("addons"):
        ctx.line("no addons requested")
        return
    s = resolve(ctx)
    items = s["items"]
    root = addons_root(ctx)
    root.mkdir(parents=True, exist_ok=True)
    kit = _kit_name(s.get("set"))
    version = s.get("version")
    kit_key = f"v{version}" if version is not None else None
    ctx.line(f"addon set {s.get('set') or 'inline'}"
             f"{f' v{version}' if version is not None else ''}: {len(items)} item(s) → {root}")

    good, names = [], set()
    for it in items:
        errs = validate(it)
        nm = it.get("name") if isinstance(it, dict) else None
        if not errs and nm in names:
            errs = [f"duplicate name {nm!r}"]
        if errs:
            msg = f"addon {nm or '?'}: refused — {'; '.join(errs)}"
            if isinstance(it, dict) and it.get("optional"):
                ctx.line(f"{msg} (optional, skipped)")
                continue
            raise BootFailure(msg)
        names.add(nm)
        good.append(it)

    # A kit only helps when something is not already in place.
    from_kit = False
    pending = [it for it in good if _head(ctx, root / it["name"]) != it["commit"]]
    kit_facts = None
    if kit and kit_key and pending and heartbeat._cfg()[0]:
        kit_facts = kits.facts(ctx.python)
        staged = root / f".{kit}.podh-kit"
        try:
            if kits.get(kit, kit_key, staged, kit_facts=kit_facts, deadline=ctx.deadline):
                for it in pending:
                    src = staged / it["name"]
                    if src.is_dir():
                        fetch.replace_dir(src, root / it["name"])
                from_kit = True
                ctx.line(f"unpacked kit {kit} {kit_key}")
        except kits.KitError as e:
            ctx.line(f"kit {kit} unusable ({e}); cloning")
        finally:
            shutil.rmtree(staged, ignore_errors=True)

    built = 0
    for it in good:
        t0 = time.time()
        try:
            how = install(ctx, it, root, from_kit)
        except BootFailure as e:
            msg = f"addon {it['name']}: {e.reason}"
            if it.get("optional"):
                ctx.line(f"{msg} (optional, continuing)")
                continue
            raise BootFailure(msg, log_tail=e.log_tail)
        built += how == "built"
        said = {"skipped": "already installed", "baked": "baked into the image"}.get(how, how)
        ctx.line(f"{it['name']} @ {it['commit'][:10]}: {said} ({time.time() - t0:.0f}s)")

    if built and kit and kit_key and not from_kit and heartbeat._cfg()[0]:
        publish(ctx, kit, kit_key, root, good, kit_facts)


def _no_abs_links(d, names) -> list:
    return [n for n in names
            if os.path.islink(os.path.join(d, n)) and (
                os.path.isabs(os.readlink(os.path.join(d, n)))
                or ".." in Path(os.readlink(os.path.join(d, n))).parts)]


def publish(ctx: Context, kit: str, key: str, root: Path, items: list, kit_facts) -> None:
    work = Path(tempfile.mkdtemp(prefix=".podh-addon-kit-", dir=str(ctx.boot_dir)))
    try:
        for it in items:
            src = root / it["name"]
            if src.is_dir():
                # Links are made per pod (they point into this pod's models root), and an
                # absolute link is exactly what an unpacking pod must refuse — so they stay out.
                shutil.copytree(src, work / it["name"], symlinks=True, ignore=_no_abs_links)
        r = kits.put(kit, key, work, kit_facts=kit_facts or kits.facts(ctx.python))
        ctx.line(f"published kit {kit} {key}" if r else f"kit {kit} {key}: another pod is publishing it")
    except (kits.KitError, OSError) as e:
        ctx.line(f"could not publish kit {kit} ({e}); not fatal")
    finally:
        shutil.rmtree(work, ignore_errors=True)
