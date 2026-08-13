"""Assert, at build time, that this image cannot do the loader's job.

Run inside every image build and by CI. It fails the build rather than reporting, because
the properties below are not style preferences — each one is a bug that already happened.

## 1. No loader modules

A pod must not be able to launch a pod, and must not carry pod-launching code at all. The
credential rule ("the pod holds no credentials") is only safe today because a RunPod API
key is absent from the pod; if the code is also absent, the rule holds structurally rather
than by luck.

## 2. No layout knowledge

The storage layout lived in the harness AND the loader, and the copies drifted three times
in one day: logs/<pod_id> against runs/<job_id>, .cache against cache, and a module path
that had moved — the last billed thirteen minutes and served 404. The harness is now told
its roots. Importing a layout module would quietly reintroduce the second copy.

## 3. Every module the shell harness names must import

`lingua-init` execs a Python module by name. Shell cannot be type-checked, so a Python
move leaves the script pointing at nothing and the failure appears only on a billed pod.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

#: Loader concerns. Present here would mean the harness can provision compute, compute
#: storage paths, or reap pods — none of which are its job.
FORBIDDEN = ("sync", "volume", "capabilities", "reaper", "runpod_api", "batch_pod",
             "podrun", "provider", "executor", "browse", "estimate", "paths", "store",
             "cache", "migrate_layout", "status_source")

#: Must be importable: the engine the harness actually runs.
REQUIRED = ("framework", "execute_job", "registry", "events", "status", "spec",
            "resume", "progress", "mount", "storage", "objectstore", "parallel",
            "resources")

INIT = pathlib.Path("/usr/local/bin/lingua-init")


def check_forbidden() -> list[str]:
    return [m for m in FORBIDDEN
            if importlib.util.find_spec(f"pod_harness.{m}") is not None]


def check_required() -> list[tuple[str, str]]:
    bad = []
    for m in REQUIRED:
        try:
            importlib.import_module(f"pod_harness.{m}")
        except Exception as e:
            bad.append((m, f"{type(e).__name__}: {e}"))
    return bad


def check_harness_script() -> list[tuple[str, str]]:
    """Every pod_harness.X named in the shell harness must import."""
    if not INIT.is_file():
        return []
    named = sorted(set(re.findall(r"pod_harness\.([a-z_]+)", INIT.read_text())))
    bad = []
    for m in named:
        try:
            importlib.import_module(f"pod_harness.{m}")
        except Exception as e:
            bad.append((m, f"{type(e).__name__}: {e}"))
    return bad


def main() -> int:
    failures = []

    if leaked := check_forbidden():
        failures.append(
            f"loader modules present in the harness: {', '.join(leaked)}\n"
            f"    The harness must not be able to launch pods or compute storage paths. "
            f"Move these to the loader.")

    if missing := check_required():
        failures.append("engine modules that do not import:\n" +
                        "\n".join(f"    {m}: {e}" for m, e in missing))

    if broken := check_harness_script():
        failures.append(
            f"{INIT} names modules that do not import:\n" +
            "\n".join(f"    {m}: {e}" for m, e in broken) +
            "\n    A shell script cannot be type-checked, so this mismatch would only "
            "appear on a billed pod.")

    if failures:
        print("INDEPENDENCE CHECK FAILED\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print(f"independence asserted: {len(REQUIRED)} engine modules import, "
          f"0 of {len(FORBIDDEN)} loader modules present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
