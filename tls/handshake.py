""" Basic TLS 1.2 handshake test.

Also usable as a TLS traffic generator to debug early TLS server
implementation. This tool emphasises flexibility in generation of TLS traffic,
not performance.

We use __future__ module for easier migration to Python3.

TLS 1.2 is specified in RFC 5246. See also these useful references:
 - https://wiki.osdev.org/SSL/TLS
 - https://wiki.osdev.org/TLS_Handshake
 - https://wiki.osdev.org/TLS_Encryption

Debugging can be simplified by enabling verbose in TlsHandshake instance. Or
by running `tshark -i lo -f 'port 443' -Y ssl -O ssl`
"""
from __future__ import print_function
from contextlib import contextmanager
import random
import socket
import ssl # OpenSSL based API
import struct
from time import sleep

from helpers import dmesg, tf_cfg
from helpers.error import Error
from scapy_ssl_tls import ssl_tls as tls

__author__ = 'Tempesta Technologies, Inc.'
__copyright__ = 'Copyright (C) 2018-2020 Tempesta Technologies, Inc.'
__license__ = 'GPL2'


GOOD_RESP = "HTTP/1.1 200"
# FIXME https://github.com/tempesta-tech/tempesta/issues/1294
TLS_HS_WARN = "Warning: Unrecognized TLS receive return code"


def x509_check_cn(cert, cn):
    """
    Decode x509 certificate in BER and check CommonName (CN, OID '2.5.4.3')
    against passed @cn value. ScaPy-TLS can not parse ASN1 from certificates
    generated by the cryptography library, so we can not use full string
    matching and have to use substring matching instead.
    """
    for f in cert.tbsCertificate.issuer:
        if f.rdn[0].type.val == '2.5.4.3':
            return str(f.rdn[0].value).endswith(cn)
    raise Error("Certificate has no CommonName")


def x509_check_issuer(cert, issuer):
    """
    The same as above, but for Issuer OrganizationName (O, OID '2.5.4.10').
    """
    for f in cert.tbsCertificate.issuer:
        if f.rdn[0].type.val == '2.5.4.10':
            return str(f.rdn[0].value).endswith(issuer)
    raise Error("Certificate has no Issuer OrganizationName")


