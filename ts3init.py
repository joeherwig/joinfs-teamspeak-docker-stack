#!/usr/bin/env python3
"""
One-shot TS3 ServerQuery bootstrap: permissions, server settings, and channels.
Grants required permissions to the Server Admin group so tsapi works without manual
configuration, then applies TS3_CONFIG (server settings + channel list) if set.
"""
import json
import os
import socket
import sys
import time

HOST = 'teamspeak'
PORT = 10011
ADMIN_PASS = os.environ['TS3_SERVERADMIN_PASSWORD']

BOOL_PERMS = [
    'b_virtualserver_client_list',
    'b_virtualserver_channel_list',
    'b_client_move_others',
    'b_channel_create_permanent',
    'b_channel_create_semi_permanent',
    'b_channel_create_temporary',
    'b_channel_modify_make_permanent',
    'b_channel_modify_make_temporary',
]

INT_PERMS = [
    ('i_client_move_power', 100),
    ('i_channel_create_child_modify_power', 75),
    ('i_channel_modify_power', 75),
]

CHANNEL_TYPE_FLAGS = {
    'permanent': {'channel_flag_permanent': '1'},
    'semi_permanent': {'channel_flag_semi_permanent': '1'},
    'temporary': {},
}

CHAT_PERMS = ['b_client_server_textmessage_send', 'b_client_channel_textmessage_send']


def ts_escape(s):
    """Escape a value for the TS3 ServerQuery protocol."""
    return s.replace('\\', '\\\\').replace(' ', '\\s').replace('|', '\\p').replace('/', '\\/')


def recv_until_error(sock, timeout=5):
    sock.settimeout(timeout)
    buf = b''
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if any(l.startswith('error id=') for l in buf.decode(errors='ignore').splitlines()):
                break
    except socket.timeout:
        pass
    return buf.decode(errors='ignore')


def send_cmd(sock, text):
    sock.sendall((text + '\n').encode())
    return recv_until_error(sock)


def parse_ts(raw):
    items = []
    for part in raw.split('|'):
        obj = {}
        for token in part.split():
            if '=' in token:
                k, v = token.split('=', 1)
                obj[k] = v.replace('\\s', ' ').replace('\\p', '|')
        if obj:
            items.append(obj)
    return items


def open_session(retries=40, delay=5):
    """Connect, login, select virtual server — retry the full sequence on any failure."""
    escaped_pass = ts_escape(ADMIN_PASS)
    for attempt in range(1, retries + 1):
        sock = None
        try:
            print(f"Attempt {attempt}/{retries}: connecting to {HOST}:{PORT}...", flush=True)
            sock = socket.create_connection((HOST, PORT), timeout=5)
            banner = recv_until_error(sock, timeout=3)
            if not banner.strip():
                # Port is open but TS3 isn't ready yet — backing off longer to avoid
                # triggering flood protection with rapid reconnects during initialization.
                print("  No banner received (TS3 initializing), waiting 20s...", flush=True)
                sock.close()
                time.sleep(20)
                continue

            resp = send_cmd(sock, f'login serveradmin {escaped_pass}')
            if 'error id=0' not in resp:
                print(f"  Login failed: {resp.strip()}", flush=True)
                sock.close()
                time.sleep(delay)
                continue

            resp = send_cmd(sock, 'use sid=1')
            if 'error id=0' not in resp:
                print(f"  use sid=1 failed: {resp.strip()}", flush=True)
                sock.close()
                time.sleep(delay)
                continue

            print("  Connected and logged in.", flush=True)
            return sock

        except OSError as exc:
            print(f"  Connection error: {exc}", flush=True)
            if sock:
                sock.close()
            time.sleep(delay)

    sys.exit(f"ERROR: Could not establish a working TS3 session after {retries} attempts.")


def load_ts3_config():
    """Parse the TS3_CONFIG env var (JSON). Returns {} if unset/empty."""
    raw = os.environ.get('TS3_CONFIG', '').strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: TS3_CONFIG is not valid JSON: {exc}")


def apply_server_settings(sock, server_cfg):
    """Apply server-wide settings (name, password, welcome message, host banner) via one serveredit call.

    Only fields explicitly present in server_cfg are touched — anything absent is
    left as whatever it already is.
    """
    parts = []

    if 'name' in server_cfg:
        parts.append(f"virtualserver_name={ts_escape(server_cfg['name'])}")

    if 'password' in server_cfg:
        # Plaintext over ServerQuery, same convention as channel passwords below —
        # TS3 hashes it server-side. Empty string explicitly clears the password.
        parts.append(f"virtualserver_password={ts_escape(server_cfg['password'])}")

    if 'welcome_message' in server_cfg:
        msg = server_cfg['welcome_message']
        encoded = msg.encode('utf-8')
        if len(encoded) > 1024:
            encoded = encoded[:1024]
            while encoded and (encoded[-1] & 0xC0) == 0x80:
                encoded = encoded[:-1]  # don't split a multi-byte UTF-8 char
            msg = encoded.decode('utf-8', errors='ignore')
            print("  WARN virtualserver_welcomemessage truncated to 1024 bytes", flush=True)
        parts.append(f"virtualserver_welcomemessage={ts_escape(msg)}")

    hostbanner = server_cfg.get('hostbanner') or {}
    if 'url' in hostbanner:
        parts.append(f"virtualserver_hostbanner_url={ts_escape(hostbanner['url'])}")
    if 'gfx_url' in hostbanner:
        parts.append(f"virtualserver_hostbanner_gfx_url={ts_escape(hostbanner['gfx_url'])}")
    if hostbanner:
        parts.append(f"virtualserver_hostbanner_mode={int(hostbanner.get('mode', 0))}")

    if not parts:
        print("No server settings in TS3_CONFIG, skipping serveredit.", flush=True)
        return

    resp = send_cmd(sock, 'serveredit ' + ' '.join(parts))
    ok = 'error id=0' in resp
    print(f"  {'OK  ' if ok else 'WARN'} serveredit ({len(parts)} field(s))", flush=True)
    if not ok:
        print(f"    response: {resp.strip()}", flush=True)


