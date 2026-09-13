import json
from typing import Any

import httpx

from app.services.ai.models import AIOptions, ChatMessage
from app.services.ai.openrouter import OpenRouterProvider
from app.services.context_window import RollingContextWindow

from .knowledge import PAGE_NAMES, retrieve_knowledge
from .metrics import calculate_workspace_metrics, search_workspace_records


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate_workspace_metrics",
            "description": "Calculate exact, deterministic metrics from the current workspace. Use instead of doing arithmetic yourself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "groups": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["inventory", "finance", "sales", "data_quality"]},
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_workspace_records",
            "description": "Look up bounded records in the current client's assets, inventory, sales, purchases or finance data.",
            "parameters": {
                "type": "object",
                "required": ["collection"],
                "properties": {
                    "collection": {
                        "type": "string",
                        "enum": ["products", "suppliers", "sales", "purchases", "finance", "commitments", "stock"],
                    },
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_samby_knowledge",
            "description": "Retrieve relevant documentation about how Samby interprets a page, field or metric.",
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
        },
    },
]


class AssistantService:
    def __init__(self, provider: OpenRouterProvider, context_max_tokens: int = 12000):
        self.provider = provider
        self.context = RollingContextWindow(context_max_tokens)

    @staticmethod
    def _system(page: str) -> str:
        return f"""You are Samby Guide, a concise and friendly contextual guide inside a business planning application.
The user is working on {PAGE_NAMES.get(page, page)}. Explain what the page asks for, interpret only supplied workspace facts, and suggest a practical next step.
Never invent records, exchange rates, totals or forecast certainty. Keep currencies and quantity units separate. Treat unknown coverage as unknown, never as zero.
Use the deterministic metric tool for arithmetic and record search for specific entities. Make clear when a statement comes from workspace data versus Samby guidance.
Answer in the user's language. Prefer a short answer with readable paragraphs or bullets; do not describe yourself as a generic chatbot."""

    def _run_tool(self, name: str, arguments: dict[str, Any], workspace: dict, page: str) -> tuple[Any, list[dict]]:
        if name == "calculate_workspace_metrics":
            return calculate_workspace_metrics(workspace, page, arguments.get("groups")), [
                {"id": "workspace-metrics", "title": "Current workspace metrics"}
            ]
        if name == "search_workspace_records":
            collection = str(arguments.get("collection", ""))
            records = search_workspace_records(
                workspace, collection, str(arguments.get("query", "")), int(arguments.get("limit", 8))
            )
            return records, [{"id": f"workspace-{collection}", "title": f"Current workspace: {collection}"}]
        if name == "retrieve_samby_knowledge":
            chunks = retrieve_knowledge(str(arguments.get("query", "")), page)
            return [
                {"id": chunk.id, "title": chunk.title, "content": chunk.content}
                for chunk in chunks
            ], [{"id": chunk.id, "title": chunk.title} for chunk in chunks]
        return {"error": "Unknown tool"}, []

    async def answer(self, workspace: dict, page: str, history: list[dict]) -> tuple[str, list[dict]]:
        latest = next((item["content"] for item in reversed(history) if item["role"] == "user"), "")
        chunks = retrieve_knowledge(latest, page)
        baseline = calculate_workspace_metrics(workspace, page)
        grounding = {
            "page": PAGE_NAMES.get(page, page),
            "business": workspace.get("profile", {}).get("name"),
            "workspace_mode": workspace.get("mode"),
            "deterministic_page_metrics": baseline,
            "retrieved_guidance": [
                {"id": chunk.id, "title": chunk.title, "content": chunk.content} for chunk in chunks
            ],
        }
        sources = [{"id": chunk.id, "title": chunk.title} for chunk in chunks]
        sources.append({"id": "workspace-metrics", "title": "Current workspace metrics"})

        conversation = [
            ChatMessage(role="system", content=self._system(page)),
            ChatMessage(role="system", content="Grounding context:\n" + json.dumps(grounding, ensure_ascii=False)),
            *[
                ChatMessage(role=item["role"], content=item["content"])
                for item in history
                if item.get("role") in {"user", "assistant"} and item.get("content")
            ],
        ]
        fitted = self.context.fit(conversation, reserve_tokens=1800)
        raw_messages = [message.model_dump() for message in fitted]
        options = AIOptions(max_tokens=900, temperature=0.25)

        try:
            response = await self.provider.complete_chat(raw_messages, options, TOOLS)
        except httpx.HTTPStatusError as error:
            # Some OpenRouter models do not expose tool calling. The baseline still
            # supplies deterministic metrics, so keep the guide available.
            if error.response.status_code not in {400, 404, 422}:
                raise
            response = await self.provider.complete_chat(raw_messages, options)

        tool_calls = response.get("tool_calls") or []
        if tool_calls:
            raw_messages.append(response)
            for call in tool_calls[:6]:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result, tool_sources = self._run_tool(function.get("name", ""), arguments, workspace, page)
                for source in tool_sources:
                    if source not in sources:
                        sources.append(source)
                raw_messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": function.get("name"),
                    "content": json.dumps(result, ensure_ascii=False),
                })
            response = await self.provider.complete_chat(raw_messages, options)

        content = response.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenRouter returned an empty guide answer")
        return content.strip(), sources
