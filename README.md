# Auto CL Web

Open-source web dashboard and HTTP API for sending SIP voice calls with text-to-speech or uploaded audio.

This version replaces the Telegram bot UI with a browser-based app. SIP credentials are loaded from environment variables or `.env`, which is ignored by git.

## Features

- Web login for users and admin
- Send voice calls from the dashboard
- Text-to-speech call audio with gTTS
- Custom audio upload support
- Credit charging by call duration
- Redeem codes
- Admin user stats and overall stats
- API client key generation, revocation, and credits
- JSON API for balance, calls, and optional CDR lookup
- Direct SIP calling through `direct_sip_call.py`

## Quick Start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open `http://127.0.0.1:5000`.

Before real calls, edit `.env` and set:

```dotenv
SIP_DOMAIN=your-sip-host
SIP_PORT=5060
SIP_USER=your-sip-user
SIP_PASSWORD=your-sip-password
```

Never commit `.env`, database files, generated audio, or logs.

## API

Authenticate with `X-API-Key` or `Authorization: Bearer KEY`.

```bash
curl http://127.0.0.1:5000/api/balance -H "X-API-Key: YOUR_KEY"
```

```bash
curl -X POST http://127.0.0.1:5000/api/call ^
  -H "X-API-Key: YOUR_KEY" ^
  -H "Content-Type: application/json" ^
  -d "{\"number\":\"017XXXXXXXX\",\"text\":\"Hello from Auto CL\"}"
```

For custom audio, send `audio_base64` instead of `text`.

## Security Notes

- SIP credentials are not stored in source code.
- `.env` is ignored by git.
- API keys are generated server-side.
- Admin password is read from environment variables.
- Use HTTPS in production.

## License

MIT
