import json
from uuid import uuid4

from app.prototype.store import ResourceError, Store, dump, now


class AssistantRepository:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _session(row, messages: list[dict] | None = None) -> dict:
        result = {key: row[key] for key in ("id", "title", "page", "created_at", "updated_at")}
        if messages is not None:
            result["messages"] = messages
        return result

    @staticmethod
    def _message(row) -> dict:
        return {
            "id": row["id"],
            "role": row["role"],
            "content": row["content"],
            "page": row["page"],
            "sources": json.loads(row["sources"]),
            "created_at": row["created_at"],
        }

    def list_sessions(self, workspace_id: str, actor: str) -> list[dict]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM assistant_sessions WHERE workspace_id=? AND actor=? "
                "ORDER BY updated_at DESC,id DESC LIMIT 50",
                (workspace_id, actor),
            ).fetchall()
        return [self._session(row) for row in rows]

    def create_session(self, workspace_id: str, actor: str, page: str, title: str) -> dict:
        session_id, stamp = str(uuid4()), now()
        clean_title = title.strip()[:100] or "New conversation"
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO assistant_sessions(id,workspace_id,actor,title,page,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (session_id, workspace_id, actor, clean_title, page, stamp, stamp),
            )
            row = db.execute("SELECT * FROM assistant_sessions WHERE id=?", (session_id,)).fetchone()
        return self._session(row, [])

    def get_session(self, workspace_id: str, actor: str, session_id: str) -> dict:
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM assistant_sessions WHERE id=? AND workspace_id=? AND actor=?",
                (session_id, workspace_id, actor),
            ).fetchone()
            if row is None:
                raise ResourceError(404, "Guide conversation was not found in this workspace.")
            messages = db.execute(
                "SELECT * FROM assistant_messages WHERE session_id=? ORDER BY created_at,id",
                (session_id,),
            ).fetchall()
        return self._session(row, [self._message(message) for message in messages])

    def add_message(
        self,
        workspace_id: str,
        actor: str,
        session_id: str,
        role: str,
        content: str,
        page: str,
        sources: list[dict] | None = None,
    ) -> dict:
        message_id, stamp = str(uuid4()), now()
        with self.store.transaction() as db:
            session = db.execute(
                "SELECT * FROM assistant_sessions WHERE id=? AND workspace_id=? AND actor=?",
                (session_id, workspace_id, actor),
            ).fetchone()
            if session is None:
                raise ResourceError(404, "Guide conversation was not found in this workspace.")
            db.execute(
                "INSERT INTO assistant_messages(id,session_id,role,content,page,sources,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (message_id, session_id, role, content, page, dump(sources or []), stamp),
            )
            title = session["title"]
            if role == "user" and title == "New conversation":
                title = content.strip().replace("\n", " ")[:60] or title
            db.execute(
                "UPDATE assistant_sessions SET title=?,page=?,updated_at=? WHERE id=?",
                (title, page, stamp, session_id),
            )
        return self.get_session(workspace_id, actor, session_id)
