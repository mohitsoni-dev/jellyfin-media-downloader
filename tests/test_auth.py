import re
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app import main


def csrf_from(response) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_legacy_media_path_maps_to_windows_folder():
    assert main.display_library_path("/media/Shows") == r"C:\Users\DELL\Desktop\media\Shows"
    assert str(main.transmission_library_path("/media/Shows")) == "/downloads/Shows"


def test_downloads_page_lists_active_torrents_only(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    transmission = Mock()
    transmission.get_torrents.return_value = [
        SimpleNamespace(
            name="Example Series",
            status="downloading",
            percent_done=0.375,
            rate_download=1536,
            format_eta=lambda: "0 days 00:02:00",
        ),
        SimpleNamespace(
            name="Finished Movie",
            status="seeding",
            percent_done=1.0,
            rate_download=0,
            format_eta=lambda: "not available",
        ),
    ]
    monkeypatch.setattr(main, "create_transmission_client", lambda: transmission)
    with TestClient(main.app) as client:
        page = client.get("/downloads", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/login"

        register_page = client.get("/register")
        client.post(
            "/register",
            data={
                "email": "downloads@example.com",
                "password": "downloads-password",
                "csrf_token": csrf_from(register_page),
            },
            follow_redirects=False,
        )
        page = client.get("/downloads")
        assert page.status_code == 200
        assert "Example Series" in page.text
        assert "37.5%" in page.text
        assert "1.5 KB/s" in page.text
        assert "Finished Movie" not in page.text
        assert "window.setInterval(refreshDownloads, 5000)" in page.text
        assert 'fetch("/api/downloads"' in page.text


def test_downloads_page_shows_transmission_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    transmission = Mock()
    transmission.get_torrents.side_effect = main.TransmissionError("RPC offline")
    monkeypatch.setattr(main, "create_transmission_client", lambda: transmission)
    with TestClient(main.app) as client:
        register_page = client.get("/register")
        client.post(
            "/register",
            data={
                "email": "downloads@example.com",
                "password": "downloads-password",
                "csrf_token": csrf_from(register_page),
            },
            follow_redirects=False,
        )
        page = client.get("/downloads")
        assert page.status_code == 200
        assert "Transmission is unavailable" in page.text


def test_downloads_status_api_requires_auth_and_returns_live_values(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    transmission = Mock()
    transmission.get_torrents.return_value = [
        SimpleNamespace(
            name="Live Series",
            status="downloading",
            percent_done=0.5,
            rate_download=2048,
            format_eta=lambda: "0 days 00:01:00",
        )
    ]
    monkeypatch.setattr(main, "create_transmission_client", lambda: transmission)
    with TestClient(main.app) as client:
        assert client.get("/api/downloads").status_code == 401
        register_page = client.get("/register")
        client.post(
            "/register",
            data={
                "email": "api-downloads@example.com",
                "password": "downloads-password",
                "csrf_token": csrf_from(register_page),
            },
            follow_redirects=False,
        )
        response = client.get("/api/downloads")
        assert response.status_code == 200
        assert response.json()["downloads"] == [
            {
                "name": "Live Series",
                "status": "downloading",
                "progress": 50.0,
                "rate": "2.0 KB/s",
                "eta": "0 days 00:01:00",
            }
        ]


def test_registration_login_and_logout(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    with TestClient(main.app) as client:
        page = client.get("/register")
        assert page.status_code == 200
        response = client.post(
            "/register",
            data={
                "email": " Person@Example.com ",
                "password": "a-secure-password",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert client.get("/").status_code == 200
        assert "person@example.com" in client.get("/").text

        page = client.get("/", follow_redirects=False)
        logout = client.post(
            "/logout", data={"csrf_token": csrf_from(page)}, follow_redirects=False
        )
        assert logout.status_code == 303
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"

        page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "email": "person@example.com",
                "password": "a-secure-password",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert client.get("/").status_code == 200


def test_registration_rejects_short_password_and_missing_csrf(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    with TestClient(main.app) as client:
        page = client.get("/register")
        rejected = client.post(
            "/register",
            data={
                "email": "person@example.com",
                "password": "short",
                "csrf_token": csrf_from(page),
            },
        )
        assert rejected.status_code == 400
        assert "at least 8 characters" in rejected.text

        csrf_rejected = client.post(
            "/register",
            data={
                "email": "person@example.com",
                "password": "a-secure-password",
                "csrf_token": "not-the-session-token",
            },
        )
        assert csrf_rejected.status_code == 403


def test_library_paths_are_shared_between_users(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    with TestClient(main.app) as first_user:
        page = first_user.get("/register")
        first_user.post(
            "/register",
            data={
                "email": "first@example.com",
                "password": "first-user-password",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        dashboard = first_user.get("/")
        saved = first_user.post(
            "/settings/library",
            data={
                "tv_shows_path": r"C:\Users\DELL\Desktop\media\Shows",
                "movies_path": r"C:\Users\DELL\Desktop\media\Movies",
                "csrf_token": csrf_from(dashboard),
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert saved.headers["location"] == "/?saved=1"

    with TestClient(main.app) as second_user:
        page = second_user.get("/register")
        second_user.post(
            "/register",
            data={
                "email": "second@example.com",
                "password": "second-user-password",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        dashboard = second_user.get("/")
        assert 'value="C:\\Users\\DELL\\Desktop\\media\\Shows"' in dashboard.text
        assert 'value="C:\\Users\\DELL\\Desktop\\media\\Movies"' in dashboard.text


def test_anonymous_user_cannot_change_library_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    with TestClient(main.app) as client:
        page = client.get("/register")
        save = client.post(
            "/settings/library",
            data={
                "tv_shows_path": "/should/not/save",
                "movies_path": "/should/not/save",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert save.status_code == 303
        assert save.headers["location"] == "/login"
        assert client.get("/register").status_code == 200
        connection = main.connect_database()
        paths = connection.execute(
            "SELECT tv_shows_path, movies_path FROM library_paths WHERE id = 1"
        ).fetchone()
        connection.close()
        assert paths["tv_shows_path"] == ""
        assert paths["movies_path"] == ""


def test_add_magnet_series_and_movie(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    transmission = Mock()
    monkeypatch.setattr(main, "create_transmission_client", lambda: transmission)
    with TestClient(main.app) as client:
        page = client.get("/register")
        client.post(
            "/register",
            data={
                "email": "media@example.com",
                "password": "media-user-password",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )

        connection = main.connect_database()
        connection.execute(
            "UPDATE library_paths SET tv_shows_path = ?, movies_path = ? WHERE id = 1",
            (
                r"C:\Users\DELL\Desktop\media\Shows",
                r"C:\Users\DELL\Desktop\media\Movies",
            ),
        )
        connection.commit()
        connection.close()

        page = client.get("/add")
        assert page.status_code == 200
        series = client.post(
            "/add",
            data={
                "magnet_link": "magnet:?xt=urn:btih:serieshash",
                "media_type": "series",
                "name": "Example Series",
                "season_number": "2",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert series.status_code == 303
        assert series.headers["location"] == "/add?started=1"

        page = client.get("/add")
        movie = client.post(
            "/add",
            data={
                "magnet_link": "magnet:?xt=urn:btih:moviehash",
                "media_type": "movie",
                "name": "Example Movie (2024)",
                "season_number": "",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert movie.status_code == 303

        connection = main.connect_database()
        items = connection.execute(
            "SELECT media_type, name, season_number, download_dir FROM magnet_requests ORDER BY id"
        ).fetchall()
        connection.close()
        assert [
            (item["media_type"], item["name"], item["season_number"], item["download_dir"])
            for item in items
        ] == [
            ("series", "Example Series", 2, "/downloads/Shows/Example Series/Season 02"),
            ("movie", "Example Movie (2024)", None, "/downloads/Movies/Example Movie (2024)"),
        ]
        assert transmission.add_torrent.call_count == 2


def test_download_rejects_library_path_outside_media_mount(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    transmission = Mock()
    monkeypatch.setattr(main, "create_transmission_client", lambda: transmission)
    with TestClient(main.app) as client:
        page = client.get("/register")
        client.post(
            "/register",
            data={
                "email": "media@example.com",
                "password": "media-user-password",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        connection = main.connect_database()
        connection.execute(
            "UPDATE library_paths SET movies_path = ? WHERE id = 1",
            (r"D:\Outside\Movies",),
        )
        connection.commit()
        connection.close()

        page = client.get("/add")
        rejected = client.post(
            "/add",
            data={
                "magnet_link": "magnet:?xt=urn:btih:moviehash",
                "media_type": "movie",
                "name": "Example Movie",
                "season_number": "",
                "csrf_token": csrf_from(page),
            },
        )
        assert rejected.status_code == 400
        assert "Choose a folder inside" in rejected.text
        transmission.add_torrent.assert_not_called()


def test_add_magnet_requires_auth_and_series_season(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "auth.db")
    with TestClient(main.app) as client:
        assert client.get("/add", follow_redirects=False).headers["location"] == "/login"

        page = client.get("/register")
        client.post(
            "/register",
            data={
                "email": "media@example.com",
                "password": "media-user-password",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        page = client.get("/add")
        rejected = client.post(
            "/add",
            data={
                "magnet_link": "magnet:?xt=urn:btih:serieshash",
                "media_type": "series",
                "name": "Example Series",
                "season_number": "",
                "csrf_token": csrf_from(page),
            },
        )
        assert rejected.status_code == 400
        assert "season number" in rejected.text

        rejected_upper_bound = client.post(
            "/add",
            data={
                "magnet_link": "magnet:?xt=urn:btih:serieshash",
                "media_type": "series",
                "name": "Example Series",
                "season_number": "1000",
                "csrf_token": csrf_from(page),
            },
        )
        assert rejected_upper_bound.status_code == 400
        assert "between 1 and 999" in rejected_upper_bound.text