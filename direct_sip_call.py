import hashlib
import ipaddress
import os
import random
import re
import socket
import struct
import sys
import time


def nat64_to_ipv4(ip):
    """Decode a NAT64 IPv6 address (64:ff9b::/96, embeds IPv4 in low 32 bits) to
    its IPv4. Many VoIP gateways advertise media as NAT64 IPv6 even though they
    are reachable over IPv4 — sending RTP to the raw IPv6 goes nowhere on an
    IPv4-only/CGNAT network. Returns the original string if not NAT64."""
    if not ip or ":" not in ip:
        return ip
    clean = ip.strip("[]")
    try:
        packed = ipaddress.IPv6Address(clean).packed
        # Well-known NAT64 prefix 64:ff9b:: -> first 12 bytes fixed, last 4 = IPv4
        if packed[:4] == b"\x00\x64\xff\x9b":
            return ".".join(str(b) for b in packed[12:16])
    except ValueError:
        pass
    return clean


SERVER = os.environ.get("SIP_DOMAIN", "")
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
    # Bracketed IPv6 host, e.g. sip:gw@[64:ff9b::67aa:e70a]:5060
    m6 = re.search(r"sip:[^@>]+@\[([^\]]+)\](?::(\d+))?", uri or "", re.I)
    if m6:
        return (nat64_to_ipv4(m6.group(1)), int(m6.group(2) or PORT))
    match = re.search(r"sip:[^@>]+@([^;:\s>]+)(?::(\d+))?", uri or "", re.I)
    if not match:
        return None
    return (nat64_to_ipv4(match.group(1)), int(match.group(2) or PORT))


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
    ip_match = re.search(r"^c=IN IP[46] ([^\r\n]+)", body, re.M)
    port_match = re.search(r"^m=audio (\d+)", body, re.M)
    return (ip_match.group(1).strip("[]"), int(port_match.group(1))) if ip_match and port_match else (None, None)


LOCAL_IP = local_ip()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 0))
LOCAL_PORT = sock.getsockname()[1]
sock.settimeout(1)

# Create RTP socket and keep it open — port is advertised in SDP, must stay bound
rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rtp_sock.bind(("0.0.0.0", 0))
rtp_port = rtp_sock.getsockname()[1]

call_id = f"call-{token(16)}@{LOCAL_IP}"
from_tag = token(8)
reg_call_id = f"reg-{token(16)}@{LOCAL_IP}"
reg_tag = token(8)


def send(message, addr=None):
    try:
        sock.sendto(message.encode(), addr or (SERVER, PORT))
    except (socket.gaierror, OSError) as exc:
        print(f"send() to {addr} failed: {exc}", flush=True)


DEBUG_SIP = os.environ.get("DEBUG_SIP", "1") == "1"


def recv_until(predicate, timeout):
    end = time.time() + timeout
    while time.time() < end:
        chunk = min(5.0, end - time.time())
        if chunk <= 0:
            break
        sock.settimeout(chunk)
        try:
            data, src = sock.recvfrom(8192)
            text = data.decode(errors="replace")
            if DEBUG_SIP:
                print(f"<<< from {src[0]}:{src[1]}\n{text}\n--- end ---", flush=True)
            else:
                print(status(text), flush=True)
            if predicate(text):
                return text
        except socket.timeout:
            try:
                sock.sendto(b"\r\n\r\n", (SERVER, PORT))
            except Exception:
                pass
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
    if contact_addr:
        ip, port = contact_addr
        ip_clean = ip.strip("[]")
        if ":" not in ip_clean:
            addr_to_add = (ip_clean, port)
            if addr_to_add not in addresses:
                addresses.append(addr_to_add)

    for attempt in range(3):
        for addr in addresses:
            send(message, addr)
        reply = recv_until(
            lambda r: re.search(rf"^CSeq:\s*{cseq}\s+BYE\b", r, re.I | re.M)
            and (status(r).startswith("SIP/2.0 200") or status(r).startswith("SIP/2.0 481")),
            1.5,
        )
        if reply:
            # 200 = clean teardown, 481 = dialog already gone. Both mean ended.
            print("Call hangup confirmed", flush=True)
            return True
        print(f"BYE retry {attempt + 1}", flush=True)
    print("BYE not confirmed (dialog likely already closed)", flush=True)
    return True


def poll_sip_bye():
    """Non-blocking check of the SIP socket for a remote-initiated BYE. If found,
    reply 200 OK and return True so the caller can stop sending audio."""
    sock.settimeout(0)
    try:
        while True:
            try:
                data, addr = sock.recvfrom(8192)
            except (BlockingIOError, socket.error):
                return False
            text = data.decode(errors="replace")
            if text.split("\r\n", 1)[0].upper().startswith("BYE "):
                ok = (
                    "SIP/2.0 200 OK\r\n"
                    f"Via: {header(text, 'Via')}\r\n"
                    f"From: {header(text, 'From')}\r\n"
                    f"To: {header(text, 'To')}\r\n"
                    f"Call-ID: {header(text, 'Call-ID')}\r\n"
                    f"CSeq: {header(text, 'CSeq')}\r\n"
                    "Content-Length: 0\r\n\r\n"
                )
                try:
                    sock.sendto(ok.encode(), addr)
                except Exception:
                    pass
                print("Remote hung up (BYE received)", flush=True)
                return True
    finally:
        sock.settimeout(1)


