"""Build-time guard: the Caddyfile's variables must be ones something actually sets.

## Why this exists

The framework's environment variables were renamed LINGUA_* → PODH_*. Every Python file
was updated, every shell script was updated, and the Caddyfile was missed — because
nothing imports it, nothing lints it, and no test exercised it.

Caddy does not treat an unknown placeholder as an error. `{$LINGUA_API_TOKEN}` with
nothing setting LINGUA_API_TOKEN expands to the empty string, so:

    @authorized header X-Lingua-Token ""      never matches a real request
    root * ""                                 serves from an empty root

Caddy started, logged `serving on :8000`, and reported healthy. The pod was reachable and
returned 404 for everything. That combination is the worst available outcome: the job kept
running and failing, invisibly, while the one instrument for diagnosing it had been broken
by the same change that broke the job. The container was dark and still billing.

This is the third rename in this project to leave a shell-adjacent file pointing at a name
that no longer exists — after `runners.execute_job` and the `x_lingua_token` FastAPI
parameter. Each was found by a pod failing in production rather than by a build failing in
CI. The pattern is stable enough to be worth a guard: Python drift is caught by imports,
and everything that is not Python has to be caught deliberately.

## What it checks

1. Every `{$VAR}` in the Caddyfile is exported by podh-init or baked as a Dockerfile ENV.
   This is the check that would have caught the outage.
2. No file under docker/harness/ that belongs to the FRAMEWORK reads a LINGUA_ variable.
   The workload's own LINGUA_* names are deliberate and left alone — podh-roots emits them
   on purpose, because renaming a workload's variables out from under it breaks it for no
   benefit. The rule is about which side owns the name, not about the prefix.
3. The auth header in the Caddyfile matches the one the contract declares, so the proxy
   and the API cannot disagree about what to look for.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CADDY = HERE / "harness" / "Caddyfile"
INIT = HERE / "harness" / "podh-init"

#: Files that are the harness itself. A LINGUA_ read here is drift; anywhere else it may
#: be a deliberate handoff to the workload.
FRAMEWORK = ["harness/Caddyfile"]

#: Set by the container runtime, not by us.
AMBIENT = {"RUNPOD_POD_ID", "HOSTNAME", "PATH", "HOME"}


def fail(msg: str) -> None:
    print(f"\n  ✗ {msg}\n", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    caddy = CADDY.read_text()
    init = INIT.read_text()
    dockerfiles = "\n".join(p.read_text() for p in HERE.glob("*.Dockerfile"))

    # 1. every placeholder resolves to something
    # A variable is legitimately available from three places, and the contract is one of
    # them: PODH_API_TOKEN is supplied BY THE LOADER in the pod env and is never set by any
    # file in this repo. Treating "not set in podh-init" as the definition of missing would
    # reject the correct configuration, so the check asks the contract what the loader
    # promises to provide — the same document both sides are already tested against.
    contract_env = json.loads((ROOT / "contract.json").read_text()).get("env", {})
    from_loader = set()
    for group in contract_env.values():
        if isinstance(group, dict):
            from_loader.update(group)

    placeholders = set(re.findall(r"\{\$([A-Z0-9_]+)\}", caddy))
    unset = []
    for var in sorted(placeholders - AMBIENT - from_loader):
        set_in_init = re.search(rf"(^|\s)(export\s+)?{var}=", init, re.M) or \
                      re.search(rf'"\$\{{{var}:?=', init)
        set_in_docker = re.search(rf"^\s*ENV\s+{var}=", dockerfiles, re.M)
        if not (set_in_init or set_in_docker):
            unset.append(var)
    if unset:
        fail("Caddyfile uses variable(s) nothing sets: " + ", ".join(unset) + "\n"
             "    Caddy expands an unset {$VAR} to the EMPTY STRING and starts anyway, so\n"
             "    this does not fail at boot — it produces a pod that logs 'serving' and\n"
             "    answers 404 for everything, including the console log you would use to\n"
             "    work out why. Set it in podh-init, or fix the name.")

    # 2. no framework file reads a variable the framework no longer sets
    for rel in FRAMEWORK:
        text = (HERE / rel).read_text()
        stale = sorted(set(re.findall(r"\{\$(LINGUA_[A-Z0-9_]+)\}", text)))
        stale += sorted(set(re.findall(r"X-Lingua-\w+", text)))
        if stale:
            fail(f"{rel} still reads renamed name(s): {', '.join(stale)}\n"
                 "    The harness reads PODH_. A workload's own LINGUA_* variables are\n"
                 "    deliberate and stay — but this file is the harness.")

    # 3. the proxy and the API agree on the header
    contract = json.loads((ROOT / "contract.json").read_text())
    declared = re.search(r"X-[\w-]+", contract.get("api", {}).get("auth", ""))
    used = re.search(r"header\s+(X-[\w-]+)", caddy)
    if declared and used and declared.group(0).lower() != used.group(1).lower():
        fail(f"Caddy matches on {used.group(1)} but the contract declares "
             f"{declared.group(0)}.\n"
             "    A mismatch here authenticates nothing: the proxy looks for one header\n"
             "    and every client sends the other.")

    print(f"  ✓ env: {len(placeholders)} Caddy placeholder(s) resolve; "
          f"header matches the contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
