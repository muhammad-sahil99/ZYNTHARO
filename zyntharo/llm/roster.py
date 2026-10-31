"""The model roster: every model a key can reach, the caps it carries, and the job it does.

Why this is data rather than prose in a config file:

* A model is not "good" in the abstract — it is good **at a job**, and the caps decide how often
  that job can be run. ``gemini-3.8-flash`` is the sharpest text model here and also the scarcest
  (20 requests/day), so it must not be spent on a 40-step tool loop; ``gemma-4-31b-it`` has 14,400
  requests/day and belongs on indexing. Encoding that as a chain means failover walks to the next
  model *for this job*, not to whatever sorts first.
* Providers are activation-by-key, never by switch. A provider with no key is simply not ready
  (``ProviderRegistry.ready``), so dropping ``GROQ_API_KEY`` into the environment turns Groq's
  models on for the chains that already name them, and nothing has to be reconfigured. Adding a
  second key widens the fleet; it does not require choosing a winner in advance.
* The ordering is by envelope, not by brand. :meth:`ModelRuntime.for_purpose` demotes any model
  whose windows are spent to the back of its chain, which is the "manager picks the job by the
  limits" behaviour: the chain says what is *suitable*, the quota says what is *available now*.

Provenance of the numbers, stated plainly because it matters:

* The Gemini rows come from the owner's own list (``gemini-3.8-flash`` etc.) and carry those
  numbers.
* The rows for OpenRouter, Groq, Cerebras, Mistral and Cloudflare come from the owner's attached
  reference sheet. Where that sheet gives a different free-tier number for a model already in the
  Gemini list (it quotes 2.5 Flash as 15 rpm / 1.5K rpd, the owner's list says 5 / 20), the
  owner's own number wins and the discrepancy is recorded in :data:`CAP_CONFLICTS` rather than
  silently averaged. Cloudflare publishes a daily *neuron* budget, which is not a request rate at
  all, so its rows carry no rpm/tpm/rpd and :attr:`RosterEntry.note` says so.
* Nothing here verifies a cap against the vendor. A changed limit shows up as a 429, which the
  Steward's cooldowns already handle; ``zyntharo models --check`` verifies *ids* only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_KEY_ENV = "GEMINI_API_KEY"


# --------------------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One model, with the caps the vendor published and the job it is kept for."""

    model_id: str
    label: str
    tags: tuple[str, ...]
    provider: str = "gemini"
    rpm: int | None = None  # None = the sheet published no per-minute request cap
    tpm: int | None = None
    rpd: int | None = None
    context_window: int = 1_000_000
    tools: bool = True
    vision: bool = True
    audio_live: bool = False
    #: False when the id was spelled from a display name because the list gave only the name.
    id_confirmed: bool = True
    note: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @property
    def size_b(self) -> float | None:
        """Parameter count in billions, when the vendor publishes one."""
        return PUBLISHED_SIZES.get(self.model_id)

    @property
    def rank(self) -> int:
        """Size class 0-4. Derived from the published size, else declared, else the middle."""
        derived = rank_for_size(self.size_b)
        if derived is not None:
            return derived
        return DECLARED_RANKS.get(self.model_id, 2)

    @property
    def rank_is_declared(self) -> bool:
        """True when no parameter count is published: the class is an assertion, not a datum."""
        return self.size_b is None

    @property
    def rank_label(self) -> str:
        return RANK_LABELS[self.rank]

    def spec(self):  # -> ModelSpec  (import kept local: config imports this module)
        from ..config import ModelSpec

        return ModelSpec(
            model_id=self.model_id,
            provider=self.provider,
            rpm=self.rpm,
            rpd=self.rpd,
            tpm=self.tpm,
            context_window=self.context_window,
            supports_tools=self.tools,
            supports_vision=self.vision,
            supports_audio_live=self.audio_live,
            tags=list(self.tags),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model_id,
            "label": self.label,
            "qualified": self.qualified,
            "rpm": self.rpm,
            "tpm": self.tpm,
            "rpd": self.rpd,
            "context_window": self.context_window,
            "audio_live": self.audio_live,
            "tags": list(self.tags),
            "id_confirmed": self.id_confirmed,
            "rank": self.rank,
            "rank_label": self.rank_label,
            "size_b": self.size_b,
            "rank_is_declared": self.rank_is_declared,
            "note": self.note,
        }


