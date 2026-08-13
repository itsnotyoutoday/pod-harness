"""Assert the harness honours contract.json. Runs in CI and in every image build.

## Why this replaces a shared library

The harness and the loader used to share Python. That did not actually guarantee
agreement — a `pip install …@main` served a stale engine from a Docker layer cache, so the
two "shared" a module while running different versions of it. A contract does better,
because both sides can be checked against it *independently*: this script proves the
harness implements the file, and the loader's suite proves every spec it emits validates.
Neither has to trust the other, and neither has to import the other.

## What is actually checked

Not that the file parses — that anything in it corresponds to real behaviour:

  * every endpoint the contract advertises is routed by serve/api.py
  * every route serve/api.py exposes is documented (an undocumented endpoint is a
    surface the loader cannot rely on, and worse, one nobody knows is public)
  * every env var the contract calls required is actually read by the harness
  * the state vocabulary matches events.STAGE_STATES / JOB_STATES exactly, because
    silently collapsing `unverified` into `ok` would make status report green for the
    precise bug class verification exists to catch
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contract.json"
API = ROOT / "serve" / "api.py"


def _routes_in_code() -> set[tuple[str, str]]:
    if not API.is_file():
        return set()
    src = API.read_text()
    out = set()
    for m in re.finditer(r'@(?:app|router)\.(get|post|delete|put|patch)\("([^"]+)"', src):
        out.add((m.group(1).upper(), m.group(2)))
    return out


def _env_read_anywhere() -> set[str]:
    """Every env var name the harness reads, Python and shell alike."""
    names: set[str] = set()
    for f in (ROOT / "src" / "pod_harness").glob("*.py"):
        names |= set(re.findall(r'environ(?:\.get)?[\(\[]"([A-Z_]+)"', f.read_text()))
    for f in (ROOT / "serve").glob("*.py"):
        names |= set(re.findall(r'environ(?:\.get)?[\(\[]"([A-Z_]+)"', f.read_text()))
    for f in (ROOT / "docker" / "harness").glob("lingua-*"):
        try:
            names |= set(re.findall(r'\$\{?([A-Z_]+)', f.read_text()))
        except Exception:
            pass
    return names


def main() -> int:
    if not CONTRACT.is_file():
        print(f"contract.json missing at {CONTRACT}")
        return 1
    c = json.loads(CONTRACT.read_text())
    failures: list[str] = []

    # -- endpoints, both directions ---------------------------------------------------
    declared = {(e["method"], e["path"]) for e in c["api"]["endpoints"]}
    actual = _routes_in_code()
    if actual:                      # skip when serve/ is absent (a non-serving image)
        missing = declared - actual
        if missing:
            failures.append(
                "contract advertises endpoints the API does not route:\n" +
                "\n".join(f"    {m} {p}" for m, p in sorted(missing)))
        undocumented = actual - declared
        if undocumented:
            failures.append(
                "the API routes endpoints the contract does not document:\n" +
                "\n".join(f"    {m} {p}" for m, p in sorted(undocumented)) +
                "\n    An undocumented endpoint is a surface the loader cannot rely on.")

    # -- env vars claimed required must really be read ---------------------------------
    read = _env_read_anywhere()
    required = set(c["env"]["required_always"]) | set(c["env"]["required_for_batch"])
    unread = {v for v in required if v not in read}
    if unread:
        failures.append(
            f"contract calls these required, but nothing reads them: {', '.join(sorted(unread))}\n"
            f"    Either the harness stopped needing it, or it silently defaults again.")

    # -- the state vocabulary must match the code exactly ------------------------------
    sys.path.insert(0, str(ROOT))
    try:
        from pod_harness import events as ev
        code_states = set(ev.JOB_STATES) | set(ev.STAGE_STATES)
        contract_states = set(
            c["events"]["event"]["properties"]["state"]["enum"])
        if code_states != contract_states:
            failures.append(
                f"state vocabulary disagrees.\n"
                f"    only in code:     {sorted(code_states - contract_states)}\n"
                f"    only in contract: {sorted(contract_states - code_states)}\n"
                f"    'unverified' collapsing into 'ok' would report green for the exact "
                f"bug class verification exists to catch.")
    except Exception as e:
        failures.append(f"could not import pod_harness.events to compare states: {e}")

    # -- response keys must exist in the code that builds them -------------------------
    # Cheap static check: the key names the contract promises should appear in serve/.
    # A loader reading `state` when the API emits `job_state` gets None and reports
    # nothing wrong, which is worse than an error.
    if API.is_file() and "responses" in c.get("api", {}):
        api_src = API.read_text()
        for ep, shape in c["api"]["responses"].items():
            if ep.startswith("_"):
                continue
            for key in shape.get("required", []):
                if f'"{key}"' not in api_src and f"'{key}'" not in api_src:
                    failures.append(
                        f"{ep}: contract promises key {key!r}, which serve/api.py never "
                        f"emits")

    if failures:
        print("CONTRACT CHECK FAILED\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print(f"contract v{c['contract_version']} honoured: "
          f"{len(declared)} endpoints, {len(required)} required env vars, "
          f"{len(code_states)} states")
    return 0


if __name__ == "__main__":
    sys.exit(main())
