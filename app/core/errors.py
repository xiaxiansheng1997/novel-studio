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


class InvalidRequest(W2AError):
    """请求参数不受支持（如 n>1）：必须显式报错，不能被静默忽略后当成正常回答。"""

    def __init__(self, message: str, *, code: str = "invalid_request"):
        super().__init__(message, type_="invalid_request_error", code=code, status=400)


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


class UpstreamRejected(ProviderError):
    """上游用 HTTP 200 + 业务错误事件回绝（不是模型回答）。

    豆包典型形态：`event: STREAM_ERROR` + `{"error_code":710022002,
    "error_msg":"当前服务访问频繁，请稍后重试"}`。如果不报出来，调用方只能
    拿到一条空回答，只能去页面上看到“系统繁忙”这种含糊提示。
    """

    # 站点侧临时故障（过载/网关错误）：不该引导用户去查登录态
    _TRANSIENT_HINTS = ("5xx", "502", "503", "504", "timeout", "timed out", "unavailable", "gateway")

    def __init__(self, provider: str, code: str = "", detail: str = ""):
        msg = f"上游返回业务错误而不是回答：{detail or '未知错误'}"
        if code:
            msg += f"（error_code={code}）"
        transient = any(h in detail.lower() for h in self._TRANSIENT_HINTS)
        if transient:
            msg += "。这通常是站点侧临时故障/过载，隔一会儿重试即可（不是登录态问题）"
        else:
            msg += (
                f"。常见原因：{provider} 的浏览器 profile 处于未登录游客态，或该设备/账号被风控；"
                f"请打开 /live 登录 {provider}（或在已弹出的 Chrome 窗口里手动发一句确认能否正常回答）后重试"
            )
        super().__init__(provider, msg, status=503 if transient else 502)
        self.type = "upstream_error"
        self.code = code or "upstream_error"


class VerifyChallenge(Exception):
    """内部信号：上游要求先完成人机验证，给自动过滑块一次机会。

    它不会被 API 层当成响应发出去；自动处理失败时会转成 HumanVerifyRequired。
    """

    def __init__(self, code: str = "", detail: str = ""):
        super().__init__(detail or "upstream requires human verify")
        self.code = code
        self.detail = detail


class HumanVerifyRequired(ProviderError):
    """上游要求人机验证，且自动处理（过滑块/等人做完）没成功，必须人工完成。"""

    def __init__(
        self,
        provider: str,
        detail: str = "",
        note: str = "",
        *,
        headed: bool = True,
        cooldown: int = 0,
        code: str = "",
    ):
        where = (
            # /live 画面与后台 capture 是同一个标签页，两种说法指向同一个页面
            f"请在 {provider} 弹出的 Chrome 窗口里手动完成验证（或直接在 /live 画面里操作，两者是同一个标签页）"
            if headed
            else f"请打开 /live 选 {provider}，在页面里手动完成验证"
        )
        msg = "上游要求完成人机验证"
        if detail:
            msg += f"（{detail[:100]}）"
        if note:
            msg += f"，自动处理未成功：{note[:120]}"
        msg += f"。{where}后重试"
        if cooldown > 0:
            msg += f"（{cooldown}s 风控冷却期内请求会被直接拒绝，验证完可在 /live 点“已验证·清除风控冷却”）"
        super().__init__(provider, msg, status=503)
        self.type = "human_verify"
        self.code = code or "human_verify"


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