#: Gemini: the owner's own list. Order is by role — the live conversation models first because
#: they are what the user hears, then text, then audio-only, then vectors.
GEMINI: tuple[RosterEntry, ...] = (
    # -- live, full-duplex (what the user talks to) ---------------------------------------
    RosterEntry(
        "gemini-2.5-flash-preview-native-audio-dialog",
        "Gemini 2.5 Flash — native audio dialog",
        tags=("live", "conversation", "voice", "humanic"),
        tpm=1_000_000,
        audio_live=True,
        id_confirmed=False,
        note="speech-to-speech: audio in, audio out, no text round trip — the most human voice here",
    ),
    RosterEntry(
        "gemini-3.8-live-preview-extended-thinking",
        "Gemini 3.8 Live — extended thinking",
        tags=("live", "conversation", "voice", "reasoning", "humanic"),
        tpm=65_000,
        audio_live=True,
        id_confirmed=False,
        note="live with thinking time; best live voice that can also reason about what it heard",
    ),
    RosterEntry(
        "gemini-3.8-live-preview",
        "Gemini 3.8 Live",
        tags=("live", "conversation", "voice", "humanic"),
        tpm=65_000,
        audio_live=True,
        id_confirmed=False,
        note="current-generation live dialogue",
    ),
    RosterEntry(
        "gemini-3-flash-live-preview",
        "Gemini 3 Flash Live",
        tags=("live", "conversation", "voice"),
        tpm=65_000,
        audio_live=True,
        id_confirmed=False,
        note="previous-generation live dialogue, kept as the failover",
    ),
    RosterEntry(
        "gemini-3.5-live-translate-preview",
        "Gemini 3.5 Live Translate",
        tags=("live", "translate", "voice"),
        tpm=20_000,
        audio_live=True,
        id_confirmed=False,
        note="interpretation only — last on the voice chain because it does not converse",
    ),
    # -- text: the thinkers ----------------------------------------------------------------
    RosterEntry(
        "gemini-3.8-flash",
        "Gemini 3.8 Flash",
        tags=("reasoning", "coding", "conversation", "vision"),
        rpm=5,
        tpm=250_000,
        rpd=20,
        note="sharpest text model available, and the scarcest: 20 requests/day is ~20 hard decisions",
    ),
    RosterEntry(
        "antigravity",
        "Antigravity",
        tags=("reasoning", "coding", "conversation", "tool_loop", "vision"),
        rpm=60,
        tpm=100_000,
        rpd=100,
        note="the workhorse: 60 rpm is enough for a full tool loop without hitting the wall",
    ),
    RosterEntry(
        "gemini-3-flash-preview",
        "Gemini 3 Flash",
        tags=("reasoning", "coding", "vision"),
        rpm=5,
        tpm=250_000,
        rpd=20,
    ),
    RosterEntry(
        "gemini-3.6-flash",
        "Gemini 3.6 Flash",
        tags=("reasoning", "coding", "conversation", "vision"),
        rpm=5,
        tpm=250_000,
        rpd=20,
    ),
    RosterEntry(
        "gemini-3.1-flash-lite",
        "Gemini 3.1 Flash Lite",
        tags=("fast", "tool_loop", "background", "cheap", "vision"),
        rpm=15,
        tpm=250_000,
        rpd=500,
    ),
    RosterEntry(
        "gemini-3.5-flash-lite",
        "Gemini 3.5 Flash Lite",
        tags=("fast", "tool_loop", "background", "cheap", "vision"),
        rpm=15,
        tpm=250_000,
        rpd=500,
    ),
    RosterEntry(
        "gemini-2.5-flash",
        "Gemini 2.5 Flash",
        tags=("fast", "general", "vision"),
        rpm=5,
        tpm=250_000,
        rpd=20,
    ),
    RosterEntry(
        "gemini-2.5-flash-lite",
        "Gemini 2.5 Flash Lite",
        tags=("fast", "cheap", "background"),
        rpm=10,
        tpm=250_000,
        rpd=20,
    ),
    RosterEntry(
        "gemini-2.5-pro",
        "Gemini 2.5 Pro",
        tags=("reasoning", "vision"),
        rpm=2,
        tpm=32_000,
        rpd=50,
        note="from the reference sheet: 2 rpm, 50 a day — a specialist, never a loop model",
    ),
    RosterEntry(
        "gemini-1.5-flash",
        "Gemini 1.5 Flash",
        tags=("fast", "general", "vision"),
        rpm=15,
        tpm=1_000_000,
        rpd=1_500,
        note="from the reference sheet; older generation, kept for its generous daily cap",
    ),
    RosterEntry(
        "gemini-robotics-er2-preview",
        "Gemini Robotics ER 2 (preview)",
        tags=("spatial", "vision"),
        rpm=5,
        tpm=250_000,
        rpd=20,
        note="spatial/embodied reasoning; not a general chat model",
    ),
    # -- open weights: the volume tier -------------------------------------------------------
    RosterEntry(
        "gemma-4-31b-it",
        "Gemma 4 31B",
        tags=("background", "bulk", "coding", "reasoning"),
        rpm=30,
        tpm=16_000,
        rpd=14_400,
        context_window=131_072,
        note="14,400 requests/day: this is what makes background work affordable",
    ),
    RosterEntry(
        "gemma-4-26b-it",
        "Gemma 4 26B",
        tags=("background", "bulk", "cheap"),
        rpm=30,
        tpm=16_000,
        rpd=14_400,
        context_window=131_072,
    ),
    # -- audio out ---------------------------------------------------------------------------
    RosterEntry(
        "gemini-3.8-flash-tts",
        "Gemini 3.8 Flash TTS",
        tags=("tts", "voice", "humanic"),
        rpm=3,
        tpm=10_000,
        rpd=10,
        context_window=32_000,
        tools=False,
        vision=False,
        note="first choice for spoken replies: newest voice set, real prosody",
    ),
    RosterEntry(
        "gemini-3.8-flash-lite-tts",
        "Gemini 3.8 Flash Lite TTS",
        tags=("tts", "voice", "humanic"),
        rpm=3,
        tpm=10_000,
        rpd=10,
        context_window=32_000,
        tools=False,
        vision=False,
    ),
    RosterEntry(
        "gemini-3.1-flash-tts-preview",
        "Gemini 3.1 Flash TTS",
        tags=("tts", "voice"),
        rpm=3,
        tpm=10_000,
        rpd=10,
        context_window=32_000,
        tools=False,
        vision=False,
    ),
    RosterEntry(
        "gemini-2.5-flash-preview-tts",
        "Gemini 2.5 Flash TTS",
        tags=("tts", "voice"),
        rpm=3,
        tpm=10_000,
        rpd=10,
        context_window=32_000,
        tools=False,
        vision=False,
    ),
    # -- audio in ----------------------------------------------------------------------------
    RosterEntry(
        "gemini-3.5-transcribe-live",
        "Gemini 3.5 Transcribe Live",
        tags=("transcribe", "voice"),
        rpm=3,
        tpm=10_000,
        rpd=25,
        context_window=32_000,
        tools=False,
        vision=False,
        id_confirmed=False,
    ),
    RosterEntry(
        "gemini-3.5-transcribe",
        "Gemini 3.5 Transcribe",
        tags=("transcribe", "voice"),
        rpm=3,
        tpm=10_000,
        rpd=25,
        context_window=32_000,
        tools=False,
        vision=False,
    ),
    # -- vectors -----------------------------------------------------------------------------
    RosterEntry(
        "gemini-embedding-2",
        "Gemini Embedding 2",
        tags=("embedding",),
        rpm=100,
        tpm=30_000,
        rpd=1000,
        context_window=8_192,
        tools=False,
        vision=False,
    ),
    RosterEntry(
        "gemini-embedding-001",
        "Gemini Embedding 1",
        tags=("embedding",),
        rpm=100,
        tpm=30_000,
        rpd=1000,
        context_window=8_192,
        tools=False,
        vision=False,
    ),
)


