"""Provider registry — the one place that knows which vendors exist.

## What this adds to runners/provider.py, and what it deliberately does not

`runners/provider.py` already defines the interface and two adapters:

    Provider (Protocol)   create / status / mount / push / submit / poll / fetch /
                          shutdown / destroy
    BaseProvider          state persistence shared by all of them
    LocalProvider         docker on this machine
    RunPodProvider        a pod, same image, data through S3

That is the abstraction, and it is already the right one. This module does not re-model it.
It adds the single missing piece for a *service*: a registry, so that choosing a provider is
data rather than an import.

The distinction matters. As long as the control plane writes

    from runners.provider import RunPodProvider
    p = RunPodProvider(...)

then RunPod is compiled into the service and "add another vendor" means editing every call
site. With a registry it is

    p = get_provider(spec.get("provider", DEFAULT))

and adding Vast, Lambda Labs, Modal or a bare box is: write the class, add one line here.
Nothing above changes — not the API, not the client, not the job spec format. That is the
whole of what portability has to mean, and it is cheap only if it is done before there is a
second vendor, not after.

## Why lazy imports

A provider must be usable with no credentials for the others present, and the control plane
must start even when boto3 or the RunPod key is missing. Importing on resolution rather than
at module load means a broken or unconfigured adapter takes out only itself, and reports why
rather than crashing the service at boot.
"""
from __future__ import annotations

import os
from typing import Any, Callable

# name -> "module:ClassName". Strings, not classes, so nothing is imported until asked for.
_REGISTRY: dict[str, str] = {
    "local":  "runners.provider:LocalProvider",
    "runpod": "runners.provider:RunPodProvider",
    # Add a vendor here and nothing above this line changes:
    # "vast":   "runners.provider_vast:VastProvider",
    # "lambda": "runners.provider_lambda:LambdaProvider",
    # "modal":  "runners.provider_modal:ModalProvider",
}

DEFAULT_PROVIDER = os.environ.get("LINGUA_PROVIDER", "local")


class ProviderUnavailable(RuntimeError):
    """Raised with a reason a caller can act on, never a bare ImportError."""


def register(name: str, path: str) -> None:
    """Register at runtime — used by tests and by out-of-tree adapters."""
    _REGISTRY[name] = path


def available() -> list[str]:
    return sorted(_REGISTRY)


def describe() -> list[dict]:
    """Which providers exist and which can actually be constructed right now.

    Reported by the control plane's discovery endpoint so a caller can see that, say,
    RunPod is known but unusable because no API key is set — rather than finding out
    through a failed submit.
    """
    out = []
    for name in available():
        try:
            get_provider_class(name)
            out.append({"name": name, "importable": True, "reason": None})
        except ProviderUnavailable as exc:
            out.append({"name": name, "importable": False, "reason": str(exc)})
    return out


def get_provider_class(name: str) -> Callable[..., Any]:
    if name not in _REGISTRY:
        raise ProviderUnavailable(
            f"unknown provider {name!r}. Registered: {available()}")
    module_path, _, cls_name = _REGISTRY[name].partition(":")
    try:
        import importlib
        mod = importlib.import_module(module_path)
    except Exception as exc:
        raise ProviderUnavailable(
            f"provider {name!r} maps to {module_path!r}, which failed to import: "
            f"{type(exc).__name__}: {exc}") from exc
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise ProviderUnavailable(
            f"provider {name!r}: {module_path!r} has no {cls_name!r}")
    return cls


def get_provider(name: str | None = None, **kw) -> Any:
    """Construct a provider by name. The only place a vendor is chosen."""
    return get_provider_class(name or DEFAULT_PROVIDER)(**kw)


def status_source_for(runner: Any, *, token: str = "", log_root: str = "") -> Any:
    """Compose the read side to match the write side.

    A provider knows where the work runs; this picks how to look at it, and chains a
    fallback so the answer survives the compute. The ordering is the whole point:

        live endpoint first   sub-second while it exists
        S3 always last        the only source that still answers afterwards

    A provider that exposes no endpoint (a serverless worker without a public port) simply
    contributes no HTTP source and the chain degrades to S3 with no branch here.
    """
    from .status_source import (ChainedStatusSource, HttpStatusSource,
                                LocalStatusSource, S3StatusSource)
    sources = []
    endpoint = ""
    try:
        st = runner.status()
        endpoint = (st.detail or {}).get("endpoint", "") if hasattr(st, "detail") else ""
    except Exception:
        pass
    if endpoint and token:
        sources.append(HttpStatusSource(endpoint, token))
    if log_root:
        sources.append(LocalStatusSource(log_root))
    sources.append(S3StatusSource())
    return ChainedStatusSource(*sources)
