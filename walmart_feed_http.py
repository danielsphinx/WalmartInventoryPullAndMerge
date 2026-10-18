"""Bounded HTTP posts for the Walmart upload scripts: every token and feed request has a timeout; a
token reply is read or refused with its status named; an exception in a script's entry function is
written to the gate log before it reaches UiPath.

Deployed beside the scripts and the never relist gate as
G:\\Python Projects\\WalmartInventoryPullAndMerge\\walmart_feed_http.py and loaded by file path
(never through sys.path) by every script there that posts a feed: the five the UiPath workflow
"Shopify Marketplace Converter - DT Filters" loads (New Products Unmatched, New Products By Match,
Update Inventory, Update Pricing, Lag Time Bulk) and the two bundle scripts beside them (New
Unmatched Bundles, New_Unmatched_Bundles_v5). Each of their token and feed requests is
`walmart_post(url, kind=..., ...)`, which is `post` below; each get_token / gettoken returns
`_http.token_from(response)`; each entry function UiPath invokes is decorated `@_http.logged(label)`.

Why. The scripts called requests.request / requests.post with no timeout, so a Walmart call that
stalls waits for ever. When that happened the UiPath Python host died and the robot showed "Invoke
Python Method: Pipe is broken" (Update Pricing, 2026-10-11 23:18 and 2026-10-15 08:38; the gate had
not yet run in those cycles, so the stall was in the HTTP calls). With a timeout a stall becomes a
RuntimeError that names the request, within minutes, instead of a dead host. And UiPath's Python
host reports ANY exception raised in the invoked method as that same broken pipe (2026-10-15
15:28:43, Update Pricing, after the lists loaded and before its gate line: get_token parsed a reply
it never checked with ET.fromstring, or read_csv failed), so the exception itself was never seen.

What post() does:
  * every call passes timeout=TIMEOUTS[kind]: (connect, read) seconds, (10, 60) for a token
    request and (10, 300) for a feed. The read figure bounds each wait for data from Walmart, not
    the whole call.
  * the request never reached Walmart (ConnectionError raised while connecting, ConnectTimeout):
    retried once, for both kinds; the second failure raises RuntimeError naming the kind, the URL
    and the attempts.
  * Walmart did not answer in time (ReadTimeout), or the connection dropped after the request was
    sent (a ConnectionError wrapping urllib3's ProtocolError, "Connection aborted"): a token request
    has no side effect and is retried once like the above; a FEED is not retried, because Walmart
    may already hold it and a second post would duplicate it. It raises RuntimeError saying so;
    check the feed status before the next run.
  * a token request answered 429 (throttled) is asked once more after RETRY_PAUSE_SECONDS; a second
    429 raises RuntimeError naming 429 and the start of the reply. A feed answered 429 is returned
    as it is (never sent twice).
  * any other exception propagates unchanged; any other HTTP status is returned, not raised (the
    scripts read a feed reply's status_code and text themselves, as before; a token reply goes to
    token_from).
Each retry prints one line. Nothing else is printed; headers (the credentials) never are.

token_from(response): the access token of a token reply. A status other than 200 raises
RuntimeError naming the status and the first 200 characters of the body. A 200 reply is read as
JSON (access_token, or accessToken) or else as XML (an accessToken element, any namespace); a reply
that yields no token either way raises RuntimeError saying so (never ParseError, never None).

logged(label): a decorator for a script's entry function. When the function raises, ONE line
"<time> EXCEPTION <label>: <type>: <message> | <frames>" is appended to the gate log (the message on
one line; the frames: the script's own last ones, then the innermost one when it lies in a library;
the same file, through the gate module's own append: rotation and read back included) and the
exception is re raised unchanged. The gate module is the `_gate` the script loaded (each patched
script holds one), else the walmart_retired_filter.py beside this file, loaded by path. Writing the
line can never replace or hide the exception. Lag Time Bulk works at load, with no entry function,
so its exceptions are not logged (an owner item in the kit's README); New_Unmatched_Bundles_v5 calls
its decorated entry function at load, so only what fails before that call goes unlogged.
"""
import datetime
import functools
import importlib.util
import json as _json
import re
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from urllib3.exceptions import ProtocolError

TIMEOUTS = {"token": (10, 60), "feed": (10, 300)}  # (connect, read) seconds per request kind
ATTEMPTS = 2  # the first try plus one retry, only where a retry cannot duplicate a feed
RETRY_PAUSE_SECONDS = 2
THROTTLED = 429  # a token reply with this status is asked for once more
BODY_SHOWN = 200  # characters of a refused token reply named in the error
EXCEPTION_FRAMES = 4  # traceback frames named on an EXCEPTION line (the innermost)
EXCEPTION_MESSAGE_CHARS = 500


def _after_sending(exc) -> bool:
    """True when the failure happened after the request went out (Walmart may have received it)."""
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return True
    return isinstance(exc, requests.exceptions.ConnectionError) and any(isinstance(a, ProtocolError) for a in exc.args)


_SECRET_RUN = re.compile(r"[A-Za-z0-9_\-]{32,}")


def _body(response) -> str:
    """The first BODY_SHOWN characters of a reply's body, with any long token like run redacted:
    this text reaches the gate log on G: and UiPath's job log, and a token body is a credential."""
    text = str(getattr(response, "text", "") or "")[:BODY_SHOWN]
    return _SECRET_RUN.sub("<redacted>", text)


