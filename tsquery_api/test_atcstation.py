"""Dev-only tests for PUT /atcstation. Run: cd tsquery_api && pip install -r requirements.txt pytest && pytest"""
import sys
import types

import pytest

try:
    import telnetlib  # noqa: F401  (removed in Python 3.13; the container uses 3.11)
except ImportError:
    sys.modules["telnetlib"] = types.ModuleType("telnetlib")

import app as tsapp  # noqa: E402

OK = 'error id=0 msg=ok'


class FakeTS:
    """Replays canned ServerQuery responses and records the commands sent."""

    def __init__(self, channels='', info='', replies=None):
        self.cmds = []
        self.channels = channels
        self.info = info
        self.replies = replies or {}

    def send(self, tn, cmd, timeout=2):
        self.cmds.append(cmd)
        if cmd.startswith('channellist'):
            return self.channels + '\n' + OK
        if cmd.startswith('channelinfo'):
            return self.info + '\n' + OK
        for prefix, reply in self.replies.items():
            if cmd.startswith(prefix):
                return reply
        if cmd.startswith('channelcreate'):
            return 'cid=42\n' + OK
        return OK


@pytest.fixture
def client(monkeypatch):
    def install(fake):
        monkeypatch.setattr(tsapp, 'open_session', lambda: object())
        monkeypatch.setattr(tsapp, 'close_session', lambda tn: None)
        monkeypatch.setattr(tsapp, 'send_cmd_on', fake.send)
        return tsapp.app.test_client()
    return install


def put(c, **entry):
    r = c.put('/atcstation', json={'comsupdate': [entry]})
    return r.status_code, r.get_json()


TEMP = 'cid=7 channel_name=EDDS_TWR channel_topic=118.800 channel_flag_permanent=0 channel_flag_semi_permanent=0'
PERM = 'cid=8 channel_name=EDDS_TWR channel_topic=118.800 channel_flag_permanent=1 channel_flag_semi_permanent=0'


def test_create(client):
    fake = FakeTS()
    _, body = put(client(fake), callsign='EDDS_TWR', com1='118.800', online=True, text='a b\nc')
    r = body['results'][0]
    assert r['action'] == 'created' and r['cid'] == 42
    cmd = next(c for c in fake.cmds if c.startswith('channelcreate'))
    assert 'channel_name=EDDS_TWR' in cmd and 'channel_topic=118.800' in cmd
    assert 'channel_flag_temporary=1' in cmd
    assert f'channel_delete_delay={tsapp.ATC_STALE_TIMEOUT}' in cmd
    assert 'channel_description=a\\sb\\nc' in cmd


def test_default_timeout_is_three_hours():
    assert tsapp._read_stale_timeout() in (10800, tsapp.ATC_STALE_TIMEOUT)
    assert tsapp.ATC_STALE_TIMEOUT == 10800


def test_unchanged_refreshes_nothing_when_equal(client):
    fake = FakeTS(channels=TEMP, replies={'channeledit': 'error id=1281 msg=database\\sempty\\sresult\\sset'})
    _, body = put(client(fake), callsign='EDDS_TWR', com1='118.800', online=True)
    assert body['results'][0]['action'] == 'unchanged'


def test_update_topic(client):
    fake = FakeTS(channels=TEMP)
    _, body = put(client(fake), callsign='EDDS_TWR', com1='118.805', online=True)
    assert body['results'][0]['action'] == 'updated'
    assert any('channel_topic=118.805' in c for c in fake.cmds)


def test_description_only_sent_when_different(client):
    fake = FakeTS(channels=TEMP, info='channel_description=same')
    put(client(fake), callsign='EDDS_TWR', com1='118.800', online=True, text='same')
    assert not any('channel_description' in c for c in fake.cmds if c.startswith('channeledit'))


def test_adopts_pilot_channel(client):
    fake = FakeTS(channels='cid=9 channel_name=118.800 channel_topic=118.800 channel_flag_permanent=0')
    _, body = put(client(fake), callsign='EDDS_TWR', com1='118.800', online=True)
    assert body['results'][0]['cid'] == 9
    assert not any(c.startswith('channelcreate') for c in fake.cmds)


def test_offline_empty_deletes(client):
    fake = FakeTS(channels=TEMP)
    _, body = put(client(fake), callsign='EDDS_TWR', online=False)
    assert body['results'][0]['action'] == 'deleted'
    assert 'channeldelete cid=7 force=0' in fake.cmds


def test_offline_occupied_sets_delay_zero(client):
    fake = FakeTS(channels=TEMP, replies={'channeldelete': 'error id=772 msg=channel\\snot\\sempty'})
    _, body = put(client(fake), callsign='EDDS_TWR', online=False)
    assert body['results'][0]['action'] == 'delete_pending'
    assert 'channeledit cid=7 channel_delete_delay=0' in fake.cmds


def test_offline_unknown_noop(client):
    fake = FakeTS()
    _, body = put(client(fake), callsign='EDDS_TWR', online=False)
    assert body['results'][0]['action'] == 'noop'
    assert not any(c.startswith(('channelcreate', 'channeldelete')) for c in fake.cmds)


def test_offline_permanent_untouched(client):
    fake = FakeTS(channels=PERM)
    _, body = put(client(fake), callsign='EDDS_TWR', online=False)
    assert body['results'][0]['action'] == 'noop'
    assert not any(c.startswith(('channeldelete', 'channeledit')) for c in fake.cmds)


@pytest.mark.parametrize('entry', [
    {'callsign': 'bad name', 'com1': '118.800', 'online': True},
    {'callsign': 'EDDS_TWR', 'com1': 'x', 'online': True},
    {'callsign': 'EDDS_TWR', 'com1': '118.800'},
    {'callsign': 'EDDS_TWR', 'com1': '118.800', 'online': True, 'text': 5},
])
def test_validation(client, entry):
    fake = FakeTS()
    _, body = put(client(fake), **entry)
    assert 'error' in body['results'][0] and fake.cmds == []


def test_missing_comsupdate(client):
    assert client(FakeTS()).put('/atcstation', json={}).status_code == 400


def test_escape_roundtrip():
    s = 'a b|c/d\\e\nf\tg'
    assert tsapp.ts_unescape(tsapp.ts_escape(s)) == s
