"""Marantz AV7706 web remote - Kodi service.

Keeps a telnet-style connection to the AVR, serves the web page in html/,
and relays state/commands between the page (WebSocket) and the AVR.
"""
import functools
import json
import logging
import os
import re
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from websocket_server import WebsocketServer

try:
    import xbmc
except ImportError:  # running outside Kodi for testing
    xbmc = None

# ---------------------------------------------------------------- settings --
AVR_HOST = '10.0.10.22'
AVR_PORT = 23
BIND_HOST = '0.0.0.0'          # listen on all interfaces of the Kodi box
HTTP_PORT = 9000
WS_PORT = 12254
START_PAGE = 'avrcontrol.html' # http://<kodi>:9000/ redirects here
OLD_PAGES = ('/avr4.html',)    # old bookmarks / home-screen icons redirect too
MAX_VOLUME_DB = 0.0            # the remote will never set master volume above this
HTML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'html')

CMD_SPACING = 0.06             # Marantz wants >= 50 ms between commands
KEEPALIVE_AFTER = 60           # seconds of silence before we poke the AVR
DEAD_AFTER = 75                # seconds of silence before we reconnect

STATUS_QUERIES = ['ZM?', 'MV?', 'MU?', 'SI?', 'MS?', 'SSSMG ?', 'CV?', 'PSDRC ?',
                  'PSDYNEQ ?', 'PSDYNVOL ?', 'PSMULTEQ: ?', 'PSLFE ?', 'PSLOM ?']

# Allowed values for the audio-setting dropdowns, and how each is written.
# (The AV7706 does not answer the old PSDCO command; it reports PSDRC instead.)
PS_OPTIONS = {
    'PSDRC': ('AUTO', 'LOW', 'MID', 'HI', 'OFF'),
    'PSDYNEQ': ('ON', 'OFF'),
    'PSDYNVOL': ('OFF', 'LIT', 'MED', 'HEV'),
    'PSMULTEQ': ('OFF', 'AUDYSSEY', 'BYP.LR', 'FLAT', 'MANUAL'),
    'PSLOM': ('ON', 'OFF'),
}
PS_SEPARATOR = {'PSMULTEQ': ':'}   # everything else uses a space
SAFE_VALUE = re.compile(r'^[A-Z0-9/.:+\- ]{1,24}$')   # inputs / sound modes
SAFE_CHANNEL = re.compile(r'^[A-Z0-9]{1,4}$')


# ----------------------------------------------------------------- logging --
def log(msg, error=False):
    if xbmc:
        xbmc.log('[service.autoexec] ' + msg, xbmc.LOGERROR if error else xbmc.LOGINFO)
    else:
        print(('ERROR: ' if error else '') + msg)


# -------------------------------------------------------- level conversion --
def level_to_db(text, zero):
    """'50' -> 0.0, '485' -> -1.5 (a third digit 5 means +0.5)."""
    text = text.strip()
    if not text.isdigit() or len(text) not in (2, 3):
        return None
    value = int(text[:2]) + (0.5 if len(text) == 3 else 0)
    return value - zero


def db_to_level(db, zero, lo, hi):
    level = round(float(db) * 2) / 2 + zero
    level = max(lo, min(hi, level))
    whole = int(level)
    return '%02d5' % whole if level != whole else '%02d' % whole


# ------------------------------------------------------------------- state --
state = {}
state_lock = threading.Lock()
ws_server = None


def broadcast(payload):
    if ws_server is None:
        return
    text = json.dumps(payload)
    for client in list(ws_server.clients):
        try:
            ws_server.send_message(client, text)
        except Exception as e:  # client went away mid-send
            log('WebSocket send to client %s failed: %s' % (client.get('id'), e))


def update_state(updates):
    with state_lock:
        changed = {k: v for k, v in updates.items() if state.get(k) != v}
        state.update(changed)
    if changed:
        broadcast({'state': changed})


def volume_cap():
    cap = MAX_VOLUME_DB
    with state_lock:
        avr_max = state.get('MVMAX')
    if avr_max is not None:
        cap = min(cap, avr_max)
    return cap