def post(url, *, kind, headers=None, data=None, json=None):
    """requests.post(url, ...) with the timeout of `kind` ("token" or "feed") and the retry rule above."""
    if kind not in TIMEOUTS:
        raise ValueError(f"walmart_feed_http: kind must be one of {sorted(TIMEOUTS)}, not {kind!r}")
    timeout = TIMEOUTS[kind]
    failure = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = requests.post(url, headers=headers, data=data, json=json, timeout=timeout)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as exc:
            failure = exc
            if kind == "feed" and _after_sending(exc):
                raise RuntimeError(
                    f"walmart_feed_http: feed POST to {url} failed after the request was sent "
                    f"({type(exc).__name__}, attempt {attempt}, timeout {timeout}); not retried: Walmart may "
                    f"already hold this feed and a second post would duplicate it. Check the feed status "
                    f"before the next run.") from exc
            why = type(exc).__name__
        else:
            if kind != "token" or getattr(response, "status_code", None) != THROTTLED:
                return response
            if attempt == ATTEMPTS:
                raise RuntimeError(f"walmart_feed_http: token POST to {url} answered HTTP {THROTTLED} (throttled) "
                                   f"{ATTEMPTS} times, the last after a {RETRY_PAUSE_SECONDS} s pause: "
                                   f"{_body(response)!r}")
            why = f"HTTP {THROTTLED}, throttled"
        if attempt < ATTEMPTS:
            print(f"walmart_feed_http: {kind} POST to {url} failed ({why}); "
                  f"retry {attempt + 1} of {ATTEMPTS} in {RETRY_PAUSE_SECONDS} s")
            time.sleep(RETRY_PAUSE_SECONDS)
    raise RuntimeError(f"walmart_feed_http: {kind} POST to {url} failed after {ATTEMPTS} attempts "
                       f"({type(failure).__name__}: {failure})") from failure


def _xml_token(text: str):
    """The text of the first accessToken element (any namespace), or None."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    for element in root.iter():
        if _is_token_name(str(element.tag).rsplit("}", 1)[-1]):
            return element.text
    return None


def _is_token_name(name: str) -> bool:
    """accessToken, access_token, AccessToken, ACCESS_TOKEN: Walmart's token endpoint has used more
    than one spelling (the snake case XML shape was seen 2026-10-15)."""
    return name.replace("_", "").lower() == "accesstoken"


def token_from(response) -> str:
    """The access token of a token reply (see the module doc); RuntimeError when there is none."""
    status = getattr(response, "status_code", None)
    if status != 200:
        raise RuntimeError(f"walmart_feed_http: the token request answered HTTP {status}, not 200: {_body(response)!r}")
    text = str(getattr(response, "text", "") or "").strip()
    token = None
    try:
        parsed = _json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        token = next((v for k, v in parsed.items() if _is_token_name(str(k))), None)
    if not token and parsed is None:
        token = _xml_token(text)
    token = str(token).strip() if isinstance(token, str) else ""
    if not token:
        raise RuntimeError("walmart_feed_http: the token reply (HTTP 200) holds no access token, neither as JSON "
                           f"nor as XML (any spelling of accessToken): {_body(response)!r}")
    return token


# --------------------------------------------------------------------------------------------------
# diagnostics: the EXCEPTION line
# --------------------------------------------------------------------------------------------------

def _gate_beside_this_file():
    """The gate module next to this file, loaded by its path (as the scripts load it)."""
    spec = importlib.util.spec_from_file_location("walmart_retired_filter", str(Path(__file__).with_name("walmart_retired_filter.py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exception_line(label: str, exc: BaseException, script_file: str = "") -> str:
    """The EXCEPTION line without its time: the label, the type, the message on one line, and the last
    frames of the traceback, outermost first: the innermost EXCEPTION_FRAMES frames of the script
    itself (`script_file`, the entry function's file), then the innermost frame of all when it lies
    elsewhere (in pandas, in this helper), so a failure deep in a library still names the script line."""
    message = " ".join(str(exc).split())
    if len(message) > EXCEPTION_MESSAGE_CHARS:
        message = message[:EXCEPTION_MESSAGE_CHARS] + " ..."
    frames = traceback.extract_tb(exc.__traceback__)
    chosen = [f for f in frames if script_file and f.filename == script_file][-EXCEPTION_FRAMES:]
    if frames and (not chosen or frames[-1] is not chosen[-1]):
        chosen.append(frames[-1])
    if not script_file:
        chosen = frames[-EXCEPTION_FRAMES:]
    where = "; ".join(f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in chosen) or "no traceback"
    return f"EXCEPTION {label}: {type(exc).__name__}: {message} | {where}"


def _log_exception(function, label: str, exc: BaseException) -> None:
    """Append the EXCEPTION line through the gate's own append and read back. Never raises."""
    try:
        gate = getattr(function, "__globals__", {}).get("_gate")
        if not callable(getattr(gate, "_write_log_line", None)):
            gate = _gate_beside_this_file()
        script_file = getattr(getattr(function, "__code__", None), "co_filename", "")
        gate._write_log_line(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                             f"{exception_line(label, exc, script_file)}")
    except Exception as failure:  # the log must never replace the exception the script raised
        try:
            print(f"[WARNING] walmart_feed_http: could not log the exception of {label}: "
                  f"{type(failure).__name__}: {failure}")
        except Exception:
            pass


def logged(label: str):
    """Decorator for a script's entry function: an exception is logged as one EXCEPTION line in the
    gate log and re raised unchanged (see the module doc)."""
    def decorate(function):
        @functools.wraps(function)
        def entry(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except BaseException as exc:
                _log_exception(function, label, exc)
                raise
        return entry
    return decorate
