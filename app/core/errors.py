"""统一错误类型。API 层把它们转成 OpenAI 风格的 JSON 错误响应。"""
from __future__ import annotations


class W2AError(Exception):
    """基类。"""

    def __init__(self, message: str, *, type_: str = "server_error", code: str | None = None, status: int = 500):
        super().__init__(message)
        self.message = message
        self.type = type_
        self.code = code
        self.status = status


class ModelNotFound(W2AError):
    def __init__(self, model: str):
        super().__init__(f"模型不存在或无法路由到任何 provider: {model!r}", type_="invalid_request_error", code="model_not_found", status=404)


class ProviderError(W2AError):
    def __init__(self, provider: str, message: str, *, status: int = 502):
        super().__init__(f"[{provider}] {message}", type_="provider_error", status=status)


class NeedsRecon(ProviderError):
    """尚未抓取真实接口（fixtures 缺失）。提示用户运行 recon。"""

    def __init__(self, provider: str):
        super().__init__(
            provider,
            "尚未抓取到该站点的真实对话接口。请先运行: python -m scripts.recon "
            f"{provider} （登录后发一条消息，工具会把请求/响应模板写入 fixtures/{provider}.json）",
            status=501,
        )


class NeedsLogin(ProviderError):
    """登录态缺失或已失效。"""

    def __init__(self, provider: str):
        super().__init__(
            provider,
            f"缺少有效登录态。请打开 /ui/live.html 选 {provider} 连接后在页面里登录（或运行 python -m scripts.login {provider}）",
            status=401,
        )


class AuthExpired(ProviderError):
    """凭证已过期，需要重新登录。"""

    def __init__(self, provider: str):
        super().__init__(
            provider,
            f"登录态已失效（{provider} 会话过期或被风控）。请打开 /ui/live.html 重新登录（或运行 python -m scripts.login {provider}）",
            status=401,
        )


class ProviderBlocked(ProviderError):
    """上游返回固定拒绝卡片：内容审核 / 风控命中 / 浏览器处于未登录游客态。

    这不是模型回答，必须显式暴露；否则调用方会把它当成“模型不想答”。
    """

    def __init__(self, provider: str, detail: str = "", *, mime: str = ""):
        msg = (
            "上游返回固定拒绝卡片（不是模型回答）：通常是内容审核/风控命中，"
            f"或 {provider} 的浏览器 profile 处于未登录游客态。"
            f"请先在 /live 打开 {provider}，确认 GET /live/loggedin/{provider} 返回 logged_in=true，"
            "再检查该问题是否命中了站点内容策略"
        )
        if mime:
            msg += f"（mime_type={mime}）"
        if detail:
            msg += f"；上游原文：{detail[:120]}"
        super().__init__(provider, msg, status=403)
        # 覆盖 ProviderError 的默认类型，便于客户端按内容审核分流
        self.type = "content_filter"
        self.code = "risk_control"