#: OpenRouter free tier. Ids contain ``/`` and often ``:free`` — the qualified form is
#: ``openrouter:deepseek/deepseek-r1:free``, and every split in the codebase is ``split(":", 1)``
#: precisely so that colon survives.
OPENROUTER: tuple[RosterEntry, ...] = (
    RosterEntry(
        "deepseek/deepseek-r1:free",
        "DeepSeek R1 (free)",
        tags=("reasoning",),
        provider="openrouter",
        rpm=10,
        tpm=40_000,
        context_window=64_000,
        note="a reasoning model on a free tier: good at the plan, slow at the conversation",
    ),
    RosterEntry(
        "deepseek/deepseek-v3:free",
        "DeepSeek V3 (free)",
        tags=("coding", "general"),
        provider="openrouter",
        rpm=10,
        tpm=40_000,
        context_window=64_000,
    ),
    RosterEntry(
        "meta-llama/llama-3.3-70b-instruct:free",
        "Llama 3.3 70B Instruct (free)",
        tags=("general", "conversation"),
        provider="openrouter",
        rpm=10,
        tpm=40_000,
        context_window=131_072,
    ),
    RosterEntry(
        "meta-llama/llama-3.1-8b-instruct:free",
        "Llama 3.1 8B Instruct (free)",
        tags=("fast", "bulk", "cheap"),
        provider="openrouter",
        rpm=20,
        tpm=80_000,
        context_window=131_072,
    ),
    RosterEntry(
        "qwen/qwen-2.5-72b-instruct:free",
        "Qwen 2.5 72B Instruct (free)",
        tags=("general", "bulk"),
        provider="openrouter",
        rpm=10,
        tpm=40_000,
        context_window=131_072,
    ),
    RosterEntry(
        "mistralai/mistral-7b-instruct:free",
        "Mistral 7B Instruct (free)",
        tags=("fast", "bulk", "cheap"),
        provider="openrouter",
        rpm=20,
        tpm=80_000,
        context_window=32_000,
    ),
    RosterEntry(
        "google/gemma-2-9b-it:free",
        "Gemma 2 9B (free)",
        tags=("fast", "bulk", "cheap"),
        provider="openrouter",
        rpm=20,
        tpm=80_000,
        context_window=8_192,
    ),
    RosterEntry("nvidia/nemotron-4-340b-instruct:free", "Nemotron 4 340B (free)",
                tags=("reasoning", "general"), provider="openrouter", rpm=20, tpm=40_000,
                context_window=128_000, note="the 340B route: a rung for a hard plan, too slow to converse with"),
    RosterEntry("openrouter/free", "OpenRouter auto-router (free)",
                tags=("general", "bulk", "cheap"), provider="openrouter", rpm=20, tpm=40_000,
                context_window=128_000,
                note="dynamic load balancing: the router picks, so the answering model is unknown until the response"),
)

#: GroqCloud free tier: the fastest token throughput of any free option here, which is why the
#: tool loop lists it early.
GROQ: tuple[RosterEntry, ...] = (
    RosterEntry(
        "llama-3.3-70b-versatile",
        "Llama 3.3 70B Versatile",
        tags=("general", "conversation", "tool_loop", "coding"),
        provider="groq",
        rpm=30,
        tpm=6_000,
        rpd=1000,
        context_window=131_072,
        note="30 rpm but only 6K tokens/minute: fast, and easy to throttle on a long prompt",
    ),
    RosterEntry(
        "llama-3.1-8b-instant",
        "Llama 3.1 8B Instant",
        tags=("fast", "bulk", "cheap"),
        provider="groq",
        rpm=30,
        tpm=20_000,
        rpd=1000,
        context_window=131_072,
    ),
    RosterEntry(
        "mixtral-8x7b-instruct",
        "Mixtral 8x7B Instruct",
        tags=("general", "bulk"),
        provider="groq",
        rpm=30,
        tpm=5_000,
        rpd=1000,
        context_window=32_768,
    ),
    RosterEntry(
        "gemma2-9b-it",
        "Gemma 2 9B IT",
        tags=("fast", "bulk", "cheap"),
        provider="groq",
        rpm=30,
        tpm=15_000,
        rpd=1000,
        context_window=8_192,
    ),
)

#: Cerebras: 60K tokens/minute on the 8B is the highest sustained throughput of the free tiers.
CEREBRAS: tuple[RosterEntry, ...] = (
    RosterEntry(
        "llama3.1-8b",
        "Llama 3.1 8B",
        tags=("fast", "bulk", "tool_loop"),
        provider="cerebras",
        rpm=30,
        tpm=60_000,
        context_window=131_072,
        note="30 rpm at 60K tokens/minute — the best free throughput for long context work",
    ),
    RosterEntry(
        "llama3.1-70b",
        "Llama 3.1 70B",
        tags=("general", "reasoning", "background"),
        provider="cerebras",
        rpm=15,
        tpm=30_000,
        context_window=131_072,
    ),
)

#: Mistral's free mode is deliberately slow (1–5 rpm). It is worth having for Codestral, not as a
#: loop model, and the chains treat it that way.
MISTRAL: tuple[RosterEntry, ...] = (
    RosterEntry(
        "mistral-large-latest",
        "Mistral Large",
        tags=("reasoning",),
        provider="mistral",
        rpm=1,
        tpm=25_000,
        context_window=128_000,
        note="1 request/minute: an escalation model, never a loop",
    ),
    RosterEntry(
        "mistral-small-latest",
        "Mistral Small",
        tags=("general", "bulk"),
        provider="mistral",
        rpm=2,
        tpm=50_000,
        context_window=128_000,
    ),
    RosterEntry(
        "codestral-latest",
        "Codestral",
        tags=("coding",),
        provider="mistral",
        rpm=2,
        tpm=50_000,
        context_window=256_000,
        note="a code specialist; 2 rpm means one considered edit, not an iteration loop",
    ),
    RosterEntry(
        "open-mistral-nemo",
        "Mistral Nemo",
        tags=("fast", "bulk", "cheap"),
        provider="mistral",
        rpm=5,
        tpm=100_000,
        context_window=131_072,
    ),
)

#: Cloudflare Workers AI. The free allowance is a daily *neuron* budget, not requests, so no
#: rpm/tpm/rpd is recorded here — inventing a request rate would be a fabricated limit.
CLOUDFLARE: tuple[RosterEntry, ...] = (
    RosterEntry(
        "@cf/meta/llama-3.1-8b-instruct",
        "Llama 3.1 8B Instruct (Workers AI)",
        tags=("fast", "bulk"),
        provider="cloudflare",
        context_window=131_072,
        note="free allowance is 10K neurons/day, not a request rate",
    ),
    RosterEntry(
        "@cf/qwen/qwen-2.5-7b-instruct",
        "Qwen 2.5 7B Instruct (Workers AI)",
        tags=("fast", "bulk", "cheap"),
        provider="cloudflare",
        context_window=32_768,
        note="free allowance is 10K neurons/day, not a request rate",
    ),
    RosterEntry(
        "@cf/mistral/mistral-7b-instruct-v0.2",
        "Mistral 7B Instruct v0.2 (Workers AI)",
        tags=("fast", "bulk", "cheap"),
        provider="cloudflare",
        context_window=32_768,
        note="free allowance is 10K neurons/day, not a request rate",
    ),
)


