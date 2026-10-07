# Local Auth

A small email-and-password authentication app. Accounts and server-side sessions are stored in SQLite; the database file is persisted in a Docker named volume.

Signed-in users can configure shared TV shows and movies folder paths from the dashboard. The values are stored once in SQLite and are visible to every account. All signed-in users can edit them. Enter full Windows paths such as `C:\Users\DELL\Desktop\media\Shows`; Compose mounts `C:/Users/DELL/Desktop/media` into Transmission, and the app translates saved paths for the torrent service. No media folder path is configured in `.env`.

The add form sends magnet links to the private Transmission service. Series are placed in `<TV shows>/<Series Name>/Season 01`; movies are placed in their own `<Movies>/<Movie Name>` folder. Include a year in the movie name when known. Torrent-provided filenames are not renamed, so Jellyfin matching still depends on the files inside the torrent following Jellyfin's episode/movie naming guidance. Only start downloads for content you have permission to retrieve and share.

## Start with Docker Compose

1. Copy `.env.example` to `.env` and replace `TRANSMISSION_PASSWORD` with a long random password.
2. Run `docker compose up --build -d`.
3. Open `http://localhost:8000` (or the configured `APP_PORT`) and create an account.

The SQLite database lives in the `auth_data` volume mounted at `/data`. Transmission settings live in `transmission_config`; downloaded media is written directly under `C:\Users\DELL\Desktop\media`. Back up these separately. `docker compose down` keeps named volumes; `docker compose down -v` deletes account and Transmission configuration data. If your media directory is elsewhere, update the bind-mount source in `compose.yaml` and `HOST_MEDIA_ROOT` in `app/main.py` to the same parent directory.

For HTTPS behind a trusted reverse proxy, set `COOKIE_SECURE=true`. Do not expose this starter directly to the public internet without adding HTTPS, request throttling, operational monitoring, and a backup plan. Passwords are hashed with Argon2. Sessions use random opaque tokens stored as hashes in SQLite, HttpOnly/SameSite cookies, CSRF tokens, and a seven-day expiry.

## Run locally

Install dependencies from `requirements.txt`, then run:

```powershell
$env:DATABASE_PATH = "./data/app.db"
uvicorn app.main:app --reload
```

## Tests

```powershell
pytest
```
