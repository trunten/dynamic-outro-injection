import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from app import main

FEED = b'''<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
<title>Sample Podcast</title><description>A test show</description>
<item><guid>one</guid><title>First episode</title><enclosure type="audio/mpeg"
url="https://cdn.example.org/episode.mp3" length="2500"/></item>
</channel></rss>'''


class FakeB2:
    def __init__(self):
        self.data = {}
        self.uploads = []

    def put_bytes(self, key, data, content_type):
        self.data[key] = data

    def put_json(self, key, data):
        self.put_bytes(key, json.dumps(data).encode(), "application/json")

    def get_bytes(self, key, max_bytes=4194304):
        from app.storage import MissingObject
        if key not in self.data:
            raise MissingObject(key)
        assert len(self.data[key]) <= max_bytes
        return self.data[key]

    def get_json(self, key):
        return json.loads(self.get_bytes(key))

    def list_current(self, prefix):
        return [{"Key": k, "Size": len(v), "LastModified": datetime.now(timezone.utc)}
                for k, v in self.data.items() if k.startswith(prefix)]

    def upload_file(self, key, path, content_type="audio/mpeg"):
        self.uploads.append(key)
        self.data[key] = Path(path).read_bytes()

    def download_file(self, key, path):
        Path(path).write_bytes(self.get_bytes(key))

    def head(self, key):
        return {"ContentLength": len(self.data[key])} if key in self.data else None

    def expire_cache(self, prefix, older_than_days, budget, reserve=0):
        assert reserve < budget

    def stream(self, key, byte_range=None):
        raw = self.get_bytes(key)
        if byte_range:
            start, end = map(int, byte_range.removeprefix("bytes=").split("-"))
            raw = raw[start:end+1]
        yield raw

    def delete_versions(self, key):
        self.data.pop(key, None)


@pytest.fixture
def fixture_app(monkeypatch):
    fake = FakeB2()
    monkeypatch.setattr(main, "settings", replace(main.settings, bucket="test",
                        endpoint="https://s3.eu-central-003.backblazeb2.com",
                        key_id="test", secret="test", max_mirrors=20,
                        public_base_url="https://mirror.example.org"))
    monkeypatch.setattr(main, "storage_instance", fake)
    main.recent_feed_checks.clear()
    main.creation_times.clear()
    main.generations_today.clear()

    async def feed(url, *args):
        assert url == "https://feed.example.org/rss.xml"
        return FEED

    async def download(url, path, max_bytes):
        Path(path).write_bytes(b"A" * 3000)
        return 3000

    async def append(original, outro, output):
        Path(output).write_bytes(b"A" * 3000 + b"Z" * 1000)
        return "test"

    def probe(path):
        return {"codec_name": "mp3", "duration": 10}

    monkeypatch.setattr(main, "fetch_feed", feed)
    monkeypatch.setattr(main, "download_episode", download)
    monkeypatch.setattr(main, "append_outro", append)
    monkeypatch.setattr(main, "probe_audio", probe)
    return fake, TestClient(main.app)


def create(client):
    return client.post("/api/mirrors", data={
        "feed_url": "https://feed.example.org/rss.xml", "permission": "on",
    }, files={"outro": ("theme.mp3", b"x" * 2000, "audio/mpeg")})


def test_create_and_lazy_audio_download(fixture_app):
    fake, client = fixture_app
    response = create(client)
    assert response.status_code == 201, response.text
    result = response.json()
    mid = result["id"]
    assert result["title"] == "Sample Podcast"
    assert result["episodes"] == 1
    assert all(not k.startswith("cache/") for k in fake.data)
    feed = client.get(f"/feed/{mid}.xml")
    assert feed.status_code == 200 and b"Sample Podcast" in feed.content
    eid = next(iter(fake.get_json(f"mirrors/{mid}/episodes.json")["episodes"]))
    audio = client.get(f"/episode/{mid}/{eid}.mp3", headers={"range": "bytes=3000-3999"})
    assert audio.status_code == 206
    assert len(audio.content) == 1000 and audio.content == b"Z" * 1000
    assert audio.headers["content-range"] == "bytes 3000-3999/4000"
    assert audio.headers["content-length"] == "1000"
    assert f"cache/{mid}/{eid}.mp3" in fake.data
    head = client.head(f"/episode/{mid}/{eid}.mp3")
    assert head.status_code == 200 and head.headers["content-length"] == "4000"
    assert head.content == b""
    assert client.get(f"/api/mirrors/{mid}").json()["processed"] == 1
    assert f'length="4000"'.encode() in fake.get_bytes(f"mirrors/{mid}/feed.xml")


