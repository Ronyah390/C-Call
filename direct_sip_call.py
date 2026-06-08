import hashlib
import os
import random
import re
import socket
import struct
import sys
import time


SERVER = os.environ.get("SIP_DOMAIN", "103.170.231.10")
PORT = int(os.environ.get("SIP_PORT", "5060"))
USER = os.environ.get("SIP_USER", "")
PASSWORD = os.environ.get("SIP_PASSWORD", "")
DOMAIN = os.environ.get("SIP_DOMAIN", SERVER)
TARGET = sys.argv[1]
AUDIO_PATH = sys.argv[2]
REPEAT = max(1, int(sys.argv[3]) if len(sys.argv) > 3 else 1)
MAX_CALL_SECONDS = max(
    1,
    int(sys.argv[4] if len(sys.argv) > 4 else os.environ.get("CALL_MAX_DURATION_SECONDS", "60")),
)


def token(n=10):
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=n))


def md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((SERVER, PORT))
        return s.getsockname()[0]
    finally:
        s.close()


def status(reply):
    return reply.split("\r\n", 1)[0]


def header(reply, name):
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", reply, re.I | re.M)
    return match.group(1).strip() if match else ""


def sip_uri(value):
    match = re.search(r"<(sip:[^>]+)>", value or "", re.I)
    if match:
        return match.group(1)
    match = re.search(r"\b(sip:[^;\s>]+)", value or "", re.I)
    return match.group(1) if match else ""


def sip_addr(uri):
    match = re.search(r"sip:[^@>]+@([^;:\s>]+)(?::(\d+))?", uri or "", re.I)
    if not match:
        return None
    return (match.group(1), int(match.group(2) or PORT))


def parse_tag(value):
    match = re.search(r";tag=([^;\r\n]+)", value)
    return match.group(1) if match else ""


def parse_challenge(reply):
    match = re.search(r"(?:WWW|Proxy)-Authenticate:\s*Digest\s+(.*?)\r\n", reply, re.I)
    if not match:
        return None, None
    params = {}
    for item in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', match.group(1)):
        params[item.group(1).lower()] = item.group(2) or item.group(3)
    return params, "Proxy-Authenticate" in reply


def digest(params, method, uri):
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    ha1 = md5(f"{USER}:{realm}:{PASSWORD}")
    ha2 = md5(f"{method}:{uri}")
    response = md5(f"{ha1}:{nonce}:{ha2}")
    return f'Digest username="{USER}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{response}", algorithm=MD5'


def parse_sdp(reply):
    body = reply.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in reply else ""
    ip_match = re.search(r"^c=IN IP4 ([^\r\n]+)", body, re.M)
    port_match = re.search(r"^m=audio (\d+)", body, re.M)
    return (ip_match.group(1), int(port_match.group(1))) if ip_match and port_match else (None, None)


LOCAL_IP = local_ip()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 0))
LOCAL_PORT = sock.getsockname()[1]
sock.settimeout(1)

call_id = f"call-{token(16)}@{LOCAL_IP}"
from_tag = token(8)
reg_call_id = f"reg-{token(16)}@{LOCAL_IP}"
reg_tag = token(8)


def send(message, addr=None):
    sock.sendto(message.encode(), addr or (SERVER, PORT))


def recv_until(predicate, timeout):
    end = time.time() + timeout
    while time.time() < end:
        sock.settimeout(max(0.1, end - time.time()))
        try:
            data, _ = sock.recvfrom(8192)
        except socket.timeout:
            return None
        text = data.decode(errors="replace")
        print(status(text), flush=True)
        if predicate(text):
            return text
    return None


def register():
    def message(cseq, auth=None):
        result = (
            f"REGISTER sip:{DOMAIN} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_PORT};rport;branch=z9hG4bK{token()}\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:{USER}@{DOMAIN}>;tag={reg_tag}\r\n"
            f"To: <sip:{USER}@{DOMAIN}>\r\n"
            f"Call-ID: {reg_call_id}\r\n"
            f"CSeq: {cseq} REGISTER\r\n"
            f"Contact: <sip:{USER}@{LOCAL_IP}:{LOCAL_PORT}>\r\n"
            "Expires: 300\r\n"
            "User-Agent: CallbotDirect/1.0\r\n"
        )
        if auth:
            result += f"Authorization: {auth}\r\n"
        return result + "Content-Length: 0\r\n\r\n"

    send(message(1))
    reply = recv_until(lambda r: status(r).startswith(("SIP/2.0 2", "SIP/2.0 4")), 5)
    if not reply:
        raise RuntimeError("REGISTER timed out")
    if status(reply).startswith("SIP/2.0 401"):
        params, _ = parse_challenge(reply)
        send(message(2, digest(params, "REGISTER", f"sip:{DOMAIN}")))
        reply = recv_until(lambda r: status(r).startswith(("SIP/2.0 2", "SIP/2.0 4")), 5)
    if not reply or not status(reply).startswith("SIP/2.0 200"):
        raise RuntimeError(f"REGISTER failed: {status(reply) if reply else 'timeout'}")