def send_audio(ip, port, max_seconds):
    """Stream audio over RTP using the pre-bound rtp_sock (same port as advertised in SDP).
    Returns True if the remote hung up during playback."""
    is_ipv6 = ":" in ip
    if is_ipv6:
        # For IPv6 remote, create a new IPv6 socket; bind to same rtp_port if possible
        rtp = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            rtp.bind(("::", rtp_port))
        except OSError:
            rtp.bind(("::", 0))
        own_sock = True
    else:
        # Reuse the already-bound IPv4 socket so source port == rtp_port from SDP
        rtp = rtp_sock
        own_sock = False

    deadline = time.monotonic() + max_seconds
    sequence = random.randint(0, 65535)
    timestamp = random.randint(0, 2**32 - 1)
    ssrc = random.randint(0, 2**32 - 1)
    payloads = []
    with open(AUDIO_PATH, "rb") as handle:
        while chunk := handle.read(160):
            payloads.append(chunk.ljust(160, b"\xff"))

    first_packet = True
    packets_since_poll = 0
    try:
        for _ in range(REPEAT):
            for payload in payloads:
                if time.monotonic() >= deadline:
                    return False
                # Marker bit (0x80 in 2nd byte) on the very first packet tells the
                # gateway this is the start of a talk spurt -> reset jitter buffer
                # and begin playout. Without it some gateways stay silent.
                pt_byte = 0x80 if first_packet else 0x00
                first_packet = False
                packet = struct.pack("!BBHII", 0x80, pt_byte, sequence, timestamp, ssrc) + payload
                try:
                    rtp.sendto(packet, (ip, port))
                except socket.error:
                    pass
                sequence = (sequence + 1) & 0xFFFF
                timestamp = (timestamp + 160) & 0xFFFFFFFF
                time.sleep(0.02)
                # Every ~0.5s, check whether the remote hung up.
                packets_since_poll += 1
                if packets_since_poll >= 25:
                    packets_since_poll = 0
                    if poll_sip_bye():
                        return True
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
    finally:
        if own_sock:
            rtp.close()
    return False


try:
    print(f"Calling {TARGET} from {LOCAL_IP}:{LOCAL_PORT}", flush=True)
    register()
    cseq = 1
    branch = invite(cseq)
    final_reply = None
    fallback_ip, fallback_port = None, None
    end = time.time() + int(os.environ.get("SIP_PROCESS_TIMEOUT_SECONDS", "90"))
    while time.time() < end:
        reply = recv_until(lambda r: status(r).startswith("SIP/2.0 "), max(0.1, end - time.time()))
        if not reply:
            break
        line = status(reply)
        if line.startswith(("SIP/2.0 100", "SIP/2.0 180", "SIP/2.0 183")):
            ip, port = parse_sdp(reply)
            if ip:
                fallback_ip, fallback_port = ip, port
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
        if not final_reply:
            print(
                "DIAGNOSIS: Got provisional responses (ringing) but no final 200 OK. "
                f"This host's IP is {LOCAL_IP} (private/CGNAT if 10.x/192.168.x/172.16-31.x). "
                "The provider's 200 OK is not routing back to us. Run on a host with a "
                "public IP, or configure your SIP trunk for NAT, then retry.",
                flush=True,
            )
        raise RuntimeError(f"Call failed: {status(final_reply) if final_reply else 'timeout'}")
    print("Call answered; sending audio", flush=True)
    to_header = header(final_reply, "To")
    contact_uri = sip_uri(header(final_reply, "Contact")) or f"sip:{TARGET}@{DOMAIN}"
    ack(cseq, to_header, branch, contact_uri)
    remote_ip, remote_port = parse_sdp(final_reply)
    if not remote_ip:
        remote_ip, remote_port = fallback_ip, fallback_port
    if not remote_ip:
        raise RuntimeError("Call answered without audio SDP")
    # Gateways often advertise media as a NAT64 IPv6 address that won't route on
    # an IPv4/CGNAT network. Decode it to the embedded IPv4 so RTP actually lands.
    decoded_ip = nat64_to_ipv4(remote_ip)
    if decoded_ip != remote_ip:
        print(f"Media address {remote_ip} decoded to IPv4 {decoded_ip}", flush=True)
        remote_ip = decoded_ip
    print(f"Sending {REPEAT}x audio to {remote_ip}:{remote_port}", flush=True)
    remote_hung_up = send_audio(remote_ip, remote_port, MAX_CALL_SECONDS)
    if remote_hung_up:
        # Remote already tore down the dialog; we answered its BYE. Nothing to do.
        print("Call completed (remote ended)", flush=True)
    else:
        # We finished playback — hang up. A 481/timeout just means the dialog is
        # already gone, which is still a completed call, not a failure.
        cseq += 1
        hangup(cseq, to_header, contact_uri)
        print("Call completed", flush=True)
finally:
    sock.close()
    rtp_sock.close()
