from fastapi import APIRouter,Depends,HTTPException
import httpx
from app.core.config import AI_AGENT_URL,AI_AGENT_SECRET
from app.core.security import get_current_user

router=APIRouter(prefix="/api/ai",tags=["ai"])

@router.get("/status")
def status(user=Depends(get_current_user)):
    return {"configured":bool(AI_AGENT_URL),"mode":"external-module"}

@router.post("/analyze")
def analyze(payload:dict,user=Depends(get_current_user)):
    if not AI_AGENT_URL:raise HTTPException(503,"AI-модуль пока не подключён")
    headers={"X-Internal-Secret":AI_AGENT_SECRET} if AI_AGENT_SECRET else {}
    with httpx.Client(timeout=60) as client:
        r=client.post(AI_AGENT_URL.rstrip("/")+"/analyze",json={"user_role":user.role,"payload":payload},headers=headers)
        r.raise_for_status();return r.json()
