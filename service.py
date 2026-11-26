"""Marantz AVR Remote - Kodi service add-on.

Keeps a telnet-style connection to a Marantz/Denon AV receiver, serves the web
page in html/, and relays state and commands between the page (WebSocket) and
the receiver. Written for and tested with a Marantz AV7705.
"""
import functools
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ADDON_ID = 'service.marantz.remote'
ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ADDON_DIR, 'resources', 'lib'))

from websocket_server import WebsocketServer  # noqa: E402  (bundled in resources/lib)

try:
    import xbmc
    import xbmcaddon
except ImportError:  # running outside Kodi for testing
    xbmc = xbmcaddon = None

# ------------------------------------------------------------------ constants --
AVR_PORT = 23
BIND_HOST = '0.0.0.0'          # listen on all interfaces of the Kodi box
START_PAGE = 'avrcontrol.html'  # http://<kodi>:<port>/ redirects here
OLD_PAGES = ('/avr4.html',)    # old bookmarks / home-screen icons redirect too
HTML_DIR = os.path.join(ADDON_DIR, 'html')

CMD_SPACING = 0.06             # Marantz wants >= 50 ms between commands
KEEPALIVE_AFTER = 60           # seconds of silence before we poke the AVR
DEAD_AFTER = 75                # seconds of silence before we reconnect

STATUS_QUERIES = ['ZM?', 'MV?', 'MU?', 'SI?', 'MS?', 'SSSMG ?', 'CV?', 'PSDRC ?',
                  'PSDYNEQ ?', 'PSDYNVOL ?', 'PSMULTEQ: ?', 'PSLFE ?', 'PSLOM ?',
                  'DIM ?', 'SSFUN ?', 'SSSOD ?']

# Allowed values for the audio-setting dropdowns, and how each is written.
# (The AV7705 does not answer the old PSDCO command; it reports PSDRC instead.)
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
IPV4 = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')
HOSTNAME = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*$')


# ------------------------------------------------------------------- logging --
def log(msg, error=False):
    if xbmc:
        xbmc.log('[%s] %s' % (ADDON_ID, msg), xbmc.LOGERROR if error else xbmc.LOGINFO)
    else:
        print(('ERROR: ' if error else '') + msg)


# ------------------------------------------------------------------ settings --
# Stored by Kodi (Settings > Add-ons > Marantz AVR Remote > Configure).
DEFAULTS = {
    'avr_host': '10.0.10.22',
    'max_volume': 80,              # absolute scale, 80 = 0 dB
    'volume_display': 'absolute',
    'http_port': 9000,
    'ws_port': 12254,
}
_test_settings = {}                # used instead of Kodi when testing outside it


def get_setting(key):
    default = DEFAULTS[key]
    if xbmcaddon is None:
        return _test_settings.get(key, default)
    addon = xbmcaddon.Addon(ADDON_ID)   # fresh object: picks up changed values
    try:
        if isinstance(default, int):
            return addon.getSettingInt(key)
        return addon.getSetting(key) or default
    except Exception as e:  # missing or malformed value
        log('Setting %s unreadable (%s), using default' % (key, e), error=True)
        return default


def set_setting(key, value):
    if xbmcaddon is None:
        _test_settings[key] = value
    else:
        xbmcaddon.Addon(ADDON_ID).setSetting(key, str(value))


def clean_host(text):
    """Return a valid IPv4 address or hostname, or None."""
    host = str(text).strip()
    m = IPV4.match(host)
    if m:
        return host if all(int(p) <= 255 for p in m.groups()) else None
    if host.replace('.', '').isdigit():   # looks like a mistyped IP, not a hostname
        return None
    return host if HOSTNAME.match(host) else None


# ---------------------------------------------------------- level conversion --
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


# --------------------------------------------------------------------- state --
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
    """Highest master volume in dB the remote may set."""
    cap = max(0, min(98, int(get_setting('max_volume')))) - 80
    with state_lock:
        avr_max = state.get('MVMAX')
    if avr_max is not None:
        cap = min(cap, avr_max)
    return cap


def page_config():
    return {
        'mv_max': volume_cap(),
        'avr_host': avr.host,
        'volume_display': get_setting('volume_display'),
    }


# --------------------------------------------------------------- AVR parsing --
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
    elif line.startswith('DIM '):
        # front display dimmer: BRI / DIM / DAR / OFF
        updates['DIM'] = line[4:].strip()
    elif line.startswith('SSSMG '):
        # sound-mode button group last used: MOV / MUS / GAM / PUR
        updates['SSSMG'] = line[6:].strip()
    elif line.startswith('SSFUN') or line.startswith('SSSOD'):
        # input names ("SSFUNSAT/CBL AppleTV") and which inputs are in use
        # ("SSSODDVD DEL"); each list ends with "SSFUN END" / "SSSOD END"
        key, rest = line[:5], line[5:]
        if rest and not rest.startswith(' '):
            code, _, value = rest.partition(' ')
            with state_lock:
                table = dict(state.get(key) or {})
            table[code] = value.strip()
            updates[key] = table
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


