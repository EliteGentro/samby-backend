import secrets

from fastapi.testclient import TestClient

from app.prototype.main import create_app


BASE = "/api/prototype"


class FakeGuide:
    async def answer(self, workspace: dict, page: str, history: list[dict]):
        assert workspace["profile"]["name"] == "Test business"
        assert page == "inventory"
        assert history[-1]["content"] == "What stock do I have?"
        return "You have 10 recorded units on hand.", [
            {"id": "workspace-metrics", "title": "Current workspace metrics"}
        ]


class FakeSpeech:
    def __init__(self, delegate):
        self.received = ""
        self.delegate = delegate

    async def synthesize(self, content: str):
        self.received = content
        return b"fake-mp3"

    async def close(self):
        await self.delegate.close()


def guest_headers(workspace: dict) -> dict:
    return {
        "X-Workspace-ID": workspace["id"],
        "X-Workspace-Key": secrets.token_urlsafe(32),
    }


def test_assistant_sessions_are_authorized_and_survive_app_restart(postgres_url, workspace):
    path = postgres_url
    headers = guest_headers(workspace)
    with TestClient(create_app(path, start_worker=False)) as client:
        created_workspace = client.post(
            f"{BASE}/workspaces/guest", json={"workspace": workspace}, headers=headers
        )
        assert created_workspace.status_code == 201, created_workspace.text
        client.app.state.assistant_service = FakeGuide()
        created = client.post(
            f"{BASE}/workspaces/{workspace['id']}/assistant/sessions",
            headers=headers,
            json={"page": "inventory", "title": "New conversation"},
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        response = client.post(
            f"{BASE}/workspaces/{workspace['id']}/assistant/sessions/{session_id}/messages",
            headers=headers,
            json={"page": "inventory", "content": "What stock do I have?"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["title"] == "What stock do I have?"
        assert [item["role"] for item in body["messages"]] == ["user", "assistant"]
        assert body["messages"][-1]["sources"][0]["id"] == "workspace-metrics"
        speech = FakeSpeech(client.app.state.speech_service)
        client.app.state.speech_service = speech
        spoken = client.post(
            f"{BASE}/workspaces/{workspace['id']}/assistant/sessions/{session_id}/messages/{body['messages'][-1]['id']}/speech",
            headers=headers,
        )
        assert spoken.status_code == 200, spoken.text
        assert spoken.content == b"fake-mp3"
        assert spoken.headers["content-type"] == "audio/mpeg"
        assert spoken.headers["cache-control"] == "private, no-store"
        assert speech.received == "You have 10 recorded units on hand."
        user_audio = client.post(
            f"{BASE}/workspaces/{workspace['id']}/assistant/sessions/{session_id}/messages/{body['messages'][0]['id']}/speech",
            headers=headers,
        )
        assert user_audio.status_code == 422
        assert client.get(
            f"{BASE}/workspaces/{workspace['id']}/assistant/sessions",
            headers={"X-Workspace-ID": workspace["id"]},
        ).status_code == 401

    with TestClient(create_app(path, start_worker=False)) as client:
        restored = client.get(
            f"{BASE}/workspaces/{workspace['id']}/assistant/sessions/{session_id}",
            headers=headers,
        )
        assert restored.status_code == 200
        assert restored.json()["messages"][-1]["content"] == "You have 10 recorded units on hand."