#: Token Harbor: a unified gateway. Its sheet publishes 60 rpm and 1,800 requests/hour but no
#: stable public base URL, so the provider is env-only rather than guessed at.
TOKEN_HARBOR: tuple[RosterEntry, ...] = (
    RosterEntry("deepseek-v4.1-flash:free", "DeepSeek V4.1 Flash (free)", tags=("fast", "general", "tool_loop"),
                provider="tokenharbor", rpm=60, note="1,800 requests/hour per account; unmetered once funded"),
    RosterEntry("mimo-v2.6-flash:free", "MiMo V2.6 Flash (free)", tags=("fast", "bulk"),
                provider="tokenharbor", rpm=60, note="1,800 requests/hour per account"),
    RosterEntry("deepseek-v4-flash:free", "DeepSeek V4 Flash (free)", tags=("general", "coding"),
                provider="tokenharbor", rpm=60, note="shared gateway rules: 1,800/hour is per account, not per model"),
    RosterEntry("mimo-v2.5:free", "MiMo V2.5 (free)", tags=("fast", "bulk"),
                provider="tokenharbor", rpm=60, note="shared gateway rules"),
)

#: SambaNova runs DeepSeek on its own silicon: 20 rpm but only **20 a day** on the free tier.
SAMBANOVA: tuple[RosterEntry, ...] = (
    RosterEntry("DeepSeek-V3.1", "DeepSeek V3.1 (SambaNova)", tags=("reasoning", "coding"),
                provider="sambanova", rpm=20, rpd=20,
                note="free tier is 20/day; the dev tier is 60 rpm / 12,000/day / 20M tokens"),
    RosterEntry("DeepSeek-V3.2", "DeepSeek V3.2 (SambaNova)", tags=("reasoning", "coding"),
                provider="sambanova", rpm=20, rpd=20, note="free tier is 20/day; dev tier 60 rpm / 12,000/day"),
)

#: LLM7.io's baseline needs no key, which makes it the cheapest rung that still reasons.
LLM7: tuple[RosterEntry, ...] = (
    RosterEntry("deepseek-r1", "DeepSeek R1 (LLM7)", tags=("reasoning",),
                provider="llm7", rpm=30, context_window=131_072,
                note="30 rpm out of the box; session keys scale it to 120"),
    RosterEntry("deepseek-v3", "DeepSeek V3 (LLM7)", tags=("general", "coding", "background"),
                provider="llm7", rpm=30, context_window=1_000_000,
                note="up to a 1M context: the widest window on any free route here"),
)

#: TeamoRouter: one model, a hard 200-a-day ceiling, recorded so the Steward paces it instead of
#: discovering the ceiling at request 201.
TEAMOROUTER: tuple[RosterEntry, ...] = (
    RosterEntry("deepseek-v4-flash-free", "DeepSeek V4.1 (TeamoRouter)", tags=("coding", "background"),
                provider="teamorouter", rpd=200, note="hard ceiling of 200/day per developer; no rpm published"),
)

#: OVHcloud's anonymous tier needs no account at all: 12 rpm, nothing to store. The endpoint is
#: still env-supplied, because the sheet names the tier and not the URL.
OVH: tuple[RosterEntry, ...] = (
    RosterEntry("deepseek-v3", "DeepSeek V3 (OVHcloud)", tags=("general", "background", "bulk"),
                provider="ovh", rpm=12, context_window=131_072,
                note="anonymous tier: no account, no key, 12 rpm"),
)

#: DeepSeek's own platform: no rate limit in the sheet, only a 5M-token bonus and a concurrency
#: ceiling. No rpm is invented; the bonus is recorded as the note it is.
DEEPSEEK: tuple[RosterEntry, ...] = (
    RosterEntry("deepseek-flash", "DeepSeek Flash (native)", tags=("fast", "general"),
                provider="deepseek", context_window=131_072,
                note="5M-token signup bonus; no rate limit published, concurrency 2,500"),
    RosterEntry("deepseek-v4-pro", "DeepSeek V4 Pro (native)", tags=("reasoning", "coding"),
                provider="deepseek", context_window=131_072,
                note="5M-token signup bonus; no rate limit published, concurrency 500"),
)


# --------------------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provider:
    """A provider block: where it is, which env var unlocks it, and what it can do."""

    name: str
    kind: str
    base_url: str
    api_key_env: str
    models: tuple[RosterEntry, ...]
    priority: int
    headers: tuple[tuple[str, str], ...] = ()
    enabled: bool = True
    #: True when the base URL needs something other than the key (Cloudflare's account id).
    needs_extra: str = ""
    #: Set when the endpoint itself comes from the environment (see resolved_base_url).
    base_url_env: str = ""
    note: str = ""

    @property
    def resolved_base_url(self) -> str:
        """The endpoint, or "" when it is env-supplied and that variable is not set.

        Token Harbor, TeamoRouter and OVH publish a tier without a stable public base URL in the
        owner's sheets. Inventing one produces the worst outcome available: a provider that looks
        configured and 404s on every call. Those declare base_url_env and stay disabled with the
        variable named until it is set.
        """
        if self.base_url_env:
            return os.environ.get(self.base_url_env, "").strip()
        return self.base_url

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.resolved_base_url)

    def config(self):  # -> ProviderConfig
        from ..config import ProviderConfig

        return ProviderConfig(
            name=self.name,
            kind=self.kind,
            base_url=self.resolved_base_url,
            api_key_env=self.api_key_env,
            priority=self.priority,
            models=[entry.spec() for entry in self.models],
            enabled=self.usable,
            headers=dict(self.headers),
        )


def _cloudflare() -> Provider:
    """Workers AI is account-scoped: without ``CF_ACCOUNT_ID`` the URL does not exist.

    The provider is therefore built *disabled* when the id is missing, and ``zyntharo models``
    says which variable is missing. A half-built URL that 404s on every call would be the kind of
    thing that looks configured and never works.
    """
    account = os.environ.get("CF_ACCOUNT_ID", "").strip()
    return Provider(
        name="cloudflare",
        kind="openai_compatible",
        base_url=f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1" if account else "",
        api_key_env="CLOUDFLARE_API_TOKEN",
        models=CLOUDFLARE,
        priority=28,
        enabled=bool(account),
        needs_extra="CF_ACCOUNT_ID",
        note="needs CLOUDFLARE_API_TOKEN and CF_ACCOUNT_ID; allowance is neurons/day",
    )


