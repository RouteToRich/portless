"""Tests for RequestTracker — in-memory state and JSONL file persistence."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from shared.llm_gateway.tracker import RequestRecord, RequestTracker


# ─────────────────────────────────── start_request


def test_start_creates_pending_record(mem_tracker):
    r = mem_tracker.start_request(caller="agent", model="qwen3-32b")
    assert r.status == "pending"
    assert r.caller == "agent"
    assert r.model == "qwen3-32b"
    assert r.request_id != ""


def test_start_adds_to_in_memory_list(mem_tracker):
    mem_tracker.start_request(caller="a", model="m")
    mem_tracker.start_request(caller="b", model="m")
    assert len(mem_tracker.get_records()) == 2


def test_start_assigns_unique_ids(mem_tracker):
    r1 = mem_tracker.start_request(caller="a", model="m")
    r2 = mem_tracker.start_request(caller="a", model="m")
    assert r1.request_id != r2.request_id


# ─────────────────────────────────── complete_request


def test_complete_fills_all_fields(mem_tracker):
    r = mem_tracker.start_request(caller="a", model="m")
    mem_tracker.complete_request(
        r,
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=750.5,
        status="success",
        fallback_model="",
        retry_count=0,
    )
    assert r.prompt_tokens == 100
    assert r.completion_tokens == 50
    assert r.total_tokens == 150
    assert r.latency_ms == 750.5
    assert r.status == "success"


def test_complete_calculates_total_tokens(mem_tracker):
    r = mem_tracker.start_request(caller="a", model="m")
    mem_tracker.complete_request(r, prompt_tokens=300, completion_tokens=100)
    assert r.total_tokens == 400


def test_complete_error_status(mem_tracker):
    r = mem_tracker.start_request(caller="a", model="m")
    mem_tracker.complete_request(r, status="error", error_message="rate limit")
    assert r.status == "error"
    assert r.error_message == "rate limit"


def test_complete_fallback_status(mem_tracker):
    r = mem_tracker.start_request(caller="a", model="m")
    mem_tracker.complete_request(r, status="fallback", fallback_model="model-b")
    assert r.status == "fallback"
    assert r.fallback_model == "model-b"


def test_complete_tracks_retry_count(mem_tracker):
    r = mem_tracker.start_request(caller="a", model="m")
    mem_tracker.complete_request(r, status="success", retry_count=2)
    assert r.retry_count == 2


# ─────────────────────────────────── JSONL file persistence


def test_session_start_marker_written(file_tracker):
    first_line = file_tracker.tracking_file.read_text().strip().split("\n")[0]
    data = json.loads(first_line)
    assert data["_marker"] == "session_start"
    assert "ts" in data


def test_record_appended_to_file(file_tracker):
    r = file_tracker.start_request(caller="agent", model="qwen3")
    file_tracker.complete_request(r, prompt_tokens=50, completion_tokens=20, status="success")

    lines = file_tracker.tracking_file.read_text().strip().split("\n")
    record_line = json.loads(lines[1])  # line 0 = session_start marker
    assert record_line["caller"] == "agent"
    assert record_line["model"] == "qwen3"
    assert record_line["status"] == "success"
    assert record_line["prompt_tokens"] == 50
    assert record_line["completion_tokens"] == 20
    assert record_line["total_tokens"] == 70


def test_error_record_written_to_file(file_tracker):
    r = file_tracker.start_request(caller="a", model="m")
    file_tracker.complete_request(r, status="error", error_message="timeout")

    lines = file_tracker.tracking_file.read_text().strip().split("\n")
    data = json.loads(lines[1])
    assert data["status"] == "error"
    assert data["error"] == "timeout"


def test_retry_count_in_file(file_tracker):
    r = file_tracker.start_request(caller="a", model="m")
    file_tracker.complete_request(r, status="success", retry_count=2)

    lines = file_tracker.tracking_file.read_text().strip().split("\n")
    data = json.loads(lines[1])
    assert data["retry_count"] == 2


def test_multiple_records_all_valid_json(file_tracker):
    for i in range(5):
        r = file_tracker.start_request(caller="a", model="m")
        file_tracker.complete_request(r, status="success", prompt_tokens=i * 10)

    lines = [
        ln for ln in file_tracker.tracking_file.read_text().strip().split("\n") if ln
    ]
    assert len(lines) == 6  # 1 marker + 5 records
    for line in lines:
        json.loads(line)  # must not raise


def test_file_disabled_no_writes(tmp_path):
    path = tmp_path / "noop.txt"
    tracker = RequestTracker(enabled=False, tracking_file=path)
    r = tracker.start_request(caller="a", model="m")
    tracker.complete_request(r, status="success")
    assert not path.exists()


def test_file_write_error_does_not_crash(file_tracker):
    # Corrupt the path after init so subsequent writes fail
    file_tracker._file_path = Path("/nonexistent/readonly/path.txt")
    r = file_tracker.start_request(caller="a", model="m")
    # Must not raise even though the write will fail
    file_tracker.complete_request(r, status="success")


def test_records_still_in_memory_after_file_error(file_tracker):
    file_tracker._file_path = Path("/nonexistent/path.txt")
    r = file_tracker.start_request(caller="a", model="m")
    file_tracker.complete_request(r, status="success")
    records = file_tracker.get_records()
    assert len(records) == 1
    assert records[0].status == "success"


# ─────────────────────────────────── get_records / get_stats


def test_get_records_respects_limit(mem_tracker):
    for _ in range(20):
        r = mem_tracker.start_request(caller="a", model="m")
        mem_tracker.complete_request(r, status="success")
    assert len(mem_tracker.get_records(limit=5)) == 5


def test_get_records_returns_most_recent(mem_tracker):
    for i in range(5):
        r = mem_tracker.start_request(caller=f"caller-{i}", model="m")
        mem_tracker.complete_request(r, status="success")
    records = mem_tracker.get_records(limit=2)
    assert records[-1].caller == "caller-4"


def test_get_stats_empty():
    tracker = RequestTracker(enabled=False)
    assert tracker.get_stats() == {"total_requests": 0}


def test_get_stats_counts(mem_tracker):
    def _complete(status, tokens=0, fallback="", retries=0):
        r = mem_tracker.start_request(caller="a", model="m")
        mem_tracker.complete_request(
            r,
            status=status,
            prompt_tokens=tokens,
            completion_tokens=tokens // 2,
            fallback_model=fallback,
            retry_count=retries,
        )

    _complete("success", tokens=100)
    _complete("success", tokens=200)
    _complete("fallback", tokens=150, fallback="model-b")
    _complete("error")

    stats = mem_tracker.get_stats()
    assert stats["total_requests"] == 4
    assert stats["successes"] == 2
    assert stats["errors"] == 1
    assert stats["fallbacks"] == 1
    assert stats["total_tokens"] == 100 + 50 + 200 + 100 + 150 + 75  # prompt + completion
    assert stats["success_rate"] == pytest.approx(0.5)


def test_get_stats_total_retries(mem_tracker):
    for retries in (0, 1, 2):
        r = mem_tracker.start_request(caller="a", model="m")
        mem_tracker.complete_request(r, status="success", retry_count=retries)
    assert mem_tracker.get_stats()["total_retries"] == 3


def test_get_stats_avg_latency(mem_tracker):
    for latency in (100.0, 300.0):
        r = mem_tracker.start_request(caller="a", model="m")
        mem_tracker.complete_request(r, status="success", latency_ms=latency)
    assert mem_tracker.get_stats()["avg_latency_ms"] == pytest.approx(200.0)


# ─────────────────────────────────── thread safety


def test_concurrent_writes_are_safe(file_tracker):
    errors = []

    def write_records():
        try:
            for _ in range(20):
                r = file_tracker.start_request(caller="t", model="m")
                file_tracker.complete_request(r, status="success", prompt_tokens=10)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_records) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    # 5 threads × 20 records = 100 records in memory
    assert len(file_tracker.get_records()) == 100
    # Every line in the file must be valid JSON
    lines = [
        ln for ln in file_tracker.tracking_file.read_text().strip().split("\n") if ln
    ]
    for line in lines:
        json.loads(line)
