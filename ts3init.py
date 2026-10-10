#!/usr/bin/env python3
"""
One-shot TS3 ServerQuery setup (idempotent, safe to run on every `docker compose up`).

  python3 ts3init.py          grant tsapi permissions, seed channels, set welcome message
  python3 ts3init.py --admins list the members of the Server Admin group and exit
  python3 ts3init.py --keys   print all unredeemed privilege keys (incl. first-start key) and exit
  python3 ts3init.py --token  print a fresh one-time Server Admin privilege key and exit
"""
import os
import socket
import sys
import time

HOST = 'teamspeak'
PORT = 10011
ADMIN_PASS = os.environ['TS3_SERVERADMIN_PASSWORD']
VORSTAND_PASS = os.environ.get('TS3_VORSTAND_PASSWORD', '')
WELCOME_MESSAGE = os.environ.get('TS3_WELCOME_MESSAGE', '').strip() or 'Welcome to the JoinFS TeamSpeak server.'
SERVER_NAME = os.environ.get('TS3_SERVER_NAME', '').strip() or 'fsc-ev.de + JoinFS \U0001f6eb'
TS_DEFAULT_WELCOME ='Welcome to TeamSpeak, check www.teamspeak.com for latest information'

BOOL_PERMS = [
    'b_virtualserver_client_list',
    'b_virtualserver_channel_list',
    'b_client_move_others',
    'b_channel_create_permanent',
    'b_channel_create_semi_permanent',
    'b_channel_create_temporary',
    'b_channel_modify_make_permanent',
    'b_channel_modify_make_temporary',
    'b_channel_join_ignore_password',
]

INT_PERMS = [
    ('i_client_move_power', 100),
    ('i_channel_create_child_modify_power', 75),
    ('i_channel_modify_power', 75),
]

# Static channel tree, in display order. Created once; existing channels are never modified
# (except TeamSpeak's initial default channel, which becomes UNICOM).
UNICOM = {
    'channel_name': 'UNICOM',
    'channel_topic': '122.800',
    'channel_description': 'This is the default channel',
    'channel_codec': '4',  # Opus Voice
    'channel_flag_default': '1',
    'channel_flag_permanent': '1',
}
CHANNEL_NAMES = [
    'FSC_Online', 'PMDG', 'Mediumgruppe', 'Rotwein Gruppe', 'Widebody', 'Erdsichtflieger',
    'Low&Slow', 'Albatrosse', 'Segelflieger', 'Nordost', 'Troubleshooting', 'Helikopter',
    'Vorstand', 'Englischkurs',
]
PASSWORD_PROTECTED = {'Vorstand'}


def ts_escape(s):
    """Escape a value for the TS3 ServerQuery protocol."""
    return (s.replace('\\', '\\\\').replace(' ', '\\s').replace('|', '\\p').replace('/', '\\/')
             .replace('\n', '\\n'))


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
                obj[k] = v.replace('\\s', ' ').replace('\\p', '|').replace('\\/', '/')
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


def props(opts):
    return ' '.join(f'{k}={ts_escape(v)}' for k, v in opts.items())


def ok(resp):
    return 'error id=0' in resp


def error_line(resp):
    return next((l.strip() for l in resp.splitlines() if l.startswith('error id=')), resp.strip())


def find_admin_sgid(sock):
    groups = parse_ts(send_cmd(sock, 'servergrouplist'))
    admin_group = next(
        (g for g in groups if g.get('name') == 'Server Admin' and g.get('type') == '1'),
        None,
    )
    sgid = admin_group['sgid'] if admin_group else '6'
    label = admin_group['name'] if admin_group else 'sgid=6 (fallback)'
    return sgid, label


