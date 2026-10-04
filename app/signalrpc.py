"""signal-cli 0.14.8 JSON-RPC client.

Talks to `signal-cli daemon --http` over the internal-only bridge network:

    POST /api/v1/rpc     JSON-RPC 2.0, single or batch
    GET  /api/v1/events  SSE stream of incoming envelopes
    GET  /api/v1/check   liveness

Only methods this app actually uses are wrapped. Parameter names are camelCase
(the wire form; the CLI spelling is hyphenated).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterator

from . import config


class SignalRPCError(RuntimeError):
    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _post(payload: dict, *, timeout: int = 30) -> dict:
    url = config.SIGNAL_RPC_URL.rstrip("/") + "/api/v1/rpc"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:  # JSON-RPC errors arrive with 4xx
        body = exc.read()
        try:
            parsed = json.loads(body)
        except Exception:
            raise SignalRPCError(f"http {exc.code}: {body[:200]!r}", code=exc.code) from exc
        err = parsed.get("error") or {}
        raise SignalRPCError(err.get("message") or f"http {exc.code}", code=err.get("code"), data=err.get("data")) from exc
    except urllib.error.URLError as exc:
        raise SignalRPCError(f"signal-cli unreachable: {exc.reason}") from exc
    return json.loads(raw)


def call(method: str, /, account: str | None = None, timeout: int = 30, **params: Any) -> Any:
    """One JSON-RPC call. Returns `result`, raises SignalRPCError on error."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method}
    body = dict(params)
    if account:
        body["account"] = account
    if body:
        payload["params"] = body
    reply = _post(payload, timeout=timeout)
    if reply.get("error"):
        err = reply["error"]
        raise SignalRPCError(err.get("message") or "jsonrpc error", code=err.get("code"), data=err.get("data"))
    return reply.get("result")


# --- identity / metadata ---------------------------------------------------


def version() -> str:
    try:
        return str(call("version", timeout=10).get("version", ""))
    except SignalRPCError:
        return ""


def list_accounts() -> list[dict]:
    res = call("listAccounts", timeout=15)
    return res if isinstance(res, list) else []


def account() -> dict | None:
    """The linked account. Numberless linked accounts report number=null."""
    accounts = list_accounts()
    return accounts[0] if accounts else None


def aci_of(account_row: dict | None) -> str:
    """The ACI, if this signal-cli build reports it. 0.14.8's listAccounts
    returns only {"number": ...}, so callers must fall back to handle_of()."""
    if not account_row:
        return ""
    for key in ("aci", "uuid", "accountUuid", "accountUuidString", "accountId"):
        val = account_row.get(key)
        if val:
            return str(val)
    return ""


def handle_of(account_row: dict | None) -> str:
    """What to pass as the `account` param: the ACI if we have one, else the
    number. Keying everything off aci-only is what made a linked account look
    unlinked on 0.14.8."""
    if not account_row:
        return ""
    return aci_of(account_row) or str(account_row.get("number") or "")


def discover_aci(account: str, number: str = "") -> str:
    """Find our own ACI without touching the key material.

    listContacts includes the account's own row, and that row carries the uuid
    even though listAccounts does not. Matching on the number keeps it honest.
    """
    if not account:
        return ""
    try:
        contacts = call("listContacts", account=account, timeout=30)
    except SignalRPCError:
        return ""
    num = (number or "").replace(" ", "")
    for c in contacts if isinstance(contacts, list) else []:
        if not isinstance(c, dict):
            continue
        if num and str(c.get("number") or "").replace(" ", "") == num:
            uuid = str(c.get("uuid") or c.get("aci") or "")
            if uuid:
                return uuid
    return ""


def list_groups(account: str) -> list[dict]:
    try:
        res = call("listGroups", account=account, timeout=60)
    except SignalRPCError:
        return []
    return res if isinstance(res, list) else []


def list_contacts(account: str) -> list[dict]:
    try:
        res = call("listContacts", account=account, timeout=60)
    except SignalRPCError:
        return []
    return res if isinstance(res, list) else []


def profile_names(account: str) -> dict[str, str]:
    """aci -> best available display name."""
    names: dict[str, str] = {}
    for c in list_contacts(account):
        aci = str(c.get("uuid") or c.get("aci") or "")
        if not aci:
            continue
        name = c.get("name") or c.get("profileName") or c.get("messageRequestResponse") or ""
        names[aci] = str(name).strip()
    return names


# --- pairing -------------------------------------------------------------


def start_link() -> str:
    """Returns the sgnl://linkdevice?... URI. Valid for ~60 seconds."""
    res = call("startLink", timeout=30)
    if isinstance(res, dict):
        return str(res.get("deviceLinkUri") or "")
    return ""


def finish_link(uri: str, device_name: str = "", *, timeout: int = 300) -> dict:
    """Blocks until the primary device answers (or the link times out)."""
    res = call("finishLink", deviceLinkUri=uri, deviceName=device_name or config.DEVICE_NAME, timeout=timeout)
    return res if isinstance(res, dict) else {}


# --- sending (wired, but never called by the roll-up) -------------------


def send(account: str, message: str, *, group_id: str | None = None, recipient: str | None = None) -> dict:
    if group_id:
        return call("send", account=account, groupId=group_id, message=message, timeout=60)
    if recipient:
        return call("send", account=account, recipient=recipient, message=message, timeout=60)
    raise SignalRPCError("send needs group_id or recipient")


# --- event stream --------------------------------------------------------


def events(last_event_id: str | None = None, *, stop: Any = None) -> Iterator[tuple[str | None, dict]]:
    """Yield (event_id, envelope) from /api/v1/events until `stop` is set.

    signal-cli keeps the last 1000 events in memory, so reconnecting with
    Last-Event-ID replays anything missed during a blip.
    """
    url = config.SIGNAL_RPC_URL.rstrip("/") + "/api/v1/events"
    headers = {"Accept": "text/event-stream"}
    if last_event_id:
        headers["Last-Event-ID"] = str(last_event_id)
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=None)
    except urllib.error.URLError as exc:
        raise SignalRPCError(f"events stream unreachable: {exc.reason}") from exc

    event_id: str | None = None
    data_lines: list[str] = []
    try:
        for raw_line in resp:  # type: ignore[union-attr]
            if stop is not None and stop.is_set():
                break
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines.clear()
                    try:
                        yield event_id, json.loads(payload)
                    except json.JSONDecodeError:
                        pass
                event_id = None
                continue
            if line.startswith(":"):
                continue  # keepalive comment
            if line.startswith("id:"):
                event_id = line[3:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
    finally:
        try:
            resp.close()  # type: ignore[union-attr]
        except Exception:
            pass