# ------------------------------------------------------------ AVR connection --
class Avr(object):
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock = None
        self.write_lock = threading.Lock()
        self.stopping = threading.Event()
        self.wake = threading.Event()     # cuts a retry wait short
        self.connected = False

    def _pause(self, seconds):
        self.wake.wait(seconds)
        self.wake.clear()

    def set_host(self, host):
        """Switch to another AVR address: drop the current connection and reconnect now."""
        if host == self.host:
            return
        log('AVR address changed to %s' % host)
        self.host = host
        with self.write_lock:
            self._close()
        self.wake.set()

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
        sock = self.sock
        for q in STATUS_QUERIES:
            # stop if the connection was replaced (reconnect or address change)
            if self.stopping.is_set() or self.sock is not sock or not self.send(q):
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
        self.wake.set()
        self._close()

    def run(self):
        backoff = 1
        while not self.stopping.is_set():
            host = self.host
            try:
                sock = socket.create_connection((host, self.port), timeout=5)
            except OSError as e:
                log('Cannot reach AVR at %s: %s (retry in %ds)' % (host, e, backoff))
                self._pause(backoff)
                backoff = 1 if self.host != host else min(backoff * 2, 30)
                continue
            if self.host != host:          # address changed while we were connecting
                sock.close()
                continue
            backoff = 1
            sock.settimeout(1.0)
            self.sock = sock
            self._set_connected(True)
            self.query_all()
            try:
                self._read_loop(sock)
            except (OSError, ConnectionError) as e:
                if self.host != host:
                    log('Leaving AVR at %s' % host)
                elif not self.stopping.is_set():
                    log('AVR connection lost: %s' % e, error=True)
            finally:
                with self.write_lock:
                    if self.sock is sock:
                        self._close()
                self._set_connected(False)
            self._pause(1)

    def _read_loop(self, sock):
        buf = b''
        last_rx = time.time()
        pinged = False
        while not self.stopping.is_set():
            if self.sock is not sock:
                raise ConnectionError('connection closed (write error or address change)')
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


avr = Avr(DEFAULTS['avr_host'], AVR_PORT)


def apply_settings():
    """(Re)read settings that can change while running."""
    host = clean_host(get_setting('avr_host'))
    if host is None:
        log('AVR address setting %r is not valid; keeping %s' % (get_setting('avr_host'), avr.host),
            error=True)
    else:
        avr.set_host(host)
    broadcast({'config': page_config()})


# ---------------------------------------------------- commands from the page --
def handle_command(msg):
    """Act on a message from the page. Returns an error string to show, or None."""
    cmd = msg['cmd']
    if cmd == 'set_avr_host':
        host = clean_host(msg['host'])
        if host is None:
            return 'Not a valid IP address or hostname: %s' % str(msg['host'])[:60]
        try:
            set_setting('avr_host', host)
        except Exception as e:
            log('Could not save AVR address: %s' % e, error=True)
            return 'Could not save the setting on Kodi: %s' % e
        avr.set_host(host)
        broadcast({'config': page_config()})
    elif cmd == 'refresh':
        avr.query_all()
    elif cmd == 'power':
        avr.send('ZMON' if msg['value'] == 'ON' else 'ZMOFF')
    elif cmd == 'mute':
        avr.send('MUON' if msg['value'] == 'ON' else 'MUOFF')
    elif cmd == 'display':
        avr.send('DIM OFF' if msg['value'] == 'OFF' else 'DIM BRI')
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
        'config': page_config(),
        'state': snapshot,
    }))


def on_message(client, server, message):
    log('FROM PAGE < %s' % message[:200])
    try:
        msg = json.loads(message)
        if isinstance(msg, dict):
            error = handle_command(msg)
            if error:
                server.send_message(client, json.dumps({'error': error}))
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        log('Ignored bad message from page: %r (%s)' % (message[:200], e), error=True)


# ---------------------------------------------------------------- web server --
class RemoteHandler(SimpleHTTPRequestHandler):
    ws_port = DEFAULTS['ws_port']

    def do_GET(self):
        if self.path == '/' or self.path in OLD_PAGES:
            self.send_response(302)
            self.send_header('Location', '/' + START_PAGE)
            self.end_headers()
            return
        if self.path.split('?')[0] == '/config.js':
            # tells the page which WebSocket port to use
            body = ('window.REMOTE_CONFIG = %s;\n' % json.dumps({'ws_port': self.ws_port})).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        # make phones pick up page changes instead of serving a stale copy
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------------- main --
if xbmc:
    class ServiceMonitor(xbmc.Monitor):
        def onSettingsChanged(self):
            log('Settings changed')
            apply_settings()


def main():
    global ws_server
    log('Starting Marantz AVR Remote')

    host = clean_host(get_setting('avr_host'))
    if host:
        avr.host = host
    http_port = int(get_setting('http_port'))
    ws_port = int(get_setting('ws_port'))
    log('AVR address %s, web page port %d, WebSocket port %d' % (avr.host, http_port, ws_port))

    threading.Thread(target=avr.run, name='avr-reader', daemon=True).start()

    httpd = None
    try:
        handler = functools.partial(type('Handler', (RemoteHandler,), {'ws_port': ws_port}),
                                    directory=HTML_DIR)
        httpd = ThreadingHTTPServer((BIND_HOST, http_port), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, name='http', daemon=True).start()
        log('Web page on port %d' % http_port)
    except OSError as e:
        log('Could not start web server on port %d: %s' % (http_port, e), error=True)
        httpd = None

    try:
        ws_server = WebsocketServer(host=BIND_HOST, port=ws_port, loglevel=logging.WARNING)
        ws_server.set_fn_new_client(on_new_client)
        ws_server.set_fn_message_received(on_message)
        ws_server.run_forever(threaded=True)
        log('WebSocket on port %d' % ws_port)
    except OSError as e:
        log('Could not start WebSocket server on port %d: %s' % (ws_port, e), error=True)
        ws_server = None

    try:
        if xbmc:
            monitor = ServiceMonitor()
            while not monitor.abortRequested():
                if monitor.waitForAbort(1):
                    break
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        log('Stopping Marantz AVR Remote')
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
