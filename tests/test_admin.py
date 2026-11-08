"""Regression and auth tests for a private operator dashboard."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import main
from app.admin_auth import COOKIE_NAME, SESSION_TTL_SECONDS, new_session, valid_session
from test_api import fixture_app, create


def secure_client():
    # Session cookie deliberately uses Secure; real Hostless runs under HTTPS.
    return TestClient(main.app, base_url="https://testserver")


def login(client, password="strong-pass-90!", action=True):
    headers = {"X-Admin-Request": "1"} if action else {}
    return client.post("/api/admin/login", headers=headers, json={"password": password})


def with_admin(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, admin_password="strong-pass-90!"))
    main.admin_login_times.clear()
    return secure_client()


def test_admin_unavailable_until_password_is_set(fixture_app):
    _fake, _client = fixture_app
    client = secure_client()
    assert client.get('/admin').status_code == 200
    assert 'Administrator sign in' in client.get('/admin').text
    assert client.get('/api/admin/overview').status_code == 503
    assert client.post('/api/admin/login', json={"password": "x"}, headers={"X-Admin-Request": "1"}).status_code == 503


def test_admin_login_cookies_bad_password_and_csrf(fixture_app, monkeypatch):
    _fake, _test = fixture_app
    client = with_admin(monkeypatch)
    assert client.get('/api/admin/overview').status_code == 401
    assert login(client, action=False).status_code == 403
    assert login(client, "wrong").status_code == 401
    assert client.get('/api/admin/session').status_code == 401
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'samesite=strict' in cookie.lower()
    assert 'path=/api/admin' in cookie.lower()
    assert client.get('/api/admin/session').json() == {"authenticated": True}
    assert client.get('/api/admin/overview').status_code == 200
    assert client.post('/api/admin/logout').status_code == 403  # CSRF header required
    assert client.post('/api/admin/logout', headers={"X-Admin-Request": "1"}).status_code == 200
    assert client.get('/api/admin/session').status_code == 401


def test_signed_session_expiration_and_password_rotation():
    issued = 1790000000
    token = new_session("old-strong-pass", issued)
    assert valid_session(token, "old-strong-pass", issued + 10)
    assert not valid_session(token, "wrong", issued + 10)
    assert not valid_session(token, "old-strong-pass", issued + SESSION_TTL_SECONDS + 1)
    assert not valid_session(token + "a", "old-strong-pass", issued + 10)
    assert not valid_session(token, "old-strong-pass", issued - 120)


def test_dashboard_current_feeds_counts_and_security(fixture_app, monkeypatch):
    fake, _client = fixture_app
    client = with_admin(monkeypatch)
    made = create(client).json()
    mirror_id = made['id']
    assert login(client).status_code == 200
    feed = client.get(f"/feed/{mirror_id}.xml")
    assert feed.status_code == 200
    index = fake.get_json(f"mirrors/{mirror_id}/episodes.json")
    eid = next(iter(index['episodes']))
    assert client.head(f"/episode/{mirror_id}/{eid}.mp3").status_code == 200
    audio = client.get(f"/episode/{mirror_id}/{eid}.mp3", headers={"Range": "bytes=0-10"})
    assert audio.status_code == 206
    results = client.get('/api/admin/overview')
    assert results.status_code == 200, results.text
    payload = results.json()
    assert payload['summary']['active_feeds'] == 1
    assert payload['summary']['deleted_feeds'] == 0
    assert payload['summary']['active_last_30_days'] == 1
    assert payload['summary']['rss_requests'] == 1
    assert payload['summary']['audio_requests'] == 1
    assert payload['active'][0]['activity']['generations'] == 1
    assert payload['active'][0]['activity']['audio_bytes_requested'] == 11
    assert payload['active'][0]['title'] == "Sample Podcast"
    assert payload['active'][0]['episode_count'] == 1
    assert 'feed_url' not in payload['active'][0]
    assert made['manage_token'] not in results.text
    assert 'manage_hash' not in results.text
    assert payload['shows'][0]['total'] == 1
    assert payload['shows'][0]['custom_outros'] == 1
    assert fake.get_json("system/activity.json")["feeds"][mirror_id]["audio_requests"] == 1
    # A new ActivityTracker simulates an app restart: totals remain stored in B2.
    monkeypatch.setattr(main, 'activity_tracker', main.ActivityTracker())
    assert client.get('/api/admin/overview').json()['summary']['audio_requests'] == 1


def test_admin_deletes_without_key_and_history_tracks_show(fixture_app, monkeypatch):
    fake, _client = fixture_app
    client = with_admin(monkeypatch)
    first = create(client).json()
    second = create(client).json()
    assert login(client).status_code == 200
    mirror_id = first['id']
    denied = client.delete(f'/api/admin/mirrors/{mirror_id}')
    assert denied.status_code == 403  # session cookie alone cannot forge delete
    assert fake.head(f'mirrors/{mirror_id}/meta.json') is not None
    deleted = client.delete(f'/api/admin/mirrors/{mirror_id}', headers={'X-Admin-Request': '1'})
    assert deleted.status_code == 200
    assert not any(k.startswith(f'mirrors/{mirror_id}/') or k.startswith(f'cache/{mirror_id}/') for k in fake.data)
    archive = fake.get_json(f'system/deleted/{mirror_id}.json')
    assert archive['deleted_by'] == 'administrator'
    assert archive['status'] == 'deleted'
    assert archive['title'] == 'Sample Podcast'
    assert 'manage_hash' not in str(archive) and 'feed_url' not in archive
    # Only an anonymised record remains; the other mirror is untouched.
    stats = client.get('/api/admin/overview').json()
    assert stats['summary']['active_feeds'] == 1
    assert stats['summary']['deleted_feeds'] == 1
    assert stats['deleted'][0]['id'] == first['id']
    assert stats['active'][0]['id'] == second['id']
    assert stats['shows'][0]['total'] == 2
    assert stats['shows'][0]['active'] == 1
    assert stats['shows'][0]['deleted'] == 1
    assert client.get(f'/feed/{mirror_id}.xml').status_code == 404


def test_creator_deletion_is_archived_and_visible_to_admin(fixture_app, monkeypatch):
    fake, _ = fixture_app
    client = with_admin(monkeypatch)
    created = create(client).json()
    mid = created['id']
    assert client.delete('/api/manage/feed', headers={'X-Manage-Token': created['manage_token']}).status_code == 200
    history = fake.get_json(f'system/deleted/{mid}.json')
    assert history['deleted_by'] == 'creator'
    assert login(client).status_code == 200
    stats = client.get('/api/admin/overview').json()
    assert stats['deleted'][0]['title'] == 'Sample Podcast'
    assert stats['summary']['active_feeds'] == 0
    assert stats['summary']['deleted_feeds'] == 1


def test_failed_deletion_is_flagged_as_pending_not_counted_as_deleted(fixture_app, monkeypatch):
    fake, _ = fixture_app
    client = with_admin(monkeypatch)
    created = create(client).json()
    mid = created['id']
    original = fake.delete_versions
    def fail_on_meta(key):
        if key.endswith('/meta.json'):
            raise RuntimeError('simulated storage error')
        return original(key)
    monkeypatch.setattr(fake, 'delete_versions', fail_on_meta)
    assert login(client).status_code == 200
    response = client.delete(f'/api/admin/mirrors/{mid}', headers={'X-Admin-Request': '1'})
    assert response.status_code == 502
    archive = fake.get_json(f'system/deleted/{mid}.json')
    assert archive['status'] == 'pending'
    stats = client.get('/api/admin/overview').json()
    assert stats['summary']['pending_deletions'] == 1
    assert stats['summary']['active_feeds'] == 1
    assert stats['summary']['deleted_feeds'] == 0


def test_login_rate_limit(fixture_app, monkeypatch):
    _fake, _ = fixture_app
    client = with_admin(monkeypatch)
    for _ in range(10):
        assert login(client, 'wrong').status_code == 401
    assert login(client, 'wrong').status_code == 429
    main.admin_login_times.clear()
    assert login(client).status_code == 200
