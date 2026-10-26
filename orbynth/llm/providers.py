"""Provider wire adapters.

ORBYNTH must talk to *any* model the user can name — hosted APIs, a local llama.cpp
server, a company gateway — so providers are declared as data
(``ProviderConfig.kind`` + ``base_url`` + ``api_key_env``) and this module holds one
adapter per wire protocol:

* ``openai_compatible`` — OpenAI, Groq, Together, OpenRouter, DeepSeek, Mistral, vLLM,
  LM Studio, Ollama (``/v1``), and anything else exposing ``/chat/completions``.
* ``gemini`` — Google Generative Language API (``generateContent`` / ``streamGenerateContent``).
* ``anthropic`` — Messages API.
* ``echo`` — deterministic offline provider used by the test suite and by the first-run
  demo when no key is configured. It exercises the whole pipeline with no network.

Transport is injectable. Tests pass a fake transport and assert on real request payloads
without touching the network; production uses the stdlib :mod:`urllib`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Protocol

from ..config import ModelSpec, ProviderConfig
from ..events import MODEL_CALL, EventBus

# --------------------------------------------------------------------------------------
# Message / request / response model
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    name: str = ""
    tool_call_id: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Non-text content parts (images/audio) for multimodal requests.
    parts: list[dict[str, Any]] = field(default_factory=list)

    def to_openai(self) -> dict[str, Any]:
        if self.role == "tool":
            return {"role": "tool", "tool_call_id": self.tool_call_id, "content": self.content}
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        if self.parts:
            content: list[dict[str, Any]] = [{"type": "text", "text": self.content}]
            for part in self.parts:
                if part.get("type") == "image":
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{part.get('mime', 'image/png')};base64,{part['data']}"},
                        }
                    )
            msg["content"] = content
        return msg

    def to_gemini(self) -> dict[str, Any]:
        role = {"assistant": "model", "tool": "user"}.get(self.role, "user")
        parts: list[dict[str, Any]] = []
        if self.content:
            parts.append({"text": self.content})
        for part in self.parts:
            if part.get("type") == "image":
                parts.append({"inline_data": {"mime_type": part.get("mime", "image/png"), "data": part["data"]}})
        for call in self.tool_calls:
            fn = call.get("function", {})
            args = fn.get("arguments", "{}")
            parts.append(
                {
                    "functionCall": {
                        "name": fn.get("name", ""),
                        "args": json.loads(args) if isinstance(args, str) else args,
                    }
                }
            )
        return {"role": role, "parts": parts or [{"text": ""}]}

    def to_anthropic(self) -> dict[str, Any]:
        role = "assistant" if self.role == "assistant" else "user"
        content: list[dict[str, Any]] = []
        if self.content:
            content.append({"type": "text", "text": self.content})
        for part in self.parts:
            if part.get("type") == "image":
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.get("mime", "image/png"),
                            "data": part["data"],
                        },
                    }
                )
        for call in self.tool_calls:
            fn = call.get("function", {})
            args = fn.get("arguments", "{}")
            content.append(
                {
                    "type": "tool_use",
                    "id": call.get("id", "call"),
                    "name": fn.get("name", ""),
                    "input": json.loads(args) if isinstance(args, str) else args,
                }
            )
        return {"role": role, "content": content or [{"type": "text", "text": ""}]}


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    capability: str = ""
    requires_confirm: bool = False

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_gemini(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _json_schema_to_gemini(self.parameters),
        }

    def to_anthropic(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens or (other.prompt_tokens + other.completion_tokens)


@dataclass(slots=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""
    latency: float = 0.0
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ProviderError(RuntimeError):
    """Provider failure with the classification the Steward needs to act on."""

    def __init__(
        self,
        message: str,
        status: int = 0,
        kind: str = "unknown",
        retry_after: float = 0.0,
        provider: str = "",
        model: str = "",
    ):
        super().__init__(message)
        self.status = status
        self.kind = kind  # auth | quota | rate_limit | server | bad_request | network | unknown
        self.retry_after = retry_after
        self.provider = provider
        self.model = model

    @property
    def quota_exhausted(self) -> bool:
        return self.kind in {"quota", "rate_limit"}

    @property
    def retryable(self) -> bool:
        return self.kind in {"quota", "rate_limit", "server", "network"}


def classify_status(status: int, body: str, provider: str = "", model: str = "") -> ProviderError:
    """Map an HTTP failure onto the error taxonomy used for failover decisions."""
    text = (body or "")[:600]
    retry_after = 0.0
    try:
        payload = json.loads(body)
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            text = str(err.get("message") or text)
            for key in ("retry_after", "retryDelay"):
                if key in err:
                    raw = str(err[key]).rstrip("s")
                    retry_after = float(raw) if raw.replace(".", "", 1).isdigit() else 0.0
    except (json.JSONDecodeError, ValueError, TypeError):
        payload = None

    lower = text.lower()
    if status == 429:
        kind = "rate_limit"
        if "quota" in lower or "exhausted" in lower or "billing" in lower:
            kind = "quota"
    elif status in (401, 403):
        kind = "auth"
    elif status in (400, 404, 422):
        kind = "bad_request"
    elif status >= 500:
        kind = "server"
    else:
        kind = "unknown"
    return ProviderError(
        f"{status}: {text[:300]}", status=status, kind=kind, retry_after=retry_after, provider=provider, model=model
    )


def _json_schema_to_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini wants a subset of JSON Schema; strip what it rejects."""
    out: dict[str, Any] = {}
    type_map = {"object": "OBJECT", "string": "STRING", "number": "NUMBER", "integer": "INTEGER", "boolean": "BOOLEAN", "array": "ARRAY"}
    if "type" in schema:
        out["type"] = type_map.get(str(schema["type"]), "STRING")
    if "description" in schema:
        out["description"] = schema["description"]
    if "properties" in schema:
        out["properties"] = {k: _json_schema_to_gemini(v) for k, v in schema["properties"].items()}
    if "required" in schema:
        out["required"] = schema["required"]
    if "items" in schema:
        out["items"] = _json_schema_to_gemini(schema["items"])
    if "enum" in schema:
        out["enum"] = schema["enum"]
    return out


