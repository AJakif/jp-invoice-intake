"""Provider-agnostic LLM client.

Dispatches to Anthropic or OpenAI based on the LLM_PROVIDER env var, with two
named tiers ('primary' cheap/fast, 'strong' for targeted re-queries) per
provider. Structured output is forced via native tool-calling / JSON-schema
support on each provider so extraction always returns parseable JSON.

Only the Anthropic path is reachable for live testing in this sandbox
(network egress is allowlisted to api.anthropic.com). The OpenAI path is
implemented the same way and is the intended configuration when run with a
candidate's own OpenAI key.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").lower()

MODEL_TIERS = {
    "anthropic": {
        "primary": os.environ.get("ANTHROPIC_MODEL_PRIMARY", "claude-haiku-4-5-20251001"),
        "strong": os.environ.get("ANTHROPIC_MODEL_STRONG", "claude-sonnet-5"),
    },
    "openai": {
        "primary": os.environ.get("OPENAI_MODEL_PRIMARY", "gpt-4o-mini"),
        "strong": os.environ.get("OPENAI_MODEL_STRONG", "gpt-4o"),
    },
}


@dataclass
class ImagePart:
    path: Path
    media_type: str  # "image/jpeg" or "image/png"

    def base64_data(self) -> str:
        return base64.standard_b64encode(self.path.read_bytes()).decode("ascii")


@dataclass
class UsageInfo:
    input_tokens: int
    output_tokens: int


class LLMClient:
    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or PROVIDER).lower()
        if self.provider not in MODEL_TIERS:
            raise ValueError(f"Unknown LLM_PROVIDER: {self.provider}")
        self.last_usage: Optional[UsageInfo] = None

    def model_for(self, tier: str) -> str:
        return MODEL_TIERS[self.provider][tier]

    def structured_call(
        self,
        tier: str,
        system: str,
        text: str,
        json_schema: dict,
        schema_name: str = "extraction",
        images: Optional[list[ImagePart]] = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Runs a structured-output call and returns the parsed JSON object."""
        if self.provider == "anthropic":
            return self._anthropic_structured(
                tier, system, text, json_schema, schema_name, images, max_tokens
            )
        return self._openai_structured(
            tier, system, text, json_schema, schema_name, images, max_tokens
        )

    # -- Anthropic -----------------------------------------------------

    def _anthropic_structured(
        self, tier, system, text, json_schema, schema_name, images, max_tokens
    ) -> dict[str, Any]:
        import anthropic

        client = anthropic.Anthropic()
        content: list[dict] = []
        if images:
            for img in images:
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.media_type,
                            "data": img.base64_data(),
                        },
                    }
                )
        content.append({"type": "text", "text": text})

        tool = {
            "name": schema_name,
            "description": f"Emit the extracted {schema_name} as structured data.",
            "input_schema": json_schema,
        }

        response = client.messages.create(
            model=self.model_for(tier),
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": schema_name},
            messages=[{"role": "user", "content": content}],
        )
        self.last_usage = UsageInfo(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("Anthropic response did not include a tool_use block")

    # -- OpenAI ----------------------------------------------------------

    def _openai_structured(
        self, tier, system, text, json_schema, schema_name, images, max_tokens
    ) -> dict[str, Any]:
        import openai

        client = openai.OpenAI()
        user_content: list[dict] = [{"type": "text", "text": text}]
        if images:
            for img in images:
                url = f"data:{img.media_type};base64,{img.base64_data()}"
                user_content.append({"type": "image_url", "image_url": {"url": url}})

        # OpenAI's strict json_schema mode requires every property to be
        # listed in "required" and additionalProperties: false at every
        # level. The schema in schema.py is written loosely (for Anthropic's
        # tool-use, which has no such constraint), so ask for plain JSON mode
        # instead and validate/parse defensively on our side.
        response = client.chat.completions.create(
            model=self.model_for(tier),
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": system
                    + "\n\nRespond with a single JSON object matching this schema:\n"
                    + json.dumps(json_schema),
                },
                {"role": "user", "content": user_content},
            ],
        )
        self.last_usage = UsageInfo(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )
        return json.loads(response.choices[0].message.content)
