from app.services.ai.models import ChatMessage


class RollingContextWindow:
    """Provider-neutral, approximate rolling context truncation.

    Four characters per token is a conservative baseline, not a tokenizer. Replace
    `estimate_tokens` with a model-specific tokenizer when exact accounting matters.
    """

    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens

    @staticmethod
    def estimate_tokens(message: ChatMessage) -> int:
        return max(1, (len(message.content) + 3) // 4) + 4

    def fit(self, messages: list[ChatMessage], reserve_tokens: int) -> list[ChatMessage]:
        budget = max(1, self.max_tokens - reserve_tokens)
        system = [message for message in messages if message.role == "system"]
        conversation = [message for message in messages if message.role != "system"]

        selected: list[ChatMessage] = []
        used = sum(self.estimate_tokens(message) for message in system)
        for message in reversed(conversation):
            cost = self.estimate_tokens(message)
            if used + cost > budget:
                break
            selected.append(message)
            used += cost

        # Preserve system instructions and the newest turns in chronological order.
        return [*system, *reversed(selected)]
