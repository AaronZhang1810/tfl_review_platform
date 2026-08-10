"""Shared Anthropic-compatible client for text and structured model calls."""

from __future__ import annotations

import json
import logging
import os
import re
import threading

logger = logging.getLogger("tlf.ai")


def _log_usage(msg, model: str) -> None:
    """Log Claude token usage per call for cost observability."""
    u = getattr(msg, "usage", None)
    if u is not None:
        logger.info("claude model=%s in_tokens=%s out_tokens=%s",
                    model, getattr(u, "input_tokens", "?"), getattr(u, "output_tokens", "?"))

# Route TLS through the OS trust store so calls can work behind TLS-intercepting
# network proxies without bundling a machine-specific CA certificate.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception as e:
    logger.warning(
        "truststore.inject_into_ssl() failed (%s); TLS falls back to the default trust "
        "store — HTTPS/API calls may fail with self-signed-cert errors behind an "
        "intercepting proxy",
        e,
    )

MODEL = os.environ.get("TLF_MODEL", "claude-sonnet-5")
FAST_MODEL = os.environ.get("TLF_FAST_MODEL", "claude-haiku-4-5-20251001")

# --------------------------------------------------------------------------- #
# Global in-flight cap
#
# A review run fans out over tables AND over the page-slices inside each table, so
# the number of threads wanting an API call at once is the PRODUCT of those two.
# Bounding it here — at the one place every call funnels through — means callers can
# parallelise freely without any of them having to know the total budget, and a
# single knob protects the gateway's rate limit.
#
# TLF_MAX_INFLIGHT tunes it. Raise it if the gateway tolerates more; lower it to 1
# to get the old strictly-serial behaviour back.
# --------------------------------------------------------------------------- #
MAX_INFLIGHT = max(1, int(os.environ.get("TLF_MAX_INFLIGHT", "6") or 6))
_inflight = threading.BoundedSemaphore(MAX_INFLIGHT)


# Substrings identifying the SDK's "too long for a single non-streaming call" refusal:
# raised when max_tokens exceeds the model's non-streaming cap (newer models cap it at
# 8192) or the ~10-minute limit. Matched on the message so it catches the client-side
# ValueError and any server-side variant alike.
_LONG_REQUEST_HINTS = ("streaming is required", "longer than 10 minutes", "long-requests")


def _create(**kw):
    """Every messages.create() in this module funnels through here, so MAX_INFLIGHT bounds
    all calls globally.

    Our thinking-enabled judge/extraction calls request ~10-12k max_tokens, which now
    exceeds several models' NON-streaming cap — the SDK then refuses the call with
    "Streaming is required for operations that may take longer than 10 minutes." Streaming
    has no such cap, so we transparently retry that one call over a stream and return the
    accumulated final message — identical content (text / tool_use / usage), just delivered
    incrementally. Normal calls stay non-streaming, so this adds no dependency on the
    gateway supporting SSE unless a call would otherwise have failed anyway.
    """
    with _inflight:
        client = _client()
        try:
            return client.messages.create(**kw)
        except Exception as e:
            if not any(h in str(e).lower() for h in _LONG_REQUEST_HINTS):
                raise
            logger.info("non-streaming call rejected as too long (%s); retrying via streaming",
                        type(e).__name__)
            with client.messages.stream(**kw) as stream:
                return stream.get_final_message()

# --------------------------------------------------------------------------- #
# Model discovery
#
# The selectable models are discovered LIVE from the user's own API key via
# `client.models.list()` — so the dropdown reflects exactly what the configured API
# account grants. We keep only Claude models, give the well-known ones friendly labels
# and a sensible order, and fall back to a small curated list if the key/SDK is
# unavailable (offline, no ANTHROPIC_API_KEY, or a network error).
# --------------------------------------------------------------------------- #

