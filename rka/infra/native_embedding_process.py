"""One reusable spawn-only FastEmbed child per API/worker process.

No database descriptors are inherited. The parent controls deadlines and kills
failed/timed-out inference instead of leaving an uninterruptible ONNX thread in
the API. Process-wide provider admission lives in embedding_resources.
"""

from __future__ import annotations

import atexit
import json
import multiprocessing
import os
import threading
import time

from rka.infra.embedding_resources import EmbeddingCallTimeout, EmbeddingResourceError


class NativeEmbeddingFailed(EmbeddingResourceError):
    code = "embedding_native_failed"


def _watch_parent():
    parent = multiprocessing.parent_process()
    if parent is None:
        return
    while parent.is_alive():
        time.sleep(0.25)
    # A killed owner cannot clean up its inference child. No DB is open here.
    os._exit(1)


def _child(connection, options):
    threading.Thread(target=_watch_parent, daemon=True).start()
    try:
        model = None
        while True:
            request = json.loads(connection.recv_bytes(1024 * 1024))
            if model is None:
                from fastembed import TextEmbedding
                model = TextEmbedding(**options)
            vectors = []
            for batch in request:
                rows = [row.tolist() for row in model.embed(batch, batch_size=len(batch))]
                if len(rows) != len(batch):
                    raise ValueError("vector count mismatch")
                vectors.extend(rows)
            payload = json.dumps({"vectors": vectors}, allow_nan=False).encode()
            if len(payload) > 16 * 1024 * 1024:
                raise ValueError("native response too large")
            connection.send_bytes(payload)
    except EOFError:
        pass
    except BaseException:
        # Do not send source text/model errors or unpickle child payloads.
        try:
            connection.send_bytes(b'{"error":"native_load_or_inference_failed"}')
        except (OSError, EOFError):
            pass
    finally:
        connection.close()


class NativeEmbeddingProcess:
    def __init__(self, *, target=_child):
        self.process = None
        self.connection = None
        self.options = None
        self.target = target

    def close(self):
        proc = self.process
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if proc is not None:
            if proc.is_alive():
                proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
                proc.join(2)
            if proc.is_alive():
                raise NativeEmbeddingFailed("native child did not stop")
            proc.close()
            self.process = None
        self.options = None

    def call(self, options, batches, *, timeout, cancelled):
        deadline = time.monotonic() + timeout
        stop_watch = threading.Event()
        timed_out = threading.Event()
        watcher = None
        def stop_watcher():
            stop_watch.set()
            if watcher is not None:
                watcher.join()
        try:
            if (self.process is None or not self.process.is_alive() or options != self.options):
                self.close()
                ctx = multiprocessing.get_context("spawn")
                parent, child = ctx.Pipe()
                proc = ctx.Process(target=self.target, args=(child, options), daemon=True)
                try:
                    proc.start()
                except BaseException:
                    parent.close()
                    child.close()
                    raise
                child.close()
                self.connection, self.process, self.options = parent, proc, options.copy()
            proc = self.process
            def watch_deadline():
                # Independent of pipe send/recv: even a child stopped mid-frame
                # cannot leave the executor holding admission indefinitely.
                while not stop_watch.wait(0.02):
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        timed_out.set()
                        if proc.is_alive():
                            proc.terminate()
                        if not stop_watch.wait(0.2) and proc.is_alive():
                            proc.kill()
                        return
            watcher = threading.Thread(target=watch_deadline, daemon=True)
            watcher.start()
            self.connection.send_bytes(json.dumps(batches).encode())
            while True:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    raise EmbeddingCallTimeout("native inference cancelled or deadline exceeded")
                if self.connection.poll(0.02):
                    response = json.loads(self.connection.recv_bytes(16 * 1024 * 1024))
                    if "error" in response:
                        raise NativeEmbeddingFailed("native model load/inference failed; retry or inspect the local model installation")
                    return response["vectors"]
                if not self.process.is_alive():
                    raise NativeEmbeddingFailed("native child exited; durable work may retry")
        except (EOFError, OSError) as exc:
            stop_watcher()
            self.close()
            if timed_out.is_set() or cancelled.is_set():
                raise EmbeddingCallTimeout("native inference cancelled or deadline exceeded") from exc
            raise NativeEmbeddingFailed("native child or pipe exited; durable work may retry") from exc
        except BaseException:
            stop_watcher()
            self.close()
            raise
        finally:
            stop_watcher()


NATIVE_PROCESS = NativeEmbeddingProcess()
atexit.register(NATIVE_PROCESS.close)
