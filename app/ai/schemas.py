from typing import List, Optional, Any, Dict
from pydantic import BaseModel


class RefineRequest(BaseModel):
    text: str
    audience: str
    mode: str  # "structure" | "improve"


class RefineResponse(BaseModel):
    text: str
    notes: List[str]


class DeliveryScoreDetails(BaseModel):
    total: float
    fillers: float
    pace: float
    gaze: float
    pauses: float
    timing: float


class DeliveryResponse(BaseModel):
    transcript: str
    scores: DeliveryScoreDetails
    metrics: Dict[str, Any]
    events: List[Dict[str, Any]]
    tips: List[str]


class JurorQuestion(BaseModel):
    id: str
    juror: str
    text: str
    audio_url: Optional[str] = None


class JuryQuestionsResponse(BaseModel):
    questions: List[JurorQuestion]


class JuryAnswerResponse(BaseModel):
    score: float
    comment: str
