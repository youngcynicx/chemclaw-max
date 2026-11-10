"""Free loopback health snapshots, with an honest pre-work baseline boundary.

Only explicit health/static-root GETs are allowed. No provider probe, response
body, credentials, redirect, retry, service start or business write is involved.
The caller records `before` before the first test effect, then compares against
that effect's actual timestamp. A missing/late baseline stays unverified.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_HEALTH_PATHS = {"/", "/health", "/healthz", "/api/core/health"}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Health timestamps must be bounded timezone-aware ISO text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid health timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("Health timestamps require a timezone")
    return parsed


def _endpoints(values):
    if not isinstance(values, dict) or not 1 <= len(values) <= 8:
        raise ValueError("Provide one to eight named loopback health endpoints")
    result = {}
    for name, url in values.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("Invalid health endpoint label")
        if not isinstance(url, str) or len(url) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError("Invalid health URL")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("Invalid health port") from None
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in _LOCAL_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in _HEALTH_PATHS
                or port is not None and not 1 <= port <= 65535):
            raise ValueError("Only credential-free loopback health/static-root URLs are allowed")
        result[name] = url
    return result


def _identities(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError("Process identities must be a bounded list")
    result = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"pid", "created_at_utc", "name"}:
            raise ValueError("Process identities accept pid/created_at_utc/name only")
        if type(value["pid"]) is not int or value["pid"] < 1:
            raise ValueError("Invalid process PID")
        _time(value["created_at_utc"])
        if not isinstance(value["name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value["name"]):
            raise ValueError("Process identity name must be a basename, not a command or path")
        result.append(dict(value))
    return sorted(result, key=lambda item: (item["pid"], item["created_at_utc"], item["name"]))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        return None


def capture_health(endpoints, *, timeout_s=2.0, phase="snapshot", process_identities=None):
    """Capture safe metadata for explicit local endpoints; GET exactly once each."""
    urls = _endpoints(endpoints)
    identities = _identities(process_identities)
    if phase not in {"before", "after", "snapshot"}:
        raise ValueError("Health phase must be before, after or snapshot")
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 10:
        raise ValueError("Health timeout must be greater than zero and at most ten seconds")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    result = {"version": 1, "phase": phase, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "GET", "provider_calls": 0, "process_identities": identities, "endpoints": {}}
    for name, url in urls.items():
        started = time.monotonic()
        record = {"url": url, "state": "unavailable", "http_status": None, "error_class": None}
        try:
            with opener.open(Request(url, method="GET"), timeout=timeout_s) as response:
                record["http_status"] = response.status
                record["state"] = "healthy" if response.status == 200 else "http_error"
                # Health is transport/status evidence; never retain model/config/body data.
        except HTTPError as error:
            record.update(state="http_error", http_status=error.code, error_class="HTTPError")
            error.close()
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            record["error_class"] = type(error).__name__
        record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        result["endpoints"][name] = record
    result["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def _save_exclusive(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())


def record_health(path, endpoints, *, phase, timeout_s=2.0, process_identities=None,
                  work_started_at_utc=None):
    """Persist a single immutable snapshot. A known-late `before` is refused."""
    if Path(path).exists():
        raise FileExistsError("Existing health evidence cannot be overwritten")
    if phase == "before" and work_started_at_utc is not None:
        if datetime.now(timezone.utc) >= _time(work_started_at_utc):
            raise ValueError("Cannot record a before baseline after test work has started")
    snapshot = capture_health(endpoints, phase=phase, timeout_s=timeout_s,
        process_identities=process_identities)
    if phase == "before" and work_started_at_utc is not None:
        if _time(snapshot["completed_at_utc"]) >= _time(work_started_at_utc):
            raise ValueError("Health baseline did not finish before test work started")
    _save_exclusive(path, snapshot)
    return snapshot


def compare_health(before, after, *, work_started_at_utc=None):
    """Report observations, never attribute a service failure to the tested code.

    Actual first-work time is required to validate that `before` was pre-work.
    Without that anchor, even two healthy snapshots are baseline_time_unverified.
    Process identity changes also make a before/after comparison incomparable.
    """
    if not isinstance(after, dict) or after.get("phase") != "after":
        raise ValueError("An after-phase health snapshot is required")
    after_at = _time(after.get("captured_at_utc"))
    after_records = after.get("endpoints")
    if not isinstance(after_records, dict):
        raise ValueError("Invalid after health snapshot")
    result = {"version": 1, "causality": "not_established", "baseline_validated": False,
        "classification": "baseline_missing", "endpoints": {},
        "scope": "Before/after status observations only; no continuous availability or causal failure proof."}
    if before is None:
        return result
    if not isinstance(before, dict) or before.get("phase") != "before":
        result["classification"] = "incomparable_phase"
        return result
    before_at = _time(before.get("captured_at_utc"))
    before_completed_at = _time(before.get("completed_at_utc"))
    if work_started_at_utc is None:
        result["classification"] = "baseline_time_unverified"
        return result
    work_at = _time(work_started_at_utc)
    if before_completed_at >= work_at:
        result["classification"] = "baseline_recorded_after_work"
        return result
    if before_completed_at < before_at or after_at < work_at or after_at < before_completed_at:
        result["classification"] = "incomparable_time_order"
        return result
    before_records = before.get("endpoints")
    if not isinstance(before_records, dict) or set(before_records) != set(after_records):
        result["classification"] = "incomparable_endpoints"
        return result
    if _identities(before.get("process_identities")) != _identities(after.get("process_identities")):
        result["classification"] = "incomparable_process_identities"
        return result
    for name, current in after_records.items():
        prior = before_records[name]
        if not isinstance(prior, dict) or not isinstance(current, dict) or prior.get("url") != current.get("url"):
            result["classification"] = "incomparable_endpoints"
            result["endpoints"] = {}
            return result
        old, new = prior.get("state"), current.get("state")
        state = ("healthy_before_and_after" if old == new == "healthy" else
            "health_degraded_observed" if old == "healthy" else
            "recovered_observed" if new == "healthy" else "baseline_unavailable")
        result["endpoints"][name] = {"before": old, "after": new, "classification": state}
    result["baseline_validated"] = True
    states = {record["classification"] for record in result["endpoints"].values()}
    result["classification"] = ("health_degraded_observed" if "health_degraded_observed" in states else
        "baseline_unavailable" if "baseline_unavailable" in states else
        "recovered_observed" if "recovered_observed" in states else "healthy_before_and_after")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--phase", required=True, choices=("before", "after"))
    capture.add_argument("--endpoint", action="append", required=True, help="name=loopback-health-URL")
    capture.add_argument("--timeout", type=float, default=2)
    capture.add_argument("--work-started-at-utc")
    compare = commands.add_parser("compare")
    compare.add_argument("--before", type=Path)
    compare.add_argument("--after", required=True, type=Path)
    compare.add_argument("--output", required=True, type=Path)
    compare.add_argument("--work-started-at-utc")
    args = parser.parse_args()
    if args.operation == "capture":
        endpoints = {}
        for value in args.endpoint:
            name, separator, url = value.partition("=")
            if not separator or name in endpoints:
                parser.error("Each endpoint needs a unique name=URL")
            endpoints[name] = url
        record_health(args.output, endpoints, phase=args.phase, timeout_s=args.timeout,
            work_started_at_utc=args.work_started_at_utc)
        print(json.dumps({"output": str(args.output), "phase": args.phase, "provider_calls": 0}))
    else:
        before = json.loads(args.before.read_text(encoding="utf-8-sig")) if args.before and args.before.is_file() else None
        after = json.loads(args.after.read_text(encoding="utf-8-sig"))
        result = compare_health(before, after, work_started_at_utc=args.work_started_at_utc)
        _save_exclusive(args.output, result)
        print(json.dumps({"output": str(args.output), "classification": result["classification"]}))


if __name__ == "__main__":
    main()
