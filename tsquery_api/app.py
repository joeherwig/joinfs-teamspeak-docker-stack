from flask import Flask, request, jsonify
import os
import telnetlib
import re
import time
import logging

_DEBUG = os.getenv('FLASK_DEBUG', '0') == '1'
_LOG_LEVEL = logging.DEBUG if _DEBUG else logging.INFO
logging.basicConfig(level=_LOG_LEVEL, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)
app.logger.setLevel(_LOG_LEVEL)

_SENSITIVE_HEADERS = {'authorization', 'cookie', 'proxy-authorization', 'x-api-key', 'x-auth-token'}


@app.before_request
def log_request():
    app.logger.info(">> %s %s", request.method, request.path)
    app.logger.debug("   headers: %s", {k: ('<redacted>' if k.lower() in _SENSITIVE_HEADERS else v)
                                        for k, v in request.headers.items()})
    if request.data:
        app.logger.debug("   body: %s", request.data.decode('utf-8', errors='replace'))

TS_HOST = os.getenv('TS3_QUERY_HOST', 'teamspeak')
TS_PORT = int(os.getenv('TS3_QUERY_PORT', '10011'))
TS_USER = os.getenv('TS3_QUERY_USER', 'serveradmin')
TS_PASS = os.getenv('TS3_QUERY_PASSWORD')


def _read_stale_timeout(default=10800):
    """Seconds an ATC station channel may stay empty before TS3 removes it (1..65535, TS3 limit)."""
    raw = os.getenv('ATC_CHANNEL_STALE_TIMEOUT', '').strip()
    if not raw:
        return default
    try:
        return max(1, min(65535, int(raw)))
    except ValueError:
        logging.warning("ATC_CHANNEL_STALE_TIMEOUT is not an integer, using default %s s", default)
        return default


ATC_STALE_TIMEOUT = _read_stale_timeout()
ATC_DESCRIPTION_MAX_BYTES = 4000
CALLSIGN_RE = re.compile(r'^[A-Za-z0-9_]{2,30}$')
FREQUENCY_RE = re.compile(r'^\d{3}\.\d{1,3}$')


def read_response(tn, timeout=5.0):
    """Read lines until a line starting with 'error id=' is received."""
    deadline = time.time() + timeout
    lines = []
    while time.time() < deadline:
        line = tn.read_until(b"\n", timeout=0.5)
        if not line:
            continue
        try:
            s = line.decode('utf-8', errors='ignore').strip()
        except Exception:
            s = line.decode(errors='ignore').strip()
        lines.append(s)
        if s.startswith('error id='):
            break
    return "\n".join(lines)