def providers() -> list[Provider]:
    """Every provider the roster knows, in the order the Steward should prefer them.

    ``openai_compatible`` covers all six because that is what they all speak. The Gemini adapter
    stays for Gemini's own ``generateContent``, which is the only one here that is not
    OpenAI-shaped.
    """
    return [
        Provider(
            name="gemini",
            kind="gemini",
            base_url=GEMINI_BASE,
            api_key_env=GEMINI_KEY_ENV,
            models=GEMINI,
            priority=10,
            note="the owner's own list; the live socket is catalogued but not implemented",
        ),
        Provider(
            name="openrouter",
            kind="openai_compatible",
            base_url="https://openrouter.ai/api/v1",
            api_key_env="OPENROUTER_API_KEY",
            models=OPENROUTER,
            priority=20,
            # OpenRouter attributes free-tier traffic with these; they are optional and harmless.
            headers=(("HTTP-Referer", "https://zyntharo.app"), ("X-Title", "ZYNTHARO")),
            note="an aggregator: one key, nine models, and the free routes in the ids",
        ),
        Provider(
            name="groq",
            kind="openai_compatible",
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
            models=GROQ,
            priority=25,
            note="fastest tokens/second; the tightest tokens/minute",
        ),
        Provider(
            name="cerebras",
            kind="openai_compatible",
            base_url="https://api.cerebras.ai/v1",
            api_key_env="CEREBRAS_API_KEY",
            models=CEREBRAS,
            priority=26,
            note="60K tokens/minute on the 8B: the best free throughput for long prompts",
        ),
        Provider(
            name="mistral",
            kind="openai_compatible",
            base_url="https://api.mistral.ai/v1",
            api_key_env="MISTRAL_API_KEY",
            models=MISTRAL,
            priority=27,
            note="1-5 rpm: escalation and code specialism, not throughput",
        ),
        _cloudflare(),
        # -- second sheet: gateways, specialist DeepSeek silicon, and two keyless routes --------
        Provider(name="tokenharbor", kind="openai_compatible", base_url="",
                 base_url_env="TOKEN_HARBOR_BASE_URL", api_key_env="TOKEN_HARBOR_API_KEY",
                 models=TOKEN_HARBOR, priority=21,
                 note="60 rpm / 1,800 an hour; needs TOKEN_HARBOR_API_KEY and TOKEN_HARBOR_BASE_URL"),
        Provider(name="sambanova", kind="openai_compatible", base_url="https://api.sambanova.ai/v1",
                 api_key_env="SAMBANOVA_API_KEY", models=SAMBANOVA, priority=22,
                 note="20 rpm but only 20 requests/day free: an escalation rung, never a loop"),
        Provider(name="llm7", kind="openai_compatible", base_url="https://api.llm7.io/v1",
                 api_key_env="LLM7_API_KEY", models=LLM7, priority=23,
                 note="baseline needs no key; a session key raises the limit to 120 rpm"),
        Provider(name="teamorouter", kind="openai_compatible", base_url="",
                 base_url_env="TEAMOROUTER_BASE_URL", api_key_env="TEAMOROUTER_API_KEY",
                 models=TEAMOROUTER, priority=24,
                 note="200 requests/day hard ceiling; needs TEAMOROUTER_BASE_URL"),
        Provider(name="ovh", kind="openai_compatible", base_url="",
                 base_url_env="OVH_AI_BASE_URL", api_key_env="", models=OVH, priority=29,
                 needs_extra="OVH_AI_BASE_URL",
                 note="anonymous 12 rpm tier: no key at all, but it needs OVH_AI_BASE_URL"),
        Provider(name="deepseek", kind="openai_compatible", base_url="https://api.deepseek.com/v1",
                 api_key_env="DEEPSEEK_API_KEY", models=DEEPSEEK, priority=30,
                 note="5M-token signup bonus; no published rate limit, so none is invented"),
    ]


BY_ID: dict[str, RosterEntry] = {entry.model_id: entry for entry in GEMINI}

#: The offline stub is declared by ``config.default_config`` (it has to exist before any roster,
#: so a fresh install with no keys still runs), but the management chain names it as its last
#: resort. Registering it here keeps every chain resolvable against one lookup table instead of
#: special-casing the terminal rung at each call site.
OFFLINE_ECHO = RosterEntry(
    "offline-echo",
    "Offline echo (no reasoning)",
    tags=("offline",),
    provider="echo",
    rpm=1000,
    rpd=100_000,
    note="performs no reasoning; it exists so a fleet with no working model still runs",
)

BY_QUALIFIED: dict[str, RosterEntry] = {
    **{entry.qualified: entry for provider in providers() for entry in provider.models},
    OFFLINE_ECHO.qualified: OFFLINE_ECHO,
}
PROVIDER_BY_NAME: dict[str, Provider] = {provider.name: provider for provider in providers()}

#: Model ids that appear in more than one source with different numbers. The owner's own list wins
#: for its own account; this table exists so the difference is visible instead of lost.
CAP_CONFLICTS: dict[str, str] = {
    "gemini:gemini-2.5-flash": "owner's list 5 rpm / 250K tpm / 20 rpd; reference sheet 15 / 1M / 1.5K",
    "gemini:gemini-2.5-flash-lite": "owner's list 10 rpm / 250K tpm / 20 rpd; reference sheet 30 / 1M / 1.5K",
    "openrouter:*:free": "sheet 1 says 10 rpm / 40K tpm; sheet 2 says 20 rpm / 50 rpd (1,000 if funded). Recorded as 10/40K; edit the entries if your account shows the wider tier",
}


# --------------------------------------------------------------------------------------
# Ranks — how big a model is, which is what decides what job it is given
# --------------------------------------------------------------------------------------

#: Rank is a size class, not a quality opinion. Small models are cheap and can be called all day;
#: a 70B+ is a considered decision. The manager assigns by this band so that "run the bulk of the
#: work on the biggest model" cannot happen by accident.
RANK_LABELS: tuple[str, ...] = ("nano", "small", "medium", "large", "frontier")