# Used only when live discovery fails; deliberately conservative (no Fable 5,
# which not all API accounts grant).
_FALLBACK_MODELS = [
    {"id": "claude-opus-4-8", "label": "Opus 4.8"},
    {"id": "claude-sonnet-5", "label": "Sonnet 5"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
]

# An Anthropic-compatible gateway may list the same model under many alias spellings —
# family-first ("claude-opus-4-8") vs version-first ("claude-4.8-opus"), plus
# dated releases ("...-20251101"), Bedrock "-v1-0" suffixes and "-1m" context
# variants. We parse each id down to (family, version) so those all collapse to a
# single clean dropdown entry.
_FAMILY_RE = re.compile(r"opus|sonnet|haiku|fable")
_DATE8_RE = re.compile(r"\d{8}")
_FAM_ORDER = {"opus": 0, "sonnet": 1, "haiku": 2, "fable": 3}
_MODELS_CACHE: list[dict] | None = None     # module-level cache (per server run)


def _parse_model(mid: str):
    """(family, version) from any spelling, or None if not a Claude chat model.
    e.g. 'claude-opus-4-8-1m' -> ('opus','4.8'); 'claude-4.5-haiku' -> ('haiku','4.5')."""
    s = mid.lower()
    fam_m = _FAMILY_RE.search(s)
    if not fam_m:
        return None
    fam = fam_m.group(0)
    # Drop the 'claude' prefix, the family word and any date stamp, then read the
    # leading run of numeric tokens as the version (stop at noise like v1 / 1m / 0).
    rest = _DATE8_RE.sub(" ", s.replace("claude", " ").replace(fam, " "))
    ver = []
    for tok in rest.replace("-", " ").replace(".", " ").split():
        if tok.isdigit():
            ver.append(tok)
        else:
            break
    if not ver:
        return None
    return fam, ".".join(ver)


def _clean_score(mid: str):
    """Rank rival spellings of one model; the min is used as the id we send.
    Prefer the canonical family-first form, no Bedrock/context/date suffixes."""
    s = mid.lower()
    return (
        0 if re.match(r"claude-(opus|sonnet|haiku|fable)-", s) else 1,
        1 if re.search(r"v\d", s) else 0,
        1 if re.search(r"\d+m\b", s) else 0,
        1 if _DATE8_RE.search(s) else 0,
        len(s), s,
    )


def _discover_models() -> list[dict]:
    """Live-list the Claude models the current API key can use, de-aliased."""
    try:
        client = _client()
        groups: dict[tuple, list[str]] = {}
        for m in client.models.list(limit=100):
            mid = getattr(m, "id", None)
            if not mid or not mid.startswith("claude-"):
                continue  # skip non-Claude models a shared gateway may also expose
            parsed = _parse_model(mid)
            if not parsed:
                continue
            groups.setdefault(parsed, []).append(mid)
        # Newest first, by family then descending version. Pad to a fixed width so
        # a bare "4" (=4.0) sorts *after* "4.6"/"4.5", not before them.
        def order(key):
            fam, ver = key
            nums = [int(x) for x in ver.split(".") if x.isdigit()][:3]
            nums += [0] * (3 - len(nums))
            return (_FAM_ORDER.get(fam, 9), tuple(-n for n in nums))
        out = []
        for key in sorted(groups, key=order):
            fam, ver = key
            best = min(groups[key], key=_clean_score)
            out.append({"id": best, "label": f"{fam.capitalize()} {ver}"})
        if out:
            return out
        logger.warning("models.list() returned no Claude models; using fallback list")
    except Exception as e:
        logger.warning("models.list() failed (%s); using fallback list", e)
    return [dict(m) for m in _FALLBACK_MODELS]


def available_models(refresh: bool = False) -> list[dict]:
    """Cached list of {id,label} the user's key can actually use."""
    global _MODELS_CACHE
    if _MODELS_CACHE is None or refresh:
        _MODELS_CACHE = _discover_models()
    return _MODELS_CACHE


def _available_ids() -> set[str]:
    return {m["id"] for m in available_models()}

# Reasoning effort → adaptive-thinking request kwargs. "low" disables thinking
# entirely (latency-sensitive path); everything else runs adaptive thinking at
# that effort level. Fixed-budget thinking (`budget_tokens`) is removed on
# current models (Sonnet 5, Opus 5, Fable 5, Opus 4.6/4.7/4.8) — adaptive
# thinking + `output_config.effort` is the replacement.
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = os.environ.get("TLF_EFFORT", "high")
# Ceiling on max_tokens. Held below 21_333 on purpose: the SDK refuses any NON-streaming
# request whose max_tokens implies >10 min of generation (_calculate_nonstreaming_timeout:
# 3600 * max_tokens / 128_000 > 600), and every call here is non-streaming. A higher
# ceiling would turn a raised per-call budget into a hard ValueError instead of a slower
# call. Raise this only together with switching to streaming.
_MT_CEILING = 20000
_MT_THINKING_FLOOR = 4096  # headroom ADDED for reasoning when adaptive thinking is on

# Per-run configuration set by the runner from the reviewer's toolbar choice.
_RUN_MODEL: str | None = None
_RUN_EFFORT: str = DEFAULT_EFFORT if DEFAULT_EFFORT in EFFORTS else "high"


def configure(model: str | None = None, effort: str | None = None) -> None:
    """Set the model / effort for subsequent calls (from the AI Review toolbar).
    Unknown values are ignored so a bad request can't wedge the client."""
    global _RUN_MODEL, _RUN_EFFORT
    if model and model in _available_ids():
        _RUN_MODEL = model
    if effort and effort in EFFORTS:
        _RUN_EFFORT = effort


def run_config() -> dict:
    return {"model": _RUN_MODEL or MODEL, "effort": _RUN_EFFORT}


def default_model() -> str:
    ids = _available_ids()
    if MODEL in ids:
        return MODEL
    models = available_models()
    return models[0]["id"] if models else MODEL


def _resolve_model(model: str | None) -> str:
    return model or _RUN_MODEL or MODEL


_NO_EFFORT_RE = re.compile(r"haiku|sonnet-4-5|opus-4-5|opus-4-1|opus-4-0|sonnet-4-0")


def _supports_effort(model: str) -> bool:
    """Whether `model` accepts thinking:{type:"adaptive"} / output_config.effort.

    Haiku 4.5 and Sonnet/Opus 4.5-and-older reject both (400) — they only take
    the legacy thinking:{type:"enabled", budget_tokens:N} form, or no thinking
    at all. FAST_MODEL (extraction, ~85% of calls) is Haiku 4.5, so this check
    is load-bearing: without it every non-"low" effort call to the fast model
    would 400."""
    return not _NO_EFFORT_RE.search((model or "").lower())


def _thinking(effort: str | None, model: str):
    """Return (request_kwargs, thinking_on) for the effort, gated on what `model`
    actually supports.

    On adaptive-capable models: thinking is disabled at "low" (latency-sensitive
    path) and adaptive otherwise; output_config.effort is always set. Disabling
    thinking is only valid at effort "high" or below, which "low" satisfies.

    On older models (Haiku 4.5, Sonnet/Opus 4.5 and earlier): effort/adaptive
    thinking aren't supported at all — return no thinking kwargs."""
    if not _supports_effort(model):
        return {}, False
    eff = effort or _RUN_EFFORT
    if eff not in EFFORTS:
        eff = "high"
    if eff == "low":
        return {"thinking": {"type": "disabled"}, "output_config": {"effort": eff}}, False
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": eff}}, True


def available() -> bool:
    # The portfolio demo is intentionally offline. Even when the launching shell has
    # credentials, demo mode must not transmit synthetic (or accidentally substituted)
    # documents to an external service.
    if os.environ.get("TLF_DEMO_MODE") == "1":
        return False
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _client():
    if os.environ.get("TLF_DEMO_MODE") == "1":
        raise RuntimeError("External AI is disabled in TLF_DEMO_MODE")
    import anthropic
    # Anthropic-compatible gateways and the public API authenticate with x-api-key.
    # Some developer shells also set a placeholder bearer token; explicitly providing
    # the API-key header prevents that unrelated value from taking precedence.
    # ANTHROPIC_BASE_URL, when present, is picked up by the SDK.
    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    return anthropic.Anthropic(api_key=key or None,
                               default_headers={"x-api-key": key} if key else None)


def is_connection_error(exc: BaseException) -> bool:
    """True when exc is (or is caused by) an Anthropic APIConnectionError — the
    'couldn't reach the API' network failure, as distinct from a 4xx/5xx response
    or a parse/logic error. anthropic.APITimeoutError subclasses it, so it's covered."""
    try:
        import anthropic
        if isinstance(exc, anthropic.APIConnectionError):
            return True
    except Exception:
        pass
    # Robust fallback if the SDK isn't importable here or the error was re-wrapped.
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}


