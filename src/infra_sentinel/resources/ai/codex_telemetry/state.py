"""Bounded, conservative request correlation and aggregate-only checkpoints."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import threading
import time

from ..contract import detail_group, localized, token_metric
from ..performance import PerformanceSample, model_performance_details, performance_details
from .protocol import MAX_DURATION_MS, MODEL_RE, RequestEvent

MAX_STREAMS = 256
MAX_SEEN = 4096
MAX_MODELS = 64
MAX_CHECKPOINT_BYTES = 32 * 1024
SAMPLE_RETENTION_SECONDS = 7 * 86400


class TelemetryState:
    def __init__(self, checkpoint: Path | None = None, *, clock=time.time):
        self._clock = clock
        self._checkpoint = checkpoint
        self._lock = threading.Lock()
        self._streams: OrderedDict[tuple[str, str], tuple[float, RequestEvent | None, bool]] = OrderedDict()
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._samples: dict[str, PerformanceSample] = {}
        self._origins: dict[str, tuple[str, str]] = {}
        self._dirty = False
        self.received = self.ignored = self.matched = self.dropped = self.rejected = self.bytes = 0
        self._load()

    def _load(self):
        if self._checkpoint is None:
            return
        try:
            with self._checkpoint.open("rb") as stream:
                raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
            if len(raw) > MAX_CHECKPOINT_BYTES:
                return
            doc = json.loads(raw)
            if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("samples"), list):
                return
            now = self._clock()
            for row in doc["samples"][:MAX_MODELS]:
                if not isinstance(row, dict) or set(row) != set(PerformanceSample.__dataclass_fields__):
                    continue
                sample = PerformanceSample(**row)
                if (isinstance(sample.model, str) and MODEL_RE.fullmatch(sample.model)
                        and type(sample.recorded_epoch) in (int, float)
                        and now - SAMPLE_RETENTION_SECONDS <= sample.recorded_epoch <= now + 30
                        and sample.streaming_ms is None and performance_details(sample, scope="codex-request")):
                    self._samples[sample.model] = sample
            self._dirty = len(self._samples) != len(doc["samples"])
        except (OSError, ValueError, TypeError, RecursionError):
            return

    def record_batch(self, events: list[RequestEvent], records: int, size: int):
        with self._lock:
            self.received += len(events)
            self.ignored += max(0, records - len(events))
            self.bytes += size
            cutoff = self._clock() - 3600
            for key in [key for key, row in self._streams.items() if row[0] < cutoff]:
                expired = self._streams.pop(key)
                self.dropped += int(expired[1] is not None)
            for event in events:
                fingerprint = hashlib.sha256(repr(event).encode()).hexdigest()
                if fingerprint in self._seen:
                    continue
                self._seen[fingerprint] = None
                if len(self._seen) > MAX_SEEN:
                    self._seen.popitem(last=False)
                key = (event.session, event.model)
                previous_epoch, pending, ambiguous = self._streams.get(key, (0, None, False))
                if event.epoch < previous_epoch:
                    # No response ID is exported. Late/reordered events invalidate
                    # that stream rather than being guessed into a previous pair.
                    self.dropped += 1
                    self._streams[key] = (previous_epoch, None, True)
                    if self._origins.get(event.model) == key:
                        self._samples.pop(event.model, None)
                        self._origins.pop(event.model, None)
                        self._dirty = True
                    continue
                if event.kind == "start":
                    pending, ambiguous = event, pending is not None or ambiguous
                elif event.kind == "failed":
                    pending, ambiguous = None, False
                    self.dropped += 1
                else:
                    elapsed = (event.epoch - pending.epoch) * 1000 + pending.duration_ms if pending else 0
                    if pending is not None and not ambiguous and 1 <= elapsed <= MAX_DURATION_MS and event.output_tokens:
                        first = event.first_token_ms
                        sample = PerformanceSample(event.epoch, event.model, event.output_tokens, elapsed,
                                                   first if first is not None and first <= elapsed else None)
                        old = self._samples.get(event.model)
                        if old is None or sample.recorded_epoch > old.recorded_epoch:
                            self._samples[event.model] = sample
                            self._origins[event.model] = key
                            self._dirty = True
                            self.matched += 1
                    elif event.output_tokens:
                        self.dropped += 1
                    pending, ambiguous = None, False
                self._streams[key] = (event.epoch, pending, ambiguous)
                self._streams.move_to_end(key)
                if len(self._streams) > MAX_STREAMS:
                    _, evicted = self._streams.popitem(last=False)
                    self.dropped += int(evicted[1] is not None)
            if len(self._samples) > MAX_MODELS:
                for model in sorted(self._samples, key=lambda m: self._samples[m].recorded_epoch)[:-MAX_MODELS]:
                    self._samples.pop(model)
                    self._origins.pop(model, None)

    def reject(self):
        with self._lock:
            self.rejected += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"received_events": self.received, "ignored_records": self.ignored,
                    "matched_requests": self.matched, "dropped_events": self.dropped,
                    "rejected_batches": self.rejected, "received_bytes": self.bytes,
                    "pending_streams": len(self._streams), "dedup_entries": len(self._seen),
                    "model_count": len(self._samples)}

    def details(self, status: str) -> list[dict]:
        with self._lock:
            samples = [s for s in self._samples.values() if self._clock() - SAMPLE_RETENTION_SECONDS <= s.recorded_epoch]
        values = self.snapshot()
        badge = {"disabled": localized("Disabled", "未启用"), "waiting": localized("Waiting for Codex", "等待 Codex 数据"),
                 "receiving": localized("Receiving", "接收中"), "unavailable": localized("Listener unavailable", "接收服务不可用")}[status]
        metrics = [token_metric(key, label, values[key], localized("since Agent start", "自采集器启动"), unit="count")
                   for key, label in [("matched_requests", localized("Matched requests", "已配对请求")),
                                      ("dropped_events", localized("Unusable events", "无法配对事件")),
                                      ("rejected_batches", localized("Rejected batches", "已拒绝批次"))]]
        return [*model_performance_details(samples, scope="codex-request"), detail_group(
            "codex-local-telemetry", localized("Local request telemetry", "本机请求遥测"), metrics, badge=badge,
            note=localized(
                "Authenticated loopback OTLP logs. Only token counts and timing are retained. Per-conversation exact-model send/completion pairs exclude time between requests spent in client tools. Warmups, missing, failed, overlapping and reordered requests do not produce a rate. Telemetry never enters the usage ledger. Existing Codex processes may need restarting after configuration.",
                "仅接收经认证的本机 OTLP 日志，只保留计数和计时。同一会话、精确模型的发送与完成事件配对，排除请求之间的客户端工具时间。预热、缺失、失败、重叠和乱序请求不生成速度。遥测不进入用量账本；配置后已有 Codex 进程可能需要重启。"))]

    def checkpoint(self):
        if self._checkpoint is None:
            return
        with self._lock:
            expired = [model for model, sample in self._samples.items()
                       if sample.recorded_epoch < self._clock() - SAMPLE_RETENTION_SECONDS]
            for model in expired:
                self._samples.pop(model)
                self._origins.pop(model, None)
            self._dirty |= bool(expired)
            if not self._dirty:
                return
            payload = json.dumps({"schema": 1, "samples": [asdict(s) for s in self._samples.values()]}, separators=(",", ":"))
            self._checkpoint.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._checkpoint.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                temporary.chmod(0o600)
                stream.write(payload)
            temporary.replace(self._checkpoint)
            self._dirty = False
