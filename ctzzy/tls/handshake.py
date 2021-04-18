import binascii
import socket
import struct
from functools import reduce

import certifi
import OpenSSL
import pyasn1_modules.rfc2560
import pyasn1_modules.rfc5280
from pyasn1.codec import ber
from pyasn1.codec.der.decoder import decode as der_decoder
from pyasn1.type.univ import ObjectIdentifier, OctetString, Sequence
from utlz import flo, namedtuple

from ctzzy.rfc6962 import SignedCertificateTimestamp
from ctzzy.sct.ee_cert import EndEntityCert, IssuerCert
from ctzzy.tls.sctlist import SignedCertificateTimestampList, TlsExtension18


def scts_from_cert(cert_der):
    '''Return list of SCTs of the SCTList SAN extension of the certificate.

    Args:
        cert_der(bytes): DER encoded ASN.1 Certificate

    Return:
        [<ctzzy.rfc6962.SignedCertificateTimestamp>, ...]
    '''
    cert, _ = der_decoder(
        cert_der, asn1Spec=pyasn1_modules.rfc5280.Certificate())
    sctlist_oid = ObjectIdentifier(value='1.3.6.1.4.1.11129.2.4.2')
    exts = []
    if 'extensions' in cert['tbsCertificate'].keys():
        exts = [extension
                for extension
                in cert['tbsCertificate']['extensions']
                if extension['extnID'] == sctlist_oid]

    if len(exts) != 0:
        extension_sctlist = exts[0]
        os_inner_der = extension_sctlist['extnValue']
        os_inner, _ = der_decoder(os_inner_der, OctetString())
        sctlist_hex = os_inner.prettyPrint().split('0x')[-1]
        sctlist_der = binascii.unhexlify(sctlist_hex)

        sctlist = SignedCertificateTimestampList(sctlist_der)
        return [SignedCertificateTimestamp(entry.sct_der)
                for entry
                in sctlist.sct_list]

    return []


def sctlist_hex_from_ocsp_pretty_print(ocsp_resp):
    sctlist_hex = None
    splitted = ocsp_resp.split('<no-name>=1.3.6.1.4.1.11129.2.4.5', 1)
    if len(splitted) > 1:
        _, after = splitted
        _, sctlist_hex_with_rest = after.split('<no-name>=0x', 1)
        sctlist_hex, _ = sctlist_hex_with_rest.split('\n', 1)
    return sctlist_hex


def scts_from_ocsp_resp(ocsp_resp_der):
    '''Return list of SCTs of the OCSP status response.

    Args:
        ocsp_resp_der(bytes): DER encoded OCSP status response

    Return:
        [<ctzzy.rfc6962.SignedCertificateTimestamp>, ...]
    '''
    if ocsp_resp_der:
        ocsp_resp,_ = der_decoder(
            ocsp_resp_der,asn1Spec=pyasn1_modules.rfc2560.OCSPResponse())

        response_bytes = ocsp_resp.getComponentByName('responseBytes')
        if response_bytes is not None:
            # os: octet string
            response_os = response_bytes.getComponentByName('response')

            der_decoder.defaultErrorState = ber.decoder.stDumpRawValue
            response,_ = der_decoder(response_os, Sequence())

            sctlist_os_hex = sctlist_hex_from_ocsp_pretty_print(
                response.prettyPrint())

            if sctlist_os_hex:
                sctlist_os_der = binascii.unhexlify(sctlist_os_hex)
                sctlist_os,_ = der_decoder(sctlist_os_der, OctetString())
                sctlist_hex = sctlist_os.prettyPrint().split('0x')[-1]
                sctlist_der = binascii.unhexlify(sctlist_hex)

                sctlist = SignedCertificateTimestampList(sctlist_der)
                return [SignedCertificateTimestamp(entry.sct_der)
                        for entry
                        in sctlist.sct_list]
    return []


def scts_from_tls_ext_18(tls_ext_18_tdf):
    '''Return list of SCTs of the TLS extension 18 server reply.

    Args:
        tls_ext_18_tdf(bytes): TDF encoded TLS extension 18 server reply.

    Return:
        [<ctzzy.rfc6962.SignedCertificateTimestamp>, ...]
    '''
    scts = []

    if tls_ext_18_tdf:
        tls_extension_18 = TlsExtension18(tls_ext_18_tdf)
        sct_list = tls_extension_18.sct_list

        scts = [SignedCertificateTimestamp(entry.sct_der)
                for entry
                in sct_list]
    return scts


TlsHandshakeResult = namedtuple(
    typename='TlsHandshakeResult',
    field_names=[
        'ee_cert_der',      # (bytes)
        'issuer_cert_der',  # (bytes) 发布者的证书？
        'more_issuer_cert_der_candidates',  # [(bytes), ...]
        'ocsp_resp_der',    # (bytes)
        'tls_ext_18_tdf',   # (bytes)
        'err',              # (str)
    ],
    lazy_vals={
        'ee_cert': lambda self: EndEntityCert(self.ee_cert_der),
        'issuer_cert': lambda self: IssuerCert(self.issuer_cert_der),
        'more_issuer_cert_candidates': lambda self: [
            IssuerCert(cert_der)
            for cert_der
            in self.more_issuer_cert_der_candidates],

        'scts_by_cert': lambda self: scts_from_cert(self.ee_cert_der),
        'scts_by_ocsp': lambda self: scts_from_ocsp_resp(self.ocsp_resp_der),
        'scts_by_tls': lambda self: scts_from_tls_ext_18(self.tls_ext_18_tdf),
    }
)


