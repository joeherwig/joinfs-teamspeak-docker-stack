#!/usr/bin/env python3
"""
One-shot TS3 ServerQuery permission setup.
Grants required permissions to the Server Admin group so tsapi works without manual configuration.
"""
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


def main():
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

    send_cmd(sock, 'quit')
    sock.close()
    print("TS3 permission setup complete.", flush=True)


if __name__ == '__main__':
    main()
