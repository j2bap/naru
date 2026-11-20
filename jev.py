"""Low-overhead adapters for the host Jev evaluator.

Naru cannot call an MCP tool from inside its Python process.  This module keeps
that boundary explicit. A host integration can pass a direct callable through
``DirectJev`` without a process hop. A standalone integration uses
``CommandJev`` with one long-lived process, so adapter startup is paid once per
task rather than once per context checkpoint. The installed ``jev mcp`` command
uses MCP JSON-RPC; other commands can use the small JSON-lines adapter
protocol. The bridge never logs prompt, state, or stderr content.
"""

import json
import io
import math
import os
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field


MAX_RESPONSE_CHARS = 1_000_000


def usage_number(value):
    """Return a provider usage number, or 0 when it is not a usable count.

    Usage is telemetry. Text, a boolean, a negative number, NaN, and infinity
    are all recorded as 0, so a bad usage field can never fail the call that
    carried a valid decision.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not 0 <= value < math.inf:
        return 0
    return value


class JevError(RuntimeError):
    """The evaluator could not produce a typed response."""


@dataclass
class JevUsage:
    attempts: int = 0
    calls: int = 0
    starts: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    errors: int = 0
    native_usage: dict = field(default_factory=dict)
    startup_latency_ms: float = 0.0
    provider_latency_ms: float = 0.0
    transport: str = "unknown"
    tokens_measured: bool = True

    def record(self, response, elapsed_ms=None):
        self.calls += 1
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        if not isinstance(usage, dict):
            self.tokens_measured = False
            return
        input_tokens = usage.get("input_tokens")
        if (
            isinstance(input_tokens, bool)
            or usage_number(input_tokens) != input_tokens
        ):
            self.tokens_measured = False
        self.input_tokens += int(usage_number(usage.get("input_tokens", 0)))
        self.cache_read_tokens += int(
            usage_number(
                usage.get(
                    "cache_read_tokens",
                    usage.get("cache_read_input_tokens", 0),
                )
            )
        )
        self.cache_write_tokens += int(
            usage_number(
                usage.get(
                    "cache_write_tokens",
                    usage.get("cache_creation_input_tokens", 0),
                )
            )
        )
        self.output_tokens += int(usage_number(usage.get("output_tokens", 0)))
        self.provider_latency_ms += float(
            usage_number(
                usage.get(
                    "provider_latency_ms",
                    usage.get("latency_ms", 0),
                )
            )
        )
        self.latency_ms += float(
            usage_number(usage.get("latency_ms", 0))
            if elapsed_ms is None
            else elapsed_ms
        )
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            self.native_usage[key] = self.native_usage.get(key, 0) + value

    def as_dict(self):
        return {
            "attempts": self.attempts,
            "calls": self.calls,
            "starts": self.starts,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "billed_input_tokens": (
                self.input_tokens
                + self.cache_read_tokens
                + self.cache_write_tokens
            ),
            "output_tokens": self.output_tokens,
            "latency_ms": round(self.latency_ms, 3),
            "errors": self.errors,
            "provider_usage": dict(self.native_usage),
            "startup_latency_ms": round(self.startup_latency_ms, 3),
            "provider_latency_ms": round(self.provider_latency_ms, 3),
            "transport_overhead_ms": round(
                max(0.0, self.latency_ms - self.provider_latency_ms), 3
            ),
            "transport": self.transport,
            "tokens_measured": self.tokens_measured,
        }


class _LineReader:
    """Read newline-terminated lines from a descriptor under a real deadline.

    ``select`` followed by ``readline()`` on a buffered text stream is not
    bounded. A peer that sends half a line holds the read until it finishes,
    and a second line that arrived with the first sits in Python's buffer
    where ``select`` cannot see it. This reader owns the only buffer and reads
    the descriptor itself, so both cases follow the deadline.
    """

    def __init__(self, descriptor, prefix=""):
        self._descriptor = descriptor
        self._prefix = prefix
        self._buffer = bytearray()
        self._skip_line = False

    def _ready(self, seconds):
        selector = selectors.DefaultSelector()
        try:
            try:
                selector.register(self._descriptor, selectors.EVENT_READ)
            except (OSError, ValueError) as error:
                raise JevError(self._prefix + "timeout_unsupported") from error
            return bool(selector.select(max(0, seconds)))
        finally:
            selector.close()

    def _read(self):
        """Buffer the bytes that are ready. Return False at end of input."""
        chunk = os.read(self._descriptor, 65536)
        if not chunk:
            return False
        if self._skip_line:
            _, newline, chunk = chunk.partition(b"\n")
            self._skip_line = not newline
        self._buffer += chunk
        return True

    def discard_pending(self):
        """Drop everything the peer wrote before the next request is sent.

        Those bytes answer an earlier request. If they end in the middle of a
        line, the rest of that line is dropped when it arrives, so the tail of
        a late answer cannot be read as the start of the next one.
        """
        while self._ready(0) and self._read():
            pass
        if self._buffer:
            self._skip_line = not self._buffer.endswith(b"\n")
            self._buffer.clear()

    def readline(self, deadline):
        """Return one line, or ``""`` at end of input.

        ``deadline`` is a ``time.monotonic()`` value. Lines already in the
        buffer are returned without waiting.
        """
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
            elif len(self._buffer) > MAX_RESPONSE_CHARS:
                raise JevError("response_too_large")
            elif not self._ready(deadline - time.monotonic()):
                raise JevError(self._prefix + "timeout")
            elif self._read():
                continue
            else:
                line = bytes(self._buffer)
                self._buffer.clear()
            if len(line) > MAX_RESPONSE_CHARS:
                raise JevError("response_too_large")
            try:
                return line.decode("utf-8")
            except UnicodeDecodeError as error:
                raise JevError("invalid_utf8") from error


def _decode(value):
    """Unwrap direct JSON and MCP text/structured-content responses."""
    if isinstance(value, str):
        try:
            return _decode(json.loads(value))
        except (json.JSONDecodeError, TypeError) as error:
            raise JevError("invalid_json") from error
    if not isinstance(value, dict):
        raise JevError("response_not_object")
    if value.get("isError") is True:
        raise JevError("mcp_error")
    if value.get("error") is not None:
        raise JevError("mcp_error")
    if "result" in value and value.get("result") is not None:
        return _decode(value["result"])
    if isinstance(value.get("answers"), dict):
        return value
    structured = value.get("structuredContent")
    if structured is not None:
        decoded = _decode(structured)
        if "usage" in value and "usage" not in decoded:
            decoded = dict(decoded)
            decoded["usage"] = value["usage"]
        return decoded
    content = value.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                decoded = _decode(item.get("text", ""))
                if "usage" in value and "usage" not in decoded:
                    decoded = dict(decoded)
                    decoded["usage"] = value["usage"]
                return decoded
    raise JevError("typed_answers_missing")


def _with_wall_latency(response, elapsed_ms):
    """Keep provider latency while exposing measured end-to-end latency."""
    response = dict(response)
    usage = response.get("usage", {})
    usage = dict(usage) if isinstance(usage, dict) else {}
    reported = usage.get("latency_ms")
    if (
        "provider_latency_ms" not in usage
        and not isinstance(reported, bool)
        and isinstance(reported, (int, float))
    ):
        usage["provider_latency_ms"] = reported
    usage["latency_ms"] = round(elapsed_ms, 3)
    response["usage"] = usage
    return response


@dataclass
class DirectJev:
    """Wrap a host-provided Jev callable without a transport process.

    ``evaluate`` receives ``(state, questions)`` and may return the direct Jev
    JSON payload or an MCP ``CallToolResult`` wrapper. The caller owns the host
    tool; this wrapper only validates and measures its result.
    """

    evaluate: object
    usage: JevUsage = field(default_factory=JevUsage)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        if not callable(self.evaluate):
            raise TypeError("DirectJev evaluate must be callable")
        self.usage.transport = "direct"

    def __call__(self, state, questions):
        with self._lock:
            self.usage.attempts += 1
            started = time.monotonic()
            try:
                response = _decode(self.evaluate(state, questions))
            except JevError:
                self.usage.errors += 1
                self.usage.latency_ms += (time.monotonic() - started) * 1000
                raise
            except Exception as error:
                self.usage.errors += 1
                self.usage.latency_ms += (time.monotonic() - started) * 1000
                raise JevError(type(error).__name__) from error
            elapsed_ms = (time.monotonic() - started) * 1000
            response = _with_wall_latency(response, elapsed_ms)
            self.usage.record(response, elapsed_ms=elapsed_ms)
            return response

    def close(self):
        """Match the command adapter lifecycle; direct calls need no cleanup."""


def host_mcp_evaluator(evaluate, model="jev-latest"):
    """Adapt a host MCP binding to Naru's direct Jev boundary.

    Host MCP bindings commonly expose keyword arguments such as
    ``evaluate(model=..., state=..., questions=...)``. Naru's policy boundary
    stays smaller and calls ``(state, questions)``. This adapter connects the
    two shapes without starting a process or copying credentials into Naru.
    """
    if not callable(evaluate):
        raise TypeError("host MCP evaluate must be callable")
    return DirectJev(
        lambda state, questions: evaluate(
            model=model, state=state, questions=questions
        )
    )


def host_stdio_evaluator(
    model="jev-latest", input_stream=None, output_stream=None, timeout=30
):
    """Bridge a parent-owned MCP binding over an interactive stdio protocol.

    The benchmark writes a request marker and JSON payload to ``output_stream``
    and waits for one JSON-encoded MCP result on ``input_stream``. A host such
    as Codex can service that request with its native MCP binding. The Jev key
    never enters the benchmark process.

    Each request carries a ``request_id`` number. A host may copy it unchanged
    into the top level of its response line. A response with a different
    ``request_id`` answers an earlier request that timed out, so it is skipped
    and the wait continues until the deadline. A response without
    ``request_id`` is accepted. On a pipe or terminal, lines that are already
    waiting when a request is sent are dropped for the same reason: the host
    must answer after it reads the request.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("host Jev timeout must be positive")
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stderr if output_stream is None else output_stream
    prefix = "NARU_JEV_REQUEST "

    def open_reader():
        """Return a deadline reader for a pipe or terminal, else None."""
        try:
            descriptor = input_stream.fileno()
        except (AttributeError, io.UnsupportedOperation, ValueError):
            # In-memory streams used by library callers are already bounded by
            # their caller. Real stdin and pipes expose fileno below.
            return None
        try:
            mode = os.fstat(descriptor).st_mode
        except OSError as error:
            raise JevError("host_timeout_unsupported") from error
        if stat.S_ISREG(mode):
            return None
        return _LineReader(descriptor, "host_")

    # The reader keeps bytes between calls, so it is opened once. DirectJev
    # serializes evaluate(), which makes this state safe without a lock.
    reader = unopened = object()
    request_id = 0

    def evaluate(state, questions):
        nonlocal reader, request_id
        if reader is unopened:
            reader = open_reader()
        request_id += 1
        request = {
            "request_id": request_id,
            "model": model,
            "state": state,
            "questions": questions,
        }
        try:
            encoded = json.dumps(request, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise JevError("request_not_json") from error
        if reader is not None:
            reader.discard_pending()
        output_stream.write(f"\n{prefix}{encoded}\n")
        output_stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            response = (
                input_stream.readline()
                if reader is None
                else reader.readline(deadline)
            )
            if not response:
                raise JevError("host_response_eof")
            if len(response) > MAX_RESPONSE_CHARS:
                raise JevError("response_too_large")
            try:
                decoded = json.loads(response)
            except json.JSONDecodeError as error:
                raise JevError("invalid_host_json") from error
            if (
                isinstance(decoded, dict)
                and decoded.get("request_id", request_id) != request_id
            ):
                continue
            return decoded

    return DirectJev(evaluate)


@dataclass
class CommandJev:
    """Invoke a Jev adapter over a persistent JSON-lines process.

    ``protocol='auto'`` recognizes the installed ``jev mcp`` command and speaks
    MCP JSON-RPC over newline-delimited stdio. Other commands use the small
    direct JSON-lines adapter protocol. ``persistent=False`` retains a
    compatibility path for one-shot JSON-lines commands, but the persistent
    path is the default because it removes process startup from every
    subsequent Jev decision.
    """

    command: str
    model: str = "jev-latest"
    timeout: int = 30
    persistent: bool = True
    usage: JevUsage = field(default_factory=JevUsage)
    protocol: str = "auto"
    argv: list = field(init=False, repr=False)
    _process: object = field(init=False, default=None, repr=False)
    _reader: object = field(init=False, default=None, repr=False)
    _mcp_initialized: bool = field(init=False, default=False, repr=False)
    _request_id: int = field(init=False, default=0, repr=False)

    def __post_init__(self):
        try:
            self.argv = shlex.split(self.command)
        except ValueError as error:
            raise ValueError(f"invalid NARU_JEV command: {error}") from error
        if not self.argv:
            raise ValueError("NARU_JEV is empty; expected a command to run")
        if shutil.which(self.argv[0]) is None:
            raise FileNotFoundError(
                f"NARU_JEV command not found: {self.argv[0]!r}"
            )
        if self.timeout < 1:
            raise ValueError("NARU_JEV timeout must be positive")
        if self.protocol == "auto":
            self.protocol = (
                "mcp"
                if os.path.basename(self.argv[0]) == "jev"
                and len(self.argv) > 1
                and self.argv[1] == "mcp"
                else "jsonl"
            )
        if self.protocol not in ("jsonl", "mcp"):
            raise ValueError("NARU_JEV protocol must be jsonl, mcp, or auto")
        if self.protocol == "mcp" and not self.persistent:
            raise ValueError("MCP Jev requires a persistent adapter")
        self.usage.transport = (
            "mcp-stdio"
            if self.protocol == "mcp"
            else "persistent" if self.persistent else "oneshot"
        )

    def _start(self):
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise JevError(type(error).__name__) from error
        self.usage.starts += 1
        self.usage.startup_latency_ms += (time.monotonic() - started) * 1000
        self._process = process
        self._reader = _LineReader(process.stdout.fileno())
        return process

    def _stop(self):
        process = self._process
        self._process = None
        self._reader = None
        self._mcp_initialized = False
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def close(self):
        """Stop a persistent adapter without exposing its stderr."""
        self._stop()

    def _readline(self, deadline):
        line = self._reader.readline(deadline)
        if not line:
            raise JevError("adapter_eof")
        return line

    def _persistent_request(self, request):
        process = self._process
        if process is None or process.poll() is not None:
            self._stop()
            process = self._start()
        else:
            # A line that is already waiting was written before this request,
            # so it answers an earlier one. A new child has no earlier request
            # and may answer before it reads, so its output is kept.
            self._reader.discard_pending()
        if self.protocol == "mcp":
            if not self._mcp_initialized:
                self._initialize_mcp(process)
            return self._mcp_call(process, request)
        request = request if isinstance(request, str) else json.dumps(
            request, separators=(",", ":")
        )
        if process.stdin is None:
            raise JevError("adapter_stdin_unavailable")
        try:
            process.stdin.write(request + "\n")
            process.stdin.flush()
        except OSError as error:
            raise JevError("adapter_write_failed") from error
        return self._readline(time.monotonic() + self.timeout)

    def _next_request_id(self):
        self._request_id += 1
        return self._request_id

    def _send_mcp(self, process, message):
        if process.stdin is None:
            raise JevError("adapter_stdin_unavailable")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (OSError, TypeError, ValueError) as error:
            raise JevError("adapter_write_failed") from error

    def _read_mcp(self, deadline):
        try:
            return json.loads(self._readline(deadline))
        except json.JSONDecodeError as error:
            raise JevError("invalid_mcp_json") from error

    def _rpc(self, request_id):
        # One deadline for the whole exchange. A timeout per line would let a
        # server that keeps sending notifications hold the call forever.
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._read_mcp(deadline)
            if not isinstance(message, dict):
                raise JevError("mcp_message_not_object")
            if message.get("id") != request_id:
                # Notifications and server requests are not part of this
                # one-way adapter contract. Ignore notifications, but do not
                # let an unrelated response satisfy the current call.
                continue
            if message.get("error") is not None:
                raise JevError("mcp_error")
            if "result" not in message:
                raise JevError("mcp_result_missing")
            return message["result"]

    def _initialize_mcp(self, process):
        started = time.monotonic()
        request_id = self._next_request_id()
        self._send_mcp(
            process,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "naru", "version": "1"},
                },
            },
        )
        result = self._rpc(request_id)
        if not isinstance(result, dict):
            raise JevError("mcp_initialize_invalid")
        self._send_mcp(
            process,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self._mcp_initialized = True
        self.usage.startup_latency_ms += (time.monotonic() - started) * 1000

    def _mcp_call(self, process, payload):
        request_id = self._next_request_id()
        self._send_mcp(
            process,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": "evaluate",
                    "arguments": payload,
                },
            },
        )
        return self._rpc(request_id)

    def _oneshot_request(self, request):
        try:
            process = subprocess.run(
                self.argv,
                input=request,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise JevError(type(error).__name__) from error
        if process.returncode:
            raise JevError("command_failed")
        if len(process.stdout) > MAX_RESPONSE_CHARS:
            raise JevError("response_too_large")
        return process.stdout

    def __call__(self, state, questions):
        payload = {"model": self.model, "state": state, "questions": questions}
        # Count the attempt first, so errors can never exceed attempts.
        self.usage.attempts += 1
        try:
            request = json.dumps(payload, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            self.usage.errors += 1
            raise JevError("request_not_json") from error
        started = time.monotonic()
        try:
            raw = (
                self._persistent_request(payload)
                if self.persistent
                else self._oneshot_request(request)
            )
            response = _decode(raw)
        except Exception as error:
            # Every failure is handled here, whatever its type. An exception
            # that escaped would leave the child alive mid-exchange, and its
            # late answer would be read by the next call.
            self.usage.errors += 1
            self.usage.latency_ms += (time.monotonic() - started) * 1000
            if self.persistent:
                self._stop()
            if isinstance(error, JevError):
                raise
            raise JevError(type(error).__name__) from error
        elapsed_ms = (time.monotonic() - started) * 1000
        response = _with_wall_latency(response, elapsed_ms)
        self.usage.record(response, elapsed_ms=elapsed_ms)
        # A one-shot adapter is still accepted by the persistent protocol on
        # its first response. If it exits after answering, restart it next time.
        if self.persistent and self._process is not None and self._process.poll() is not None:
            self._stop()
        return response


def demo():
    """Offline checks for the typed command boundary."""
    import sys

    response = {
        "answers": {
            "source-index": {
                "type": "choice",
                "choice": "omit",
                "confidence": 0.93,
            }
        },
        "usage": {
            "input_tokens": 4,
            "cache_read_input_tokens": 1,
            "cache_creation_input_tokens": 2,
            "output_tokens": 2,
        },
    }
    command = shlex.join(
        [sys.executable, "-c", "import json; print(json.dumps(" + repr(response) + "))"]
    )
    # The generated expression uses Python literals, so the subprocess needs
    # only the stdlib and cannot access this process's request state.
    jev = CommandJev(command, persistent=False)
    result = jev({"goal": "g"}, {"source-index": {"type": "choice"}})
    assert result["answers"]["source-index"]["choice"] == "omit"
    assert (
        jev.usage.calls == 1
        and jev.usage.input_tokens == 4
        and jev.usage.cache_read_tokens == 1
        and jev.usage.cache_write_tokens == 2
        and jev.usage.as_dict()["cache_read_tokens"] == 1
        and jev.usage.as_dict()["cache_write_tokens"] == 2
    )

    stream_code = (
        "import json,sys\n"
        "response="
        + repr(response)
        + "\nfor line in sys.stdin:\n print(json.dumps(response), flush=True)\n"
    )
    stream_command = shlex.join([sys.executable, "-u", "-c", stream_code])
    stream = CommandJev(stream_command)
    stream({"goal": "g"}, {"source-index": {"type": "choice"}})
    stream({"goal": "g"}, {"source-index": {"type": "choice"}})
    assert stream.usage.calls == 2 and stream.usage.starts == 1
    assert stream.usage.transport == "persistent"
    stream.close()

    # An adapter that writes one line too many must not shift every later
    # answer by one request.
    extra_code = (
        "import json,sys\n"
        "response="
        + repr(response)
        + "\ncount=0\nfor line in sys.stdin:\n count+=1\n"
        " out=json.dumps(dict(response, count=count))+'\\n'\n"
        " sys.stdout.write(out+out); sys.stdout.flush()\n"
    )
    extra = CommandJev(shlex.join([sys.executable, "-u", "-c", extra_code]))
    counts = [
        extra({"goal": "g"}, {"source-index": {"type": "choice"}})["count"]
        for _ in range(3)
    ]
    assert counts == [1, 2, 3], counts

    # Any failure inside a persistent call is one counted error, a stopped
    # child, and a JevError, whatever the exception type was.
    child = extra._process

    def broken(_request):
        raise KeyError("not a transport error")

    extra._persistent_request = broken
    try:
        extra({"goal": "g"}, {})
    except JevError as error:
        assert str(error) == "KeyError"
    else:
        raise AssertionError("an unexpected adapter failure was not reported")
    assert extra.usage.errors == 1 and extra.usage.attempts == 4
    assert extra._process is None and child.poll() is not None
    try:
        extra({"goal": object()}, {})
    except JevError as error:
        assert str(error) == "request_not_json"
    else:
        raise AssertionError("an unserializable request was accepted")
    assert extra.usage.errors == 2 and extra.usage.attempts == 5
    extra.close()

    mcp_code = (
        "import json,sys\n"
        "response="
        + repr(response)
        + "\nfor line in sys.stdin:\n"
        " message=json.loads(line)\n"
        " if message.get('method') == 'initialize':\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],"
        "'result':{'protocolVersion':'2024-11-05','capabilities':{},"
        "'serverInfo':{'name':'test','version':'1'}}}), flush=True)\n"
        " elif message.get('method') == 'tools/call':\n"
        "  assert message['params']['name'] == 'evaluate'\n"
        "  assert set(message['params']['arguments']) == {'model','state','questions'}\n"
        "  result={'content':[{'type':'text','text':json.dumps(response)}],"
        "'isError':False}\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],"
        "'result':result}), flush=True)\n"
    )
    mcp_command = shlex.join([sys.executable, "-u", "-c", mcp_code])
    mcp = CommandJev(mcp_command, protocol="mcp")
    mcp({"goal": "g"}, {"source-index": {"type": "choice"}})
    mcp({"goal": "g"}, {"source-index": {"type": "choice"}})
    assert mcp.usage.calls == 2 and mcp.usage.starts == 1
    assert mcp.usage.transport == "mcp-stdio"
    assert mcp.usage.startup_latency_ms >= 0
    mcp.close()

    # The server sends notifications for longer than the timeout and then
    # answers. One deadline covers the call, so the answer comes too late.
    flood_code = (
        "import json,sys,time\n"
        "for line in sys.stdin:\n"
        " message=json.loads(line)\n"
        " if message.get('method') == 'initialize':\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{}}),"
        " flush=True)\n"
        " elif message.get('method') == 'tools/call':\n"
        "  end=time.time()+2.5\n"
        "  while time.time() < end:\n"
        "   print(json.dumps({'jsonrpc':'2.0','method':'notifications/progress'}),"
        " flush=True)\n"
        "   time.sleep(0.1)\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':message['id'],"
        "'result':{'answers':{}}}), flush=True)\n"
    )
    flood = CommandJev(
        shlex.join([sys.executable, "-u", "-c", flood_code]),
        protocol="mcp",
        timeout=1,
    )
    try:
        flood({"goal": "g"}, {})
    except JevError as error:
        assert str(error) == "timeout"
    else:
        raise AssertionError("MCP notifications extended the call deadline")
    assert flood.usage.errors == 1 and flood._process is None
    flood.close()

    # Needs the installed command. CI has none, and this unguarded check
    # failed every run of the mutation suite there.
    if shutil.which("jev"):
        auto = CommandJev("jev mcp")
        assert auto.protocol == "mcp" and auto.usage.transport == "mcp-stdio"
        auto.close()

    wrapped = {
        "content": [{"type": "text", "text": json.dumps(response)}],
        "isError": False,
    }
    assert _decode({"jsonrpc": "2.0", "id": 1, "result": wrapped})[
        "answers"
    ] == response["answers"]
    assert _decode(wrapped)["answers"] == response["answers"]

    host_seen = {}

    def host_mcp(*, model, state, questions):
        host_seen.update(model=model, state=state, questions=questions)
        return wrapped

    host = host_mcp_evaluator(host_mcp)
    host_result = host({"goal": "g"}, {"source-index": {"type": "choice"}})
    assert host_result["answers"] == response["answers"]
    assert host.usage.transport == "direct"
    assert host_seen["model"] == "jev-latest"
    assert host_seen["state"] == {"goal": "g"}
    assert host_seen["questions"] == {"source-index": {"type": "choice"}}
    host.close()

    from io import StringIO

    host_input = StringIO(json.dumps(wrapped) + "\n")
    host_output = StringIO()
    stdio = host_stdio_evaluator(
        input_stream=host_input,
        output_stream=host_output,
    )
    stdio_result = stdio({"goal": "g"}, {"source-index": {"type": "choice"}})
    assert stdio_result["answers"] == response["answers"]
    request_line = next(
        line for line in host_output.getvalue().splitlines()
        if line.startswith("NARU_JEV_REQUEST ")
    )
    request = json.loads(request_line.removeprefix("NARU_JEV_REQUEST "))
    assert request["request_id"] == 1
    assert request["model"] == "jev-latest"
    assert request["state"] == {"goal": "g"}
    assert request["questions"] == {"source-index": {"type": "choice"}}
    assert stdio.usage.transport == "direct"
    stdio.close()

    read_fd, write_fd = os.pipe()
    silent_input = os.fdopen(read_fd, "r")
    try:
        silent = host_stdio_evaluator(
            input_stream=silent_input,
            output_stream=StringIO(),
            timeout=0.01,
        )
        try:
            silent({"goal": "g"}, {"source-index": {"type": "choice"}})
        except JevError as error:
            assert str(error) == "host_timeout"
        else:
            raise AssertionError("a silent host pipe was allowed to block")
        # The host answers the timed-out request late. The next request must
        # not take that line as its own answer.
        os.write(write_fd, (json.dumps(wrapped) + "\n").encode())
        try:
            silent({"goal": "g"}, {"source-index": {"type": "choice"}})
        except JevError as error:
            assert str(error) == "host_timeout"
        else:
            raise AssertionError("a late host answer was used for the next request")
    finally:
        silent_input.close()
        os.close(write_fd)

    # A response that names another request is skipped, not returned.
    stale = dict(response, request_id=7, stale=True)
    current = dict(response, request_id=1)
    matched = host_stdio_evaluator(
        input_stream=StringIO(json.dumps(stale) + "\n" + json.dumps(current) + "\n"),
        output_stream=StringIO(),
    )({"goal": "g"}, {"source-index": {"type": "choice"}})
    assert "stale" not in matched and matched["request_id"] == 1

    read_fd, write_fd = os.pipe()
    try:
        reader = _LineReader(read_fd)
        # Two lines in one chunk: the second is served from the buffer.
        os.write(write_fd, b"one\ntwo\n")
        assert reader.readline(time.monotonic() + 1) == "one\n"
        assert reader.readline(time.monotonic()) == "two\n"
        os.write(write_fd, b"\xff\n")
        try:
            reader.readline(time.monotonic() + 1)
        except JevError as error:
            assert str(error) == "invalid_utf8"
        else:
            raise AssertionError("invalid UTF-8 was returned as a line")

        # A peer that sends a line slowly cannot hold the read past the
        # deadline. The writer finishes by itself so a broken reader fails
        # this check instead of hanging it.
        def slow_line():
            for _ in range(30):
                os.write(write_fd, b"x")
                time.sleep(0.01)
            os.write(write_fd, b"\n")

        writer = threading.Thread(target=slow_line)
        writer.start()
        try:
            reader.readline(time.monotonic() + 0.05)
        except JevError as error:
            assert str(error) == "timeout"
        else:
            raise AssertionError("a partial line outlived the read deadline")
        finally:
            # The first half of the late line is discarded here. Its second
            # half arrives afterwards and must be dropped with it.
            reader.discard_pending()
            writer.join()
        os.write(write_fd, b"next\n")
        assert reader.readline(time.monotonic() + 1) == "next\n"
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert usage_number(7) == 7 and usage_number(2.5) == 2.5
    assert all(
        usage_number(value) == 0
        for value in ("n/a", None, True, -1, float("inf"), float("nan"))
    )
    unusable = JevUsage()
    unusable.record({"usage": {"input_tokens": float("inf"), "output_tokens": -2}})
    assert unusable.input_tokens == 0 and unusable.output_tokens == 0
    assert unusable.calls == 1 and not unusable.tokens_measured

    try:
        _decode({"isError": True, "content": [{"type": "text", "text": "secret"}]})
    except JevError as error:
        assert str(error) == "mcp_error"
    else:
        raise AssertionError("MCP error response was accepted")
    try:
        _decode('{"answers": []}')
    except JevError as error:
        assert str(error) == "typed_answers_missing"
    else:
        raise AssertionError("untyped Jev response was accepted")

    failed_command = shlex.join(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('SECRET-STDERR'); sys.exit(1)",
        ]
    )
    failed = CommandJev(failed_command, persistent=False)
    try:
        failed({}, {})
    except JevError as error:
        assert str(error) == "command_failed"
        assert "SECRET-STDERR" not in str(error)
    else:
        raise AssertionError("failed Jev command was accepted")
    print("ok — Jev bridge checks passed")


if __name__ == "__main__":
    demo()
