import json

import httpx
import pytest

from app.core.config import Settings
from app.services.speech import ElevenLabsSpeechService


@pytest.mark.asyncio
async def test_eleven_labs_synthesizes_clean_speech_without_exposing_key():
    captured = {}

    async def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(200, content=b"mp3-audio")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        database_url="postgresql://app:app@localhost/app",
        eleven_labs_api_key="secret-key",
        eleven_labs_voice_id="voice-123",
    )
    service = ElevenLabsSpeechService(settings, client)

    audio = await service.synthesize(
        "## Inventory\n\nYou have **10 units**. [Review details](https://example.com)."
    )

    request = captured["request"]
    assert audio == b"mp3-audio"
    assert request.url.path == "/v1/text-to-speech/voice-123"
    assert request.url.params["output_format"] == "mp3_44100_128"
    assert request.headers["xi-api-key"] == "secret-key"
    assert request.headers["accept"] == "audio/mpeg"
    assert json.loads(request.content) == {
        "text": "Inventory You have 10 units. Review details.",
        "model_id": "eleven_flash_v2_5",
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_eleven_labs_requires_server_side_key():
    settings = Settings(
        database_url="postgresql://app:app@localhost/app",
        eleven_labs_api_key="",
    )
    service = ElevenLabsSpeechService(settings)
    with pytest.raises(RuntimeError, match="ELEVEN_LABS_API_KEY"):
        await service.synthesize("Hello")
    await service.close()
