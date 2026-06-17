"""Media-path probe: place a call, send RTP to the (NAT64-decoded) IPv4 media
address, and simultaneously count RTP packets coming BACK from the gateway.

If we receive RTP back, the IPv4 media path is alive (so silence is a codec/
timing issue). If we receive nothing, the gateway is not accepting our IPv4 RTP.
"""
import hashlib
import ipaddress
import os
import random
import re
import socket
import struct
import sys
import time

SERVER = os.environ["SIP_DOMAIN"]
PORT = int(os.environ.get("SIP_PORT", "5060"))
USER = os.environ["SIP_USER"]
PASSWORD = os.environ["SIP_PASSWORD"]
DOMAIN = SERVER
TARGET = sys.argv[1]
AUDIO_PATH = sys.argv[2]
SEND_SECONDS = int(sys.argv[3]) if len(sys.argv) > 3 else 10


def nat64_to_ipv4(ip):
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


def token(n=10):
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=n))


def md5(v):
    return hashlib.md5(v.encode()).hexdigest()


def status(r):
    return r.split("\r\n", 1)[0]


def header(r, name):
    m = re.search(rf"^{re.escape(name)}:\s*(.+)$", r, re.I | re.M)
    return m.group(1).strip() if m else ""


def sip_uri(v):
    m = re.search(r"<(sip:[^>]+)>", v or "", re.I)
    return m.group(1) if m else (re.search(r"\b(sip:[^;\s>]+)", v or "", re.I) or [None, ""])[1]


def parse_challenge(r):
    m = re.search(r"(?:WWW|Proxy)-Authenticate:\s*Digest\s+(.*?)\r\n", r, re.I)
    if not m:
        return None, None
    p = {}
    for it in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', m.group(1)):
        p[it.group(1).lower()] = it.group(2) or it.group(3)
    return p, "Proxy-Authenticate" in r


def digest(p, method, uri):
    ha1 = md5(f"{USER}:{p.get('realm','')}:{PASSWORD}")
    ha2 = md5(f"{method}:{uri}")
    resp = md5(f"{ha1}:{p.get('nonce','')}:{ha2}")
    return (f'Digest username="{USER}", realm="{p.get("realm","")}", nonce="{p.get("nonce","")}", '
            f'uri="{uri}", response="{resp}", algorithm=MD5')


def parse_sdp(r):
    body = r.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in r else ""
    ip = re.search(r"^c=IN IP[46] ([^\r\n]+)", body, re.M)
    port = re.search(r"^m=audio (\d+)", body, re.M)
    return (ip.group(1).strip("[]"), int(port.group(1))) if ip and port else (None, None)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((SERVER, PORT))
    ip = s.getsockname()[0]
    s.close()
    return ip


LOCAL_IP = local_ip()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 0))
LOCAL_PORT = sock.getsockname()[1]
sock.settimeout(1)

rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rtp.bind(("0.0.0.0", 0))
rtp_port = rtp.getsockname()[1]

call_id = f"call-{token(16)}@{LOCAL_IP}"
from_tag = token(8)
reg_call_id = f"reg-{token(16)}@{LOCAL_IP}"
reg_tag = token(8)


def send(msg, addr=None):
    try:
        sock.sendto(msg.encode(), addr or (SERVER, PORT))
    except OSError as e:
        print("send err", e)


def recv_until(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        sock.settimeout(max(0.1, min(5.0, end - time.time())))
        try:
            data, _ = sock.recvfrom(8192)
            text = data.decode(errors="replace")
            print("   <<<", status(text))
            if pred(text):
                return text
        except socket.timeout:
            send("\r\n\r\n")
    return None


def register():
    def msg(cseq, auth=None):
        r = (f"REGISTER sip:{DOMAIN} SIP/2.0\r\n"
             f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_PORT};rport;branch=z9hG4bK{token()}\r\n"
             "Max-Forwards: 70\r\n"
             f"From: <sip:{USER}@{DOMAIN}>;tag={reg_tag}\r\n"
             f"To: <sip:{USER}@{DOMAIN}>\r\n"
             f"Call-ID: {reg_call_id}\r\nCSeq: {cseq} REGISTER\r\n"
             f"Contact: <sip:{USER}@{LOCAL_IP}:{LOCAL_PORT}>\r\nExpires: 300\r\n"
             "User-Agent: Probe/1.0\r\n")
        if auth:
            r += f"Authorization: {auth}\r\n"
        return r + "Content-Length: 0\r\n\r\n"
    send(msg(1))
    rep = recv_until(lambda r: status(r).startswith(("SIP/2.0 2", "SIP/2.0 4")), 5)
    if rep and status(rep).startswith("SIP/2.0 401"):
        p, _ = parse_challenge(rep)
        send(msg(2, digest(p, "REGISTER", f"sip:{DOMAIN}")))
        rep = recv_until(lambda r: status(r).startswith("SIP/2.0 200"), 5)
    if not rep or not status(rep).startswith("SIP/2.0 200"):
        raise RuntimeError("REGISTER failed")
    print("Registered OK")


