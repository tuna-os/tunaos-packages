import importlib.util
import io
import pathlib
import sys
import types
from urllib.error import HTTPError

sys.modules.setdefault("tideforge_cache", types.SimpleNamespace())
SPEC = importlib.util.spec_from_file_location(
    "verify_tideforge_source",
    pathlib.Path(__file__).parents[1] / "scripts" / "verify-tideforge-source.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def test_download_retries_transient_http_failure(monkeypatch):
    attempts = iter([HTTPError("https://example.test", 502, "bad gateway", {}, None), Response(b"ok")])

    def respond(*_args, **_kwargs):
        result = next(attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(MODULE, "urlopen", respond)
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)

    digest, payload = MODULE.download(MODULE.Request("https://example.test"), keep_payload=True)

    assert digest == "2689367b205c16ce32ed4200942b8b8b1e262dfc70d9bc9fbc77c49699a4f1df"
    assert payload == b"ok"


def test_download_does_not_retry_permanent_http_failure(monkeypatch):
    calls = 0

    def fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise HTTPError("https://example.test", 404, "not found", {}, None)

    monkeypatch.setattr(MODULE, "urlopen", fail)

    try:
        MODULE.download(MODULE.Request("https://example.test"), keep_payload=False)
    except HTTPError as exc:
        assert exc.code == 404
    else:
        raise AssertionError("permanent HTTP error was not raised")
    assert calls == 1