class TlsHandshake:
    """
    Class for custom TLS handshakes - mainly ScaPy-TLS wrapper.
    Update the fields defined in __init__() to customize a handshake.

    Use higher @io_to values to debug/test Tempesta in debug mode.
    Use True for @verbose to see debug output for the handshake.
    """
    def __init__(self, addr=None, port=443, io_to=0.5, chunk=None,
                 sleep_time=0.001, verbose=False):
        if addr:
            self.addr = addr
        else:
            self.addr = tf_cfg.cfg.get('Tempesta', 'ip')
        self.port = port
        self.io_to = io_to # seconds, maybe not so small fraction of a second.
        self.chunk = chunk
        self.sleep_time = sleep_time if sleep_time >= 0.001 else 0.001;
        # We should be able to send at least 10KB with 1ms chunk delay for RTO.
        if self.chunk and self.chunk < 10000:
            io_to = 10000 / self.chunk * 0.001
            if self.io_to < io_to:
                self.io_to = io_to
        self.verbose = verbose
        # Service members.
        self.sock = None
        # Additional handshake options.
        self.sni = ['tempesta-tech.com'] # vhost string names.
        self.exts = [] # Extra extensions
        self.sign_algs = []
        self.elliptic_curves = []
        self.ciphers = []
        self.compressions = []
        self.renegotiation_info = []
        self.inject = None
        # HTTP server response (headers and body), if any.
        self.http_resp = None
        # Host request header value, taken from SNI by default.
        self.host = None
        # Server certificate.
        self.cert = None
        # Random session ticket by default.
        self.set_ticket_data('ticket_data')
        # Session id, must be filled for resume
        self.session_id = ''

    def set_ticket_data(self, data):
        """ Set session ticket data. Following values are possible:
        - 'None' - session ticket extension will be disabled;
        - '' (empty string) - empty session ticket extension will be added;
        - '<str>' - arbitrary string will be inserted as session ticket.
        'data' can be represented either as string or as
        scapy_ssl_tls.TLSSessionTicket.
        """
        if data is None:
            self.ticket_data = None
            return
        if type(data) is tls.TLSSessionTicket:
            self.ticket_data = data.ticket
        else:
            self.ticket_data = data

    @contextmanager
    def socket_ctx(self):
        try:
            yield
        finally:
            self.sock.close()

    def conn_estab(self):
        assert not self.sock, "Connection has already been established"
        self.sock = tls.TLSSocket(socket.socket(), client=True)
        # Set large enough send and receive timeouts which will be used by
        # default.
        self.sock.settimeout(self.io_to)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO,
                             struct.pack('ll', self.io_to * 1000, 0))
        if self.chunk:
            # Send data immediately w/o coalescing.
            self.sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        self.sock.connect((self.addr, self.port))

    def send_recv(self, pkt):
        """
        Mainly a copy&paste from tls_do_round_trip(), but uses custom timeout to
        be able to fully read all data from Tempesta in verbose debugging mode
        (serial console verbose logging may be extremely slow).
        """
        assert self.sock, "Try to read and write on invalid socket"
        resp = tls.TLS()
        try:
            if self.chunk:
                prev_timeout = self.sock.gettimeout()
                self.sock.settimeout(self.io_to)
                if self.sock.ctx.must_encrypt:
                    __s = str(tls.tls_to_raw(pkt, self.sock.tls_ctx, True,
                                             self.sock.compress_hook,
                                             self.sock.pre_encrypt_hook,
                                             self.sock.encrypt_hook))
                else:
                    __s = str(pkt)
                n = self.chunk
                for chunk in [__s[i:i + n] for i in xrange(0, len(__s), n)]:
                    """
                    This is a simple and ugly way to send many TCP segments,
                    but it's the most applicable with other ways - some of
                    them however gives desired effect, but in too complex way:
                    1. SO_MAX_PACING_RATE and fair queue (TC) policing cares
                       about total rate - it sends full TCP segments and
                       introduces huge delays to satisfy the policy.
                    2. TCP_MAXSEG socket option as well as reducing TCP read
                       and write memory like
                         sysctl -w net.ipv4.tcp_wmem='16 16 32'
                         sysctl -w net.ipv4.tcp_rmem='16 16 32'
                       negatively affect the whole system settings (per socket
                       setting also influenced by the global sysctls) and do
                       not work with GSO & GRO - a peer still receives large
                       frags.
                    3. iptables --set-mss allows to change SYN packets only.
                    4. Setting interface MTU also doesn't work properly against
                       segmentation offloads.
                    """
                    self.sock._s.sendall(chunk)
                    sleep(self.sleep_time)
                self.sock.tls_ctx.insert(pkt, self.sock._get_pkt_origin('out'))
                self.sock.settimeout(prev_timeout)
            else:
                self.sock.sendall(pkt, timeout=self.io_to)
            resp = self.sock.recvall(timeout=self.io_to)
            if resp.haslayer(tls.TLSAlert):
                alert = resp[tls.TLSAlert]
                if alert.level != tls.TLSAlertLevel.WARNING:
                    level = tls.TLS_ALERT_LEVELS.get(alert.level, "unknown")
                    desc = tls.TLS_ALERT_DESCRIPTIONS.get(alert.description,
                                                          "unknown description")
                    raise tls.TLSProtocolError("%s alert returned by server: %s"
                                               % (level.upper(), desc.upper()),
                                               pkt, resp)
        except socket.error as sock_except:
            raise tls.TLSProtocolError(sock_except, pkt, resp)
        return resp

    def inject_bad(self, fuzzer):
        """
        Inject a bad record after @self.inject normal records.
        """
        if self.inject is None or not fuzzer:
            return
        if self.inject > 0:
            self.inject -= 1
            return
        self.sock.send(next(fuzzer))

    def extra_extensions(self):
        # Add ServerNameIndication (SNI) extension by specified vhosts.
        if self.sni:
            sns = [tls.TLSServerName(data=sname) for sname in self.sni]
            self.exts += [tls.TLSExtension() /
                          tls.TLSExtServerNameIndication(server_names=sns)]
        if self.sign_algs:
            self.exts += self.sign_algs
        else:
            self.exts += [tls.TLSExtension() / tls.TLSExtSignatureAlgorithms()]
        if self.elliptic_curves:
            self.exts += self.elliptic_curves
        else:
            self.exts += [tls.TLSExtension() / tls.TLSExtSupportedGroups()]
        if self.renegotiation_info:
            self.exts += self.renegotiation_info
        else:
            self.exts += [tls.TLSExtension() /
                          tls.TLSExtRenegotiationInfo(data="")]
        # We're must be good with standard, but unsupported options.
        self.exts += [
            tls.TLSExtension(type=0x3), # TrustedCA, RFC 6066 6.
            tls.TLSExtension() / tls.TLSExtCertificateStatusRequest(),
            tls.TLSExtension(type=0xf0), # Bad extension, just skipped

            tls.TLSExtension() /
            tls.TLSExtALPN(protocol_name_list=[
                tls.TLSALPNProtocol(data="http/1.1"),
                tls.TLSALPNProtocol(data="http/2.0")]),

            tls.TLSExtension() /
            tls.TLSExtMaxFragmentLength(fragment_length=0x04), # 4096 bytes

            tls.TLSExtension() /
            tls.TLSExtCertificateURL(certificate_urls=[
                tls.TLSURLAndOptionalHash(url="http://www.tempesta-tech.com")]),

            tls.TLSExtension() /
            tls.TLSExtHeartbeat(
                mode=tls.TLSHeartbeatMode.PEER_NOT_ALLOWED_TO_SEND),
        ]
        if self.ticket_data is not None:
            self.exts += [
                tls.TLSExtension() /
                tls.TLSExtSessionTicketTLS(data=self.ticket_data)
            ]
        return self.exts

    def send_12_alert(self, level, desc):
        self.sock.sendall(tls.TLSRecord(version='TLS_1_2') /
                          tls.TLSAlert(level=level, description=desc))

    def _do_12_hs(self, fuzzer=None):
        """
        Test TLS 1.2 handshake: establish a new TCP connection and send
        predefined TLS handshake records. This test is suitable for debug build
        of Tempesta FW, which replaces random and time functions with
        deterministic data. The test doesn't actually verify any functionality,
        but rather just helps to debug the core handshake functionality.
        """
        try:
            self.conn_estab()
        except socket.error:
            return False

        c_h = tls.TLSClientHello(
            gmt_unix_time=0x22222222,
            random_bytes='\x11' * 28,
            session_id=self.session_id,
            session_id_length=len(self.session_id),
            cipher_suites=[
                tls.TLSCipherSuite.ECDHE_ECDSA_WITH_AES_128_GCM_SHA256] +
                self.ciphers,
            compression_methods=[tls.TLSCompressionMethod.NULL] +
                self.compressions,
            # EtM isn't supported - just try to negate an unsupported extension.
            extensions=[
                tls.TLSExtension(type=0x16), # Encrypt-then-MAC
                tls.TLSExtension() / tls.TLSExtECPointsFormat()]
            + self.extra_extensions()
        )
        msg1 = tls.TLSRecord(version='TLS_1_2') / \
               tls.TLSHandshakes(handshakes=[tls.TLSHandshake() / c_h])
        if self.verbose:
            msg1.show()

        # Send ClientHello and read ServerHello, ServerCertificate,
        # ServerKeyExchange, ServerHelloDone.
        self.inject_bad(fuzzer)
        resp = self.send_recv(msg1)
        if not resp.haslayer(tls.TLSCertificate):
            return False
        self.cert = resp[tls.TLSCertificate].data
        assert self.cert, "No certificate received"
        if self.verbose:
            resp.show()

        # Check that before encryption non-critical alerts are just ignored.
        self.send_12_alert(tls.TLSAlertLevel.WARNING,
                           tls.TLSAlertDescription.RECORD_OVERFLOW)

        cke_h = tls.TLSHandshakes(
            handshakes=[tls.TLSHandshake() /
                        self.sock.tls_ctx.get_client_kex_data(val=0xdeadbabe)])
        msg1 = tls.TLSRecord(version='TLS_1_2') / cke_h
        msg2 = tls.TLSRecord(version='TLS_1_2') / tls.TLSChangeCipherSpec()
        if self.verbose:
            msg1.show()
            msg2.show()

        self.inject_bad(fuzzer)
        self.sock.sendall(tls.TLS.from_records([msg1, msg2]))
        # Now we can calculate the final session checksum, send ClientFinished,
        # and receive ServerFinished.
        cf_h = tls.TLSHandshakes(
            handshakes=[tls.TLSHandshake() /
                        tls.TLSFinished(
                            data=self.sock.tls_ctx.get_verify_data())])
        msg1 = tls.TLSRecord(version='TLS_1_2') / cf_h
        if self.verbose:
            msg1.show()

        self.inject_bad(fuzzer)
        resp = self.send_recv(msg1)
        if self.verbose:
            resp.show()
            print(self.sock.tls_ctx)
        return True

    def _do_12_hs_resume(self, master_secret, ticket, fuzzer=None):
        """
        Test abbreviated TLS 1.2 handshake: establish a new TCP connection
        and send predefined TLS handshake records.
        """
        try:
            self.conn_estab()
        except socket.error:
            return False

        self.sock.tls_ctx.resume_session(master_secret)
        self.set_ticket_data(ticket)
        # Session must be non-null for resumption.
        self.session_id = '\x38' * 32

        c_h = tls.TLSClientHello(
            gmt_unix_time=0x22222222,
            random_bytes='\x11' * 28,
            session_id=self.session_id,
            session_id_length=len(self.session_id),
            cipher_suites=[
                tls.TLSCipherSuite.ECDHE_ECDSA_WITH_AES_128_GCM_SHA256] +
                self.ciphers,
                extensions=[
                    tls.TLSExtension(type=0x16), # Encrypt-then-MAC
                    tls.TLSExtension() / tls.TLSExtECPointsFormat()]
                + self.extra_extensions()
        )
        msg = tls.TLSRecord(version='TLS_1_2') / \
            tls.TLSHandshakes(
                handshakes=[tls.TLSHandshake() / c_h]
            )
        if self.verbose:
            msg.show()

        # Send ClientHello and read ServerHello, ServerCertificate,
        # ServerKeyExchange, ServerHelloDone.
        self.inject_bad(fuzzer)
        resp = self.send_recv(msg)
        if not resp.haslayer(tls.TLSChangeCipherSpec):
            return False
        if self.verbose:
            resp.show()

        msg = tls.TLSRecord(version='TLS_1_2') / tls.TLSChangeCipherSpec()
        if self.verbose:
            msg.show()
        self.inject_bad(fuzzer)
        self.sock.sendall(tls.TLS.from_records([msg]))
        # Now we can calculate the final session checksum, send ClientFinished.
        cf_h = tls.TLSHandshakes(
            handshakes=[tls.TLSHandshake() /
                        tls.TLSFinished(
                            data=self.sock.tls_ctx.get_verify_data())])
        msg = tls.TLSRecord(version='TLS_1_2') / cf_h
        if self.verbose:
            msg.show()
        self.send_recv(tls.TLS.from_records([msg]))
        return True

    def __get_host(self):
        if self.host:
            return self.host
        if self.sni:
            return self.sni[0]
        return "tempesta-tech.com"

    def _do_12_req(self, fuzzer=None):
        """ Send an HTTP request and get a response. """
        self.inject_bad(fuzzer)
        req = "GET / HTTP/1.1\r\nHost: %s\r\n\r\n" % self.__get_host()
        resp = self.send_recv(tls.TLSPlaintext(data=req))
        if resp.haslayer(tls.TLSRecord):
            self.http_resp = resp[tls.TLSRecord].data
            res = self.http_resp.startswith(GOOD_RESP)
        else:
            res = False
        if self.verbose:
            if res:
                print("==> Got response from server")
                resp.show()
                print("\n=== PASSED ===\n")
            else:
                print("\n=== FAILED ===\n")
        return res

    def do_12(self, fuzzer=None):
        with self.socket_ctx():
            if not self._do_12_hs(fuzzer):
                return False
            return self._do_12_req(fuzzer)

    def do_12_resume(self, master_secret, ticket, fuzzer=None):
        with self.socket_ctx():
            if not self._do_12_hs_resume(master_secret, ticket, fuzzer):
                return False
            return self._do_12_req(fuzzer)

