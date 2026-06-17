# C-Call IVR

C-Call IVR is a self-hosted Flask web console for outbound SIP voice calls, one-way voice broadcasts, editable IVR menus, Bangla TTS prompts, ecommerce order calls, and call analytics.

It runs with local SQLite by default, so a new Windows user can usually start it by double-clicking `run.bat`.

## What Is Included

- **Outbound SIP calling** with your own SIP trunk credentials.
- **Direct voice broadcasts** to one number or bulk numbers.
- **Text-to-speech audio** using gTTS, ElevenLabs, or Google Gemini TTS.
- **Audio upload support** with automatic FFmpeg conversion to 8 kHz mu-law for RTP playback.
- **Editable IVR flow builder** with menu, message, transfer, and hangup nodes.
- **Editable demo IVR template** for a Bangla solar panel business.
- **Node editing** for prompt text, node name, type, transfer number, timeout, retries, uploaded audio, and regenerated TTS audio.
- **Flow deletion** from the Flows page or Edit Flow page.
- **DTMF/keypad tracking** for customer journeys.
- **Call recordings** and playback when recording is enabled.
- **Ecommerce order-call API** with API key generation, WooCommerce example, and a local test-order form.
- **Bangla in-app guide** for first-time users.
- **Provider transfer support** using SIP REFER when available.
- **App-side bridge fallback** that can dial an agent and relay RTP audio if SIP REFER is blocked.
- **SQLite by default**, with optional PostgreSQL/Docker mode through `DATABASE_URL`.

## Transfer and Bridging Notes

Transfer nodes work in two stages:

1. The app first tries a normal SIP blind transfer using `REFER`.
2. If the provider does not support `REFER`, the app tries app-side bridging:
   - keeps the customer call alive,
   - places a second outbound call to the agent number,
   - relays RTP audio between customer and agent.

Important limitation: app-side bridging needs your SIP account/provider to allow two simultaneous outbound calls from the same account. If the provider blocks concurrent calls, the bridge may fail with an agent-call timeout or rejection. For production-grade call-center transfer, Asterisk or FreeSWITCH is still the most reliable option.

## Windows Quick Start

1. Download or clone this repository.
2. Double-click:

```bat
run.bat
```

The launcher will:

- check for Python and try to install Python 3.12 with `winget` if missing,
- check for FFmpeg and try to install it with `winget` if missing,
- create `.venv`,
- install Python packages from `requirements.txt`,
- create `.env` from `.env.example` if needed,
- initialize the database,
- start the app at `http://127.0.0.1:5000`.

If `winget` is not available, install Python 3.10+ and FFmpeg manually, then run `run.bat` again.

## First Setup in the Web UI

Open `http://127.0.0.1:5000`, then go to **Settings**.

Configure:

- `SIP_DOMAIN`
- `SIP_PORT`
- `SIP_USER`
- `SIP_PASSWORD`
- `Dial Format`

For Bangladesh numbers, try `Local BD` first. If calls fail, try `E.164` or `Digits only`, depending on your provider.

For TTS:

- gTTS works without an API key and is a good default.
- ElevenLabs needs an ElevenLabs API key. Use **Test API**, **Fetch Voices**, and **Test Voice**.
- Gemini needs a Google AI Studio API key. The recommended model is `gemini-2.5-flash-preview-tts`.

## Demo IVR

Go to **Flows** and click:

```text
Add Bangla Solar Panel Demo IVR
```

This creates an editable Bangla IVR flow for a solar panel business:

- `1` new quotation
- `2` package information
- `3` service support
- `4` EMI/payment information
- `0` representative transfer
- `9` return to main menu from submenus

The demo intentionally does not include a real transfer phone number. Edit the transfer node and add your own agent number before testing transfer/bridge.

## Ecommerce Order Call API

The Ecommerce page creates an API key and exposes:

```http
POST /api/v1/order-call
```

You can authenticate with:

```http
X-API-Key: YOUR_API_KEY
```

Example JSON:

```json
{
  "phone": "+8801700000000",
  "order_id": "ORD-1001",
  "customer_name": "Test Customer",
  "total_amount": "500 BDT",
  "items": "Solar package",
  "message": "Optional custom message"
}
```

Use the **Test Order Call** form on the Ecommerce page before integrating WooCommerce, Shopify, n8n, Zapier, or a custom backend.

## Manual Install

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open:

```text
http://127.0.0.1:5000
```

## Optional PostgreSQL Mode

By default the app uses SQLite at:

```text
instance/c_call_ivr.db
```

To use PostgreSQL, set `DATABASE_URL` in `.env`:

```env
DATABASE_URL=postgresql://ivr:ivr@127.0.0.1:5432/ivr
```

Then run `run.bat`. If Docker is available, the launcher can start the included `docker-compose.yml`.

## Files

| File | Purpose |
| --- | --- |
| `app.py` | Flask routes, settings, dashboard, flows, ecommerce API |
| `ivr_engine.py` | SIP/RTP IVR engine, DTMF, REFER transfer, app-side bridge |
| `direct_sip_call.py` | Direct one-way SIP call helper |
| `call_runner.py` | Background call orchestration |
| `audio.py` | TTS generation, audio conversion, phone helpers |
| `db.py` | SQLite/PostgreSQL database wrapper |
| `templates/` | Web UI templates |
| `static/style.css` | UI styling |
| `run.bat` / `run.ps1` | Windows one-click launcher |
| `seed_demo.py` | Optional solar-panel demo seeder |

## What Is Not Committed

The repository intentionally ignores:

- `.env`
- `.venv/`
- `instance/`
- generated audio
- uploaded audio
- recordings
- local database files
- logs

This prevents saved SIP credentials, API keys, phone numbers, recordings, and local call history from being pushed.

## Troubleshooting

- **No SIP ready status:** Check SIP domain, username, password, and port.
- **SIP 503 or call rejected:** Change Dial Format in Settings.
- **TTS preview fails:** Test API key from Settings, fetch voices, and try another model/voice.
- **Call audio is distorted:** If Settings preview is clean, it is usually RTP/network/provider jitter.
- **Transfer failed: REFER timed out:** Provider likely blocks SIP REFER. The app will try bridge fallback.
- **Bridge failed:** Provider may block two simultaneous calls, or the agent did not answer.
- **Audio conversion fails:** Install FFmpeg or set `FFMPEG_PATH` in `.env`.

## License

MIT
