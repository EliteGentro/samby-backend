from typing import Literal

from pydantic import BaseModel, Field, model_validator

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class AIOptions(BaseModel):
    """Capabilities normalized across providers.

    An adapter may translate or reject unsupported options instead of leaking a
    provider-specific payload into route code.
    """

    model: str | None = None
    max_tokens: int = Field(default=800, ge=1, le=32768)
    temperature: float = Field(default=0.7, ge=0, le=2)
    show_thinking: bool = False
    reasoning_effort: ReasoningEffort | None = None
    reasoning_max_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def one_reasoning_control(self) -> "AIOptions":
        if self.reasoning_effort is not None and self.reasoning_max_tokens is not None:
            raise ValueError("Set reasoning_effort or reasoning_max_tokens, not both")
        return self


class AIChunk(BaseModel):
    type: Literal["content", "reasoning", "done"]
    text: str = ""


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    options: AIOptions = Field(default_factory=AIOptions)
