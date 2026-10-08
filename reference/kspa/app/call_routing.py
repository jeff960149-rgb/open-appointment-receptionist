from collections.abc import Sequence
from html import escape
import re
from urllib.parse import unquote
from xml.sax.saxutils import quoteattr


def build_ai_stream_twiml(public_base_url: str, caller_number: str) -> str:
    websocket_url = public_base_url.replace("https://", "wss://", 1).replace(
        "http://", "ws://", 1
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<Stream url={quoteattr(websocket_url + '/media-stream')}>"
        f"<Parameter name=\"caller_number\" value={quoteattr(caller_number)} />"
        "</Stream>"
        "</Connect></Response>"
    )


def build_human_dial_twiml(
    human_numbers: Sequence[str],
    timeout_seconds: int,
    human_sip_uris: Sequence[str] = (),
) -> str:
    number_twiml = "".join(
        '<Number>'
        f"{escape(human_number)}"
        "</Number>"
        for human_number in human_numbers
    )
    sip_twiml = "".join(
        '<Sip>'
        f"{escape(human_sip_uri)}"
        "</Sip>"
        for human_sip_uri in human_sip_uris
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Dial answerOnBridge=\"true\" timeout=\"{timeout_seconds}\" "
        'action="/human-dial-result" method="POST">'
        f"{number_twiml}{sip_twiml}"
        "</Dial>"
        "</Response>"
    )


def build_human_screen_twiml() -> str:
    """Compatibility for in-flight calls that still reference the old screening URL."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def build_human_screen_result_twiml(digits: str) -> str:
    """Let older screening callbacks connect without requiring a keypress."""
    return build_human_screen_twiml()


def normalize_nanp_destination(to_value: str) -> str:
    """Extract a US/Canada E.164 number from a SIP INVITE destination."""
    value = unquote(to_value).strip().strip("<>")
    if value.casefold().startswith(("sip:", "sips:")):
        value = value.split(":", 1)[1].split("@", 1)[0]
    value = value.split(";", 1)[0]
    digits = re.sub(r"[^0-9+]", "", value)
    if re.fullmatch(r"[2-9]\d{9}", digits):
        return f"+1{digits}"
    if re.fullmatch(r"1[2-9]\d{9}", digits):
        return f"+{digits}"
    if re.fullmatch(r"\+1[2-9]\d{9}", digits):
        return digits
    return ""


def build_sip_outbound_twiml(to_value: str, caller_id: str) -> str:
    """Bridge an authenticated SIP call to a NANP destination only."""
    destination = normalize_nanp_destination(to_value)
    if not destination or not re.fullmatch(r"\+1[2-9]\d{9}", caller_id):
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response><Say>Please dial a valid U.S. or Canada number.</Say>'
            "<Hangup/></Response>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Dial callerId={quoteattr(caller_id)}>'
        f"<Number>{escape(destination)}</Number>"
        "</Dial></Response>"
    )


def should_fallback_to_ai(dial_call_status: str, dial_bridged: str = "") -> bool:
    status = dial_call_status.strip().casefold()
    bridged = dial_bridged.strip().casefold() == "true"
    return status != "completed" or not bridged


def should_route_to_ai(
    dial_call_status: str,
    ai_fallback_enabled: bool,
    dial_bridged: str = "",
    human_only_caller: bool = False,
) -> bool:
    return ai_fallback_enabled and not human_only_caller and should_fallback_to_ai(
        dial_call_status,
        dial_bridged,
    )
