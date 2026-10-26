"""Caller messages follow the operator's fixed normalization vocabulary."""
from __future__ import annotations


def public_error(code: str, message: str) -> dict[str, str]:
    raw = (message or "").lower()
    kind = (code or "UPSTREAM_FAILED").upper()
    public = "生成失败，积分已返还，请重试~"
    category = "upstream_error"
    if kind in {"UPSTREAM_SUBMIT_OUTCOME_UNKNOWN", "UPSTREAM_TIMEOUT"}:
        public = "提交结果未确认，请核对上游任务，暂勿重复提交~"
        category = "submission_unknown"
    elif kind == "QUEUE_CANCELLED":
        public = "队列排队服务中断，请稍后再试~"
        category = "provider_unavailable"
    elif "reference download failed:" in raw:
        # Source transport diagnostics may contain arbitrary host names. Classify
        # their validated reason before scanning upstream content-policy words.
        if "reference media exceeds" in raw:
            public = "素材超限，请修改后再试~"
        elif "wrong content type" in raw:
            public = "素材格式不支持，请修改后再试~"
        else:
            public = "素材下载失败，请检查素材链接后重试~"
        category = "invalid_request"
    elif any(value in raw for value in ("真人", "real person", "real-person")):
        public = "参考图片中检测到可能存在真人，暂不支持，请更换图片后重试，积分已返还~"
        category = "content_policy"
    elif kind == "CONTENT_MODERATION_FAILED" or any(value in raw for value in (
            "content policy", "inappropriate", "sensitive", "moderation", "违规", "敏感", "审核未通过", "版权", "copyright")):
        public = "检测到内容有敏感或违规情况，请修改后重试，积分已返还～"
        category = "content_policy"
    elif kind == "MEDIA_DOWNLOAD_FAILED" or any(value in raw for value in (
            "media download failed", "reference download failed")):
        public = "素材下载失败，请检查素材链接后重试~"
        category = "invalid_request"
    elif kind == "MEDIA_LIMIT_EXCEEDED" or "reference media exceeds" in raw:
        public = "素材超限，请修改后再试~"
        category = "invalid_request"
    elif kind == "MEDIA_FORMAT_UNSUPPORTED" or "wrong content type" in raw:
        public = "素材格式不支持，请修改后再试~"
        category = "invalid_request"
    elif kind == "VIDEO_EDIT_CONSTRAINT" or "duration" in raw and any(value in raw for value in ("invalid", "must", "unsupported")):
        public = "素材时长不支持，请修改后再试~"
        category = "invalid_request"
    elif kind in {"ACCOUNT_POOL_UNAVAILABLE", "ACCOUNT_MISSING", "BROWSERERROR", "INSUFFICIENT_CREDITS", "UPSTREAM_TEMPORARY", "MEDIA_UPLOAD_FAILED", "CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "LOGIN_FAILED"} or any(value in raw for value in (
            "insufficient", "not enough credit", "余额不足", "积分不足", "维护", "登录", "login", "captcha", "rate limit", "risk control", "风控", "验证码",
            "not available for this account", "restricted for this account")):
        public = "上游维护中，请稍后再试~"
        category = "provider_unavailable"
    return {"code": kind, "message": public, "public_message": public, "category": category}