# --------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    """Injectable HTTP transport so tests never touch the network."""

    def send(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> TransportResponse: ...

    def stream(
        self, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, dict[str, str], Iterator[bytes]]: ...


class UrllibTransport:
    """Stdlib-only HTTP transport (no requests/httpx dependency)."""

    def send(self, url: str, headers: dict[str, str], body: bytes, timeout: float = 120.0) -> TransportResponse:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return TransportResponse(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:
            return TransportResponse(exc.code, dict(exc.headers or {}), exc.read())
        except urllib.error.URLError as exc:
            raise ProviderError(f"network error: {exc.reason}", kind="network") from exc

    def stream(
        self, url: str, headers: dict[str, str], body: bytes, timeout: float = 300.0
    ) -> tuple[int, dict[str, str], Iterator[bytes]]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), iter([exc.read()])
        except urllib.error.URLError as exc:
            raise ProviderError(f"network error: {exc.reason}", kind="network") from exc
        return resp.status, dict(resp.headers), iter(resp)


class RecordingTransport:
    """Wraps a transport and records every request — used in tests and the doctor command."""

    def __init__(self, inner: Transport):
        self.inner = inner
        self.requests: list[dict[str, Any]] = []

    def send(self, url: str, headers: dict[str, str], body: bytes, timeout: float = 120.0) -> TransportResponse:
        self.requests.append({"url": url, "headers": dict(headers), "body": body.decode("utf-8", "replace")})
        return self.inner.send(url, headers, body, timeout)

    def stream(self, url: str, headers: dict[str, str], body: bytes, timeout: float = 300.0):
        self.requests.append({"url": url, "headers": dict(headers), "body": body.decode("utf-8", "replace")})
        return self.inner.stream(url, headers, body, timeout)


# --------------------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class LLMRequest:
    messages: list[Message]
    model: str
    temperature: float = 0.2
    max_tokens: int = 4096
    tools: list[ToolSpec] = field(default_factory=list)
    json_mode: bool = False
    stream: bool = False
    system: str = ""

    def with_system(self, system: str) -> "LLMRequest":
        if system and not any(m.role == "system" for m in self.messages):
            self.messages = [Message(role="system", content=system), *self.messages]
        return self


class ProviderAdapter:
    """Base adapter. Subclasses implement ``payload`` and ``parse``."""

    kind = "base"

    def __init__(self, provider: ProviderConfig, api_key: str = "", transport: Transport | None = None):
        self.provider = provider
        self.api_key = api_key
        self.transport: Transport = transport or UrllibTransport()

    # -- to override -------------------------------------------------------------------
    def url(self, model: str, stream: bool, key: str = "") -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        raise NotImplementedError

    def payload(self, request: LLMRequest, stream: bool) -> dict[str, Any]:
        raise NotImplementedError

    def parse(self, data: dict[str, Any], model: str) -> LLMResponse:
        raise NotImplementedError

    def parse_stream_event(self, event: dict[str, Any], state: dict[str, Any]) -> str:
        """Consume one SSE event, mutating ``state``; return newly emitted text."""
        return ""

    # -- shared driver -----------------------------------------------------------------
    def call(self, request: LLMRequest) -> LLMResponse:
        started = time.time()
        url = self.url(request.model, request.stream, self.api_key)
        body = json.dumps(self.payload(request, request.stream)).encode("utf-8")
        resp = self.transport.send(url, self.headers(), body, timeout=180.0)
        if resp.status >= 400:
            raise classify_status(resp.status, resp.body.decode("utf-8", "replace"), self.provider.name, request.model)
        try:
            data = json.loads(resp.body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"malformed response: {exc}", kind="server", provider=self.provider.name) from exc
        out = self.parse(data, request.model)
        out.latency = time.time() - started
        out.provider = self.provider.name
        return out

    def stream_call(self, request: LLMRequest) -> Iterator[str]:
        request = request
        url = self.url(request.model, True, self.api_key)
        body = json.dumps(self.payload(request, True)).encode("utf-8")
        status, headers, lines = self.transport.stream(url, self.headers(), body, timeout=600.0)
        if status >= 400:
            chunks = list(lines)
            raise classify_status(status, b"".join(chunks).decode("utf-8", "replace"), self.provider.name, request.model)
        state: dict[str, Any] = {"buffer": b""}
        for raw in lines:
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            state["buffer"] += raw
            while b"\n" in state["buffer"]:
                line, state["buffer"] = state["buffer"].split(b"\n", 1)
                stripped = line.strip()
                if not stripped.startswith(b"data:"):
                    continue
                chunk = stripped[5:].strip()
                if chunk in (b"", b"[DONE]"):
                    if chunk == b"[DONE]":
                        return
                    continue
                try:
                    event = json.loads(chunk.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                text = self.parse_stream_event(event, state)
                if text:
                    yield text


class OpenAICompatibleAdapter(ProviderAdapter):
    kind = "openai_compatible"

    def url(self, model: str, stream: bool, key: str = "") -> str:
        base = (self.provider.base_url or "https://api.openai.com/v1").rstrip("/")
        return f"{base}/chat/completions"

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.provider.headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def payload(self, request: LLMRequest, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [m.to_openai() for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if request.tools:
            body["tools"] = [t.to_openai() for t in request.tools]
            body["tool_choice"] = "auto"
        if request.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def parse(self, data: dict[str, Any], model: str) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or "",
            tool_calls=message.get("tool_calls") or [],
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
            ),
            model=data.get("model", model),
            finish_reason=choice.get("finish_reason", ""),
            raw=data,
        )

    def parse_stream_event(self, event: dict[str, Any], state: dict[str, Any]) -> str:
        if usage := event.get("usage"):
            state["usage"] = usage
        choices = event.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        calls = delta.get("tool_calls") or []
        if calls:
            acc: list[dict[str, Any]] = state.setdefault("tool_calls", [])
            for call in calls:
                index = int(call.get("index", len(acc) - 1 if acc else 0))
                while len(acc) <= index:
                    acc.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                slot = acc[index]
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
        return delta.get("content") or ""


class GeminiAdapter(ProviderAdapter):
    kind = "gemini"

    def url(self, model: str, stream: bool, key: str = "") -> str:
        base = (self.provider.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        verb = "streamGenerateContent" if stream else "generateContent"
        suffix = "?alt=sse&key=" if stream else "?key="
        return f"{base}/models/{model}:{verb}{suffix}{key}"

    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", **self.provider.headers}

    def payload(self, request: LLMRequest, stream: bool) -> dict[str, Any]:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        contents = [m.to_gemini() for m in request.messages if m.role != "system"]
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        if request.json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if request.tools:
            body["tools"] = [{"functionDeclarations": [t.to_gemini() for t in request.tools]}]
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return body

    def parse(self, data: dict[str, Any], model: str) -> LLMResponse:
        candidates = data.get("candidates") or [{}]
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text_bits: list[str] = []
        calls: list[dict[str, Any]] = []
        for part in parts:
            if "text" in part:
                text_bits.append(part["text"])
            elif "functionCall" in part:
                fn = part["functionCall"]
                calls.append(
                    {
                        "id": f"call_{fn.get('name', 'tool')}",
                        "type": "function",
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": json.dumps(fn.get("args", {})),
                        },
                    }
                )
        usage_meta = data.get("usageMetadata") or {}
        return LLMResponse(
            text="".join(text_bits),
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=int(usage_meta.get("promptTokenCount", 0)),
                completion_tokens=int(usage_meta.get("candidatesTokenCount", 0)),
                total_tokens=int(usage_meta.get("totalTokenCount", 0)),
            ),
            model=model,
            finish_reason=candidates[0].get("finishReason", ""),
            raw=data,
        )

    def parse_stream_event(self, event: dict[str, Any], state: dict[str, Any]) -> str:
        out = self.parse(event, state.get("model", ""))
        if out.tool_calls:
            state.setdefault("tool_calls", []).extend(out.tool_calls)
        if out.usage.total_tokens:
            state["usage"] = {
                "prompt_tokens": out.usage.prompt_tokens,
                "completion_tokens": out.usage.completion_tokens,
                "total_tokens": out.usage.total_tokens,
            }
        return out.text


class AnthropicAdapter(ProviderAdapter):
    kind = "anthropic"

    def url(self, model: str, stream: bool, key: str = "") -> str:
        base = (self.provider.base_url or "https://api.anthropic.com/v1").rstrip("/")
        return f"{base}/messages"

    def headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            **self.provider.headers,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def payload(self, request: LLMRequest, stream: bool) -> dict[str, Any]:
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        msgs = [m.to_anthropic() for m in request.messages if m.role != "system"]
        body: dict[str, Any] = {
            "model": request.model,
            "messages": msgs,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": stream,
        }
        if system:
            body["system"] = system
        if request.tools:
            body["tools"] = [t.to_anthropic() for t in request.tools]
        return body

    def parse(self, data: dict[str, Any], model: str) -> LLMResponse:
        text_bits: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_bits.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    {
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input", {}))},
                    }
                )
        usage = data.get("usage") or {}
        return LLMResponse(
            text="".join(text_bits),
            tool_calls=calls,
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
            ),
            model=data.get("model", model),
            finish_reason=data.get("stop_reason", ""),
            raw=data,
        )

    def parse_stream_event(self, event: dict[str, Any], state: dict[str, Any]) -> str:
        etype = event.get("type")
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            return delta.get("text", "")
        if etype == "message_start" and (usage := (event.get("message") or {}).get("usage")):
            state["usage"] = {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": 0, "total_tokens": 0}
        if etype == "message_delta" and (usage := event.get("usage")):
            state.setdefault("usage", {})["completion_tokens"] = usage.get("output_tokens", 0)
        return ""


class EchoAdapter(ProviderAdapter):
    """Offline deterministic provider.

    Used by the test suite and by the first-run experience when no API key exists yet, so
    the agents, orchestrator, quota manager and HUD all run end-to-end with zero keys.
    If the last user message looks like a tool request (``use: <name> {json}``) it emits a
    real tool call; otherwise it echoes a structured plan.
    """

    kind = "echo"

    def url(self, model: str, stream: bool, key: str = "") -> str:
        return "echo://local"

    def headers(self) -> dict[str, str]:
        return {}

    def payload(self, request: LLMRequest, stream: bool) -> dict[str, Any]:
        return {"model": request.model, "messages": [m.to_openai() for m in request.messages], "stream": stream}

    def call(self, request: LLMRequest) -> LLMResponse:
        started = time.time()
        tool_results = [msg for msg in request.messages if msg.role == "tool"]
        has_report_tool = any(tool.name == "agent_report" for tool in request.tools)
        if tool_results and has_report_tool:
            # Close the loop: a real pipeline ends with a report, and so does the stub, so
            # every subsystem downstream of a completed task is exercised offline.
            last_result = tool_results[-1]
            failed = "\"error\"" in last_result.content and "\"ok\": false" in last_result.content.replace(" ", " ")
            arguments = {
                "status": "partial" if failed else "done",
                "summary": f"executed {len(tool_results)} tool call(s); last was {last_result.name}",
                "evidence": [f"{last_result.name}: {last_result.content[:160]}"],
                "blockers": ["offline echo provider: no real reasoning performed"] if failed else [],
                "next_step": "assign a real model to this role for genuine task execution",
                "confidence": 0.5,
            }
            return LLMResponse(
                tool_calls=[{"id": "echo_report", "type": "function", "function": {"name": "agent_report", "arguments": json.dumps(arguments)}}],
                usage=Usage(prompt_tokens=8, completion_tokens=8),
                model=request.model,
                provider=self.provider.name,
                latency=time.time() - started,
                finish_reason="tool_calls",
            )
        last = ""
        for msg in reversed(request.messages):
            if msg.role == "user":
                last = msg.content
                break
        tool_calls: list[dict[str, Any]] = []
        text = ""
        if "use:" in last and request.tools:
            payload = last.split("use:", 1)[1].strip()
            segments = payload.split(" ", 1)
            name = segments[0].strip()
            raw_args = segments[1].strip() if len(segments) > 1 else "{}"
            if any(t.name == name for t in request.tools):
                tool_calls = [
                    {"id": f"echo_{name}", "type": "function", "function": {"name": name, "arguments": raw_args}}
                ]
            else:
                text = f"no such tool: {name}"
        elif "do:" in last and request.tools:
            tool = request.tools[0]
            tool_calls = [
                {"id": f"echo_{tool.name}", "type": "function", "function": {"name": tool.name, "arguments": "{}"}}
            ]
        else:
            text = f"[echo:{request.model}] acknowledged: {last[:400]}"
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            usage=Usage(prompt_tokens=max(1, len(last) // 4), completion_tokens=max(1, len(text) // 4)),
            model=request.model,
            provider=self.provider.name,
            latency=time.time() - started,
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    def stream_call(self, request: LLMRequest) -> Iterator[str]:
        for word in self.call(request).text.split(" "):
            yield word + " "

    def parse(self, data: dict[str, Any], model: str) -> LLMResponse:  # pragma: no cover - not used
        return LLMResponse(text=str(data))


ADAPTERS: dict[str, type[ProviderAdapter]] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "gemini": GeminiAdapter,
    "anthropic": AnthropicAdapter,
    "echo": EchoAdapter,
}


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


class ProviderRegistry:
    """Builds adapters from config, resolves keys, and reports health."""

    def __init__(
        self,
        providers: Iterable[ProviderConfig],
        secrets: Any = None,
        bus: EventBus | None = None,
        transport: Transport | None = None,
    ):
        self.secrets = secrets
        self.bus = bus or EventBus()
        self.transport = transport
        self.providers: dict[str, ProviderConfig] = {}
        self.adapters: dict[str, ProviderAdapter] = {}
        for provider in providers:
            self.add(provider)

    def add(self, provider: ProviderConfig) -> ProviderAdapter:
        adapter_cls = ADAPTERS.get(provider.kind)
        if adapter_cls is None:
            raise ValueError(f"unknown provider kind: {provider.kind!r} (have {sorted(ADAPTERS)})")
        key = ""
        if self.secrets is not None:
            key = self.secrets.get(provider.api_key_env, provider.api_key_ref) or ""
        adapter = adapter_cls(provider, api_key=key, transport=self.transport)
        self.providers[provider.name] = provider
        self.adapters[provider.name] = adapter
        return adapter

    def get(self, provider_name: str) -> ProviderAdapter | None:
        return self.adapters.get(provider_name)

    def for_model(self, qualified: str) -> tuple[ProviderAdapter, str]:
        if ":" not in qualified:
            raise ValueError(f"model must be 'provider:model', got {qualified!r}")
        provider_name, model_id = qualified.split(":", 1)
        adapter = self.adapters.get(provider_name)
        if adapter is None:
            raise KeyError(f"provider not configured: {provider_name}")
        return adapter, model_id

    def ready(self, provider_name: str) -> bool:
        """Whether the provider can actually be called right now.

        Three cases, and the distinction matters in real deployments:
          * ``echo`` — the offline stub, always available;
          * a provider that declares **no** key at all — a local server, a llama.cpp or LM Studio
            endpoint on localhost — is keyless by design and therefore usable;
          * a provider that *declares* a key but does not have one — not usable, and the Steward
            must not pick it (that is the trap this check exists to prevent).
        """
        provider = self.providers.get(provider_name)
        if provider is None:
            return False
        if provider.kind == "echo":
            return True
        if not provider.enabled:
            return False
        if not provider.api_key_env and not provider.api_key_ref:
            return True
        return bool(self.adapters[provider_name].api_key)

    def inventory(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name, provider in self.providers.items():
            for model in provider.models:
                out.append(
                    {
                        "provider": name,
                        "kind": provider.kind,
                        "model": model.model_id,
                        "qualified": model.qualified,
                        "ready": self.ready(name),
                        "rpm": model.rpm,
                        "rpd": model.rpd,
                        "tpm": model.tpm,
                        "tags": model.tags,
                        "audio_live": model.supports_audio_live,
                    }
                )
        return out

    # -- calls -------------------------------------------------------------------------
    def complete(self, qualified: str, request: LLMRequest, agent_id: str = "") -> LLMResponse:
        adapter, model_id = self.for_model(qualified)
        request.model = model_id
        self.bus.emit(
            MODEL_CALL,
            source=agent_id or "runtime",
            provider=adapter.provider.name,
            model=model_id,
            messages=len(request.messages),
            tools=len(request.tools),
        )
        response = adapter.call(request)
        response.provider = adapter.provider.name
        response.model = response.model or model_id
        return response

    def stream(self, qualified: str, request: LLMRequest, agent_id: str = "") -> Iterator[str]:
        adapter, model_id = self.for_model(qualified)
        request.model = model_id
        request.stream = True
        yield from adapter.stream_call(request)
