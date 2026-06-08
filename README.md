# C-Call

Open-source self-hosted web dashboard for sending SIP voice calls with text-to-speech or uploaded audio.

## Features

- No registration or login
- Browser dashboard for sending SIP voice calls
- Single-call and bulk-call modes
- Settings page for SIP credentials
- Text-to-speech call audio with gTTS
- Custom audio upload support
- Local call history
- Optional CDR import for provider duration, status, and cost
- Optional AmarIP CDR fetch
- Direct SIP calling through `direct_sip_call.py`
- Built-in SMSLayer promo banner

## Clone And Run On Windows

```powershell
git clone https://github.com/Ronyah390/C-Call.git
cd C-Call
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open:

`http://127.0.0.1:5000`

## Clone And Run On Android Termux

Install from the official F-Droid Termux build, then run:

```bash
pkg update
pkg install python git ffmpeg
git clone https://github.com/Ronyah390/C-Call.git
cd C-Call
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open from the same Android device:

`http://127.0.0.1:5000`

If you want to open the Termux server from another phone or PC on the same Wi-Fi, start it with:

```bash
HOST=0.0.0.0 python app.py
```

Then open:

`http://PHONE_LOCAL_IP:5000`

Termux notes:

- Use Wi-Fi when testing SIP. Mobile data often uses carrier-grade NAT and may block or break SIP/RTP UDP traffic.
- If the web page loads but calls fail, check `instance/direct-sip.log`.
- `gTTS` needs internet access to generate text-to-speech audio.
- `ffmpeg` is required for uploaded audio conversion.
- Some SIP providers reject calls from mobile networks even when the same credentials work on a PC.

## Configure SIP

Open `Settings` in the webpage and save:

- SIP domain
- SIP port
- SIP user
- SIP password
- Dial format

The settings are stored in the local app database under `instance/`, which is ignored by git. A person cloning the project can configure the app from the browser without editing source code.

The default dial format is local Bangladesh format, for example `017XXXXXXXX`. If SIP registration works but calls fail with `SIP/2.0 503 Service Unavailable`, try changing `Dial format` in Settings. Some providers expect `+880...`, some expect `880...`, and others expect local `01...` numbers.

## Bulk Calls

Open the dashboard, switch to `Bulk call`, and paste numbers one per line, separate them by commas, or upload a `.csv` file:

```text
017XXXXXXXX
018XXXXXXXX
+88019XXXXXXXX
```

The same text-to-speech message or uploaded audio is used for every number in that batch.

Numbers are normalized automatically. These formats are accepted:

- `017XXXXXXXX`
- `88017XXXXXXXX`
- `+88017XXXXXXXX`

## CDR Data

C-Call does not assume every SIP provider exposes CDR in the same format.

By default, call history uses local app data. If your SIP provider gives you CDR data, you can import it from the dashboard as a CSV file. C-Call tries to recognize common columns such as:

- `number`, `callee`, `callee_number`, `destination`, `dst`, `to`
- `duration`, `duration_seconds`, `billable_seconds`, `billable_duration`
- `cost`, `call_cost`, `price`, `charge`, `amount`
- `status`, `disposition`, `hangup_cause`, `sip_status_code`
- `date`, `start_time`, `created_at`, `timestamp`

If you use AmarIP, open `Settings`, choose `AmarIP API` as the CDR mode, save your AmarIP details, then use `Fetch AmarIP` from the dashboard.

## Requirements

- Python 3.11+
- `ffmpeg` for uploaded audio conversion
- Internet access for gTTS text-to-speech generation
- A working SIP account for outbound calls

## Sponsored Banner

The app includes a promotional banner for SMSLayer:

`https://smslayer.fun`

Bulk SMS from `0.19tk/SMS`, with `5` free credits for new users.

## Production Notes

- Use a strong `FLASK_SECRET_KEY`.
- Use HTTPS if exposed outside your local machine.
- Keep `instance/`, `.env`, generated audio, and logs private.

## License

MIT