#: Parameter counts the vendors actually publish, in billions. Sparse mixture-of-experts models
#: carry their total parameter count, which is why Mixtral sits at 47 and DeepSeek at 671.
PUBLISHED_SIZES: dict[str, float] = {
    "gemma-4-26b-it": 26, "gemma-4-31b-it": 31,
    "llama-3.1-8b-instant": 8, "llama-3.3-70b-versatile": 70,
    "llama3.1-8b": 8, "llama3.1-70b": 70,
    "meta-llama/llama-3.1-8b-instruct:free": 8, "meta-llama/llama-3.3-70b-instruct:free": 70,
    "qwen/qwen-2.5-72b-instruct:free": 72,
    "mistralai/mistral-7b-instruct:free": 7, "google/gemma-2-9b-it:free": 9,
    "gemma2-9b-it": 9, "mixtral-8x7b-instruct": 47,
    "@cf/meta/llama-3.1-8b-instruct": 8, "@cf/qwen/qwen-2.5-7b-instruct": 7,
    "@cf/mistral/mistral-7b-instruct-v0.2": 7,
    "nvidia/nemotron-4-340b-instruct:free": 340,
    "mistral-small-latest": 24, "mistral-large-latest": 123, "codestral-latest": 22,
    "open-mistral-nemo": 12, "deepseek-v3": 671, "deepseek-r1": 671,
    "deepseek/deepseek-r1:free": 671, "deepseek/deepseek-v3:free": 671,
}

#: Models whose vendors do not publish a parameter count. Their rank is a *declared class*, and
#: ``rank_is_declared`` says so — the alternative is guessing a size and presenting it as fact.
DECLARED_RANKS: dict[str, int] = {
    "gemini-2.5-flash-preview-native-audio-dialog": 3,
    "gemini-3.8-live-preview-extended-thinking": 3, "gemini-3.8-live-preview": 3,
    "gemini-3-flash-live-preview": 2, "gemini-3.5-live-translate-preview": 2,
    "gemini-3.8-flash": 3, "antigravity": 3, "gemini-3-flash-preview": 2, "gemini-3.6-flash": 2,
    "gemini-3.1-flash-lite": 1, "gemini-3.5-flash-lite": 1,
    "gemini-2.5-flash": 2, "gemini-2.5-flash-lite": 1, "gemini-2.5-pro": 3,
    "gemini-1.5-flash": 2, "gemini-robotics-er2-preview": 3,
    "gemini-3.8-flash-tts": 0, "gemini-3.8-flash-lite-tts": 0, "gemini-3.1-flash-tts-preview": 0,
    "gemini-2.5-flash-preview-tts": 0, "gemini-3.5-transcribe-live": 0, "gemini-3.5-transcribe": 0,
    "gemini-embedding-2": 0, "gemini-embedding-001": 0,
    "DeepSeek-V3.1": 4, "DeepSeek-V3.2": 4,
    "deepseek-v4.1-flash:free": 2, "mimo-v2.6-flash:free": 1, "mimo-v2.5:free": 1,
    "deepseek-v4-flash:free": 3, "deepseek-v4-flash-free": 2,
    "deepseek-flash": 2, "deepseek-v4-pro": 4,
    "openrouter/free": 2,
}

#: The band of model size each job should run in. In-band rungs are tried first; anything outside
#: the band is still a fallback, but it is never the first choice. Bulk work in band 0-2 means a
#: 340B or a 671B can never be spent on tagging, and reasoning in band 3-4 means an 8B never
#: adjudicates a plan while a real one exists.
RANK_BANDS: dict[str, tuple[int, int]] = {
    "conversation": (2, 3),
    "reasoning": (3, 4),
    "coding": (2, 4),
    "tool_loop": (1, 3),
    "background": (0, 2),
    "bulk": (0, 2),
    "vision": (2, 4),
    "spatial": (3, 4),
    "management": (1, 3),
    "voice_live": (2, 4),
    "speech": (0, 1),
    "transcribe": (0, 1),
    "embedding": (0, 1),
}


def rank_for_size(size_b: float | None) -> int | None:
    """Map a parameter count to a rank. None in, None out — an unpublished size is not a guess."""
    if size_b is None:
        return None
    if size_b < 4:
        return 0
    if size_b <= 9:
        return 1
    if size_b <= 40:
        return 2
    if size_b <= 99:
        return 3
    return 4


# --------------------------------------------------------------------------------------
# Transports — what this build can actually call
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Transport:
    name: str
    implemented: bool
    detail: str


#: Plain HTTPS ``generateContent`` / ``chat/completions``: implemented in
#: :mod:`zyntharo.llm.providers` and used by every text, TTS and embedding model below.
GENERATE_CONTENT = Transport(
    "generateContent (HTTPS)",
    True,
    "implemented in llm.providers.GeminiAdapter",
)

#: The Live API is a WebSocket protocol (``BidiGenerateContent``) with its own framing, audio
#: chunks and turn-taking. It is *not* implemented here, and pretending otherwise is the exact
#: failure this project forbids: the models are catalogued with their real caps and marked
#: blocked, and the voice path stays on the turn-based tier that does work.
LIVE_SOCKET = Transport(
    "BidiGenerateContent (WebSocket)",
    False,
    "no live socket adapter in this build; the voice session uses turn-based neural speech instead",
)


# --------------------------------------------------------------------------------------
# Purposes — the job → ordered chain table (fully qualified, cross-provider)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Purpose:
    """A job, the models that can do it, and why they are in that order."""

    name: str
    job: str
    chain: tuple[str, ...]
    transport: Transport = GENERATE_CONTENT
    why: str = ""
    wants_tools: bool = True
    wants_vision: bool = False

    @property
    def rank_band(self) -> tuple[int, int]:
        """The size band this job belongs in. A rung outside it is a fallback, not a first choice."""
        return RANK_BANDS.get(self.name, (0, 4))

    def rank_fits(self, rank: int) -> bool:
        low, high = self.rank_band
        return low <= rank <= high

    def models(self) -> tuple[RosterEntry, ...]:
        return tuple(BY_QUALIFIED[q] for q in self.chain if q in BY_QUALIFIED)