def test_delete_requires_management_token(fixture_app):
    fake, client = fixture_app
    result = create(client).json()
    mid = result["id"]
    assert client.delete(f"/api/mirrors/{mid}", headers={"x-manage-token": "bad"}).status_code == 403
    assert client.delete(f"/api/mirrors/{mid}", headers={"x-manage-token": result["manage_token"]}).status_code == 200
    assert not fake.data
    assert client.get(f"/feed/{mid}.xml").status_code == 404


def test_permission_and_url_checks(fixture_app):
    fake, client = fixture_app
    assert client.post("/api/mirrors", data={"feed_url":"https://feed.example.org/rss.xml"},
        files={"outro":("theme.mp3", b"x" * 2000, "audio/mpeg")}).status_code == 422
    result = client.post("/api/mirrors", data={"feed_url":"http://localhost/internal", "permission":"on"},
        files={"outro":("theme.mp3", b"x" * 2000, "audio/mpeg")})
    assert result.status_code == 422


def test_monthly_egress_budget_persists_in_bucket(fixture_app, monkeypatch):
    fake, client = fixture_app
    monkeypatch.setattr(main, "settings", replace(main.settings, max_monthly_egress_bytes=5000))
    result = create(client).json()
    mid = result["id"]
    eid = next(iter(fake.get_json(f"mirrors/{mid}/episodes.json")["episodes"]))
    url = f"/episode/{mid}/{eid}.mp3"
    assert client.get(url).status_code == 200  # 4,000 bytes reserved.
    assert client.head(url).status_code == 200  # HEAD does not consume egress.
    assert client.get(url).status_code == 429  # Would exceed 5,000-byte cap.
    records = [k for k in fake.data if k.startswith("system/egress-")]
    assert len(records) == 1
    assert fake.get_json(records[0])["bytes"] == 4000


def test_website_and_health(fixture_app):
    _fake, client = fixture_app
    page = client.get("/")
    assert page.status_code == 200
    assert "Create your personalised feed" in page.text
    assert client.get("/health").json()["storage_configured"] is True


def test_default_outro_visibility_and_lazy_generation(fixture_app):
    fake, client = fixture_app
    assert client.get('/api/default-outro').json() == {
        'label': 'Default (ATP End Theme)', 'available': False,
    }
    # This is an admin-provisioned object, not an upload from the public form.
    fake.data['atp.mp3'] = b'Z' * 2048
    assert client.get('/api/default-outro').json()['available'] is True

    created = client.post('/api/mirrors', data={
        'feed_url': 'https://feed.example.org/rss.xml',
        'outro_choice': 'default', 'permission': 'on',
    })
    assert created.status_code == 201, created.text
    mid = created.json()['id']
    meta = fake.get_json(f'mirrors/{mid}/meta.json')
    assert meta['outro_key'] == 'atp.mp3'
    assert meta['outro_choice'] == 'default'
    assert f'mirrors/{mid}/outro.mp3' not in fake.data
    assert f'mirrors/{mid}/outro.mp3' not in fake.uploads
    assert not any(k.startswith('cache/') for k in fake.data)
    eid = next(iter(fake.get_json(f'mirrors/{mid}/episodes.json')['episodes']))
    audio = client.get(f'/episode/{mid}/{eid}.mp3')
    assert audio.status_code == 200
    assert len(audio.content) == 4000
    assert client.delete(f'/api/mirrors/{mid}', headers={
        'x-manage-token': created.json()['manage_token'],
    }).status_code == 200
    assert 'atp.mp3' in fake.data  # Never delete the shared admin-owned audio.


def test_default_outro_must_be_provisioned(fixture_app):
    fake, client = fixture_app
    response = client.post('/api/mirrors', data={
        'feed_url': 'https://feed.example.org/rss.xml',
        'outro_choice': 'default', 'permission': 'on',
    })
    assert response.status_code == 503
    assert 'not configured' in response.json()['detail']
    assert not any(k.startswith('mirrors/') for k in fake.data)


def test_reject_invalid_outro_choice(fixture_app):
    fake, client = fixture_app
    response = client.post('/api/mirrors', data={
        'feed_url': 'https://feed.example.org/rss.xml',
        'outro_choice': 'raw', 'permission': 'on',
    })
    assert response.status_code == 422
    assert not fake.data


def test_custom_outro_stored_independently(fixture_app):
    fake, client = fixture_app
    response = client.post('/api/mirrors', data={
        'feed_url': 'https://feed.example.org/rss.xml',
        'outro_choice': 'custom', 'permission': 'on',
    }, files={'outro': ('custom.mp3', b'X' * 2048, 'audio/mpeg')})
    assert response.status_code == 201, response.text
    mid = response.json()['id']
    assert fake.get_json(f'mirrors/{mid}/meta.json')['outro_key'] == f'mirrors/{mid}/outro.mp3'
    assert f'mirrors/{mid}/outro.mp3' in fake.data