class TlsHandshakeStandard:
    """
    This class uses OpenSSL backend, so all its routines less customizable,
    but are good to test TempestaTLS behavior with standard tools and libs.
    """
    def __init__(self, addr=None, port=443, io_to=0.5, verbose=False):
        if addr:
            self.addr = addr
        else:
            self.addr = tf_cfg.cfg.get('Tempesta', 'ip')
        self.port = port
        self.io_to = io_to
        self.verbose = verbose

    def try_tls_vers(self, version):
        klog = dmesg.DmesgFinder(ratelimited=False)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.io_to)
        sock.connect((self.addr, self.port))
        try:
            tls_sock = ssl.wrap_socket(sock, ssl_version=version)
        except ssl.SSLError as e:
            # Correct connection termination with PROTOCOL_VERSION alert.
            if e.reason == "TLSV1_ALERT_PROTOCOL_VERSION":
                return True
        except IOError as e:
            if self.verbose:
                print("TLS handshake failed w/o warning")
        if self.verbose:
            print("Connection of unsupported TLS 1.%d established" % version)
        return False

    def do_old(self):
        """
        Test TLS 1.0 and TLS 1.1 handshakes.
        Modern OpenSSL versions don't support SSLv{1,2,3}.0, so use TLSv1.{0,1}
        just to test that we correctly drop wrong TLS connections. We do not
        support SSL as well and any SSL record is treated as a broken TLS
        record, so fuzzing of normal TLS fields should be used to test TLS
        fields processing.
        """
        for version in (ssl.PROTOCOL_TLSv1, ssl.PROTOCOL_TLSv1_1):
            if not self.try_tls_vers(version):
                return False
        return True
