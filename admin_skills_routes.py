"""
Новые admin-эндпоинты: аналитика, каталог навыков, BANT/эскалация по сессии.
Подключаются из main.py через include_router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from config import settings
from memory.dialogue_memory import DialogueMemory
from skills import SKILLS_CATALOG


def _check_admin(x_admin_token: str = Header(default="")):
    if x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid admin token")


router = APIRouter(prefix="/admin/api", dependencies=[Depends(_check_admin)])
_memory = DialogueMemory()


@router.get("/skills")
async def list_skills():
    """Каталог навыков оркестратора для UI (группы, статусы, описания)."""
    groups: dict[str, list[dict]] = {}
    for s in SKILLS_CATALOG:
        groups.setdefault(s["group"], []).append(s)
    counts = {
        "active":      sum(1 for s in SKILLS_CATALOG if s["status"] == "active"),
        "beta":        sum(1 for s in SKILLS_CATALOG if s["status"] == "beta"),
        "coming_soon": sum(1 for s in SKILLS_CATALOG if s["status"] == "coming_soon"),
        "total":       len(SKILLS_CATALOG),
    }
    return {
        "counts": counts,
        "groups": [{"name": k, "skills": v} for k, v in groups.items()],
        "all":    SKILLS_CATALOG,
    }


@router.get("/stats")
async def stats():
    """Сводная аналитика: сессии, интенты, BANT-температура, эскалации."""
    return await _memory.get_stats()


@router.get("/sessions-enriched")
async def sessions_enriched():
    """Список сессий с metadata (BANT, escalated, nps) — для раздела Диалоги."""
    return await _memory.get_sessions_enriched()


@router.get("/session/{session_id}/meta")
async def session_meta(session_id: str):
    return await _memory.get_session_meta(session_id)


class EscalateBody(BaseModel):
    escalated: bool = True


@router.post("/session/{session_id}/escalate")
async def toggle_escalation(session_id: str, body: EscalateBody):
    await _memory.upsert_session_meta(session_id, escalated=body.escalated)
    return {"session_id": session_id, "escalated": body.escalated}


class NpsBody(BaseModel):
    score: int


@router.post("/session/{session_id}/nps")
async def set_nps(session_id: str, body: NpsBody):
    if not 0 <= body.score <= 10:
        raise HTTPException(status_code=400, detail="score must be 0..10")
    await _memory.upsert_session_meta(session_id, nps_score=body.score)
    return {"session_id": session_id, "nps_score": body.score}
