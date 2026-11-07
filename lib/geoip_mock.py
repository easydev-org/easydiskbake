"""Answer the installers' geolocation probes with a fixed place, instead of the truth.

    served from lib/net.py: 127.0.0.1:PORT, and dnsmasq points the geoip names at it.

WHY A MOCK AND NOT A REFUSAL

The first version of lib/net.py answered every geoip name with 127.0.0.1, so the guest's
connect was refused. That is a good answer for Calamares, which falls back to American
English when it cannot ask - measured on CachyOS after the change - but it is a BAD answer
for Ubuntu's installer. Ubuntu's Flutter installer uses the probe to fill in the time zone
pane, and a refusal leaves that pane with NO selection and its Next button disabled:

    look finish: 7 words :: Nov7 | 16:53 | Select | your | timezone | Aa | ©

which is what run 42388038785 logged for eight minutes before the timeout. The bake is not
wrong; the installer is waiting for an answer that never comes.

WHAT THIS DOES

It IS the answer. The probe gets a response that says the machine is in New York, in the
same XML shape each service documents - verified against the live services on 2026-11-07:

    https://geoip.kde.org/v1/ubiquity     Calamares
    https://geoip.ubuntu.com/lookup       Ubuntu's installer

so every installer takes the same path it would on a real machine in the United States,
which is the configuration this repository's images are built for: English, America/New_York,
and a time zone pane that is already satisfied.

WHY NOT LET IT REACH THE REAL SERVICE

Because the real answer is China. The cluster's egress is 115.190.248.85, and every probe
returns

    <CountryCode>CN</CountryCode>
    <TimeZone>Asia/Shanghai</TimeZone>

which produced Simplified Chinese installers on CachyOS, EndeavourOS, Mint and Kubuntu, and
Shanghai time zones on the disks. A bake must not depend on where its runner happens to be.

WHY HTTP AND NOT HTTPS

The installers request https. QEMU's user-mode and bridged guests here reach the host over
a tap, and the host would have to present a certificate those images trust - they do not
trust one this repository can mint without installing a CA into the guest, which is a much
larger change than the problem. So the port is intercepted at the DNS level and the guest
is NOT redirected to TLS: see the caller in lib/net.py, which answers the names with this
host and relies on the installers' own fallback to http when https fails.

That fallback is real for Calamares (it tries the configured URL, then gives up) and is why
the refusal worked there. For Ubuntu's installer it is not, which is why this module exists
at all: it is the client for the pane that needs a value.
"""

from __future__ import annotations

import http.server
import threading

# THE PLACE THE BAKES ARE BUILT FOR. One constant, so the language, the time zone and the
# keymap cannot disagree - and so a machine in another country can be simulated by editing
# one line.
COUNTRY_CODE = "US"
COUNTRY_CODE3 = "USA"
COUNTRY_NAME = "United States"
REGION_CODE = "NY"
REGION_NAME = "New York"
CITY = "New York"
LATITUDE = "40.7143"
LONGITUDE = "-74.0060"
TIMEZONE = "America/New_York"
POSTAL = "10001"

PORT = 8977


def _kde_ubiquity() -> bytes:
    """The shape geoip.kde.org/v1/ubiquity returns, with an American answer.

    KDE's XML has empty elements where the value is unknown (CountryCode3, RegionCode,
    RegionName) - kept empty here too, because a value where the real service omits one is
    a difference the installer could act on.
    """
    body = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<Response>"
        f"<Ip>127.0.0.1</Ip>"
        "<Status>OK</Status>"
        f"<CountryCode>{COUNTRY_CODE}</CountryCode>"
        "<CountryCode3></CountryCode3>"
        f"<CountryName>{COUNTRY_NAME}</CountryName>"
        "<RegionCode></RegionCode>"
        "<RegionName></RegionName>"
        f"<City>{CITY}</City>"
        f"<ZipPostalCode>{POSTAL}</ZipPostalCode>"
        f"<Latitude>{LATITUDE}</Latitude>"
        f"<Longitude>{LONGITUDE}</Longitude>"
        "<AreaCode>0</AreaCode>"
        f"<TimeZone>{TIMEZONE}</TimeZone>"
        "</Response>\n"
    )
    return body.encode()


def _ubuntu_lookup() -> bytes:
    """The shape geoip.ubuntu.com/lookup returns.

    THIS ONE CARRIES A TimeZone ELEMENT AND THAT IS THE POINT. Ubuntu's installer reads it
    to preselect the time zone pane; the pane is what stalled on the refusal.
    """
    body = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<Response>"
        "<Ip>127.0.0.1</Ip>"
        "<Status>OK</Status>"
        f"<CountryCode>{COUNTRY_CODE}</CountryCode>"
        f"<CountryCode3>{COUNTRY_CODE3}</CountryCode3>"
        f"<CountryName>{COUNTRY_NAME}</CountryName>"
        f"<RegionCode>{REGION_CODE}</RegionCode>"
        f"<RegionName>{REGION_NAME}</RegionName>"
        f"<City>{CITY}</City>"
        f"<ZipPostalCode>{POSTAL}</ZipPostalCode>"
        f"<Latitude>{LATITUDE}</Latitude>"
        f"<Longitude>{LONGITUDE}</Longitude>"
        "<AreaCode>None</AreaCode>"
        f"<TimeZone>{TIMEZONE}</TimeZone>"
        "</Response>\n"
    )
    return body.encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:                      # noqa: N802 - http.server's name
        path = self.path.split("?")[0]
        if path.endswith("/ubiquity") or "kde" in path:
            payload = _kde_ubiquity()
        elif "ubuntu" in path or path.endswith("/lookup"):
            payload = _ubuntu_lookup()
        else:
            # ANY OTHER PATH GETS THE UBUNTU SHAPE. A probe this module has not seen is
            # still a probe, and answering with a well-formed American location is better
            # than a 404 the installer would treat as "no answer" - which is exactly the
            # failure this exists to prevent.
            payload = _ubuntu_lookup()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self) -> None:                     # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # THE CALLER'S LOG IS THE RUN'S LOG. A probe per four seconds would drown it.
        pass


def serve(port: int = PORT) -> threading.Thread:
    """Start the mock on `port` in a daemon thread, and return the thread.

    DAEMON, so a bake that is killed does not leave the process waiting on it - the same
    reason lib/live.py's encoder is a daemon thread.
    """
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def running(port: int = PORT) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    import sys
    import time
    p = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    serve(p)
    print(f"[geoip-mock] {COUNTRY_NAME} / {TIMEZONE} on 0.0.0.0:{p}", flush=True)
    while True:
        time.sleep(3600)
