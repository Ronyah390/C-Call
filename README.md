# C-Call

Open-source self-hosted web dashboard for sending SIP voice calls with text-to-speech or uploaded audio.

## Features

- No registration or login
- Browser dashboard for sending SIP voice calls
- Settings page for SIP credentials
- Text-to-speech call audio with gTTS
- Custom audio upload support
- Local call history
- Direct SIP calling through `direct_sip_call.py`
- Built-in SMSLayer promo banner

## Clone And Run

```bash
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

## Configure SIP

Open `Settings` in the webpage and save:

- SIP domain
- SIP port
- SIP user
- SIP password

The settings are stored in the local app database under `instance/`, which is ignored by git. A person cloning the project can configure the app from the browser without editing source code.

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