def create_socket(ctx):
    '''
    Args:
        ctx(OpenSSL.SSL.Context): OpenSSL context object
    '''
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    return OpenSSL.SSL.Connection(context=ctx, socket=raw_sock)


def create_context(scts_tls, scts_ocsp, timeout):
    '''
    Args:
        scts_tls: If True, register callback for TSL extension 18 (for SCTs)
        scts_ocsp: If True, register callback for OCSP-response (for SCTs)
        timeout(int): timeout in seconds
    '''

    def verify_callback(conn, cert, errnum, depth, ok):
        return 1  # True

    ctx = OpenSSL.SSL.Context(OpenSSL.SSL.TLSv1_2_METHOD)

    ctx.set_verify(OpenSSL.SSL.VERIFY_PEER, verify_callback)
    ca_filename = certifi.where()  # 用来返回cacert.pem的路径
    ctx.load_verify_locations(ca_filename)  # 验证证书的有效性

    ctx.tls_ext_18_tdf = None  # 不懂
    if scts_tls:
        from ctzzy.tls.openssl_build import ffi, lib  # 完全不懂

        @ffi.def_extern()
        def serverinfo_cli_parse_cb(ssl, ext_type, _in, inlen, al, arg):
            if ext_type == 18:
                def reduce_func(accum_value, current):
                    fmt = accum_value[0] + current[0]
                    values = accum_value[1] + (current[1],)
                    return fmt, values

                initializer = ('!', ())
                fmt, values = reduce(reduce_func[
                                         ('H', ext_type),
                                         ('H', inlen),
                                         (flo('{inlen}s'), bytes(ffi.buffer(_in, inlen))),
                                     ], initializer)
                ctx.tls_ext_18_tdf = struct.pack(fmt, *values)
            return 1  # True

        # register callback for TLS extension result into the SSL context
        # created with PyOpenSSL, using OpenSSL "directly"
        if not lib.SSL_CTX_add_client_custom_ext(ffi.cast('struct ssl_ctx_st *',
                                                          ctx._context),
                                                 18,
                                                 ffi.NULL, ffi.NULL, ffi.NULL,
                                                 lib.serverinfo_cli_parse_cb,
                                                 ffi.NULL):
            import sys
            sys.stderr.write('Unable to add custom extension 18\n')
            lib.ERR_print_errors_fp(sys.stderr)
            sys.exit(1)

    ctx.ocsp_resp_der = None
    if scts_ocsp:
        def ocsp_client_callback(connection, ocsp_data, data):
            ctx.ocsp_resp_der = ocsp_data
            return True

        ctx.set_ocsp_client_callback(ocsp_client_callback, data=None)

    ctx.set_timeout(timeout)

    return ctx


def do_handshake(domain, port=443, scts_tls=True, scts_ocsp=True, timeout=5):
    '''
     Args:
         domain: string with domain name,
                 for example: 'ritter.vg', or 'www.ritter.vg'
         scts_tls: If True, register callback for TSL extension 18 (for SCTs)
         scts_ocsp: If True, register callback for OCSP-response (for SCTs)
         timeout(int): timeout in seconds
     '''
    ctx = create_context(scts_tls, scts_ocsp, timeout)
    sock = create_socket(ctx)
    sock.set_tlsext_host_name(domain.encode())
    sock.request_ocsp()
    sock.set_tlsext_host_name(domain.encode())

    issuer_cert_x509 = None
    more_issuer_cert_x509_candidates = []
    ee_cert_x509 = None
    ocsp_resp_der = None
    tls_ext_18_tdf = None
    err = ''

    try:
        sock.connect((domain, port))
        sock.do_handshake()

        # ee:end entity 叶子证书，终端的证书
        ee_cert_x509 = sock.get_peer_certificate()

        # [x509, ...]
        chain_x509s = sock.get_peer_cert_chain()
        if len(chain_x509s) > 1:
            issuer_cert_x509 = chain_x509s[1]  # root cert?
        more_issuer_cert_x509_candidates = [ee_cert_x509] + chain_x509s
        print("debug: len(chain_x509s) = %d" % len(chain_x509s))

        ctx = sock.get_context()
        if scts_tls:
            if ctx.tls_ext_18_tdf:
                tls_ext_18_tdf = ctx.tls_ext_18_tdf

        if scts_ocsp:
            if ctx.ocsp_resp_der:
                ocsp_resp_der = ctx.ocsp_resp_der

    except Exception as exc:
        exc_str = str(exc)
        if exc_str == '':
            exc_str = str(type(exc))
        err = domain + ': ' + exc_str
    finally:
        sock.close()

    ee_cert_der = None
    if ee_cert_x509:
        ee_cert_der = OpenSSL.crypto.dump_certificate(
            type=OpenSSL.crypto.FILETYPE_ASN1,
            cert=ee_cert_x509)

    issuer_cert_der = None
    if issuer_cert_x509:
        # https://tools.ietf.org/html/rfc5246#section-7.4.2
        issuer_cert_der = OpenSSL.crypto.dump_certificate(
            type=OpenSSL.crypto.FILETYPE_ASN1,
            cert=issuer_cert_x509)

    more_issuer_cert_der_candidates = [
        OpenSSL.crypto.dump_certificate(type=OpenSSL.crypto.FILETYPE_ASN1,
                                        cert=cert_509)
        for cert_509
        in more_issuer_cert_x509_candidates
    ]

    return TlsHandshakeResult(ee_cert_der, issuer_cert_der,
                              more_issuer_cert_der_candidates,
                              ocsp_resp_der, tls_ext_18_tdf,
                              err)
