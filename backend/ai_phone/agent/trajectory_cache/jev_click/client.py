"""Decisions transports, deliberately separate from PHONE_VLM and AUX chat."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


class JevError(RuntimeError):
    """Safe diagnostic: never contains an upstream response body or credential."""


@dataclass(frozen=True)
class JevConfig:
    provider: str
    url: str
    model: str
    key: str = field(repr=False)
    timeout: float

    @classmethod
    def from_settings(cls, settings):
        provider = str(settings.jev_provider).strip().lower()
        defaults = {
            "openrouter": ("https://openrouter.ai/api/alpha/decisions", "openai/gpt-6-luna-decisions"),
            "openai": ("https://api.openai.com/v1/decisions", "gpt-6-luna"),
        }
        if provider not in defaults:
            raise ValueError("AI_PHONE_JEV_PROVIDER must be openrouter or openai")
        if not str(settings.jev_api_key).strip():
            raise ValueError("AI_PHONE_JEV_API_KEY is required when Jev is enabled")
        url, model = defaults[provider]
        url = str(settings.jev_api_url or url).strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("AI_PHONE_JEV_API_URL must be a credential-free HTTPS Decisions endpoint")
        if not parsed.path.rstrip("/").endswith("/decisions"):
            raise ValueError("AI_PHONE_JEV_API_URL must end with /decisions, not a chat endpoint")
        model = str(settings.jev_model or model).strip()
        if not model:
            raise ValueError("AI_PHONE_JEV_MODEL must not be blank")
        return cls(
            provider,
            url,
            model,
            str(settings.jev_api_key).strip(),
            float(settings.jev_timeout_sec),
        )


class DecisionsClient:
    def __init__(self, config: JevConfig, *, transport=None):
        self.config = config
        self.http = httpx.AsyncClient(timeout=config.timeout, transport=transport)

    def payload(self, text: list[str], image: bytes, choices: dict[str, str]) -> dict:
        data_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        instructions = "只定位当前缓存点击目标，不规划下一步。不能确认时选择NOT_FOUND或UNCERTAIN。"
        if "WAIT" in choices:
            instructions += "仅画面明确可见加载迹象时可选WAIT；目标已可操作应选候选，不能因目标缺失或网络异常猜测加载。"
        if self.config.provider == "openrouter":
            return {
                "model": self.config.model,
                "state": [*text, {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}],
                "questions": {"node": {"type": "choice", "instructions": instructions, "criteria": choices}},
            }
        return {
            "model": self.config.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        *({"type": "input_text", "text": t} for t in text),
                        {"type": "input_image", "image_url": data_url, "detail": "high"},
                    ],
                }
            ],
            "questions": [
                {
                    "name": "node",
                    "type": "choice",
                    "instructions": instructions,
                    "choices": [{"value": k, "description": v} for k, v in choices.items()],
                }
            ],
        }

    async def choose(self, text: list[str], image: bytes, choices: dict[str, str]) -> dict:
        # No automatic model/channel fallback, including on HTTP errors.
        try:
            response = await self.http.post(
                self.config.url,
                json=self.payload(text, image, choices),
                headers={"Authorization": f"Bearer {self.config.key}"},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise JevError(f"Decisions HTTP {exc.response.status_code}") from None
        except Exception as exc:
            raise JevError(f"Decisions transport {type(exc).__name__}") from None
        try:
            answers = data["answers"]
            if self.config.provider == "openrouter":
                answer = answers["node"]
            else:
                matches = [a for a in answers if a.get("name") == "node"]
                if len(matches) != 1:
                    raise ValueError("missing/duplicate node answer")
                answer = matches[0]
            selected = answer.get("choice")
            if answer.get("type") not in (None, "choice") or selected not in choices:
                raise ValueError("invalid/refused choice")
            return {
                "choice": selected,
                "confidence": answer.get("confidence"),
                "probabilities": answer.get("probabilities"),
                "usage": data.get("usage", {}),
            }
        except (KeyError, TypeError, ValueError, AttributeError):
            raise JevError("Decisions invalid or refused node answer") from None

    async def aclose(self):
        await self.http.aclose()
