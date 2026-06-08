# Auto CL Web

Open-source web dashboard for sending SIP voice calls with text-to-speech or uploaded audio.

## Features

- Browser-based dashboard for users and admins
- Send voice calls with text-to-speech audio
- Upload custom audio for calls
- Credit charging by call duration
- Redeem codes for user credits
- Admin user stats and overall call stats
- Admin-managed SIP settings from the webpage
- Direct SIP calling through `direct_sip_call.py`

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

Default admin account:

```text
admin@example.com
change-me
```

You can change the default admin login before first run by editing only these values in `.env`:

```dotenv
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=change-me
```

## SIP Setup

After login as admin, open `Admin` and save:

- SIP domain
- SIP port
- SIP user
- SIP password

The SIP password is stored in the local app database under `instance/`, which is ignored by git. Users cloning the project do not need to edit source files to configure SIP.

## Production Notes

- Use a strong `FLASK_SECRET_KEY`.
- Change the default admin password.
- Use HTTPS in production.
- Keep `instance/`, `.env`, generated audio, and logs private.
- Install `ffmpeg`; it is required for uploaded audio conversion.

## License

MIT
