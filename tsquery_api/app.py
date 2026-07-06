from flask import Flask, request, jsonify
import os
import telnetlib
import time
import logging

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s')

app = Flask(__name__)
app.logger.setLevel(logging.DEBUG)


@app.before_request
def log_request():
    app.logger.info(">> %s %s", request.method, request.path)
    app.logger.debug("   headers: %s", dict(request.headers))
    if request.data:
        app.logger.debug("   body: %s", request.data.decode('utf-8', errors='replace'))

TS_HOST = os.getenv('TS3_QUERY_HOST', 'teamspeak')
TS_PORT = int(os.getenv('TS3_QUERY_PORT', '10011'))
TS_USER = os.getenv('TS3_QUERY_USER', 'serveradmin')
TS_PASS = os.getenv('TS3_QUERY_PASSWORD')


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
    return s.replace('\\', '\\\\').replace(' ', '\\s').replace('|', '\\p').replace('/', '\\/')


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
                v = v.replace('\\s', ' ').replace('\\p', '|').replace('\\/', '/')
                obj[k] = v
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081)