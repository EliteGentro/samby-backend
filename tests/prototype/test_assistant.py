from fastapi.testclient import TestClient

from app.prototype.main import create_app
from app.services.assistant.knowledge import retrieve_knowledge
from app.services.assistant.metrics import calculate_workspace_metrics


class FakeGuide:
    async def answer(self, workspace, page, history):
        assert workspace["profile"]["name"] == "Test business"
        assert page == "inventory"
        assert history[-1]["content"] == "Summarize this page"
        return "You have 10 pieces on hand.", [{"id": "workspace-metrics", "title": "Current workspace metrics"}]


def test_metrics_and_knowledge_are_grounded(workspace):
    metrics = calculate_workspace_metrics(workspace, "inventory")
    assert metrics["inventory"]["on_hand_by_unit"] == {"pieces": 10.0}
    assert metrics["inventory"]["inventory_value_by_currency"] == {"MXN": 20.0}
    assert metrics["sales"]["revenue_by_currency"] == {"MXN": 15.0}
    assert any(chunk.id == "inventory-basics" for chunk in retrieve_knowledge("available stock", "inventory"))


def test_assistant_session_is_authorized_and_persists(tmp_path, workspace):
    database = tmp_path / "assistant.sqlite3"
    guest_key = "assistant-test-guest-key-1234567890"
    headers = {"X-Workspace-Key": guest_key}

    with TestClient(create_app(database, start_worker=False)) as client:
        created_workspace = client.post("/api/prototype/workspaces/guest", json={"workspace": workspace}, headers=headers)
        assert created_workspace.status_code == 201
        client.app.state.assistant_service = FakeGuide()

        unauthorized = client.get(f"/api/prototype/workspaces/{workspace['id']}/assistant/sessions")
        assert unauthorized.status_code == 401

        created = client.post(
            f"/api/prototype/workspaces/{workspace['id']}/assistant/sessions",
            json={"page": "inventory", "title": "New conversation"}, headers=headers,
        )
        assert created.status_code == 201
        session_id = created.json()["id"]

        answered = client.post(
            f"/api/prototype/workspaces/{workspace['id']}/assistant/sessions/{session_id}/messages",
            json={"page": "inventory", "content": "Summarize this page"}, headers=headers,
        )
        assert answered.status_code == 200
        assert [message["role"] for message in answered.json()["messages"]] == ["user", "assistant"]
        assert answered.json()["messages"][-1]["sources"][0]["id"] == "workspace-metrics"

    with TestClient(create_app(database, start_worker=False)) as client:
        loaded = client.get(
            f"/api/prototype/workspaces/{workspace['id']}/assistant/sessions/{session_id}", headers=headers
        )
        assert loaded.status_code == 200
        assert loaded.json()["messages"][-1]["content"] == "You have 10 pieces on hand."