# ------------------------------------------------------------- AVR parsing --
PS_LINE = re.compile(r'^(PSDRC|PSDYNEQ|PSDYNVOL|PSMULTEQ|PSLOM)[ :](.+)$')
CV_LINE = re.compile(r'^CV([A-Z0-9]+) (\d{2,3})$')
LFE_LINE = re.compile(r'^PSLFE (\d{2})$')


def parse_line(line):
    updates = {}
    if line.startswith('MVMAX'):
        db = level_to_db(line[5:], 80)
        if db is not None:
            updates['MVMAX'] = db
    elif line.startswith('MV'):
        db = level_to_db(line[2:], 80)
        if db is not None:
            updates['MV'] = db
    elif line.startswith('MU'):
        updates['MU'] = line[2:]
    elif line.startswith('ZM'):
        updates['ZM'] = line[2:]
    elif line.startswith('SI'):
        updates['SI'] = line[2:]
    elif line.startswith('MS'):
        updates['MS'] = line[2:]
    elif line.startswith('SSSMG '):
        # sound-mode button group last used: MOV / MUS / GAM / PUR
        updates['SSSMG'] = line[6:].strip()
    elif line.startswith('CV'):
        m = CV_LINE.match(line)
        if m:
            db = level_to_db(m.group(2), 50)
            if db is not None:
                updates['CV' + m.group(1)] = db
    elif line.startswith('PSLFE'):
        m = LFE_LINE.match(line)
        if m:
            updates['PSLFE'] = -int(m.group(1))
    else:
        m = PS_LINE.match(line)
        if m:
            updates[m.group(1)] = m.group(2).strip()

    if updates:
        update_state(updates)


# ---------------------------------------------------------- AVR connection --
class Avr(object):
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.write_lock = threading.Lock()
        self.stopping = threading.Event()
        self.connected = False

    def send(self, cmd):
        with self.write_lock:
            sock = self.sock
            if sock is None:
                log('AVR not connected, dropped command %r' % cmd)
                return False
            try:
                sock.sendall(cmd.encode('ascii') + b'\r')
                log('TO AVR > %s' % cmd)
            except OSError as e:
                log('AVR write failed: %s' % e, error=True)
                self._close()
                return False
            time.sleep(CMD_SPACING)
            return True

    def query_all(self):
        threading.Thread(target=self._query_all, name='avr-query', daemon=True).start()

    def _query_all(self):
        for q in STATUS_QUERIES:
            if self.stopping.is_set() or not self.send(q):
                return
            time.sleep(0.2)

    def _close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _set_connected(self, value):
        if value != self.connected:
            self.connected = value
            log('AVR %s' % ('connected' if value else 'disconnected'))
            broadcast({'avr': value})

    def stop(self):
        self.stopping.set()
        self._close()

    def run(self):
        backoff = 1
        while not self.stopping.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5)
            except OSError as e:
                log('Cannot reach AVR at %s: %s (retry in %ds)' % (self.host, e, backoff))
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1
            sock.settimeout(1.0)
            self.sock = sock
            self._set_connected(True)
            self.query_all()
            try:
                self._read_loop(sock)
            except (OSError, ConnectionError) as e:
                if not self.stopping.is_set():
                    log('AVR connection lost: %s' % e, error=True)
            finally:
                with self.write_lock:
                    if self.sock is sock:
                        self._close()
                self._set_connected(False)
            self.stopping.wait(1)

    def _read_loop(self, sock):
        buf = b''
        last_rx = time.time()
        pinged = False
        while not self.stopping.is_set():
            if self.sock is not sock:
                raise ConnectionError('socket closed after write error')
            try:
                chunk = sock.recv(1024)
            except socket.timeout:
                silent = time.time() - last_rx
                if silent > DEAD_AFTER:
                    raise ConnectionError('no reply from AVR for %ds' % silent)
                if silent > KEEPALIVE_AFTER and not pinged:
                    pinged = True
                    self.send('ZM?')
                continue
            if not chunk:
                raise ConnectionError('AVR closed the connection')
            last_rx = time.time()
            pinged = False
            buf += chunk
            while b'\r' in buf:
                raw, buf = buf.split(b'\r', 1)
                line = raw.decode('ascii', 'replace').strip()
                if not line:
                    continue
                log('FROM AVR < %s' % line)
                try:
                    parse_line(line)
                except Exception as e:
                    log('Could not handle AVR line %r: %s' % (line, e), error=True)


