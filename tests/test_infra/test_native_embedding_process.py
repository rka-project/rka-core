"""Real child processes and pipe boundaries; no downloads or model inference."""

import json
import os
import threading
import time

import pytest

from rka.infra.native_embedding_process import NativeEmbeddingProcess, NativeEmbeddingFailed
from rka.infra.embedding_resources import EmbeddingCallTimeout


def synthetic_child(connection, options):
    if options.get("hang_before_read"):
        time.sleep(10)
    while True:
        batches = json.loads(connection.recv_bytes())
        if options.get("die"):
            os._exit(7)
        if options.get("hang"):
            time.sleep(10)
        if options.get("error"):
            connection.send_bytes(b'{"error":"synthetic"}')
        else:
            connection.send_bytes(json.dumps({"vectors": [[len(text)] for batch in batches for text in batch]}).encode())


def test_child_reuse_and_model_switch_reap_old_child():
    runtime = NativeEmbeddingProcess(target=synthetic_child)
    try:
        event = threading.Event()
        assert runtime.call({}, [["abc"]], timeout=5, cancelled=event) == [[3]]
        first = runtime.process.pid
        assert runtime.call({}, [["a", "ab"]], timeout=5, cancelled=event) == [[1], [2]]
        assert runtime.process.pid == first
        runtime.call({"model": "other"}, [["x"]], timeout=5, cancelled=event)
        assert runtime.process.pid != first
    finally:
        runtime.close()


@pytest.mark.parametrize("options", [{"hang": True}, {"die": True}, {"error": True}])
def test_failure_kills_child_and_next_call_recovers(options):
    runtime = NativeEmbeddingProcess(target=synthetic_child)
    try:
        with pytest.raises((EmbeddingCallTimeout, NativeEmbeddingFailed, EOFError, ConnectionError)):
            runtime.call(options, [["x"]], timeout=0.6, cancelled=threading.Event())
        assert runtime.process is None
        assert runtime.call({}, [["abc"]], timeout=5, cancelled=threading.Event()) == [[3]]
    finally:
        runtime.close()


def test_cancellation_reaps_child_before_releasing_call():
    runtime = NativeEmbeddingProcess(target=synthetic_child)
    event = threading.Event()
    timer = threading.Timer(0.5, event.set)
    timer.start()
    try:
        with pytest.raises(EmbeddingCallTimeout):
            runtime.call({"hang": True}, [["x"]], timeout=10, cancelled=event)
        assert runtime.process is None
    finally:
        timer.cancel()
        timer.join()
        runtime.close()


def test_deadline_terminates_child_even_while_request_pipe_is_blocked():
    runtime = NativeEmbeddingProcess(target=synthetic_child)
    started = time.monotonic()
    try:
        with pytest.raises(EmbeddingCallTimeout):
            runtime.call({"hang_before_read": True}, [["x" * 2048] * 128], timeout=0.6, cancelled=threading.Event())
        assert time.monotonic() - started < 4
        assert runtime.process is None
    finally:
        runtime.close()
