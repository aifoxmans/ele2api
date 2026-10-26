"""Credit evidence tied to this task, never inferred from account balances."""
from __future__ import annotations

import math
from typing import Any


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def input_evidence(assets: dict) -> dict:
    videos = assets.get("videos") or []
    durations = [number(item.get("duration")) for item in videos if isinstance(item, dict)]
    return {"reference_seconds": sum(durations) if len(durations) == len(videos) and all(
                item is not None for item in durations) else None,
            **{key + "_count": len(assets.get(plural) or [])
               for key, plural in (("image", "images"), ("video", "videos"), ("audio", "audios"))}}


def cost_values(response: dict) -> dict:
    evidence = dict(response.get("cost_evidence") or {})
    source = str(response.get("cost_source") or evidence.get("source") or "unknown")
    credits = number(response.get("credits_consumed"))
    if credits is None:
        credits = number(evidence.get("charged"))
    if credits is None and source in {"precharged", "quote"}:
        credits = number(evidence.get("quoted"))
    # actual_cost is accepted only when the protocol parser explicitly marks
    # a documented task receipt, never an account delta or a display estimate.
    if credits is None and source in {"actual", "precharged", "quote"}:
        credits = number(response.get("actual_cost"))
    if credits is None or source not in {"actual", "precharged", "quote"}:
        return {"cost_evidence": evidence} if evidence else {}
    evidence.update(source="quote" if source == "precharged" else source)
    if source == "actual":
        evidence["charged"] = credits
        return {"actual_cost": credits, "cost_evidence": evidence}
    evidence["quoted"] = credits
    return {"estimated_cost": credits, "cost_evidence": evidence}


def build_observations(task: dict[str, Any]) -> list[dict[str, Any]]:
    request = task.get("normalized") or {}
    evidence = request.get("_cost_evidence") or {}
    charged = number(evidence.get("charged"))
    quoted = number(evidence.get("quoted"))
    source = "actual" if charged is not None else str(evidence.get("source") or "unknown")
    if source not in {"actual", "precharged", "quote"}:
        source, charged, quoted = "unknown", None, None
    return [{"task_id": str(task.get("id") or ""),
             "attempt_id": str(task.get("run_id") or task.get("id") or ""),
             "upstream_task_id": str(task.get("run_id") or ""),
             "channel": "ele2api", "execution_source": "ele2api",
             "model": str(task.get("model") or request.get("model") or ""),
             "upstream_model": str(request.get("upstream_model") or ""),
             "duration": number(request.get("duration")),
             "requested_duration": number(request.get("duration")),
             "reference_seconds": number(evidence.get("reference_seconds")),
             "resolution": str(request.get("resolution") or "unknown"),
             "billing_context": {key + "_count": len(request.get(plural) or [])
                 for key, plural in (("image", "images"), ("video", "videos"), ("audio", "audios"))},
             "unit": "credits", "charged": charged, "quoted": quoted,
             "refunded": number(evidence.get("refunded")), "source": source,
             "status": str(task.get("status") or "unknown"),
             "observed_at": task.get("updated_at") or task.get("created_at") or 0}]
