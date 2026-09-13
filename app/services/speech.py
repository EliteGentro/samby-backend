import html
import re

import httpx

from app.core.config import Settings


class ElevenLabsSpeechService:
    """Server-side ElevenLabs adapter for saved Samby Guide answers."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=5)
        )
        self._owns_client = client is None

    @staticmethod
    def spoken_text(markdown: str) -> str:
        """Turn common response Markdown into natural speech input."""
        text = re.sub(r"!\[([^]]*)\]\([^)]*\)", r"\1", markdown)
        text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"```(?:[^\n]*)\n?(.*?)```", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}|>|[-+*]|\d+[.)])\s*", "", text)
        text = re.sub(r"[*_~]", "", text)
        text = re.sub(r"https?://\S+", "", text)
        text = text.replace("|", ", ")
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    async def synthesize(self, markdown: str) -> bytes:
        if not self.settings.eleven_labs_api_key:
            raise RuntimeError("ELEVEN_LABS_API_KEY is not configured")
        text = self.spoken_text(markdown)
        if not text:
            raise ValueError("The assistant answer has no speakable text")
        response = await self.client.post(
            (
                f"{self.settings.eleven_labs_base_url.rstrip('/')}"
                f"/text-to-speech/{self.settings.eleven_labs_voice_id}"
            ),
            params={"output_format": "mp3_44100_128"},
            headers={
                "xi-api-key": self.settings.eleven_labs_api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": self.settings.eleven_labs_model,
            },
        )
        response.raise_for_status()
        if not response.content:
            raise RuntimeError("ElevenLabs returned empty audio")
        return response.content

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
