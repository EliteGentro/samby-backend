import json
from uuid import uuid4

from app.prototype.store import ResourceError, Store, dump, now


class AssistantRepository:
    def __init__(self, store: Store):
        self.store = store

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

    def _session(self, db, workspace_id: str, actor: str, session_id: str, with_messages: bool = False) -> dict:
        row = db.execute(
            "SELECT * FROM assistant_sessions WHERE id=? AND workspace_id=? AND actor=?",
            (session_id, workspace_id, actor),
        ).fetchone()
        if row is None:
            raise ResourceError(404, "Assistant conversation was not found in this workspace.")
        result = {
            "id": row["id"], "title": row["title"], "page": row["page"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        if with_messages:
            messages = db.execute(
                "SELECT * FROM assistant_messages WHERE session_id=? ORDER BY created_at,id",
                (session_id,),
            ).fetchall()
            result["messages"] = [self._message(message) for message in messages]
        return result

    def create(self, workspace_id: str, actor: str, title: str, page: str) -> dict:
        session_id, stamp = str(uuid4()), now()
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO assistant_sessions(id,workspace_id,actor,title,page,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (session_id, workspace_id, actor, title, page, stamp, stamp),
            )
            return self._session(db, workspace_id, actor, session_id, True)

    def list(self, workspace_id: str, actor: str) -> list[dict]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id FROM assistant_sessions WHERE workspace_id=? AND actor=? ORDER BY updated_at DESC,id DESC LIMIT 50",
                (workspace_id, actor),
            ).fetchall()
            return [self._session(db, workspace_id, actor, row["id"]) for row in rows]

    def get(self, workspace_id: str, actor: str, session_id: str) -> dict:
        with self.store.connection() as db:
            return self._session(db, workspace_id, actor, session_id, True)

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
            session = self._session(db, workspace_id, actor, session_id)
            db.execute(
                "INSERT INTO assistant_messages(id,session_id,role,content,page,sources,created_at) VALUES(?,?,?,?,?,?,?)",
                (message_id, session_id, role, content, page, dump(sources or []), stamp),
            )
            title = session["title"]
            if role == "user" and title == "New conversation":
                title = " ".join(content.split())[:64]
            db.execute(
                "UPDATE assistant_sessions SET title=?,page=?,updated_at=? WHERE id=?",
                (title or "New conversation", page, stamp, session_id),
            )
            row = db.execute("SELECT * FROM assistant_messages WHERE id=?", (message_id,)).fetchone()
            return self._message(row)