def invite(cseq, auth_header=None, auth=None):
    branch = f"z9hG4bK{token()}"
    sdp = ("v=0\r\n"
           f"o=- {int(time.time())} {int(time.time())} IN IP4 {LOCAL_IP}\r\n"
           f"s=Probe\r\nc=IN IP4 {LOCAL_IP}\r\nt=0 0\r\n"
           f"m=audio {rtp_port} RTP/AVP 0 8 101\r\n"
           "a=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n"
           "a=rtpmap:101 telephone-event/8000\r\na=sendrecv\r\n")
    m = (f"INVITE sip:{TARGET}@{DOMAIN} SIP/2.0\r\n"
         f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_PORT};rport;branch={branch}\r\n"
         "Max-Forwards: 70\r\n"
         f'From: "Probe" <sip:{USER}@{DOMAIN}>;tag={from_tag}\r\n'
         f"To: <sip:{TARGET}@{DOMAIN}>\r\nCall-ID: {call_id}\r\n"
         f"CSeq: {cseq} INVITE\r\n"
         f"Contact: <sip:{USER}@{LOCAL_IP}:{LOCAL_PORT}>\r\n"
         "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, INFO\r\n"
         "User-Agent: Probe/1.0\r\nContent-Type: application/sdp\r\n")
    if auth:
        m += f"{auth_header}: {auth}\r\n"
    m += f"Content-Length: {len(sdp)}\r\n\r\n{sdp}"
    send(m)
    return branch


def ack(cseq, to_h, branch, ruri):
    send(f"ACK {ruri} SIP/2.0\r\n"
         f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_PORT};rport;branch={branch}\r\n"
         "Max-Forwards: 70\r\n"
         f'From: "Probe" <sip:{USER}@{DOMAIN}>;tag={from_tag}\r\n'
         f"To: {to_h}\r\nCall-ID: {call_id}\r\nCSeq: {cseq} ACK\r\nContent-Length: 0\r\n\r\n")


def bye(cseq, to_h, ruri):
    send(f"BYE {ruri} SIP/2.0\r\n"
         f"Via: SIP/2.0/UDP {LOCAL_IP}:{LOCAL_PORT};rport;branch=z9hG4bK{token()}\r\n"
         "Max-Forwards: 70\r\n"
         f'From: "Probe" <sip:{USER}@{DOMAIN}>;tag={from_tag}\r\n'
         f"To: {to_h}\r\nCall-ID: {call_id}\r\nCSeq: {cseq} BYE\r\nContent-Length: 0\r\n\r\n")


print(f"Probe calling {TARGET} from {LOCAL_IP}:{LOCAL_PORT}, rtp_port={rtp_port}")
register()
cseq = 1
branch = invite(cseq)
final = None
fb_ip = fb_port = None
end = time.time() + 45
while time.time() < end:
    rep = recv_until(lambda r: status(r).startswith("SIP/2.0 "), max(0.1, end - time.time()))
    if not rep:
        break
    line = status(rep)
    if line.startswith(("SIP/2.0 100", "SIP/2.0 180", "SIP/2.0 183")):
        ip, port = parse_sdp(rep)
        if ip:
            fb_ip, fb_port = ip, port
        continue
    if line.startswith(("SIP/2.0 401", "SIP/2.0 407")):
        p, isp = parse_challenge(rep)
        ack(cseq, header(rep, "To"), branch, f"sip:{TARGET}@{DOMAIN}")
        cseq += 1
        branch = invite(cseq, "Proxy-Authorization" if isp else "Authorization",
                        digest(p, "INVITE", f"sip:{TARGET}@{DOMAIN}"))
        continue
    final = rep
    break

if not final or not status(final).startswith("SIP/2.0 200"):
    raise RuntimeError(f"Call not answered: {status(final) if final else 'timeout'}")

to_h = header(final, "To")
ruri = sip_uri(header(final, "Contact")) or f"sip:{TARGET}@{DOMAIN}"
ack(cseq, to_h, branch, ruri)
rip, rport = parse_sdp(final)
if not rip:
    rip, rport = fb_ip, fb_port
decoded = nat64_to_ipv4(rip)
print(f"\nANSWERED. Media advertised: {rip}:{rport}  ->  using IPv4 {decoded}:{rport}\n")

# Load audio
payloads = []
with open(AUDIO_PATH, "rb") as f:
    while chunk := f.read(160):
        payloads.append(chunk.ljust(160, b"\xff"))

seq = random.randint(0, 65535)
ts = random.randint(0, 2**32 - 1)
ssrc = random.randint(0, 2**32 - 1)
rtp.setblocking(False)
sent = 0
recv = 0
recv_srcs = {}
deadline = time.monotonic() + SEND_SECONDS
i = 0
while time.monotonic() < deadline:
    payload = payloads[i % len(payloads)]
    i += 1
    pkt = struct.pack("!BBHII", 0x80, 0x00 if sent else 0x80, seq, ts, ssrc) + payload
    try:
        rtp.sendto(pkt, (decoded, rport))
        sent += 1
    except OSError as e:
        print("rtp send err", e)
    seq = (seq + 1) & 0xFFFF
    ts = (ts + 160) & 0xFFFFFFFF
    # drain inbound
    for _ in range(8):
        try:
            d, src = rtp.recvfrom(2048)
            recv += 1
            recv_srcs[src] = recv_srcs.get(src, 0) + 1
        except OSError:
            break
    time.sleep(0.02)

print(f"\n==== RESULT ====")
print(f"RTP packets SENT to {decoded}:{rport}: {sent}")
print(f"RTP packets RECEIVED back: {recv}")
print(f"  sources: {recv_srcs}")
if recv:
    print("=> MEDIA PATH IS ALIVE over IPv4. Silence is a codec/timing issue, not routing.")
else:
    print("=> NO RTP returned. Gateway is NOT sending media to our IPv4 socket.")

cseq += 1
bye(cseq, to_h, ruri)
recv_until(lambda r: status(r).startswith("SIP/2.0 200") and "BYE" in r, 3)
sock.close()
rtp.close()
print("Probe done.")