def apply_chat_permission(sock, server_cfg, groups):
    """Toggle server-wide chat permissions on the 'Normal' server group, if explicitly configured."""
    if 'allow_client_chat' not in server_cfg:
        return

    allow = bool(server_cfg['allow_client_chat'])
    permvalue = '1' if allow else '0'

    normal_group = next(
        (g for g in groups if g.get('name') == 'Normal' and g.get('type') == '1'),
        None,
    )
    sgid = normal_group['sgid'] if normal_group else '8'
    label = normal_group['name'] if normal_group else 'sgid=8 (fallback)'
    print(f"Setting allow_client_chat={allow} on server group sgid={sgid} ({label})", flush=True)

    for perm in CHAT_PERMS:
        resp = send_cmd(sock, f'servergroupaddperm sgid={sgid} permsid={perm} permvalue={permvalue} permnegated=0 permskip=0')
        ok = 'error id=0' in resp or 'error id=2702' in resp  # 2702 = duplicate, already set
        print(f"  {'OK  ' if ok else 'WARN'} {perm}={permvalue}", flush=True)


def sync_channels(sock, channels_cfg):
    """Create or update each configured channel so TS3_CONFIG stays the source of truth."""
    if not channels_cfg:
        return

    existing = parse_ts(send_cmd(sock, 'channellist'))
    by_name = {c.get('channel_name'): c for c in existing}

    for entry in channels_cfg:
        name = entry.get('name')
        if not name:
            print("  WARN channel entry missing 'name', skipping", flush=True)
            continue

        chan_type = entry.get('type', 'permanent')
        type_flags = CHANNEL_TYPE_FLAGS.get(chan_type)
        if type_flags is None:
            print(f"  WARN unknown channel type '{chan_type}' for '{name}', defaulting to permanent", flush=True)
            type_flags = CHANNEL_TYPE_FLAGS['permanent']

        fields = [
            f"channel_topic={ts_escape(entry.get('topic', ''))}",
            f"channel_password={ts_escape(entry.get('password', ''))}",
            f"channel_flag_permanent={type_flags.get('channel_flag_permanent', '0')}",
            f"channel_flag_semi_permanent={type_flags.get('channel_flag_semi_permanent', '0')}",
        ]

        existing_channel = by_name.get(name)
        if existing_channel is None:
            parts = [f"channel_name={ts_escape(name)}"] + fields
            resp = send_cmd(sock, 'channelcreate ' + ' '.join(parts))
            ok = 'error id=0' in resp
            print(f"  {'CREATED' if ok else 'WARN   '} channel '{name}'", flush=True)
        else:
            cid = existing_channel.get('cid')
            parts = [f"cid={cid}"] + fields
            resp = send_cmd(sock, 'channeledit ' + ' '.join(parts))
            ok = 'error id=0' in resp
            print(f"  {'UPDATED' if ok else 'WARN   '} channel '{name}' (cid={cid})", flush=True)

        if not ok:
            print(f"    response: {resp.strip()}", flush=True)


def main():
    config = load_ts3_config()
    server_cfg = config.get('server') or {}
    channels_cfg = config.get('channels') or []

    sock = open_session()

    groups = parse_ts(send_cmd(sock, 'servergrouplist'))
    admin_group = next(
        (g for g in groups if g.get('name') == 'Server Admin' and g.get('type') == '1'),
        None,
    )
    sgid = admin_group['sgid'] if admin_group else '6'
    label = admin_group['name'] if admin_group else 'sgid=6 (fallback)'
    print(f"Granting permissions to server group sgid={sgid} ({label})", flush=True)

    for perm in BOOL_PERMS:
        resp = send_cmd(sock, f'servergroupaddperm sgid={sgid} permsid={perm} permvalue=1 permnegated=0 permskip=0')
        ok = 'error id=0' in resp or 'error id=2702' in resp  # 2702 = duplicate, already set
        print(f"  {'OK  ' if ok else 'WARN'} {perm}", flush=True)

    for perm, value in INT_PERMS:
        resp = send_cmd(sock, f'servergroupaddperm sgid={sgid} permsid={perm} permvalue={value} permnegated=0 permskip=0')
        ok = 'error id=0' in resp or 'error id=2702' in resp
        print(f"  {'OK  ' if ok else 'WARN'} {perm}={value}", flush=True)

    apply_server_settings(sock, server_cfg)
    apply_chat_permission(sock, server_cfg, groups)
    sync_channels(sock, channels_cfg)

    send_cmd(sock, 'quit')
    sock.close()
    print("TS3 bootstrap complete.", flush=True)


if __name__ == '__main__':
    main()