PURPOSES: dict[str, Purpose] = {
    "conversation": Purpose(
        name="conversation",
        job="talking to the owner in text: chat, questions, decisions",
        chain=(
            "gemini:antigravity",
            "gemini:gemini-3.8-flash",
            "gemini:gemini-3.6-flash",
            "groq:llama-3.3-70b-versatile",
            "gemini:gemini-3.5-flash-lite",
            "openrouter:meta-llama/llama-3.3-70b-instruct:free",
            "gemini:gemma-4-31b-it",
        ),
        why="60 rpm carries a real back-and-forth; Groq joins when its key exists, because 30 rpm of a 70B is more conversation than 20 requests/day of anything",
    ),
    "reasoning": Purpose(
        name="reasoning",
        job="hard decisions: planning a task, judging a risk, reviewing a plan",
        chain=(
            "gemini:gemini-3.8-flash",
            "gemini:antigravity",
            "openrouter:deepseek/deepseek-r1:free",
            "gemini:gemini-3-flash-preview",
            "cerebras:llama3.1-70b",
            "gemini:gemma-4-31b-it",
        ),
        why="quality first — a bad plan costs more than a model call; a free reasoning model sits behind the scarce ones",
    ),
    "coding": Purpose(
        name="coding",
        job="writing and repairing code: plugins, self-modification, new capabilities",
        chain=(
            "gemini:gemini-3.8-flash",
            "gemini:antigravity",
            "gemini:gemini-3.6-flash",
            "mistral:codestral-latest",
            "openrouter:deepseek/deepseek-v3:free",
            "gemini:gemma-4-31b-it",
            "gemini:gemini-3.5-flash-lite",
        ),
        why="the strongest text models first, then a code specialist, then the models whose daily caps allow iterating all afternoon",
    ),
    "tool_loop": Purpose(
        name="tool_loop",
        job="executing actions: an Operative's many-step tool loop",
        chain=(
            "gemini:antigravity",
            "gemini:gemini-3.1-flash-lite",
            "groq:llama-3.3-70b-versatile",
            "cerebras:llama3.1-8b",
            "gemini:gemini-3.5-flash-lite",
            "gemini:gemini-3.6-flash",
            "gemini:gemma-4-31b-it",
        ),
        why="a tool loop spends one call per step, so rate limit beats elo: 60 rpm, then the 500-a-day lites, then Groq and Cerebras where a key exists",
        wants_tools=True,
    ),
    "background": Purpose(
        name="background",
        job="memory compression, indexing, summaries, watchdogs — work nobody is waiting on",
        chain=(
            "gemini:gemma-4-31b-it",
            "gemini:gemma-4-26b-it",
            "gemini:gemini-3.1-flash-lite",
            "cerebras:llama3.1-70b",
            "openrouter:qwen/qwen-2.5-72b-instruct:free",
            "groq:llama-3.3-70b-versatile",
        ),
        why="14,400 requests/day is the only envelope that can run continuously; never spend a 20-a-day model on this",
    ),
    "bulk": Purpose(
        name="bulk",
        job="classification, tagging, extraction over many items",
        chain=(
            "gemini:gemma-4-26b-it",
            "gemini:gemini-3.5-flash-lite",
            "groq:llama-3.1-8b-instant",
            "cerebras:llama3.1-8b",
            "openrouter:meta-llama/llama-3.1-8b-instruct:free",
            "cloudflare:@cf/meta/llama-3.1-8b-instruct",
            "gemini:gemini-3.1-flash-lite",
        ),
        why="cheapest per call; every rung is a 1K+-a-day envelope so bulk work never starves the thinking models",
    ),
    "management": Purpose(
        name="management",
        job="the Steward's own work: watching quota, errors and the fleet, and assigning models",
        chain=(
            "gemini:antigravity",
            "groq:llama-3.3-70b-versatile",
            "cerebras:llama3.1-70b",
            "llm7:deepseek-v3",
            "openrouter:meta-llama/llama-3.3-70b-instruct:free",
            "gemini:gemma-4-31b-it",
            "ovh:deepseek-v3",
            # The terminal rung is deliberate. The echo stub performs no reasoning, but it is
            # always ready, so a manager whose every model is exhausted still reads quotas,
            # records memory and keeps the HUD alive instead of collapsing the fleet with it.
            "echo:offline-echo",
        ),
        why="a manager that stops managing takes the fleet with it: five providers, then the 14,400-a-day Gemma, then the keyless OVH tier, and finally the offline stub that cannot reason but cannot fail",
    ),
    "vision": Purpose(
        name="vision",
        job="reading the screen: OCR triage, layout understanding, what is on the display",
        chain=(
            "gemini:gemini-3.8-flash",
            "gemini:antigravity",
            "gemini:gemini-3-flash-preview",
            "gemini:gemini-3.6-flash",
            "gemini:gemini-2.5-flash",
        ),
        why="screen reading is a judgement call; the open-weight free tiers are not listed because their vision support is not assumed, only Gemini's",
        wants_vision=True,
    ),
    "spatial": Purpose(
        name="spatial",
        job="spatial and embodied reasoning (robotics preview)",
        chain=("gemini:gemini-robotics-er2-preview", "gemini:gemini-3.8-flash", "gemini:gemini-3.6-flash"),
        why="a specialist model, kept for the specialist job; general models behind it",
        wants_vision=True,
    ),
    "voice_live": Purpose(
        name="voice_live",
        job="speaking with the owner — the voice they actually hear",
        chain=(
            "gemini:gemini-2.5-flash-preview-native-audio-dialog",
            "gemini:gemini-3.8-live-preview-extended-thinking",
            "gemini:gemini-3.8-live-preview",
            "gemini:gemini-3-flash-live-preview",
            "gemini:gemini-3.5-live-translate-preview",
        ),
        transport=LIVE_SOCKET,
        why="most human first: true speech-to-speech, then the newest live model that can think, then the older live, then the interpreter",
        wants_tools=True,
    ),
    "speech": Purpose(
        name="speech",
        job="rendering a reply as audio, when the live socket is not in use",
        chain=(
            "gemini:gemini-3.8-flash-tts",
            "gemini:gemini-3.8-flash-lite-tts",
            "gemini:gemini-3.1-flash-tts-preview",
            "gemini:gemini-2.5-flash-preview-tts",
        ),
        why="newest voice set first; each rung has only 10 requests/day, so the chain is also the daily budget",
        wants_tools=False,
    ),
    "transcribe": Purpose(
        name="transcribe",
        job="turning recorded speech into text",
        chain=("gemini:gemini-3.5-transcribe-live", "gemini:gemini-3.5-transcribe"),
        transport=LIVE_SOCKET,
        why="the live transcriber streams; the batch one is the fallback",
        wants_tools=False,
    ),
    "embedding": Purpose(
        name="embedding",
        job="vectors for memory recall and similarity",
        chain=("gemini:gemini-embedding-2", "gemini:gemini-embedding-001"),
        why="100 rpm and 1,000 a day: enough to index a working day of memory",
        wants_tools=False,
    ),
}