def invite(cseq, auth_header=None, auth=None, branch=None):
    branch = branch or f"z9hG4bK{token()}"
    sdp = (
        "v=0\r\n"
        f"o=- {int(time.time())} {int(time.time())} IN IP4 {LOCAL_IP}\r\n"
        "s=Callbot\r\n"
        f"c=IN IP4 {LOCAL_IP}\r\n"
        "t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0 8 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        "a=sendrecv\r\n"
    )
    message = (
        f"INVITE sip:{TARGET}@{DOMAIN} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_PORT};rport;branch={branch}\r\n"
        "Max-Forwards: 70\r\n"
        f'From: "Callbot" <sip:{USER}@{DOMAIN}>;tag={from_tag}\r\n'
        f"To: <sip:{TARGET}@{DOMAIN}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} INVITE\r\n"
        f"Contact: <sip:{USER}@{LOCAL_IP}:{LOCAL_PORT}>\r\n"
        "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, INFO\r\n"
        "User-Agent: CallbotDirect/1.0\r\n"
        "Content-Type: application/sdp\r\n"
    )
    if auth:
        message += f"{auth_header}: {auth}\r\n"
    message += f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
    send(message)
    return branch


def ack(cseq, to_header, branch, request_uri=None):
    request_uri = request_uri or f"sip:{TARGET}@{DOMAIN}"
    send(
        f"ACK {request_uri} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_PORT};rport;branch={branch}\r\n"
        "Max-Forwards: 70\r\n"
        f'From: "Callbot" <sip:{USER}@{DOMAIN}>;tag={from_tag}\r\n'
        f"To: {to_header}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} ACK\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def bye_message(cseq, to_header, request_uri=None):
    request_uri = request_uri or f"sip:{TARGET}@{DOMAIN}"
    return (
        f"BYE {request_uri} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_PORT};rport;branch=z9hG4bK{token()}\r\n"
        "Max-Forwards: 70\r\n"
        f'From: "Callbot" <sip:{USER}@{DOMAIN}>;tag={from_tag}\r\n'
        f"To: {to_header}\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} BYE\r\n"
        "Content-Length: 0\r\n\r\n"
    )


def hangup(cseq, to_header, request_uri=None):
    message = bye_message(cseq, to_header, request_uri)
    addresses = [(SERVER, PORT)]
    contact_addr = sip_addr(request_uri)
    if contact_addr and contact_addr not in addresses:
        addresses.append(contact_addr)

    for attempt in range(5):
        for addr in addresses:
            send(message, addr)
        reply = recv_until(
            lambda r: status(r).startswith("SIP/2.0 200") and re.search(rf"^CSeq:\s*{cseq}\s+BYE\b", r, re.I | re.M),
            1.5,
        )
        if reply:
            print("Call hangup confirmed", flush=True)
            return True
        print(f"BYE retry {attempt + 1}", flush=True)
    print("BYE not confirmed", flush=True)
    return False


def send_audio(ip, port, max_seconds):
    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    deadline = time.monotonic() + max_seconds
    sequence = random.randint(0, 65535)
    timestamp = random.randint(0, 2**32 - 1)
    ssrc = random.randint(0, 2**32 - 1)
    payloads = []
    with open(AUDIO_PATH, "rb") as handle:
        while chunk := handle.read(160):
            payloads.append(chunk.ljust(160, b"\xff"))
    for _ in range(REPEAT):
        for payload in payloads:
            if time.monotonic() >= deadline:
                rtp.close()
                return
            packet = struct.pack("!BBHII", 0x80, 0, sequence, timestamp, ssrc) + payload
            rtp.sendto(packet, (ip, port))
            sequence = (sequence + 1) & 0xFFFF
            timestamp = (timestamp + 160) & 0xFFFFFFFF
            time.sleep(0.02)
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    rtp.close()


rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rtp_sock.bind(("0.0.0.0", 0))
rtp_port = rtp_sock.getsockname()[1]
rtp_sock.close()

try:
    print(f"Calling {TARGET} from {LOCAL_IP}:{LOCAL_PORT}", flush=True)
    register()
    cseq = 1
    branch = invite(cseq)
    final_reply = None
    end = time.time() + int(os.environ.get("SIP_PROCESS_TIMEOUT_SECONDS", "90"))
    while time.time() < end:
        reply = recv_until(lambda r: status(r).startswith("SIP/2.0 "), max(0.1, end - time.time()))
        if not reply:
            break
        line = status(reply)
        if line.startswith(("SIP/2.0 100", "SIP/2.0 180", "SIP/2.0 183")):
            continue
        if line.startswith(("SIP/2.0 401", "SIP/2.0 407")):
            params, is_proxy = parse_challenge(reply)
            ack(cseq, header(reply, "To"), branch)
            cseq += 1
            branch = invite(cseq, "Proxy-Authorization" if is_proxy else "Authorization", digest(params, "INVITE", f"sip:{TARGET}@{DOMAIN}"))
            continue
        final_reply = reply
        break
    if not final_reply or not status(final_reply).startswith("SIP/2.0 200"):
        raise RuntimeError(f"Call failed: {status(final_reply) if final_reply else 'timeout'}")
    print("Call answered; sending audio", flush=True)
    to_header = header(final_reply, "To")
    contact_uri = sip_uri(header(final_reply, "Contact")) or f"sip:{TARGET}@{DOMAIN}"
    ack(cseq, to_header, branch, contact_uri)
    remote_ip, remote_port = parse_sdp(final_reply)
    if not remote_ip:
        raise RuntimeError("Call answered without audio SDP")
    send_audio(remote_ip, remote_port, MAX_CALL_SECONDS)
    cseq += 1
    if not hangup(cseq, to_header, contact_uri):
        raise RuntimeError("Call hangup was not confirmed")
    print("Call completed", flush=True)
finally:
    sock.close()