def grant_permissions(sock, sgid):
    for perm in BOOL_PERMS:
        resp = send_cmd(sock, f'servergroupaddperm sgid={sgid} permsid={perm} permvalue=1 permnegated=0 permskip=0')
        good = ok(resp) or 'error id=2702' in resp  # 2702 = duplicate, already set
        print(f"  {'OK  ' if good else 'WARN'} {perm}{'' if good else '  <- ' + error_line(resp)}", flush=True)

    for perm, value in INT_PERMS:
        resp = send_cmd(sock, f'servergroupaddperm sgid={sgid} permsid={perm} permvalue={value} permnegated=0 permskip=0')
        good = ok(resp) or 'error id=2702' in resp
        print(f"  {'OK  ' if good else 'WARN'} {perm}={value}{'' if good else '  <- ' + error_line(resp)}", flush=True)


def seed_channels(sock):
    """Create the static channel tree. Matches by name, so reruns create nothing."""
    existing = {
        c['channel_name']: c
        for c in parse_ts(send_cmd(sock, 'channellist -topic -flags'))
        if 'channel_name' in c
    }
    created = 0
    prev_cid = '0'

    # UNICOM: reuse TeamSpeak's initial default channel instead of making a second default.
    unicom = existing.get('UNICOM')
    if unicom is None:
        default = next((c for c in existing.values() if c.get('channel_flag_default') == '1'), None)
        if default is not None:
            resp = send_cmd(sock, f"channeledit cid={default['cid']} {props(UNICOM)}")
            print(f"  {'OK  ' if ok(resp) else 'WARN'} default channel '{default['channel_name']}' -> UNICOM", flush=True)
            unicom = default
        else:
            resp = send_cmd(sock, f'channelcreate {props(UNICOM)}')
            unicom = next((c for c in parse_ts(resp) if 'cid' in c), None)
            print(f"  {'OK  ' if ok(resp) and unicom else 'WARN'} created UNICOM", flush=True)
        created += 1
    else:
        print("  --   UNICOM exists", flush=True)
    if unicom and 'cid' in unicom:
        prev_cid = unicom['cid']

    for name in CHANNEL_NAMES:
        channel = existing.get(name)
        if channel is not None:
            print(f"  --   {name} exists", flush=True)
            if name in PASSWORD_PROTECTED and VORSTAND_PASS and channel.get('channel_flag_password') != '1':
                resp = send_cmd(sock, f"channeledit cid={channel['cid']} channel_password={ts_escape(VORSTAND_PASS)}")
                print(f"  {'OK  ' if ok(resp) else 'WARN'} password set on existing {name}", flush=True)
            prev_cid = channel['cid']
            continue

        opts = {'channel_name': name, 'channel_flag_permanent': '1', 'channel_order': prev_cid}
        if name in PASSWORD_PROTECTED:
            if not VORSTAND_PASS:
                print(f"  WARN {name} skipped: TS3_VORSTAND_PASSWORD is empty", flush=True)
                continue
            opts['channel_password'] = VORSTAND_PASS
        resp = send_cmd(sock, f'channelcreate {props(opts)}')
        made = next((c for c in parse_ts(resp) if 'cid' in c), None)
        print(f"  {'OK  ' if ok(resp) and made else 'WARN'} created {name}", flush=True)
        if made:
            prev_cid = made['cid']
            created += 1
    print(f"Channel seeding done ({created} created/converted).", flush=True)


def set_welcome_message(sock):
    """Set a neutral welcome message, but only while it is still empty or TeamSpeak's default."""
    info = next(iter(parse_ts(send_cmd(sock, 'serverinfo'))), {})
    current = info.get('virtualserver_welcomemessage', '').strip()
    if current and current != TS_DEFAULT_WELCOME:
        print("  --   welcome message already customised, left untouched", flush=True)
        return
    resp = send_cmd(sock, f'serveredit virtualserver_welcomemessage={ts_escape(WELCOME_MESSAGE)}')
    print(f"  {'OK  ' if ok(resp) else 'WARN'} welcome message", flush=True)


def set_server_name(sock):
    """Enforce the server title (virtualserver_name); re-applied on every run."""
    resp = send_cmd(sock, f'serveredit virtualserver_name={ts_escape(SERVER_NAME)}')
    print(f"  {'OK  ' if ok(resp) else 'WARN'} server name", flush=True)