def ts_escape(s: str) -> str:
    return (s.replace('\\', '\\\\').replace(' ', '\\s').replace('|', '\\p').replace('/', '\\/')
             .replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t').replace('\v', '\\v'))


_UNESCAPES = {'s': ' ', 'p': '|', '/': '/', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '\\': '\\'}


def ts_unescape(s: str) -> str:
    """Inverse of ts_escape (single pass, so an escaped backslash followed by 's' stays intact)."""
    return re.sub(r'\\(.)', lambda m: _UNESCAPES.get(m.group(1), m.group(0)), s)


def open_session():
    """Open a TS3 ServerQuery session and return the authenticated Telnet connection."""
    tn = telnetlib.Telnet(TS_HOST, TS_PORT, timeout=5)
    read_response(tn, timeout=1)  # banner
    tn.write(f"login {TS_USER} {TS_PASS}\n".encode())
    read_response(tn, timeout=1)
    tn.write(b"use sid=1\n")
    read_response(tn, timeout=1)
    return tn


def close_session(tn):
    try:
        tn.write(b"quit\n")
    finally:
        tn.close()


def send_cmd_on(tn, cmd: str, timeout=2):
    """Send one command on an already-open session and return the response."""
    tn.write((cmd + "\n").encode())
    return read_response(tn, timeout=timeout)


def send_cmd(cmd: str):
    """Open a session, run one command, close. Used by single-operation API routes."""
    tn = open_session()
    try:
        return send_cmd_on(tn, cmd)
    finally:
        close_session(tn)


def parse_ts_response(raw: str):
    # split raw TS3 response into objects separated by |
    # and parse fields separated by spaces into key/value pairs.
    out = []
    for line in raw.splitlines():
        if not line or line.startswith('error id='):
            continue
        for item in line.split('|'):
            obj = {}
            for token in item.split():
                if '=' not in token:
                    continue
                k, v = token.split('=', 1)
                obj[k] = ts_unescape(v)
            if obj:
                out.append(obj)
    return out


def find_client_by_callsign(callsign: str):
    raw = send_cmd('clientlist')
    clients = parse_ts_response(raw)
    prefix = f"{callsign}_"
    matched = [c for c in clients
               if c.get('client_nickname', '') == callsign
               or c.get('client_nickname', '').startswith(prefix)]
    return matched[0] if matched else None


def decode_client_entry(client: dict):
    decoded = {
        'clid': int(client.get('clid')) if client.get('clid') and client.get('clid').isdigit() else client.get('clid'),
        'cid': int(client.get('cid')) if client.get('cid') and client.get('cid').isdigit() else client.get('cid'),
        'client_database_id': int(client.get('client_database_id')) if client.get('client_database_id') and client.get('client_database_id').isdigit() else client.get('client_database_id'),
        'client_nickname': client.get('client_nickname'),
        'client_type': int(client.get('client_type')) if client.get('client_type') and client.get('client_type').isdigit() else client.get('client_type'),
    }
    return decoded


@app.route('/channel', methods=['PUT'])
def create_channel():
    data = request.json or {}
    name = data.get('name')
    if not name:
        return jsonify({'error': 'name is required'}), 400
    cpid = data.get('cpid')
    permanent = data.get('permanent', True)
    password = data.get('password')
    parts = [f"channel_name={ts_escape(name)}"]
    if cpid is not None:
        parts.append(f"cpid={int(cpid)}")
    if permanent:
        parts.append('channel_flag_permanent=1')
    if password:
        parts.append(f"channel_password={ts_escape(password)}")
    cmd = 'channelcreate ' + ' '.join(parts)
    raw = send_cmd(cmd)
    parsed = parse_ts_response(raw)
    return jsonify({'raw': raw, 'parsed': parsed})


@app.route('/users', methods=['GET'])
def list_users():
    raw = send_cmd('clientlist')
    parsed = parse_ts_response(raw)
    clients = [decode_client_entry(client) for client in parsed]
    return jsonify({'raw': raw, 'clients': clients})


@app.route('/channels', methods=['GET'])
def list_channels():
    raw = send_cmd('channellist')
    parsed = parse_ts_response(raw)
    return jsonify({'raw': raw, 'channels': parsed})


@app.route('/move', methods=['PUT'])
def move_user():
    data = request.json or {}
    clid = data.get('clid')
    cid = data.get('cid')
    if clid is None or cid is None:
        return jsonify({'error': 'clid and cid are required'}), 400
    cmd = f'clientmove clid={int(clid)} cid={int(cid)}'
    raw = send_cmd(cmd)
    parsed = parse_ts_response(raw)
    return jsonify({'raw': raw, 'parsed': parsed})


def process_single_com_update(callsign: str, frequency: str):
    """Move a single client to the channel matching frequency using one TS3 session."""
    tn = open_session()
    try:
        # 1. Find client first — bail out before touching channels if user isn't on TS3
        clients = parse_ts_response(send_cmd_on(tn, 'clientlist'))
        prefix = f"{callsign}_"
        client = next(
            (c for c in clients
             if c.get('client_nickname', '') == callsign
             or c.get('client_nickname', '').startswith(prefix)),
            None,
        )
        if client is None:
            return {'callsign': callsign, 'frequency': frequency, 'error': f'client not found for callsign {callsign}'}

        # 2. Find channel by topic
        channels = parse_ts_response(send_cmd_on(tn, 'channellist -topic'))
        normalized = frequency.strip().lower()
        channel = next((c for c in channels if c.get('channel_topic', '').lower() == normalized), None)

        # 3. Create channel only now that we know the client exists
        created = False
        if channel is None:
            parts = [
                f"channel_name={ts_escape(frequency)}",
                f"channel_topic={ts_escape(frequency)}",
                'channel_flag_permanent=1',
            ]
            parsed_create = parse_ts_response(send_cmd_on(tn, 'channelcreate ' + ' '.join(parts)))
            channel = parsed_create[0] if parsed_create else None
            created = True
            if channel is None or 'cid' not in channel:
                return {'callsign': callsign, 'frequency': frequency, 'error': 'failed to create channel'}

        logging.info("usertochannel: callsign=%s frequency=%s cid=%s created=%s", callsign, frequency, channel.get('cid'), created)

        # 4. Move client
        raw_move = send_cmd_on(tn, f"clientmove clid={int(client['clid'])} cid={int(channel['cid'])}")
        parsed_move = parse_ts_response(raw_move)

        # 5. Mark newly created channel as temporary so TS3 removes it when empty
        temp_set_result = None
        if created and 'error id=0' in raw_move:
            raw_temp = send_cmd_on(tn, f'channeledit cid={int(channel["cid"])} channel_flag_permanent=0')
            temp_set_result = parse_ts_response(raw_temp)

        return {
            'callsign': callsign,
            'frequency': frequency,
            'channel': channel,
            'channel_created': created,
            'client': client,
            'move': {'raw': raw_move, 'parsed': parsed_move},
            'set_temporary': temp_set_result,
        }
    finally:
        close_session(tn)


@app.route('/usertochannel', methods=['PUT', 'POST'])
def user_to_channel():
    data = request.json or {}
    updates = data.get('comsupdate')
    if not updates or not isinstance(updates, list):
        return jsonify({'error': 'comsupdate array is required'}), 400

    results = []
    for entry in updates:
        callsign = entry.get('callsign')
        com1 = entry.get('com1')
        if not callsign or not com1:
            results.append({'callsign': callsign, 'error': 'callsign and com1 are required'})
            continue
        results.append(process_single_com_update(callsign, com1))

    return jsonify({'results': results})


def _ts_error(raw: str):
    """Return (id, message) of a failing ServerQuery response, or None on success."""
    line = next((l for l in raw.splitlines() if l.startswith('error id=')), None)
    if line is None:
        return ('?', 'no response from TeamSpeak')
    err = {}
    for token in line.split():
        if '=' in token:
            k, v = token.split('=', 1)
            err[k] = ts_unescape(v)
    if err.get('id') == '0':
        return None
    return (err.get('id', '?'), err.get('msg', ''))


def _truncate_utf8(text: str, max_bytes: int):
    data = text.encode('utf-8')
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode('utf-8', errors='ignore'), True


def _is_temporary(channel: dict) -> bool:
    return channel.get('channel_flag_permanent') != '1' and channel.get('channel_flag_semi_permanent') != '1'


def process_station_update(entry):
    """Create/update/remove the temporary channel for one ATC station report."""
    if not isinstance(entry, dict):
        return {'error': 'entry must be an object'}
    callsign = entry.get('callsign')
    com1 = entry.get('com1')
    online = entry.get('online')
    text = entry.get('text')
    if not isinstance(callsign, str) or not CALLSIGN_RE.match(callsign):
        return {'callsign': callsign if isinstance(callsign, str) else None,
                'error': 'callsign is required (2-30 chars: letters, digits, underscore)'}
    if not isinstance(online, bool):
        return {'callsign': callsign, 'error': 'online (boolean) is required'}
    if online and (not isinstance(com1, str) or not FREQUENCY_RE.match(com1)):
        return {'callsign': callsign, 'error': 'com1 is required in the format 118.800'}
    if text is not None and not isinstance(text, str):
        return {'callsign': callsign, 'error': 'text must be a string'}

    truncated = False
    if text is not None:
        text, truncated = _truncate_utf8(text, ATC_DESCRIPTION_MAX_BYTES)
        if truncated:
            logging.warning("atcstation: description for %s truncated to %d bytes", callsign, ATC_DESCRIPTION_MAX_BYTES)

    def fail(raw, what):
        err = _ts_error(raw)
        logging.warning("atcstation: %s failed for %s: id=%s msg=%s", what, callsign, err[0], err[1])
        return {'callsign': callsign, 'error': f'{what} failed (ts3 error {err[0]}: {err[1]})'}

    tn = open_session()
    try:
        channels = parse_ts_response(send_cmd_on(tn, 'channellist -topic -flags'))
        channel = next((c for c in channels if c.get('channel_name') == callsign), None)
        result = {'callsign': callsign}

        if not online:
            if channel is None:
                return {**result, 'action': 'noop'}
            cid = int(channel['cid'])
            if not _is_temporary(channel):
                logging.info("atcstation: %s is not temporary, leaving it untouched", callsign)
                return {**result, 'action': 'noop', 'cid': cid}
            # TS3 refuses to delete an occupied channel (force=0): then expire it as soon as it is empty.
            if _ts_error(send_cmd_on(tn, f'channeldelete cid={cid} force=0')) is None:
                logging.info("atcstation: %s deleted (cid=%s)", callsign, cid)
                return {**result, 'action': 'deleted', 'cid': cid}
            raw = send_cmd_on(tn, f'channeledit cid={cid} channel_delete_delay=0')
            err = _ts_error(raw)
            if err is not None and err[0] != '1281':
                return fail(raw, 'channeledit')
            logging.info("atcstation: %s occupied, delete delay set to 0 (cid=%s)", callsign, cid)
            return {**result, 'action': 'delete_pending', 'cid': cid, 'delete_delay': 0}

        if channel is None:
            # A pilot-created frequency channel (temporary, same topic) is renamed instead of duplicated.
            adopt = next((c for c in channels
                          if c.get('channel_topic', '').strip() == com1 and _is_temporary(c)), None)
            if adopt is not None:
                cid = int(adopt['cid'])
                parts = [f'cid={cid}', f'channel_name={ts_escape(callsign)}',
                         f'channel_delete_delay={ATC_STALE_TIMEOUT}']
                if text is not None:
                    parts.append(f'channel_description={ts_escape(text)}')
                raw = send_cmd_on(tn, 'channeledit ' + ' '.join(parts))
                if _ts_error(raw) is not None:
                    return fail(raw, 'channeledit')
                logging.info("atcstation: renamed pilot channel cid=%s to %s", cid, callsign)
                return {**result, 'action': 'updated', 'cid': cid, 'topic': com1,
                        'delete_delay': ATC_STALE_TIMEOUT, 'description_updated': text is not None,
                        'description_truncated': truncated}
            parts = [f'channel_name={ts_escape(callsign)}', f'channel_topic={ts_escape(com1)}',
                     'channel_flag_temporary=1', f'channel_delete_delay={ATC_STALE_TIMEOUT}']
            if text:
                parts.append(f'channel_description={ts_escape(text)}')
            raw = send_cmd_on(tn, 'channelcreate ' + ' '.join(parts))
            if _ts_error(raw) is not None:
                return fail(raw, 'channelcreate')
            created = next((c for c in parse_ts_response(raw) if 'cid' in c), None)
            logging.info("atcstation: %s created cid=%s", callsign, created and created['cid'])
            return {**result, 'action': 'created', 'cid': int(created['cid']) if created else None,
                    'topic': com1, 'delete_delay': ATC_STALE_TIMEOUT,
                    'description_updated': bool(text), 'description_truncated': truncated}

        cid = int(channel['cid'])
        temporary = _is_temporary(channel)
        edits = []
        if channel.get('channel_topic', '') != com1:
            edits.append(f'channel_topic={ts_escape(com1)}')
        if temporary:
            edits.append(f'channel_delete_delay={ATC_STALE_TIMEOUT}')
        description_updated = False
        if text is not None:
            info = next(iter(parse_ts_response(send_cmd_on(tn, f'channelinfo cid={cid}'))), {})
            if info.get('channel_description', '') != text:
                edits.append(f'channel_description={ts_escape(text)}')
                description_updated = True
        action = 'unchanged'
        if edits:
            raw = send_cmd_on(tn, f'channeledit cid={cid} ' + ' '.join(edits))
            err = _ts_error(raw)
            if err is not None and err[0] != '1281':  # 1281 = nothing changed (e.g. delay already equal)
                return fail(raw, 'channeledit')
            if err is None:
                action = 'updated'
        return {**result, 'action': action, 'cid': cid, 'topic': com1,
                'delete_delay': ATC_STALE_TIMEOUT if temporary else None,
                'description_updated': description_updated, 'description_truncated': truncated}
    finally:
        close_session(tn)


@app.route('/atcstation', methods=['PUT', 'POST'])
def atc_station():
    data = request.get_json(silent=True)
    updates = data.get('comsupdate') if isinstance(data, dict) else None
    if not updates or not isinstance(updates, list):
        return jsonify({'error': 'comsupdate array is required'}), 400
    results = []
    for entry in updates:
        try:
            results.append(process_station_update(entry))
        except Exception as exc:  # one broken entry must not abort the others
            logging.exception("atcstation: unexpected error")
            results.append({'callsign': entry.get('callsign') if isinstance(entry, dict) else None,
                            'error': f'internal error: {type(exc).__name__}'})
    return jsonify({'results': results})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081)