def preflight() -> tuple[bool, str]:
    """One cheap round-trip to confirm the API is reachable before a full run.

    Returns (ok, detail). ok=False ONLY on a connection error (the outage we must
    abort on); auth / rate-limit / other errors return ok=True so the real calls
    surface them precisely — preflight guards connectivity, nothing else.

    Uses models.list(limit=1): zero output tokens, the same endpoint _discover_models
    already trusts. max_retries=0 + a short timeout make it fail FAST instead of
    inheriting the SDK's retry backoff (part of what made the incident a slow storm).
    detail only ever stringifies the SDK exception (message is 'Connection error.');
    the API key lives in request headers, never in str(e), so it is never leaked.
    """
    try:
        _client().with_options(timeout=15.0, max_retries=0).models.list(limit=1)
        return True, ""
    except Exception as e:
        if is_connection_error(e):
            return False, f"Connection error: {e}"
        logger.warning("preflight non-connection error (%s); proceeding", e)
        return True, ""


def call_text(system, user: str, model: str | None = None, max_tokens: int = 1500) -> str:
    """Plain text completion. `system` may be a string or a list of cache-able blocks.
    Applies the run's adaptive-thinking effort when enabled."""
    m = _resolve_model(model)
    think, thinking_on = _thinking(None, m)
    if thinking_on:
        max_tokens = min(_MT_CEILING, max_tokens + _MT_THINKING_FLOOR)
    msg = _create(
        model=m, max_tokens=max_tokens,
        system=system if system else None,
        messages=[{"role": "user", "content": user}], **think,
    )
    _log_usage(msg, m)
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _tool_result(msg, tool_name: str):
    for b in msg.content:
        if getattr(b, "type", "") == "tool_use" and b.name == tool_name:
            return b.input
    return None