#: Which job each standing role does by default.
ROLE_PURPOSE: dict[str, str] = {
    "sovereign": "conversation",
    # The Steward is the only role whose failure is systemic, so it runs its own chain with its
    # own last resort rather than borrowing the reasoning chain.
    "steward": "management",
    "foreman": "reasoning",
    "artificer": "coding",
    "operative": "tool_loop",
    "archivist": "background",
    "warden": "reasoning",
    "cartographer": "tool_loop",
    "courier": "tool_loop",
    "scrivener": "tool_loop",
    "probe": "bulk",
}

#: Roles whose whole output is spoken, so the voice chain decides before the text chain does.
VOICE_ROLES = ("sovereign",)


def chain(purpose: str) -> tuple[str, ...]:
    """The qualified model ids for a job, best first. Unknown purposes raise, loudly."""
    entry = PURPOSES.get(purpose)
    if entry is None:
        raise KeyError(f"unknown purpose {purpose!r} (have {sorted(PURPOSES)})")
    return entry.chain


def ranked_chain(purpose: str) -> tuple[str, ...]:
    """The chain with in-band rungs first, out-of-band rungs after, order otherwise preserved.

    This is the manager's assignment rule in one function: a job's size band decides who is
    *eligible to start*, and everything else stays available as a fallback.
    """
    entry = PURPOSES[purpose]
    in_band: list[str] = []
    out_of_band: list[str] = []
    for qualified in entry.chain:
        model = BY_QUALIFIED.get(qualified)
        if model is None or entry.rank_fits(model.rank):
            in_band.append(qualified)
        else:
            out_of_band.append(qualified)
    return tuple(in_band + out_of_band)


def chain_for_role(role: str) -> tuple[str, ...]:
    """The chain an agent in this role should fall back through."""
    return chain(ROLE_PURPOSE.get(role, "conversation"))


def purpose_for_role(role: str) -> Purpose:
    return PURPOSES[ROLE_PURPOSE.get(role, "conversation")]


def default_model(role: str) -> str:
    """The first model of the role's chain — what a fresh config assigns."""
    return chain_for_role(role)[0]


def purpose_report() -> list[dict[str, Any]]:
    """The table ``zyntharo models`` prints: job, order, and whether the transport exists."""
    rows: list[dict[str, Any]] = []
    for purpose in PURPOSES.values():
        rows.append(
            {
                "purpose": purpose.name,
                "job": purpose.job,
                "chain": list(purpose.chain),
                "transport": purpose.transport.name,
                "implemented": purpose.transport.implemented,
                "why": purpose.why,
            }
        )
    return rows


def providers_report() -> list[dict[str, Any]]:
    """Per-provider rows for the CLI: what it is, how to unlock it, what it carries."""
    rows: list[dict[str, Any]] = []
    for provider in providers():
        rows.append(
            {
                "name": provider.name,
                "kind": provider.kind,
                "key_env": provider.api_key_env,
                "needs_extra": provider.needs_extra or provider.base_url_env,
                "enabled": provider.usable,
                "base_url_env": provider.base_url_env,
                "models": len(provider.models),
                "priority": provider.priority,
                "base_url": provider.base_url,
                "note": provider.note,
            }
        )
    return rows


# --------------------------------------------------------------------------------------
# Config + verification
# --------------------------------------------------------------------------------------


def gemini_provider():
    """Kept for callers that only want Gemini; :func:`provider_configs` returns everything."""
    return PROVIDER_BY_NAME["gemini"].config()


def provider_configs() -> list[Any]:
    """Every provider, as ``ProviderConfig`` objects, disabled ones included."""
    return [provider.config() for provider in providers()]


def probe(
    base_url: str = GEMINI_BASE,
    api_key: str = "",
    fetch: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Ask the account which models it actually serves, and diff that against the roster.

    Returns ``{"ok", "detail", "served", "missing", "unlisted"}``. ``missing`` is a roster id the
    account does not serve — a broken entry the Steward must stop choosing. ``unlisted`` ids are
    new models, reported so the roster can grow. ``fetch`` is injectable so the check can be
    exercised without a key or a network; the default only ever reads.
    """
    if not api_key:
        return {"ok": False, "detail": f"no {GEMINI_KEY_ENV} stored", "served": [], "missing": [], "unlisted": []}
    url = f"{base_url.rstrip('/')}/models?pageSize=200&key={api_key}"
    fetch = fetch or _urllib_fetch
    try:
        status, body = fetch(url, {"Content-Type": "application/json"}, timeout)
    except Exception as exc:  # noqa: BLE001 - a probe failure is a fact, not a crash
        return {"ok": False, "detail": f"request failed: {exc}", "served": [], "missing": [], "unlisted": []}
    if status >= 400:
        hint = {
            400: "the key was rejected",
            403: "the key is not enabled for this API",
            429: "rate limited while probing",
        }.get(status, "")
        return {"ok": False, "detail": f"HTTP {status} {hint}".strip(), "served": [], "missing": [], "unlisted": []}
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "detail": "malformed model list", "served": [], "missing": [], "unlisted": []}
    # The API names models "models/gemini-3.8-flash"; take the last path segment so the diff is
    # against ids and not against resource paths.
    served = sorted(
        str(row.get("name", "")).rstrip("/").rsplit("/", 1)[-1]
        for row in data.get("models", [])
        if row.get("name")
    )
    known = set(BY_ID)
    return {
        "ok": True,
        "detail": f"{len(served)} models served by the account",
        "served": served,
        "missing": sorted(known - set(served)),
        "unlisted": sorted(set(served) - known),
    }


def _urllib_fetch(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:  # a 401 is a result, not an exception to swallow
        return int(exc.code), exc.read() or b""


def summary() -> dict[str, Any]:
    """Counts the CLI and the doctor use, so neither has to walk the catalogue itself."""
    everything = [entry for provider in providers() for entry in provider.models]
    return {
        "models": len(everything),
        "by_rank": {RANK_LABELS[rank]: sum(1 for e in everything if e.rank == rank) for rank in range(5)},
        "providers": len(providers()),
        "by_provider": {provider.name: len(provider.models) for provider in providers()},
        "live": sum(1 for e in everything if e.audio_live),
        "tts": sum(1 for e in everything if "tts" in e.tags),
        "embedding": sum(1 for e in everything if "embedding" in e.tags),
        "purposes": len(PURPOSES),
        "unconfirmed_ids": sorted(f"{e.provider}:{e.model_id}" for e in everything if not e.id_confirmed),
        "live_transport_implemented": LIVE_SOCKET.implemented,
        "live_transport_detail": LIVE_SOCKET.detail,
        "cap_conflicts": dict(CAP_CONFLICTS),
    }
