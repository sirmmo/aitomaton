"""aitomaton-laya — laya's Router behind one generic endpoint.

laya (convaiinnovations/laya) answers typed questions (choice, noul, ...) about a state in a single
non-autoregressive forward pass, with calibrated probabilities. aitomaton asks it two things per
issue: which action to take, and how likely the error is a code defect.

POST /predict  {"state": <str|dict>, "questions": {name: {"type", "instructions", "criteria"?}}}
           ->  laya's answer as is ({"answers": {...}, "routing": {...}}) plus "ms"
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from pydantic import BaseModel

from laya import Router

app = FastAPI(title="aitomaton-laya")
_router: Router | None = None


def _r() -> Router:
    global _router
    if _router is None:
        _router = Router()          # downloads/loads the checkpoint on first use
    return _router


@app.on_event("startup")
def _warm() -> None:
    _r().predict("warmup", {"ok": {"type": "noul", "instructions": "Is this a warmup?"}})


class PredictRequest(BaseModel):
    state: str | dict
    questions: dict[str, dict]


@app.post("/predict")
def predict(req: PredictRequest) -> dict:
    t0 = time.time()
    out = dict(_r().predict(req.state, req.questions))
    out["ms"] = round((time.time() - t0) * 1000, 1)
    return out


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