def call_structured(system, user: str, tool_name: str, schema: dict,
                    model: str | None = None, max_tokens: int = 4000,
                    effort: str | None = None) -> dict:
    """Force the model to emit an object matching `schema` via tool-use.

    Adaptive thinking is incompatible with a forced tool_choice, so when the effort
    enables thinking we first try tool_choice=auto WITH thinking (better reasoning on
    hard extractions); if the model returns no tool call we fall back to the reliable
    forced path with thinking disabled.

    `effort` overrides the run's effort for THIS call. Extraction passes "low" so no
    thinking budget is carved out of max_tokens — the whole budget goes to transcribing
    the page, which avoids truncating a dense table (a truncated tool call raises below)."""
    m = _resolve_model(model)
    tool = {"name": tool_name, "description": f"Return the {tool_name} result.",
            "input_schema": schema}
    think, thinking_on = _thinking(effort, m)
    if thinking_on:
        mt = min(_MT_CEILING, max_tokens + _MT_THINKING_FLOOR)
        try:
            msg = _create(
                model=m, max_tokens=mt, system=system if system else None,
                tools=[tool], tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": user}], **think,
            )
            _log_usage(msg, m)
            out = _tool_result(msg, tool_name)
            if out is not None:
                return out
        except Exception as e:
            # A connection failure won't be cured by an immediate second full forced
            # attempt — that just doubles the retry storm against a dead endpoint.
            # Re-raise so the caller records ONE connection failure and the run fails
            # loudly instead of silently falling through to the forced path.
            if is_connection_error(e):  # also catches APITimeoutError (subclass)
                logger.error(
                    "thinking structured call failed with connection error (%s); "
                    "NOT retrying forced", e,
                )
                raise
            logger.warning("thinking structured call failed (%s); retrying forced", e)
    # Reliable path: force the tool, no thinking. Disabling thinking is only valid
    # at effort "high" or below (on models that support effort at all), so cap it
    # here regardless of the run's configured level.
    forced_kwargs = {"thinking": {"type": "disabled"}, "output_config": {"effort": "high"}} \
        if _supports_effort(m) else {}
    msg = _create(
        model=m, max_tokens=max_tokens,
        system=system if system else None,
        tools=[tool], tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": user}], **forced_kwargs,
    )
    _log_usage(msg, m)
    out = _tool_result(msg, tool_name)
    if out is not None:
        return out
    if getattr(msg, "stop_reason", "") == "max_tokens":
        raise RuntimeError(f"output truncated at max_tokens={max_tokens}; raise the budget")
    # Fallback: try to parse text as JSON.
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return json.loads(text)


def cached_system_block(text: str) -> dict:
    """A system block marked for prompt caching (SAP/Protocol context)."""
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
