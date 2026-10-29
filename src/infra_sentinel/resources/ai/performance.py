"""Privacy-bounded performance diagnostics, separate from usage accounting."""

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import datetime
import math
from typing import Any

from .contract import detail_group, localized, token_metric


# Short residual windows amplify timestamp rounding and buffered delivery.
MIN_ESTIMATED_STREAM_MS = 250


@dataclass(frozen=True)
class PerformanceSample:
    recorded_epoch: float
    model: str
    output_tokens: int
    duration_ms: float | None = None
    first_token_ms: float | None = None
    streaming_ms: float | None = None


def latest_model_performance(samples: Iterable[PerformanceSample | None]) -> list[PerformanceSample]:
    """Keep each exact model's latest usable completed sample, newest first."""
    latest: dict[str, PerformanceSample] = {}
    for sample in samples:
        if sample is None or not performance_details(sample):
            continue
        previous = latest.get(sample.model)
        if previous is None or sample.recorded_epoch > previous.recorded_epoch:
            latest[sample.model] = sample
    return sorted(latest.values(), key=lambda sample: (-sample.recorded_epoch, sample.model))


def model_performance_details(
    samples: Iterable[PerformanceSample | None], *, scope: str = "request",
) -> list[dict[str, Any]]:
    return [group for sample in latest_model_performance(samples) for group in performance_details(sample, scope=scope)]


def _milliseconds(value: object, *, zero: bool = False) -> float | None:
    if type(value) not in (int, float) or not 0 <= value <= 2**53 - 1 or not math.isfinite(value):
        return None
    return float(value) if (value == 0 and zero) or value >= 0.000001 else None