avr = Avr(AVR_HOST, AVR_PORT)


# ---------------------------------------------------- commands from the page --
def handle_command(msg):
    cmd = msg['cmd']
    if cmd == 'refresh':
        avr.query_all()
    elif cmd == 'power':
        avr.send('ZMON' if msg['value'] == 'ON' else 'ZMOFF')
    elif cmd == 'mute':
        avr.send('MUON' if msg['value'] == 'ON' else 'MUOFF')
    elif cmd == 'mv':
        cap = volume_cap()
        if 'step' in msg:
            with state_lock:
                current = state.get('MV')
            if msg['step'] == 'UP':
                if current is None or current + 0.5 <= cap:
                    avr.send('MVUP')
            else:
                avr.send('MVDOWN')
        else:
            db = min(float(msg['db']), cap)
            avr.send('MV' + db_to_level(db, 80, 0, 98))
    elif cmd == 'cv':
        ch = str(msg['ch'])
        if SAFE_CHANNEL.match(ch):
            avr.send('CV%s %s' % (ch, db_to_level(msg['db'], 50, 38, 62)))
    elif cmd == 'lfe':
        avr.send('PSLFE %02d' % max(0, min(10, int(round(-float(msg['db']))))))
    elif cmd == 'ps':
        key, value = str(msg['key']), str(msg['value'])
        if value in PS_OPTIONS.get(key, ()):
            avr.send(key + PS_SEPARATOR.get(key, ' ') + value)
    elif cmd in ('si', 'ms'):
        value = str(msg['value'])
        if SAFE_VALUE.match(value):
            avr.send(cmd.upper() + value)
    else:
        log('Unknown command from page: %r' % msg)


def on_new_client(client, server):
    log('Web client %d connected from %s' % (client['id'], client['address'][0]))
    with state_lock:
        snapshot = dict(state)
    server.send_message(client, json.dumps({
        'avr': avr.connected,
        'config': {'mv_max': volume_cap()},
        'state': snapshot,
    }))


def on_message(client, server, message):
    log('FROM PAGE < %s' % message[:200])
    try:
        msg = json.loads(message)
        if isinstance(msg, dict):
            handle_command(msg)
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        log('Ignored bad message from page: %r (%s)' % (message[:200], e), error=True)


# -------------------------------------------------------------- web server --
class RemoteHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path in OLD_PAGES:
            self.send_response(302)
            self.send_header('Location', '/' + START_PAGE)
            self.end_headers()
            return
        super().do_GET()

    def end_headers(self):
        # make phones pick up page changes instead of serving a stale copy
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


# --------------------------------------------------------------------- main --
def main():
    global ws_server
    log('Starting Marantz remote')

    threading.Thread(target=avr.run, name='avr-reader', daemon=True).start()

    httpd = None
    try:
        handler = functools.partial(RemoteHandler, directory=HTML_DIR)
        httpd = ThreadingHTTPServer((BIND_HOST, HTTP_PORT), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, name='http', daemon=True).start()
        log('Web page on port %d' % HTTP_PORT)
    except OSError as e:
        log('Could not start web server on port %d: %s' % (HTTP_PORT, e), error=True)
        httpd = None

    try:
        ws_server = WebsocketServer(host=BIND_HOST, port=WS_PORT, loglevel=logging.WARNING)
        ws_server.set_fn_new_client(on_new_client)
        ws_server.set_fn_message_received(on_message)
        ws_server.run_forever(threaded=True)
        log('WebSocket on port %d' % WS_PORT)
    except OSError as e:
        log('Could not start WebSocket server on port %d: %s' % (WS_PORT, e), error=True)
        ws_server = None

    try:
        if xbmc:
            monitor = xbmc.Monitor()
            while not monitor.abortRequested():
                if monitor.waitForAbort(1):
                    break
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log('Stopping Marantz remote')
        avr.stop()
        if ws_server is not None:
            try:
                ws_server.shutdown_gracefully()
            except Exception as e:
                log('WebSocket shutdown: %s' % e)
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    main()
