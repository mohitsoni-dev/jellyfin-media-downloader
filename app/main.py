import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pwdlib import PasswordHash
from transmission_rpc import Client as TransmissionClient
from transmission_rpc.error import TransmissionError


DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "/data/app.db"))
HOST_MEDIA_ROOT = PureWindowsPath("C:/Users/DELL/Desktop/media")
TRANSMISSION_MEDIA_ROOT = PurePosixPath("/downloads")
LEGACY_MEDIA_ROOT = PurePosixPath("/media")
SESSION_COOKIE = "auth_session"
SESSION_TTL = 60 * 60 * 24 * 7
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
PASSWORD_HASHER = PasswordHash.recommended()
DUMMY_HASH = PASSWORD_HASHER.hash("not-a-real-account-password")


def connect_database() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    with connect_database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
            CREATE TABLE IF NOT EXISTS library_paths (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                tv_shows_path TEXT NOT NULL DEFAULT '',
                movies_path TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS magnet_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                magnet_link TEXT NOT NULL,
                media_type TEXT NOT NULL CHECK (media_type IN ('series', 'movie')),
                name TEXT NOT NULL,
                season_number INTEGER,
                download_dir TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                CHECK (
                    (media_type = 'series' AND season_number IS NOT NULL) OR
                    (media_type = 'movie' AND season_number IS NULL)
                )
            );
            """
        )
        magnet_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(magnet_requests)")
        }
        if "download_dir" not in magnet_columns:
            connection.execute(
                "ALTER TABLE magnet_requests ADD COLUMN download_dir TEXT NOT NULL DEFAULT ''"
            )
        connection.execute("INSERT OR IGNORE INTO library_paths (id) VALUES (1)")
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="Local Auth", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def get_database():
    connection = connect_database()
    try:
        yield connection
    finally:
        connection.close()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_transmission_client() -> TransmissionClient:
    return TransmissionClient(
        host=os.getenv("TRANSMISSION_HOST", "127.0.0.1"),
        port=int(os.getenv("TRANSMISSION_PORT", "9091")),
        username=os.getenv("TRANSMISSION_USER") or None,
        password=os.getenv("TRANSMISSION_PASSWORD") or None,
        timeout=15,
    )


def format_download_rate(rate: int) -> str:
    if rate < 1024:
        return f"{rate} B/s"
    if rate < 1024 * 1024:
        return f"{rate / 1024:.1f} KB/s"
    return f"{rate / (1024 * 1024):.1f} MB/s"


def active_download_rows(torrents) -> list[dict]:
    active_statuses = {"checking", "check pending", "download pending", "downloading"}
    rows = []
    for torrent in torrents:
        status = str(torrent.status)
        if status not in active_statuses:
            continue
        eta = torrent.format_eta()
        if eta in {"unknown", "not available"}:
            eta = "—"
        rows.append(
            {
                "name": torrent.name,
                "hash": getattr(torrent, "hash_string", ""),
                "status": status,
                "progress": max(0, min(100, float(torrent.percent_done) * 100)),
                "rate": format_download_rate(int(torrent.rate_download)),
                "eta": eta,
            }
        )
    return rows


def safe_folder_name(name: str) -> str:
    folder_name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', " ", name)
    folder_name = " ".join(folder_name.split()).strip(" .")
    if not folder_name or folder_name in {".", ".."}:
        raise ValueError("The name must contain characters that can be used in a folder name.")
    if folder_name.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        folder_name = f"_{folder_name}"
    return folder_name


def transmission_library_path(configured_path: str) -> PurePosixPath:
    windows_path = PureWindowsPath(configured_path)
    if windows_path.is_absolute():
        if ".." in windows_path.parts:
            raise ValueError("Folder paths cannot move above the media directory.")
        try:
            relative_path = windows_path.relative_to(HOST_MEDIA_ROOT)
        except ValueError as error:
            raise ValueError(
                f"Choose a folder inside {HOST_MEDIA_ROOT}, which is mounted for downloads."
            ) from error
        return TRANSMISSION_MEDIA_ROOT.joinpath(*relative_path.parts)

    legacy_path = PurePosixPath(configured_path)
    if legacy_path.is_absolute() and legacy_path.is_relative_to(LEGACY_MEDIA_ROOT):
        if ".." in legacy_path.parts:
            raise ValueError("Folder paths cannot move above the media directory.")
        relative_path = legacy_path.relative_to(LEGACY_MEDIA_ROOT)
        return TRANSMISSION_MEDIA_ROOT.joinpath(*relative_path.parts)

    raise ValueError(
        f"Enter an absolute Windows folder path inside {HOST_MEDIA_ROOT}."
    )


def display_library_path(configured_path: str) -> str:
    legacy_path = PurePosixPath(configured_path)
    if legacy_path.is_absolute() and legacy_path.is_relative_to(LEGACY_MEDIA_ROOT):
        relative_path = legacy_path.relative_to(LEGACY_MEDIA_ROOT)
        return str(HOST_MEDIA_ROOT.joinpath(*relative_path.parts))
    return configured_path


def jellyfin_download_directory(
    connection: sqlite3.Connection,
    media_type: str,
    name: str,
    season_number: int | None,
) -> str:
    folders = connection.execute(
        "SELECT tv_shows_path, movies_path FROM library_paths WHERE id = 1"
    ).fetchone()
    configured_path = folders["tv_shows_path"] if media_type == "series" else folders["movies_path"]
    if not configured_path:
        folder_label = "TV shows" if media_type == "series" else "movies"
        raise ValueError(f"Set the shared {folder_label} folder on the dashboard first.")

    library_root = transmission_library_path(configured_path)

    item_directory = library_root / safe_folder_name(name)
    if media_type == "series":
        item_directory /= f"Season {season_number:02d}"
    if not item_directory.is_relative_to(TRANSMISSION_MEDIA_ROOT):
        raise ValueError("The download folder must be inside the mounted media directory.")
    return str(item_directory)


def get_or_create_session(request: Request, connection: sqlite3.Connection):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session = connection.execute(
            "SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (hash_token(token), int(time.time())),
        ).fetchone()
        if session:
            return session, None

    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO sessions (token_hash, csrf_token, expires_at) VALUES (?, ?, ?)",
        (hash_token(token), csrf_token, int(time.time()) + SESSION_TTL),
    )
    connection.commit()
    session = connection.execute(
        "SELECT * FROM sessions WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()
    return session, token


def set_session_cookie(response: Response, token: str | None) -> None:
    if token:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
            path="/",
        )


def rotate_session(connection: sqlite3.Connection, session, user_id: int) -> str:
    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (session["token_hash"],))
    token = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
        (hash_token(token), user_id, secrets.token_urlsafe(32), int(time.time()) + SESSION_TTL),
    )
    connection.commit()
    return token


def verify_csrf(session, csrf_token: str) -> None:
    if not secrets.compare_digest(session["csrf_token"], csrf_token):
        raise HTTPException(status_code=403, detail="Your form expired. Please try again.")


def render_template(request: Request, name: str, context: dict, token: str | None = None,
                    status_code: int = 200) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )
    set_session_cookie(response, token)
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, connection=Depends(get_database)):
    session, token = get_or_create_session(request, connection)
    if not session["user_id"]:
        response = RedirectResponse("/login", status_code=303)
        set_session_cookie(response, token)
        return response

    user = connection.execute(
        "SELECT email FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()
    if not user:
        response = RedirectResponse("/login", status_code=303)
        set_session_cookie(response, token)
        return response
    paths = connection.execute(
        "SELECT tv_shows_path, movies_path FROM library_paths WHERE id = 1"
    ).fetchone()
    return render_template(
        request,
        "dashboard.html",
        {
            "request": request,
            "email": user["email"],
            "csrf_token": session["csrf_token"],
            "tv_shows_path": display_library_path(paths["tv_shows_path"]),
            "movies_path": display_library_path(paths["movies_path"]),
            "saved": request.query_params.get("saved") == "1",
        },
        token,
    )


@app.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request, connection=Depends(get_database)):
    session, token = get_or_create_session(request, connection)
    if not session["user_id"]:
        response = RedirectResponse("/login", status_code=303)
        set_session_cookie(response, token)
        return response

    try:
        downloads = active_download_rows(create_transmission_client().get_torrents())
        service_error = None
    except TransmissionError:
        downloads = []
        service_error = "Transmission is unavailable. Start the download service to see live status."

    return render_template(
        request,
        "downloads.html",
        {
            "request": request,
            "csrf_token": session["csrf_token"],
            "downloads": downloads,
            "service_error": service_error,
        },
        token,
    )


@app.get("/api/downloads")
def downloads_status(request: Request, connection=Depends(get_database)):
    session, token = get_or_create_session(request, connection)
    if not session["user_id"]:
        response = JSONResponse({"error": "Authentication required."}, status_code=401)
        set_session_cookie(response, token)
        return response

    try:
        downloads = active_download_rows(create_transmission_client().get_torrents())
    except TransmissionError:
        return JSONResponse({"error": "Transmission is unavailable."}, status_code=503)

    response = JSONResponse({"downloads": downloads})
    set_session_cookie(response, token)
    return response


@app.post("/downloads/{torrent_hash}/stop")
def stop_download(
    torrent_hash: str,
    request: Request,
    csrf_token: str = Form(),
    connection=Depends(get_database),
):
    session, token = get_or_create_session(request, connection)
    if not session["user_id"]:
        response = JSONResponse({"error": "Authentication required."}, status_code=401)
        set_session_cookie(response, token)
        return response
    verify_csrf(session, csrf_token)
    if not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", torrent_hash):
        return JSONResponse({"error": "Invalid torrent identifier."}, status_code=400)

    try:
        create_transmission_client().stop_torrent(torrent_hash)
    except TransmissionError:
        return JSONResponse({"error": "Transmission could not stop this download."}, status_code=503)

    response = JSONResponse({"stopped": True})
    set_session_cookie(response, token)
    return response


@app.post("/settings/library")
def save_library_paths(
    request: Request,
    tv_shows_path: str = Form(max_length=1024),
    movies_path: str = Form(max_length=1024),
    csrf_token: str = Form(),
    connection=Depends(get_database),
):
    session, token = get_or_create_session(request, connection)
    if not session["user_id"]:
        response = RedirectResponse("/login", status_code=303)
        set_session_cookie(response, token)
        return response
    verify_csrf(session, csrf_token)
    tv_shows_path = tv_shows_path.strip()
    movies_path = movies_path.strip()
    if "\x00" in tv_shows_path or "\x00" in movies_path:
        raise HTTPException(status_code=400, detail="Folder paths cannot contain null characters.")

    connection.execute(
        "UPDATE library_paths SET tv_shows_path = ?, movies_path = ? WHERE id = 1",
        (tv_shows_path, movies_path),
    )
    connection.commit()
    response = RedirectResponse("/?saved=1", status_code=303)
    set_session_cookie(response, token)
    return response


@app.get("/add", response_class=HTMLResponse)
def add_item_page(request: Request, connection=Depends(get_database)):
    session, token = get_or_create_session(request, connection)
    if not session["user_id"]:
        response = RedirectResponse("/login", status_code=303)
        set_session_cookie(response, token)
        return response
    return render_template(
        request,
        "add_item.html",
        {
            "request": request,
            "csrf_token": session["csrf_token"],
            "media_type": "series",
            "magnet_link": "",
            "name": "",
            "season_number": "",
            "started": request.query_params.get("started") == "1",
        },
        token,
    )


@app.post("/add", response_class=HTMLResponse)
def add_item(
    request: Request,
    magnet_link: str = Form(max_length=4096),
    media_type: str = Form(max_length=10),
    name: str = Form(max_length=200),
    season_number: str = Form(default="", max_length=4),
    csrf_token: str = Form(),
    connection=Depends(get_database),
):
    session, token = get_or_create_session(request, connection)
    if not session["user_id"]:
        response = RedirectResponse("/login", status_code=303)
        set_session_cookie(response, token)
        return response
    verify_csrf(session, csrf_token)

    magnet_link = magnet_link.strip()
    name = name.strip()
    error = None
    if urlsplit(magnet_link).scheme.lower() != "magnet" or not urlsplit(magnet_link).query:
        error = "Enter a valid magnet link."
    elif media_type not in {"series", "movie"}:
        error = "Choose series or movie."
    elif not name:
        error = "Enter a name."

    parsed_season = None
    if not error and media_type == "series":
        try:
            parsed_season = int(season_number)
            if not 1 <= parsed_season <= 999:
                error = "Season number must be between 1 and 999."
        except ValueError:
            error = "Enter a season number for the series."

    if error:
        return render_template(
            request,
            "add_item.html",
            {
                "request": request,
                "csrf_token": session["csrf_token"],
                "media_type": media_type,
                "magnet_link": magnet_link,
                "name": name,
                "season_number": season_number,
                "error": error,
            },
            token,
            400,
        )

    try:
        download_dir = jellyfin_download_directory(
            connection, media_type, name, parsed_season
        )
    except ValueError as path_error:
        return render_template(
            request,
            "add_item.html",
            {
                "request": request,
                "csrf_token": session["csrf_token"],
                "media_type": media_type,
                "magnet_link": magnet_link,
                "name": name,
                "season_number": season_number,
                "error": str(path_error),
            },
            token,
            400,
        )

    try:
        create_transmission_client().add_torrent(magnet_link, download_dir=download_dir)
    except TransmissionError:
        return render_template(
            request,
            "add_item.html",
            {
                "request": request,
                "csrf_token": session["csrf_token"],
                "media_type": media_type,
                "magnet_link": magnet_link,
                "name": name,
                "season_number": season_number,
                "error": "Transmission could not start this download. Check that the download service is running.",
            },
            token,
            503,
        )

    connection.execute(
        """INSERT INTO magnet_requests
           (user_id, magnet_link, media_type, name, season_number, download_dir, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            session["user_id"],
            magnet_link,
            media_type,
            name,
            parsed_season,
            download_dir,
            int(time.time()),
        ),
    )
    connection.commit()
    response = RedirectResponse("/add?started=1", status_code=303)
    set_session_cookie(response, token)
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, connection=Depends(get_database)):
    session, token = get_or_create_session(request, connection)
    if session["user_id"]:
        return RedirectResponse("/", status_code=303)
    return render_template(
        request, "login.html", {"request": request, "csrf_token": session["csrf_token"]}, token
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    csrf_token: str = Form(),
    connection=Depends(get_database),
):
    session, token = get_or_create_session(request, connection)
    verify_csrf(session, csrf_token)
    normalized_email = email.strip().lower()
    user = connection.execute(
        "SELECT id, password_hash FROM users WHERE email = ?", (normalized_email,)
    ).fetchone()
    password_hash = user["password_hash"] if user else DUMMY_HASH
    valid_password = PASSWORD_HASHER.verify(password, password_hash)
    if not user or not valid_password:
        return render_template(
            request,
            "login.html",
            {"request": request, "csrf_token": session["csrf_token"],
             "error": "Email or password is incorrect.", "email": normalized_email},
            token,
            401,
        )

    new_token = rotate_session(connection, session, user["id"])
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, new_token)
    return response


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, connection=Depends(get_database)):
    session, token = get_or_create_session(request, connection)
    if session["user_id"]:
        return RedirectResponse("/", status_code=303)
    return render_template(
        request,
        "register.html",
        {"request": request, "csrf_token": session["csrf_token"]},
        token,
    )


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    csrf_token: str = Form(),
    connection=Depends(get_database),
):
    session, token = get_or_create_session(request, connection)
    verify_csrf(session, csrf_token)
    normalized_email = email.strip().lower()
    error = None
    if len(normalized_email) > 254 or "@" not in normalized_email:
        error = "Enter a valid email address."
    elif len(password) < 8:
        error = "Use a password with at least 8 characters."
    elif connection.execute(
        "SELECT 1 FROM users WHERE email = ?", (normalized_email,)
    ).fetchone():
        error = "An account with that email already exists."

    if error:
        return render_template(
            request,
            "register.html",
            {"request": request, "csrf_token": session["csrf_token"],
             "error": error, "email": normalized_email},
            token,
            400,
        )

    try:
        cursor = connection.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (normalized_email, PASSWORD_HASHER.hash(password), int(time.time())),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        return render_template(
            request,
            "register.html",
            {"request": request, "csrf_token": session["csrf_token"],
             "error": "An account with that email already exists.", "email": normalized_email},
            token,
            400,
        )

    new_token = rotate_session(connection, session, cursor.lastrowid)
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, new_token)
    return response


@app.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(),
    connection=Depends(get_database),
):
    session, _ = get_or_create_session(request, connection)
    verify_csrf(session, csrf_token)
    connection.execute("DELETE FROM sessions WHERE token_hash = ?", (session["token_hash"],))
    connection.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True,
                           samesite="lax")
    return response