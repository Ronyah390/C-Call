# E:\c-call-ivr\ivr_engine.py
"""Interactive IVR SIP/RTP engine.

Extends C-Call's one-way `direct_sip_call.py` into a two-way engine that:
  * REGISTERs and places an outbound INVITE,
  * plays each menu node's prompt over RTP,
  * captures the caller's DTMF keypresses (RFC 2833 / telephone-event),
  * walks the flow's menu tree, branching on each digit,
  * records the caller's audio to disk,
  * emits a structured event for *every move* so the orchestrator can
    persist the full customer journey.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import random
import re
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from audio import RECORDINGS_DIR, ensure_dirs, ffmpeg_path, ulaw_path_for


def nat64_to_ipv4(ip: str) -> str:
    """Decode a NAT64 IPv6 address (64:ff9b::/96, embeds IPv4 in low 32 bits) to
    its IPv4. VoIP gateways often advertise media as NAT64 IPv6 even though they
    are reachable over IPv4; sending RTP to the raw IPv6 goes nowhere on an
    IPv4-only/CGNAT network. Returns the original string if not NAT64."""
    if not ip or ":" not in ip:
        return ip
    clean = ip.strip("[]")
    try:
        packed = ipaddress.IPv6Address(clean).packed
        if packed[:4] == b"\x00\x64\xff\x9b":
            return ".".join(str(b) for b in packed[12:16])
    except ValueError:
        pass
    return clean

# DTMF event index -> character
_DTMF_CHARS = {i: str(i) for i in range(10)}
_DTMF_CHARS.update({10: "*", 11: "#", 12: "A", 13: "B", 14: "C", 15: "D"})


def _token(n: int = 10) -> str:
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=n))


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _status(reply: str) -> str:
    return reply.split("\r\n", 1)[0]


def _header(reply: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", reply, re.I | re.M)
    return match.group(1).strip() if match else ""


def _sip_uri(value: str) -> str:
    match = re.search(r"<(sip:[^>]+)>", value or "", re.I)
    if match:
        return match.group(1)
    match = re.search(r"\b(sip:[^;\s>]+)", value or "", re.I)
    return match.group(1) if match else ""


def _sip_addr(uri: str, default_port: int) -> tuple[str, int] | None:
    m6 = re.search(r"sip:[^@>]+@\[([^\]]+)\](?::(\d+))?", uri or "", re.I)
    if m6:
        return (nat64_to_ipv4(m6.group(1)), int(m6.group(2) or default_port))
    match = re.search(r"sip:[^@>]+@([^;:\s>]+)(?::(\d+))?", uri or "", re.I)
    if not match:
        return None
    return (nat64_to_ipv4(match.group(1)), int(match.group(2) or default_port))


def _parse_challenge(reply: str):
    match = re.search(r"(?:WWW|Proxy)-Authenticate:\s*Digest\s+(.*?)\r\n", reply, re.I)
    if not match:
        return None, False
    params = {}
    for item in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', match.group(1)):
        params[item.group(1).lower()] = item.group(2) or item.group(3)
    return params, "Proxy-Authenticate" in reply


def _parse_sdp_audio(reply: str) -> tuple[str | None, int | None]:
    body = reply.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in reply else ""
    ip = re.search(r"^c=IN IP[46] ([^\r\n]+)", body, re.M)
    port = re.search(r"^m=audio (\d+)", body, re.M)
    return (ip.group(1).strip("[]"), int(port.group(1))) if ip and port else (None, None)


def _parse_telephone_event_pt(reply: str, default: int = 101) -> int:
    match = re.search(r"^a=rtpmap:(\d+)\s+telephone-event/8000", reply, re.I | re.M)
    return int(match.group(1)) if match else default


# --- flow data structures --------------------------------------------------

@dataclass
class Node:
    id: int
    name: str
    node_type: str = "menu"            # menu | message | hangup | transfer
    prompt_audio: str = ""             # .ulaw basename
    transfer_number: str = ""
    gather_timeout_seconds: int = 8
    max_retries: int = 2
    transitions: dict[str, int] = field(default_factory=dict)  # digit -> node id


@dataclass
class Flow:
    id: int
    root_node_id: int
    nodes: dict[int, Node]


@dataclass
class Event:
    type: str
    node_id: int | None = None
    digit: str = ""
    response_ms: int = 0
    detail: str = ""
    at_offset_ms: int = 0


@dataclass
class CallResult:
    status: str = "failed"             # completed | no_answer | busy | failed
    sip_final_status: str = ""
    ring_seconds: int = 0
    talk_seconds: int = 0
    total_seconds: int = 0
    digits_pressed: int = 0
    drop_node_id: int | None = None
    last_node_id: int | None = None
    reached_terminal: bool = False
    recording_path: str = ""
    error: str = ""


@dataclass
class BridgeLeg:
    target: str
    call_id: str
    from_tag: str
    to_header: str
    contact_uri: str
    branch: str
    cseq: int
    rtp: socket.socket
    rtp_port: int
    remote_ip: str
    remote_port: int
    te_pt: int = 101


EventSink = Callable[[Event], None]


class IvrCall:
    """Drives a single outbound IVR call."""

    def __init__(self, target: str, flow: Flow, sip: dict[str, str],
                 on_event: EventSink, *, record: bool = True,
                 max_call_seconds: int | None = None,
                 invite_timeout: int | None = None):
        self.target = target
        self.flow = flow
        self.on_event = on_event
        self.record = record
        self.server = sip["SIP_DOMAIN"]
        self.port = int(sip.get("SIP_PORT") or 5060)
        self.user = sip["SIP_USER"]
        self.password = sip["SIP_PASSWORD"]
        self.domain = sip.get("SIP_DOMAIN", self.server)
        self.max_call_seconds = int(
            max_call_seconds or os.environ.get("CALL_MAX_DURATION_SECONDS", "120")
        )
        self.invite_timeout = int(
            invite_timeout or os.environ.get("SIP_PROCESS_TIMEOUT_SECONDS", "90")
        )

        self.local_ip = self._local_ip()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.local_port = self.sock.getsockname()[1]
        self.sock.settimeout(1)

        # Symmetric RTP: one socket for send + receive.
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("0.0.0.0", 0))
        self.rtp_port = self.rtp.getsockname()[1]
        self.rtp.setblocking(False)

        self.call_id = f"call-{_token(16)}@{self.local_ip}"
        self.from_tag = _token(8)
        self.reg_call_id = f"reg-{_token(16)}@{self.local_ip}"
        self.reg_tag = _token(8)

        self._rtp_seq = random.randint(0, 65535)
        self._rtp_ts = random.randint(0, 2**32 - 1)
        self._rtp_ssrc = random.randint(0, 2**32 - 1)
        self._answered_at = 0.0
        self._te_pt = 101                       # telephone-event payload type (remote)
        self._seen_dtmf_ts: set[int] = set()    # dedupe DTMF by RTP timestamp
        self._recording = bytearray()
        self._seq = 0
        self._digits_pressed = 0
        self.cancel_requested = False
        self._transfer_target = ""

    # --- low level ---------------------------------------------------------

    def _local_ip(self) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((self.server, self.port))
            return s.getsockname()[0]
        finally:
            s.close()

    def _send(self, message: str, addr=None) -> None:
        self.sock.sendto(message.encode(), addr or (self.server, self.port))

    def _recv_until(self, predicate, timeout: float):
        end = time.time() + timeout
        while time.time() < end:
            # Wait in chunks of 5 seconds to allow sending NAT keep-alives
            chunk = min(5.0, end - time.time())
            if chunk <= 0:
                break
            self.sock.settimeout(chunk)
            try:
                data, _ = self.sock.recvfrom(8192)
                text = data.decode(errors="replace")
                if predicate(text):
                    return text
            except socket.timeout:
                # Send CRLF keep-alive to keep the NAT pinhole open
                try:
                    self.sock.sendto(b"\r\n\r\n", (self.server, self.port))
                except Exception:
                    pass
        return None


    def _digest(self, params: dict, method: str, uri: str) -> str:
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        ha1 = _md5(f"{self.user}:{realm}:{self.password}")
        ha2 = _md5(f"{method}:{uri}")
        response = _md5(f"{ha1}:{nonce}:{ha2}")
        return (f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", response="{response}", algorithm=MD5')

    def _ms_since_answer(self) -> int:
        if not self._answered_at:
            return 0
        return int((time.monotonic() - self._answered_at) * 1000)

    def _emit(self, type_: str, **kw) -> None:
        self._seq += 1
        ev = Event(type=type_, at_offset_ms=self._ms_since_answer(), **kw)
        self.on_event(ev)

    def request_cancel(self) -> None:
        self.cancel_requested = True

    # --- SIP ----------------------------------------------------------------

    def _register(self) -> None:
        def message(cseq, auth=None):
            result = (
                f"REGISTER sip:{self.domain} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch=z9hG4bK{_token()}\r\n"
                "Max-Forwards: 70\r\n"
                f"From: <sip:{self.user}@{self.domain}>;tag={self.reg_tag}\r\n"
                f"To: <sip:{self.user}@{self.domain}>\r\n"
                f"Call-ID: {self.reg_call_id}\r\n"
                f"CSeq: {cseq} REGISTER\r\n"
                f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>\r\n"
                "Expires: 300\r\nUser-Agent: IvrEngine/1.0\r\n"
            )
            if auth:
                result += f"Authorization: {auth}\r\n"
            return result + "Content-Length: 0\r\n\r\n"

        self._send(message(1))
        reply = self._recv_until(lambda r: _status(r).startswith(("SIP/2.0 2", "SIP/2.0 4")), 5)
        if not reply:
            raise RuntimeError("REGISTER timed out")
        if _status(reply).startswith("SIP/2.0 401"):
            params, _ = _parse_challenge(reply)
            self._send(message(2, self._digest(params, "REGISTER", f"sip:{self.domain}")))
            reply = self._recv_until(lambda r: _status(r).startswith(("SIP/2.0 2", "SIP/2.0 4")), 5)
        if not reply or not _status(reply).startswith("SIP/2.0 200"):
            raise RuntimeError(f"REGISTER failed: {_status(reply) if reply else 'timeout'}")
        self._emit("registered")

    def _invite_message(self, cseq, auth_header=None, auth=None, branch=None) -> str:
        branch = branch or f"z9hG4bK{_token()}"
        self._last_branch = branch
        sdp = (
            "v=0\r\n"
            f"o=- {int(time.time())} {int(time.time())} IN IP4 {self.local_ip}\r\n"
            "s=IvrEngine\r\n"
            f"c=IN IP4 {self.local_ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {self.rtp_port} RTP/AVP 0 8 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-16\r\n"
            "a=sendrecv\r\n"
        )
        message = (
            f"INVITE sip:{self.target}@{self.domain} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch={branch}\r\n"
            "Max-Forwards: 70\r\n"
            f'From: "IVR" <sip:{self.user}@{self.domain}>;tag={self.from_tag}\r\n'
            f"To: <sip:{self.target}@{self.domain}>\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {cseq} INVITE\r\n"
            f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>\r\n"
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, INFO, REFER, NOTIFY\r\n"
            "User-Agent: IvrEngine/1.0\r\nContent-Type: application/sdp\r\n"
        )
        if auth:
            message += f"{auth_header}: {auth}\r\n"
        return message + f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"

    def _ack(self, cseq, to_header, branch, request_uri=None) -> None:
        request_uri = request_uri or f"sip:{self.target}@{self.domain}"
        self._send(
            f"ACK {request_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch={branch}\r\n"
            "Max-Forwards: 70\r\n"
            f'From: "IVR" <sip:{self.user}@{self.domain}>;tag={self.from_tag}\r\n'
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n"
        )

    def _hangup(self, cseq, to_header, request_uri=None) -> bool:
        request_uri = request_uri or f"sip:{self.target}@{self.domain}"
        message = (
            f"BYE {request_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch=z9hG4bK{_token()}\r\n"
            "Max-Forwards: 70\r\n"
            f'From: "IVR" <sip:{self.user}@{self.domain}>;tag={self.from_tag}\r\n'
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {cseq} BYE\r\nContent-Length: 0\r\n\r\n"
        )
        addresses = [(self.server, self.port)]
        contact_addr = _sip_addr(request_uri, self.port)
        if contact_addr:
            ip, port = contact_addr
            ip_clean = ip.strip("[]")
            is_ip6 = ":" in ip_clean
            if not is_ip6:
                addr_to_add = (ip_clean, port)
                if addr_to_add not in addresses:
                    addresses.append(addr_to_add)
        for _ in range(5):
            for addr in addresses:
                self._send(message, addr)
            reply = self._recv_until(
                lambda r: _status(r).startswith("SIP/2.0 200")
                and re.search(rf"^CSeq:\s*{cseq}\s+BYE\b", r, re.I | re.M),
                1.5,
            )
            if reply:
                return True
        return False

    def _refer(self, cseq: int, to_header: str, transfer_target: str,
               request_uri: str | None = None, auth_header: str | None = None,
               auth: str | None = None) -> tuple[bool, str, int]:
        request_uri = request_uri or f"sip:{self.target}@{self.domain}"
        transfer_target = (transfer_target or "").strip()
        if not transfer_target:
            return False, "missing transfer target", cseq
        if transfer_target.lower().startswith("sip:"):
            refer_to = transfer_target
        else:
            refer_to = f"sip:{transfer_target}@{self.domain}"

        message = (
            f"REFER {request_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch=z9hG4bK{_token()}\r\n"
            "Max-Forwards: 70\r\n"
            f'From: "IVR" <sip:{self.user}@{self.domain}>;tag={self.from_tag}\r\n'
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {cseq} REFER\r\n"
            f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>\r\n"
            f"Refer-To: <{refer_to}>\r\n"
            f"Referred-By: <sip:{self.user}@{self.domain}>\r\n"
            "User-Agent: IvrEngine/1.0\r\n"
        )
        if auth:
            message += f"{auth_header}: {auth}\r\n"
        message += "Content-Length: 0\r\n\r\n"

        addresses = [(self.server, self.port)]
        contact_addr = _sip_addr(request_uri, self.port)
        if contact_addr:
            ip, port = contact_addr
            ip_clean = ip.strip("[]")
            if ":" not in ip_clean:
                addr_to_add = (ip_clean, port)
                if addr_to_add not in addresses:
                    addresses.append(addr_to_add)

        for addr in addresses:
            self._send(message, addr)

        reply = self._recv_until(
            lambda r: re.search(rf"^CSeq:\s*{cseq}\s+REFER\b", r, re.I | re.M)
            and _status(r).startswith("SIP/2.0 "),
            5,
        )
        if not reply:
            return False, "REFER timed out", cseq

        status = _status(reply)
        if status.startswith(("SIP/2.0 401", "SIP/2.0 407")) and not auth:
            params, is_proxy = _parse_challenge(reply)
            return self._refer(
                cseq + 1,
                to_header,
                transfer_target,
                request_uri,
                "Proxy-Authorization" if is_proxy else "Authorization",
                self._digest(params, "REFER", request_uri),
            )
        if status.startswith(("SIP/2.0 200", "SIP/2.0 202")):
            return True, status, cseq
        return False, status, cseq

    def _invite_bridge_leg(self, target: str, timeout: int = 45) -> BridgeLeg:
        target = (target or "").strip()
        if not target:
            raise RuntimeError("transfer target is missing")

        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp.bind(("0.0.0.0", 0))
        rtp_port = rtp.getsockname()[1]
        rtp.setblocking(False)

        call_id = f"bridge-{_token(16)}@{self.local_ip}"
        from_tag = _token(8)

        def build_invite(cseq: int, auth_header: str | None = None, auth: str | None = None,
                         branch: str | None = None) -> tuple[str, str]:
            branch = branch or f"z9hG4bK{_token()}"
            sdp = (
                "v=0\r\n"
                f"o=- {int(time.time())} {int(time.time())} IN IP4 {self.local_ip}\r\n"
                "s=IvrBridge\r\n"
                f"c=IN IP4 {self.local_ip}\r\n"
                "t=0 0\r\n"
                f"m=audio {rtp_port} RTP/AVP 0 8 101\r\n"
                "a=rtpmap:0 PCMU/8000\r\n"
                "a=rtpmap:8 PCMA/8000\r\n"
                "a=rtpmap:101 telephone-event/8000\r\n"
                "a=fmtp:101 0-16\r\n"
                "a=sendrecv\r\n"
            )
            msg = (
                f"INVITE sip:{target}@{self.domain} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch={branch}\r\n"
                "Max-Forwards: 70\r\n"
                f'From: "IVR Bridge" <sip:{self.user}@{self.domain}>;tag={from_tag}\r\n'
                f"To: <sip:{target}@{self.domain}>\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq} INVITE\r\n"
                f"Contact: <sip:{self.user}@{self.local_ip}:{self.local_port}>\r\n"
                "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, INFO\r\n"
                "User-Agent: IvrEngine/1.0\r\n"
                "Content-Type: application/sdp\r\n"
            )
            if auth:
                msg += f"{auth_header}: {auth}\r\n"
            return msg + f"Content-Length: {len(sdp)}\r\n\r\n{sdp}", branch

        def ack(cseq: int, to_header: str, branch: str, request_uri: str) -> None:
            self._send(
                f"ACK {request_uri} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch={branch}\r\n"
                "Max-Forwards: 70\r\n"
                f'From: "IVR Bridge" <sip:{self.user}@{self.domain}>;tag={from_tag}\r\n'
                f"To: {to_header}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq} ACK\r\n"
                "Content-Length: 0\r\n\r\n"
            )

        cseq = 1
        message, branch = build_invite(cseq)
        self._send(message)
        final_reply = None
        fallback_ip, fallback_port = None, None
        end = time.time() + timeout
        while time.time() < end:
            reply = self._recv_until(
                lambda r: _status(r).startswith("SIP/2.0 ")
                and re.search(rf"^Call-ID:\s*{re.escape(call_id)}\b", r, re.I | re.M),
                max(0.1, end - time.time()),
            )
            if not reply:
                break
            line = _status(reply)
            if line.startswith(("SIP/2.0 100", "SIP/2.0 180", "SIP/2.0 183")):
                ip, port = _parse_sdp_audio(reply)
                if ip:
                    fallback_ip, fallback_port = ip, port
                continue
            if line.startswith(("SIP/2.0 401", "SIP/2.0 407")):
                params, is_proxy = _parse_challenge(reply)
                cseq += 1
                message, branch = build_invite(
                    cseq,
                    "Proxy-Authorization" if is_proxy else "Authorization",
                    self._digest(params, "INVITE", f"sip:{target}@{self.domain}"),
                )
                self._send(message)
                continue
            final_reply = reply
            break

        if not final_reply:
            rtp.close()
            raise RuntimeError("agent call timed out")

        final_status = _status(final_reply)
        if not final_status.startswith("SIP/2.0 200"):
            rtp.close()
            raise RuntimeError(f"agent call failed: {final_status}")

        to_header = _header(final_reply, "To")
        contact_uri = _sip_uri(_header(final_reply, "Contact")) or f"sip:{target}@{self.domain}"
        ack(cseq, to_header, branch, contact_uri)
        remote_ip, remote_port = _parse_sdp_audio(final_reply)
        if not remote_ip:
            remote_ip, remote_port = fallback_ip, fallback_port
        if remote_ip:
            remote_ip = nat64_to_ipv4(remote_ip)
        if not remote_ip or not remote_port:
            rtp.close()
            raise RuntimeError("agent answered without audio SDP")
        if ":" in remote_ip:
            try:
                rtp.close()
                rtp = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                rtp.bind(("::", rtp_port))
                rtp.setblocking(False)
            except Exception:
                pass
        return BridgeLeg(
            target=target,
            call_id=call_id,
            from_tag=from_tag,
            to_header=to_header,
            contact_uri=contact_uri,
            branch=branch,
            cseq=cseq,
            rtp=rtp,
            rtp_port=rtp_port,
            remote_ip=remote_ip,
            remote_port=remote_port,
            te_pt=_parse_telephone_event_pt(final_reply),
        )

    def _hangup_bridge_leg(self, leg: BridgeLeg) -> None:
        leg.cseq += 1
        message = (
            f"BYE {leg.contact_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};rport;branch=z9hG4bK{_token()}\r\n"
            "Max-Forwards: 70\r\n"
            f'From: "IVR Bridge" <sip:{self.user}@{self.domain}>;tag={leg.from_tag}\r\n'
            f"To: {leg.to_header}\r\n"
            f"Call-ID: {leg.call_id}\r\n"
            f"CSeq: {leg.cseq} BYE\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        addresses = [(self.server, self.port)]
        contact_addr = _sip_addr(leg.contact_uri, self.port)
        if contact_addr:
            ip, port = contact_addr
            ip_clean = ip.strip("[]")
            if ":" not in ip_clean:
                addr_to_add = (ip_clean, port)
                if addr_to_add not in addresses:
                    addresses.append(addr_to_add)
        for addr in addresses:
            try:
                self._send(message, addr)
            except Exception:
                pass

    def _poll_sip_bye_for_call(self, call_id: str) -> bool:
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(0)
        try:
            data, addr = self.sock.recvfrom(8192)
            text = data.decode(errors="replace")
            if not text.split("\r\n", 1)[0].upper().startswith("BYE "):
                return False
            if _header(text, "Call-ID") != call_id:
                return False
            via = _header(text, "Via")
            from_hdr = _header(text, "From")
            to_hdr = _header(text, "To")
            cseq = _header(text, "CSeq")
            ok = (
                "SIP/2.0 200 OK\r\n"
                f"Via: {via}\r\n"
                f"From: {from_hdr}\r\n"
                f"To: {to_hdr}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq}\r\n"
                "Content-Length: 0\r\n\r\n"
            )
            self.sock.sendto(ok.encode(), addr)
            return True
        except BlockingIOError:
            return False
        except socket.error:
            return False
        finally:
            self.sock.settimeout(old_timeout)

    def _bridge_audio(self, customer_ip: str, customer_port: int, agent: BridgeLeg) -> int:
        started = time.monotonic()
        deadline = started + self.max_call_seconds
        customer_addr = (customer_ip, customer_port)
        agent_addr = (agent.remote_ip, agent.remote_port)
        last_packet = time.monotonic()

        while time.monotonic() < deadline and not self.cancel_requested:
            moved = False
            try:
                while True:
                    data, addr = self.rtp.recvfrom(2048)
                    if addr[0] == customer_ip:
                        agent.rtp.sendto(data, agent_addr)
                        last_packet = time.monotonic()
                        moved = True
            except BlockingIOError:
                pass
            except socket.error:
                pass

            try:
                while True:
                    data, addr = agent.rtp.recvfrom(2048)
                    if addr[0] == agent.remote_ip:
                        self.rtp.sendto(data, customer_addr)
                        last_packet = time.monotonic()
                        moved = True
            except BlockingIOError:
                pass
            except socket.error:
                pass

            if self._poll_sip_bye_for_call(self.call_id):
                self.cancel_requested = True
                break
            if self._poll_sip_bye_for_call(agent.call_id):
                break
            if time.monotonic() - last_packet > 90:
                break
            if not moved:
                time.sleep(0.01)

        return int(time.monotonic() - started)

    def _bridge_transfer(self, customer_ip: str, customer_port: int, transfer_target: str) -> tuple[bool, str]:
        agent = None
        try:
            self._emit("bridge_dialing", detail=transfer_target)
            agent = self._invite_bridge_leg(transfer_target)
            self._emit("bridge_answered", detail=transfer_target)
            bridged_seconds = self._bridge_audio(customer_ip, customer_port, agent)
            return True, f"bridged {bridged_seconds}s"
        except Exception as exc:
            return False, str(exc)
        finally:
            if agent:
                self._hangup_bridge_leg(agent)
                try:
                    agent.rtp.close()
                except Exception:
                    pass

    # --- RTP ---------------------------------------------------------------

    def _poll_sip_bye(self) -> None:
        """Non-blocking check of SIP socket for incoming BYE; respond 200 and set cancel."""
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(0)
        try:
            data, addr = self.sock.recvfrom(8192)
            text = data.decode(errors="replace")
            first_line = text.split("\r\n", 1)[0].upper()
            if first_line.startswith("BYE "):
                via = _header(text, "Via")
                from_hdr = _header(text, "From")
                to_hdr = _header(text, "To")
                cid = _header(text, "Call-ID")
                cseq = _header(text, "CSeq")
                ok = (
                    "SIP/2.0 200 OK\r\n"
                    f"Via: {via}\r\n"
                    f"From: {from_hdr}\r\n"
                    f"To: {to_hdr}\r\n"
                    f"Call-ID: {cid}\r\n"
                    f"CSeq: {cseq}\r\n"
                    "Content-Length: 0\r\n\r\n"
                )
                try:
                    self.sock.sendto(ok.encode(), addr)
                except Exception:
                    pass
                self.cancel_requested = True
                self._emit("hangup", detail="remote BYE received")
        except socket.error:
            pass
        finally:
            self.sock.settimeout(old_timeout)

    def _drain_rtp(self, remote_ip: str, remote_port: int) -> str | None:
        """Read pending RTP packets. Record audio, decode DTMF digit if found."""
        digit = None
        for _ in range(64):
            try:
                data, _addr = self.rtp.recvfrom(2048)
            except (BlockingIOError, socket.error):
                break
            if len(data) < 12:
                continue
            pt = data[1] & 0x7F
            ts = struct.unpack("!I", data[4:8])[0]
            payload = data[12:]
            if pt == self._te_pt:
                d = self._decode_dtmf(payload, ts)
                if d is not None:
                    digit = d
            elif self.record and payload:
                self._recording.extend(payload)
        # Check SIP socket for remote-initiated BYE
        self._poll_sip_bye()
        return digit

    def _decode_dtmf(self, payload: bytes, ts: int) -> str | None:
        if len(payload) < 1 or ts in self._seen_dtmf_ts:
            return None
        self._seen_dtmf_ts.add(ts)
        event = payload[0]
        return _DTMF_CHARS.get(event)

    def _play_prompt(self, audio_name: str, remote_ip: str, remote_port: int,
                      allow_barge: bool, valid_digits: set[str]) -> str | None:
        """Stream a .ulaw prompt. Returns digit on barge-in."""
        path = ulaw_path_for(audio_name) if audio_name else None
        deadline = time.monotonic() + self.max_call_seconds
        if not path or not path.exists():
            return None
        with open(path, "rb") as handle:
            payloads = [chunk.ljust(160, b"\xff") for chunk in iter(lambda: handle.read(160), b"")]
        first_packet = True
        for payload in payloads:
            if time.monotonic() >= deadline or self.cancel_requested:
                break
            # Marker bit on the first packet of the prompt starts the talk spurt.
            pt_byte = 0x80 if first_packet else 0x00
            first_packet = False
            packet = struct.pack("!BBHII", 0x80, pt_byte, self._rtp_seq, self._rtp_ts, self._rtp_ssrc) + payload
            try:
                self.rtp.sendto(packet, (remote_ip, remote_port))
            except socket.error:
                pass
            self._rtp_seq = (self._rtp_seq + 1) & 0xFFFF
            self._rtp_ts = (self._rtp_ts + 160) & 0xFFFFFFFF
            time.sleep(0.02)
            digit = self._drain_rtp(remote_ip, remote_port)
            if digit and allow_barge and digit in valid_digits:
                return digit
        return None

    def _gather_digit(self, remote_ip: str, remote_port: int, timeout: int) -> str | None:
        """Wait up to `timeout` seconds for DTMF."""
        end = time.monotonic() + max(1, timeout)
        while time.monotonic() < end:
            if self.cancel_requested:
                return None
            digit = self._drain_rtp(remote_ip, remote_port)
            if digit is not None:
                return digit
            time.sleep(0.02)
        return None

    # --- Flow Logic --------------------------------------------------------

    def _run_flow(self, remote_ip: str, remote_port: int, result: CallResult) -> None:
        flow = self.flow
        node = flow.nodes.get(flow.root_node_id)
        visited_guard = 0
        while node is not None and visited_guard < 100:
            if self.cancel_requested:
                self._emit("hangup", node_id=node.id, detail="canceled by operator")
                result.drop_node_id = node.id
                return
            visited_guard += 1
            result.last_node_id = node.id
            valid_digits = set(node.transitions.keys())

            barge = self._play_prompt(
                node.prompt_audio, remote_ip, remote_port,
                allow_barge=bool(valid_digits), valid_digits=valid_digits,
            )
            self._emit("prompt_played", node_id=node.id, detail=node.name)

            if node.node_type == "hangup":
                self._emit("completed", node_id=node.id, detail="reached hangup node")
                result.reached_terminal = True
                return
            if node.node_type == "transfer":
                self._emit("transfer", node_id=node.id, detail=node.transfer_number)
                self._transfer_target = node.transfer_number
                result.reached_terminal = True
                return
            if node.node_type == "message" and not valid_digits:
                self._emit("completed", node_id=node.id, detail="message delivered")
                result.reached_terminal = True
                return

            prompt_end = time.monotonic()
            attempt, advanced = 0, False
            while attempt <= node.max_retries and not advanced:
                if self.cancel_requested:
                    return
                digit = barge if barge else self._gather_digit(
                    remote_ip, remote_port, node.gather_timeout_seconds
                )
                barge = None
                if digit is None:
                    self._emit("no_input", node_id=node.id, detail=f"attempt {attempt + 1}")
                    attempt += 1
                    if attempt <= node.max_retries:
                        self._play_prompt(node.prompt_audio, remote_ip, remote_port,
                                          bool(valid_digits), valid_digits)
                        prompt_end = time.monotonic()
                    continue

                self._digits_pressed += 1
                response_ms = int((time.monotonic() - prompt_end) * 1000)
                self._emit("dtmf", node_id=node.id, digit=digit, response_ms=response_ms)

                if digit in node.transitions:
                    next_id = node.transitions[digit]
                    self._emit("branch", node_id=node.id, digit=digit,
                               detail=f"-> node {next_id}")
                    node = flow.nodes.get(next_id)
                    advanced = True
                else:
                    self._emit("invalid_digit", node_id=node.id, digit=digit)
                    attempt += 1
                    if attempt <= node.max_retries:
                        self._play_prompt(node.prompt_audio, remote_ip, remote_port,
                                          bool(valid_digits), valid_digits)
                        prompt_end = time.monotonic()

            if not advanced:
                self._emit("timeout", node_id=result.last_node_id, detail="no valid input")
                result.drop_node_id = result.last_node_id
                return
        result.reached_terminal = True

    # --- Run ---------------------------------------------------------------

    def run(self) -> CallResult:
        result = CallResult()
        ensure_dirs()
        started = time.monotonic()
        ring_start = started
        try:
            self._register()
            cseq = 1
            message = self._invite_message(cseq)
            self._send(message)
            branch = self._last_branch
            self._emit("ringing")

            final_reply = None
            fallback_ip, fallback_port = None, None
            end = time.time() + self.invite_timeout
            while time.time() < end:
                reply = self._recv_until(lambda r: _status(r).startswith("SIP/2.0 "),
                                          max(0.1, end - time.time()))
                if not reply:
                    break
                line = _status(reply)
                if line.startswith(("SIP/2.0 100", "SIP/2.0 180", "SIP/2.0 183")):
                    ip, port = _parse_sdp_audio(reply)
                    if ip:
                        fallback_ip, fallback_port = ip, port
                    continue
                if line.startswith(("SIP/2.0 401", "SIP/2.0 407")):
                    params, is_proxy = _parse_challenge(reply)
                    self._ack(cseq, _header(reply, "To"), branch)
                    cseq += 1
                    message = self._invite_message(
                        cseq, "Proxy-Authorization" if is_proxy else "Authorization",
                        self._digest(params, "INVITE", f"sip:{self.target}@{self.domain}"),
                    )
                    self._send(message)
                    branch = self._last_branch
                    continue
                final_reply = reply
                break

            result.ring_seconds = int(time.monotonic() - ring_start)
            if not final_reply:
                result.status, result.sip_final_status = "no_answer", "timeout"
                self._emit("error", detail="no final response")
                return result
            final_status = _status(final_reply)
            result.sip_final_status = final_status
            if not final_status.startswith("SIP/2.0 200"):
                if "486" in final_status or "600" in final_status:
                    result.status = "busy"
                elif any(c in final_status for c in ("408", "480", "487", "603")):
                    result.status = "no_answer"
                else:
                    result.status = "failed"
                self._emit("error", detail=final_status)
                return result

            # Answered.
            self._answered_at = time.monotonic()
            talk_start = self._answered_at
            self._te_pt = _parse_telephone_event_pt(final_reply)
            self._emit("answered", detail=final_status)
            to_header = _header(final_reply, "To")
            contact_uri = _sip_uri(_header(final_reply, "Contact")) or f"sip:{self.target}@{self.domain}"
            self._ack(cseq, to_header, branch, contact_uri)
            remote_ip, remote_port = _parse_sdp_audio(final_reply)
            if not remote_ip:
                remote_ip, remote_port = fallback_ip, fallback_port
            # Decode NAT64 IPv6 media address to its embedded IPv4 so RTP routes
            # on an IPv4/CGNAT network (gateways advertise media as 64:ff9b::/96).
            if remote_ip:
                decoded = nat64_to_ipv4(remote_ip)
                if decoded != remote_ip:
                    self._emit("info", detail=f"media {remote_ip} -> IPv4 {decoded}")
                    remote_ip = decoded
            if not remote_ip:
                result.status = "failed"
                result.error = "answered without audio SDP"
                self._emit("error", detail=result.error)
            else:
                if ":" in remote_ip:
                    # Genuine (non-NAT64) IPv6 remote: switch the RTP socket to v6.
                    try:
                        self.rtp.close()
                        self.rtp = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                        self.rtp.bind(("::", self.rtp_port))
                        self.rtp.setblocking(False)
                    except Exception:
                        pass
                self._run_flow(remote_ip, remote_port, result)
                result.status = "canceled" if self.cancel_requested else "completed"
                if self._transfer_target and not self.cancel_requested:
                    cseq += 1
                    ok, detail, cseq = self._refer(cseq, to_header, self._transfer_target, contact_uri)
                    if ok:
                        self._emit("transfer_accepted", detail=detail)
                    else:
                        self._emit("transfer_failed", detail=f"REFER failed: {detail}; trying app bridge")
                        ok, detail = self._bridge_transfer(remote_ip, remote_port, self._transfer_target)
                        if ok:
                            self._emit("bridge_completed", detail=detail)
                        else:
                            result.status = "failed"
                            result.error = f"bridge transfer failed: {detail}"
                            self._emit("bridge_failed", detail=result.error)

            cseq += 1
            self._hangup(cseq, to_header, contact_uri)
            self._emit("hangup")
            result.talk_seconds = int(time.monotonic() - talk_start)
            result.digits_pressed = self._digits_pressed
            result.recording_path = self._finalize_recording()
            return result
        except Exception as exc:
            result.status = result.status if result.status != "failed" else "failed"
            result.error = str(exc)
            self._emit("error", detail=str(exc))
            return result
        finally:
            result.total_seconds = int(time.monotonic() - started)
            try:
                self.sock.close()
            except Exception:
                pass
            try:
                self.rtp.close()
            except Exception:
                pass

    def _finalize_recording(self) -> str:
        if not self.record or not self._recording:
            return ""
        ensure_dirs()
        ulaw_file = RECORDINGS_DIR / f"{self.call_id.split('@')[0]}.ulaw"
        ulaw_file.write_bytes(bytes(self._recording))
        wav_file = ulaw_file.with_suffix(".wav")
        try:
            import subprocess
            subprocess.run(
                [ffmpeg_path(), "-y", "-f", "mulaw", "-ar", "8000", "-ac", "1",
                 "-i", str(ulaw_file), str(wav_file)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return str(wav_file)
        except Exception:
            return str(ulaw_file)