def remove_initial_tokens(sock):
    """Delete unredeemed privilege keys without a description (the one TeamSpeak generates on first
    start). While one exists, every client is prompted for it on connect. Keys created with
    `--token` carry a description and are left alone."""
    resp = send_cmd(sock, 'tokenlist')
    if 'error id=1281' in resp:  # database empty result: no tokens
        print("  --   no unredeemed privilege keys", flush=True)
        return
    if not ok(resp):
        error = next((l for l in resp.splitlines() if l.startswith('error id=')), resp.strip())
        print(f"  WARN tokenlist failed: {error}", flush=True)
        return
    tokens = [t for t in parse_ts(resp) if 'token' in t]
    print(f"  found {len(tokens)} unredeemed privilege key(s)", flush=True)
    for t in tokens:
        desc = t.get('token_description', '').strip()
        shown = f"{t['token'][:4]}... type={t.get('token_type')} id1={t.get('token_id1')} description='{desc}'"
        if desc:
            print(f"  --   kept {shown}", flush=True)
            continue
        r = send_cmd(sock, f"tokendelete token={t['token']}")
        print(f"  {'OK  ' if ok(r) else 'WARN'} removed {shown}", flush=True)


def print_keys(sock):
    """Print all unredeemed privilege keys in full (including TeamSpeak's first-start key)."""
    resp = send_cmd(sock, 'tokenlist')
    if 'error id=1281' in resp:
        print("No unredeemed privilege keys.", flush=True)
        return
    if not ok(resp):
        sys.exit(f"ERROR: tokenlist failed: {resp.strip()}")
    for t in parse_ts(resp):
        if 'token' in t:
            print(f"{t['token']}   (group id {t.get('token_id1')}, '{t.get('token_description', '')}')", flush=True)


def print_admins(sock, sgid):
    """List the members of the Server Admin group (identity = client_unique_identifier)."""
    resp = send_cmd(sock, f'servergroupclientlist sgid={sgid} -names')
    if 'error id=1281' in resp:
        print("Server Admin group has no members.", flush=True)
        return
    if not ok(resp):
        sys.exit(f"ERROR: servergroupclientlist failed: {resp.strip()}")
    for c in parse_ts(resp):
        if 'cldbid' in c:
            print(f"cldbid={c['cldbid']}  nickname='{c.get('client_nickname', '')}'  "
                  f"uid={c.get('client_unique_identifier', '')}", flush=True)


def print_token(sock, sgid):
    resp = send_cmd(sock, f'tokenadd tokentype=0 tokenid1={sgid} tokenid2=0 tokendescription=manual-admin')
    token = next((c['token'] for c in parse_ts(resp) if 'token' in c), None)
    if not ok(resp) or not token:
        sys.exit(f"ERROR: tokenadd failed: {resp.strip()}")
    print(f"Server Admin privilege key (single use): {token}", flush=True)


def main():
    sock = open_session()
    sgid, label = find_admin_sgid(sock)

    if '--admins' in sys.argv[1:]:
        print_admins(sock, sgid)
        send_cmd(sock, 'quit')
        sock.close()
        return

    if '--keys' in sys.argv[1:]:
        print_keys(sock)
        send_cmd(sock, 'quit')
        sock.close()
        return

    if '--token' in sys.argv[1:]:
        print_token(sock, sgid)
        send_cmd(sock, 'quit')
        sock.close()
        return

    print(f"Granting permissions to server group sgid={sgid} ({label})", flush=True)
    grant_permissions(sock, sgid)
    print("Seeding channels", flush=True)
    seed_channels(sock)
    print("Server name and welcome message", flush=True)
    set_server_name(sock)
    set_welcome_message(sock)
    print("Privilege keys", flush=True)
    remove_initial_tokens(sock)
    info = next(iter(parse_ts(send_cmd(sock, 'serverinfo'))), {})
    print(f"  virtualserver_ask_for_privilegekey={info.get('virtualserver_ask_for_privilegekey')}", flush=True)

    send_cmd(sock, 'quit')
    sock.close()
    print("TS3 setup complete.", flush=True)


if __name__ == '__main__':
    main()
