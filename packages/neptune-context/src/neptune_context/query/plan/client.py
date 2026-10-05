"""The model seam (ADR 0005 §4): an injected client protocol, a replay client and the live one.

``ModelClient.complete`` turns one ``ModelRequest`` into one ``ModelResponse`` or raises
``ModelUnavailable``; the planner calls it once per plan and never again for the same question.
``ReplayClient`` answers from recorded responses keyed by the request's SHA-256 and refuses an
unrecorded request (CI never reaches a network). ``AnthropicClient`` is the live implementation
and imports the SDK only when built, so the package works without the optional extra.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from neptune.identity import canonical_json

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping
    from pathlib import Path

    from neptune.model.jsonvalue import JsonObject

DEFAULT_MODEL: Final = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS: Final = 4096
# Why generation stopped, as the planner needs it: a normal end, a cut, a refusal, or anything else.
Stop = Literal["end", "max_tokens", "refusal", "other"]
RECORDED_BY = ("live", "synthetic")


class ModelUnavailable(Exception):  # noqa: N818  (a domain condition, not an error class)
    """The model could not be asked or did not answer (network, auth, quota, no recording)."""


class RecordingMissing(ModelUnavailable):
    """Replay has no recorded response for this request: re-record, never guess."""


@dataclass(frozen=True)
class ModelRequest:
    """Everything that determines a response: the model, both prompts, the output schema, a cap."""

    model: str
    system: str
    user: str
    schema: JsonObject
    max_tokens: int = DEFAULT_MAX_TOKENS

    def to_json(self) -> JsonObject:
        return {
            "max_tokens": self.max_tokens,
            "model": self.model,
            "schema": self.schema,
            "system": self.system,
            "user": self.user,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_json.dumps(self.to_json())).hexdigest()


@dataclass(frozen=True)
class ModelResponse:
    """What came back: the text (``None`` when there is none), why it stopped and who answered."""

    text: str | None
    stop: Stop
    model: str


@runtime_checkable
class ModelClient(Protocol):
    @property
    def client_id(self) -> str:
        """A stable name for this implementation (``anthropic``, ``replay``), kept in lineage."""
        ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True)
class Recording:
    """One recorded exchange. ``recorded_by`` says whether a live model produced the text."""

    request_sha256: str
    model: str
    stop: Stop
    text: str | None
    recorded_by: Literal["live", "synthetic"]

    def to_json(self) -> JsonObject:
        """No ``null`` (canonical JSON has none): a response without text omits ``text``."""
        out: JsonObject = {
            "model": self.model,
            "recorded_by": self.recorded_by,
            "request_sha256": self.request_sha256,
            "stop": self.stop,
        }
        if self.text is not None:
            out["text"] = self.text
        return out

    @staticmethod
    def from_json(value: Any) -> Recording:
        if not isinstance(value, dict):
            raise ValueError("a recording is a JSON object")
        sha, model, stop, text, by = (
            value.get(k) for k in ("request_sha256", "model", "stop", "text", "recorded_by")
        )
        if not (isinstance(sha, str) and len(sha) == 64 and isinstance(model, str)):
            raise ValueError("a recording names its request_sha256 and model")
        if stop not in ("end", "max_tokens", "refusal", "other"):
            raise ValueError(f"a recording's stop is end, max_tokens, refusal or other: {stop!r}")
        if not (text is None or isinstance(text, str)) or by not in RECORDED_BY:
            raise ValueError("a recording has text (or null) and recorded_by live or synthetic")
        return Recording(sha, model, stop, text, by)


def dump_recordings(recordings: Iterable[Recording]) -> str:
    """JSON Lines, one recording per line, ordered by request hash: byte-stable."""
    ordered = sorted(recordings, key=lambda r: r.request_sha256)
    return "".join(canonical_json.dumps(r.to_json()).decode() + "\n" for r in ordered)


def load_recordings(path: Path) -> dict[str, Recording]:
    out: dict[str, Recording] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            recording = Recording.from_json(json.loads(line))
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"{path.name}:{number}: {error}") from error
        if recording.request_sha256 in out:
            raise ValueError(f"{path.name}:{number}: request recorded twice")
        out[recording.request_sha256] = recording
    return out


class ReplayClient:
    """Answers from recordings; an unrecorded request is ``RecordingMissing``, never a guess."""

    client_id: Final = "replay"

    def __init__(self, recordings: Mapping[str, Recording]) -> None:
        self._recordings = dict(recordings)

    def complete(self, request: ModelRequest) -> ModelResponse:
        recording = self._recordings.get(request.sha256)
        if recording is None:
            raise RecordingMissing(
                f"no recording for request {request.sha256}; re-record with "
                "packages/neptune-context/scripts/record_planner_golden.py"
            )
        return ModelResponse(recording.text, recording.stop, recording.model)


class RecordingClient:
    """Wraps a live client and keeps every exchange as a ``Recording`` (the re-record script)."""

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self.recordings: list[Recording] = []

    @property
    def client_id(self) -> str:
        return self._inner.client_id

    def complete(self, request: ModelRequest) -> ModelResponse:
        response = self._inner.complete(request)
        self.recordings.append(
            Recording(request.sha256, response.model, response.stop, response.text, "live")
        )
        return response

    def __iter__(self) -> Iterator[Recording]:
        return iter(self.recordings)


_STOPS: Final[dict[str, Stop]] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def anthropic_arguments(request: ModelRequest) -> dict[str, Any]:
    """The Messages API arguments for ``request``: the schema is the output contract.

    Sampling parameters are left out (the model rejects non-default values), thinking is adaptive
    at ``medium`` effort, and no refusal fallback is configured: a refusal is a visible
    ``MODEL_REFUSED`` failure and the lineage names the one model that answered.
    """
    return {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": request.system,
        "messages": [{"role": "user", "content": request.user}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": request.schema},
        },
    }


class AnthropicClient:
    """The default live client: the Anthropic SDK with schema-constrained output.

    Needs the ``anthropic`` extra (``pip install neptune-context[anthropic]``) and credentials
    from the environment (``ANTHROPIC_API_KEY`` or an ``ant auth login`` profile).
    """

    client_id: Final = "anthropic"

    def __init__(self, sdk_client: Any = None) -> None:
        self._errors: tuple[type[Exception], ...] = ()
        try:
            import anthropic
        except ImportError as error:
            if sdk_client is None:
                raise ModelUnavailable(
                    "the anthropic package is not installed: pip install neptune-context[anthropic]"
                ) from error
        else:
            self._errors = (anthropic.APIError,)
            if sdk_client is None:
                sdk_client = anthropic.Anthropic()
        self._sdk = sdk_client

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            message = self._sdk.messages.create(**anthropic_arguments(request))
        except self._errors as error:
            raise ModelUnavailable(f"{type(error).__name__}: {error}") from error
        stop = _STOPS.get(str(message.stop_reason), "other")
        texts = [block.text for block in message.content if block.type == "text"]
        return ModelResponse("".join(texts) if texts else None, stop, str(message.model))
