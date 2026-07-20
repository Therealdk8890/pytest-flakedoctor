"""Repro blob encoding, decoding, and refusal behavior."""

from __future__ import annotations

import pytest

from flakedoctor._axes import FS, HASHSEED, NETWORK, RNG, TIME, merge
from flakedoctor._repro import PREFIX, Repro, ReproFormatError, decode

NODEID = "tests/test_billing.py::test_rollover"


def _roundtrip(values, **kwargs) -> Repro:
    blob = Repro(values=values, nodeid=NODEID, tool="0.1.0", **kwargs).encode()
    assert blob.startswith(PREFIX)
    return decode(blob, nodeid=NODEID)


def test_roundtrip_time_axis():
    value = TIME.provocations()[1]  # month-end
    back = _roundtrip([value])
    assert len(back.values) == 1
    assert back.values[0].axis == "time"
    assert back.values[0].value == value.value
    assert back.values[0].sandbox == value.sandbox


def test_roundtrip_each_axis():
    for axis in (TIME, RNG, NETWORK, FS, HASHSEED):
        value = axis.provocations()[0]
        back = _roundtrip([value])
        assert back.values[0].axis == axis.id
        assert back.values[0].value == value.value
        assert back.values[0].sandbox == value.sandbox
        assert back.values[0].env == value.env


def test_hashseed_travels_as_env_not_sandbox():
    value = HASHSEED.provocations()[0]
    back = _roundtrip([value])
    assert back.hashseed == value.value
    assert back.sandbox_kwargs() is None  # nothing for hermetic to do


def test_sandbox_kwargs_switch_untouched_axes_off():
    kwargs = _roundtrip([TIME.provocations()[0]]).sandbox_kwargs()
    # hermetic defaults everything ON, so a single-axis repro must disable the rest.
    assert kwargs["clock"] == "virtual"
    assert kwargs["rng"] == "off"
    assert kwargs["network"] == "off"
    assert kwargs["fs"] == "off"


def test_timezone_is_pinned_into_sandbox_env():
    back = _roundtrip([TIME.provocations()[0]], tz="America/Chicago")
    assert back.tz == "America/Chicago"
    assert back.sandbox_kwargs()["env"]["TZ"] == "America/Chicago"


def test_confirm_and_fingerprint_survive():
    back = _roundtrip([TIME.provocations()[0]], confirm=(10, 10), fingerprint="abc123")
    assert back.confirm == (10, 10)
    assert back.fingerprint == "abc123"


def test_rebuilds_arbitrary_frozen_instant():
    """Boundary bisection will emit instants not in the fixed provocation list."""
    custom = TIME.provocations()[0]
    custom = type(custom)(
        axis="time",
        value="frozen@2021-06-15T12:34:56+00:00",
        sandbox={"clock": "virtual", "now": "2021-06-15T12:34:56+00:00", "tick": 1e-6},
        label="custom",
    )
    back = _roundtrip([custom])
    assert back.values[0].sandbox["now"] == "2021-06-15T12:34:56+00:00"


def test_rejects_non_blob():
    with pytest.raises(ReproFormatError, match="not a flakedoctor repro blob"):
        decode("just some text")


def test_rejects_corrupt_blob():
    with pytest.raises(ReproFormatError, match="corrupt"):
        decode(PREFIX + "bm90LXZhbGlkLXpsaWI=")


def test_rejects_unknown_axis():
    import base64, json, zlib

    payload = {"v": 1, "axes": {"quantum": {"v": 1, "value": "x"}}}
    raw = zlib.compress(json.dumps(payload).encode())
    blob = PREFIX + base64.urlsafe_b64encode(raw).decode()
    with pytest.raises(ReproFormatError, match="does not know"):
        decode(blob)


def test_rejects_future_payload_version():
    import base64, json, zlib

    payload = {"v": 99, "axes": {}}
    raw = zlib.compress(json.dumps(payload).encode())
    blob = PREFIX + base64.urlsafe_b64encode(raw).decode()
    with pytest.raises(ReproFormatError, match="payload version 99"):
        decode(blob)


def test_rejects_future_axis_payload_version():
    import base64, json, zlib

    payload = {"v": 1, "axes": {"time": {"v": 7, "value": "frozen@x"}}}
    raw = zlib.compress(json.dumps(payload).encode())
    blob = PREFIX + base64.urlsafe_b64encode(raw).decode()
    with pytest.raises(ReproFormatError, match="cannot apply"):
        decode(blob)


def test_rejects_blob_recorded_for_another_test():
    blob = Repro(values=[TIME.provocations()[0]], nodeid=NODEID).encode()
    with pytest.raises(ReproFormatError, match="different test"):
        decode(blob, nodeid="tests/test_other.py::test_thing")


def test_blob_cannot_smuggle_arbitrary_sandbox_kwargs():
    """Values are rebuilt from axis definitions, never taken verbatim."""
    import base64, json, zlib

    payload = {
        "v": 1,
        "axes": {"time": {"v": 1, "value": "frozen@2020-01-01T00:00:00+00:00"}},
        "evil": {"record": "/tmp/pwned"},
    }
    raw = zlib.compress(json.dumps(payload).encode())
    blob = PREFIX + base64.urlsafe_b64encode(raw).decode()
    kwargs = decode(blob).sandbox_kwargs()
    assert "record" not in kwargs
    assert set(kwargs) <= {"clock", "rng", "network", "fs", "now", "tick", "env"}


def test_merge_of_no_values_means_no_sandbox():
    sandbox, env = merge([])
    assert sandbox is None
    assert env == {}


# --------------------------------------- decode robustness (marker safety)

def test_decode_rejects_non_string_blob():
    with pytest.raises(ReproFormatError, match="must be a string"):
        decode(b"fd1:whatever")  # a bytes marker arg must not raise TypeError


def test_decode_tolerates_non_numeric_confirm():
    import base64, json, zlib
    payload = {"v": 1, "axes": {"time": {"v": 1, "value": "frozen@2020-01-01T00:00:00+00:00"}},
               "confirm": ["x", "y"]}
    blob = PREFIX + base64.urlsafe_b64encode(zlib.compress(json.dumps(payload).encode())).decode()
    r = decode(blob)  # must not raise ValueError from int("x")
    assert r.confirm == (0, 0)


def test_decode_rejects_axes_as_list():
    import base64, json, zlib
    payload = {"v": 1, "axes": [1, 2, 3]}
    blob = PREFIX + base64.urlsafe_b64encode(zlib.compress(json.dumps(payload).encode())).decode()
    with pytest.raises(ReproFormatError):  # not a bare AttributeError
        decode(blob)