def performance_details(sample: PerformanceSample | None, *, scope: str = "request") -> list[dict[str, Any]]:
    """Validate timing; label derived estimates separately from observed streams."""
    if sample is None or type(sample.output_tokens) is not int or not 0 <= sample.output_tokens <= 2**53 - 1:
        return []
    if type(sample.recorded_epoch) not in (int, float) or not 0 <= sample.recorded_epoch <= 2**53 - 1 or not math.isfinite(sample.recorded_epoch):
        return []
    try:
        recorded = datetime.fromtimestamp(sample.recorded_epoch).astimezone().isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return []
    duration = _milliseconds(sample.duration_ms)
    first = _milliseconds(sample.first_token_ms, zero=True)
    streaming = _milliseconds(sample.streaming_ms)
    badge = localized(f"{sample.model} · {recorded}", f"{sample.model} · {recorded}")
    sample_context = {"model": sample.model, "recorded_at": recorded}
    groups = []
    if duration is not None:
        turn = scope == "turn"
        codex_request = scope == "codex-request"
        prefix = "turn" if turn else "request"
        metrics = [
            token_metric(prefix + "-output-rate",
                         localized("Turn average output", "轮次平均输出") if turn else localized("Request average output", "请求平均输出"),
                         sample.output_tokens * 1000 / duration,
                         localized("reported output, including reasoning", "来源记录的输出，含推理 Token"), unit="tokens/s"),
            token_metric(prefix + "-duration",
                         localized("Turn duration", "整轮耗时") if turn else localized("Request duration", "请求耗时"),
                         duration / 1000,
                         localized("includes waiting and tools", "包含等待和工具执行") if turn else localized("includes first-output waiting", "包含首输出等待"), unit="seconds"),
        ]
        if first is not None and first <= duration:
            metrics.append(token_metric("first-token-latency", localized("First output latency", "首输出延迟"),
                                        first / 1000, localized("source/client timing", "来源或客户端计时"), unit="seconds"))
        groups.append(detail_group(
            prefix + "-performance", localized("Turn average", "轮次平均") if turn else localized("Request average", "请求平均"),
            metrics, badge=badge, sample=sample_context,
            note=localized(
                "Latest completed user turn: summed assistant output including reasoning divided by the full turn span, including waiting and tools. Not pure model generation speed."
                if turn else
                "Latest matched local Codex model request: reported output including reasoning divided by WebSocket send-to-completion time. Includes request waiting and transport, excludes client-tool gaps between requests. Per-conversation exact-model pairs only; not server decoding speed. The sample may come from another local chat or subagent."
                if codex_request else
                "Latest recorded completed request: reported output divided by request duration, including first-output waiting. DSH starts at step entry, so request preparation is included; retries without a reliable start omit this average. Tools after the response are excluded.",
                "最近完成的用户轮次：汇总助手输出（含推理）除以整轮时间，包含等待和工具执行，不代表纯模型生成速度。"
                if turn else
                "本机最近可配对的单次 Codex 模型请求：输出（含推理）除以 WebSocket 发送至完成时间；包含请求等待和传输，排除请求之间的客户端工具时间。仅同会话、精确模型配对；不是服务端解码速度，样本可能来自其他本机对话或子 Agent。"
                if codex_request else
                "最近记录的完整请求：来源输出除以请求耗时，包含首输出等待。DSH 从步骤开始计时，包含请求前处理；重试缺少可靠起点时不显示请求平均。响应之后的工具执行不计入。",
            ),
        ))
        if (codex_request and streaming is None and first is not None and first > 0
                and sample.output_tokens > 1 and duration - first >= MIN_ESTIMATED_STREAM_MS):
            residual = duration - first
            estimate = detail_group(
                "estimated-stream-performance", localized("Estimated stream rate", "估算流速"),
                [
                    token_metric("estimated-stream-output-rate", localized("Estimated stream output", "估算流速"),
                                 sample.output_tokens * 1000 / residual,
                                 localized("output / (request duration − first-output latency)",
                                           "输出 ÷（请求耗时 − 首输出延迟）"), unit="tokens/s"),
                    token_metric("estimated-stream-duration", localized("Time after first output", "扣除首输出等待后耗时"),
                                 residual / 1000, localized("derived from the same request", "由同一次请求推算"), unit="seconds"),
                    token_metric("request-duration", localized("Request duration", "请求耗时"),
                                 duration / 1000, localized("same request", "同一次请求"), unit="seconds"),
                    token_metric("first-token-latency", localized("First output latency", "首输出延迟"),
                                 first / 1000, localized("same request", "同一次请求"), unit="seconds"),
                ], badge=badge, sample=sample_context,
                note=localized(
                    "Estimate from the same request: reported output tokens / (request duration − first-output latency). Includes reasoning tokens; token counts and stream boundaries are not individually aligned. Buffering can inflate this estimate. Not measured stream timing or server decoding speed. Omitted for missing/invalid timing, fewer than two output tokens, or a residual window below 250 ms.",
                    "同一次请求的估算：来源输出 Token ÷（请求耗时 − 首输出延迟）。输出含推理 Token，计数与流起止未逐 Token 对齐，缓冲可能放大估算值；不是实测流式计时或服务端解码速度。缺少或非法计时、输出不足两个 Token、剩余时间不足 250 ms 时不估算。",
                ),
            )
            estimate["estimated"] = True
            groups.append(estimate)
    if streaming is not None:
        groups.append(detail_group(
            "client-stream-performance", localized("Client stream rate", "客户端流速"),
            [
                token_metric("client-stream-output-rate", localized("Client stream output", "客户端流速"),
                             sample.output_tokens * 1000 / streaming,
                             localized("first output through stream finish", "首输出至流结束"), unit="tokens/s"),
                token_metric("client-stream-duration", localized("Stream duration", "流式耗时"),
                             streaming / 1000, localized("excludes first-output waiting", "不含首输出等待"), unit="seconds"),
            ], badge=badge, sample=sample_context,
            note=localized(
                "Client-observed delivery rate, including reasoning. Buffering and atomic delivery can inflate this value; it is not server decoding speed and must not be compared with turn or request averages.",
                "客户端观测的交付流速，包含推理。缓冲和批量交付可能放大数值；它不是服务端解码速度，不能与轮次平均或请求平均直接比较。",
            ),
        ))
    return groups
