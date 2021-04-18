'''Verify Signed Certificate Timestamps (SCTs) delivered from one or several
hosts by X.509v3 extension, TLS extension, or OCSP stapling.

A lot of the functionality originally comes and have been learned from the
script sct-verify.py written by Pier Carlo Chiodi:
https://github.com/pierky/sct-verify/blob/master/sct-verify.py (under GPL)

He also described the SCT verification steps very well in his blog:
https://blog.pierky.com/certificate-transparency-manually-verify-sct-with-openssl/
'''

import argparse
import logging
import struct
import os

from pathlib import Path

from utlz import first_paragraph, text_with_newlines

from ctzzy.tls.handshake import do_handshake
from ctzzy.ctlog import download_log_list, get_log_list, read_log_list
from ctzzy.ctlog import Logs, set_operator_names
from ctzzy.sct.verification import verify_scts
from ctzzy.sct.signature_input import create_signature_input_precert
from ctzzy.sct.signature_input import create_signature_input
from ctzzy.utils.string import to_hex
from ctzzy.utils.logger import VERBOSE, init_logger, setup_logging, logger
from ctzzy._version import __version__


def create_parser():
    parser = argparse.ArgumentParser(description=first_paragraph(__doc__))
    parser.add_argument('-v', '--version',
                        action='version',
                        default=False,
                        version=__version__,
                        help='print version number')
    parser.add_argument('-df','--domain-file',
                        required=True,
                        help="A file that contains a list of domain names.")
    meg = parser.add_mutually_exclusive_group() # 创建一个互斥组。 argparse 将会确保互斥组中只有一个参数在命令行中可用
    meg.add_argument('--short',
                     dest='loglevel',
                     action='store_const',
                     const=logging.INFO,
                     default=VERBOSE,  # default loglevel if nothing set
                     help='show short results and warnings/errors only')
    meg.add_argument('--debug',
                     dest='loglevel',
                     action='store_const',
                     const=logging.DEBUG,
                     help='show more for diagnostic purposes')

    meg1 = parser.add_mutually_exclusive_group()
    meg1.add_argument('--cert-only',
                      dest='verification_tasks',
                      action='store_const',
                      const=[verify_scts_by_cert],
                      default=[verify_scts_by_cert,
                               verify_scts_by_tls,
                               verify_scts_by_ocsp],
                      help='only verify SCTs included in the certificate')
    meg1.add_argument('--tls-only',
                      dest='verification_tasks',
                      action='store_const',
                      const=[verify_scts_by_tls], #调用对应的函数
                      help='only verify SCTs gathered from TLS handshake')
    meg1.add_argument('--ocsp-only',
                      dest='verification_tasks',
                      action='store_const',
                      const=[verify_scts_by_ocsp],
                      help='only verify SCTs gathered via OCSP request')

    meg2 = parser.add_mutually_exclusive_group()
    meg2.add_argument('--log-list',
                      dest='log_list_filename',
                      metavar='<filename>',
                      help='filename of a log list in JSON format')
    meg2.add_argument('--latest-logs',
                      dest='fetch_ctlogs',
                      action='store_const',
                      const=download_log_list,
                      default=get_log_list,
                      help='for SCT verification against known CT Logs '
                           "(compliant with Chrome's CT policy) "
                           'download latest version of '
                           'https://www.gstatic.com/ '
                           'ct/log_list/v2/all_logs_list.json '
                           '-- use built-in log list really_all_logs.json '
                           'from 2020-04-05 if --latest-logs or --log-list '
                           'are not set')
    return parser


def verify_scts_by_cert(res, ctlogs):
    '''
    Args:
        res(ctzzy.tls.TlsHandshakeResult)
        ctlogs([<ctzzy.ctlog.Log>, ...])

    Return:
        [<ctzzy.sct.verification.SctVerificationResult>, ...]
    '''
    return verify_scts(
        ee_cert=res.ee_cert,
        scts=res.scts_by_cert,
        logs=ctlogs, # CT 列表
        issuer_cert=res.issuer_cert,
        more_issuer_cert_candidates=res.more_issuer_cert_candidates,
        sign_input_func=create_signature_input_precert)


def verify_scts_by_tls(res, ctlogs):
    '''
    Args:
        res(ctzzy.tls.TlsHandshakeResult)
        ctlogs([<ctzzy.ctlog.Log>, ...])

    Return:
        [<ctzzy.sct.verification.SctVerificationResult>, ...]
    '''
    return verify_scts(
        ee_cert=res.ee_cert,
        scts=res.scts_by_tls,
        logs=ctlogs,
        issuer_cert=None,
        more_issuer_cert_candidates=None,
        sign_input_func=create_signature_input)


def verify_scts_by_ocsp(res, ctlogs):
    '''
    Args:
        res(ctzzy.tls.TlsHandshakeResult)
        ctlogs([<ctzzy.ctlog.Log>, ...])

    Return:
        [<ctzzy.sct.verification.SctVerificationResult>, ...]
    '''
    return verify_scts(
        ee_cert=res.ee_cert,
        scts=res.scts_by_ocsp,
        logs=ctlogs,
        issuer_cert=None,
        more_issuer_cert_candidates=None,
        sign_input_func=create_signature_input)


# for more convenient command output
verify_scts_by_cert.__name__ = 'SCTs by Certificate'
verify_scts_by_tls.__name__ = 'SCTs by TLS'
verify_scts_by_ocsp.__name__ = 'SCTs by OCSP'


def show_signature_verbose(signature):
    '''Print out signature as hex string to logger.verbose.

    Args:
        signature(bytes)
    '''
    sig_offset = 0
    while sig_offset < len(signature):
        if len(signature) - sig_offset > 16:
            bytes_to_read = 16
        else:
            bytes_to_read = len(signature) - sig_offset
        sig_bytes = struct.unpack_from('!%ds' % bytes_to_read,
                                       signature,
                                       sig_offset)[0]
        if sig_offset == 0:
            logger.verbose('Signature : %s' % to_hex(sig_bytes))
        else:
            logger.verbose('            %s' % to_hex(sig_bytes))
        sig_offset = sig_offset + bytes_to_read


def show_verification(verification):
    '''
    Args:
        verification(ctzzy.sct.verification.SctVerificationResult)
    '''
    sct = verification.sct

    sct_log_id1, sct_log_id2 = [to_hex(val)
                                for val
                                in struct.unpack("!16s16s", sct.log_id.tdf)]
    logger.info('```')
    logger.verbose('=' * 59)
    logger.verbose('Version   : %s' % sct.version_hex)
    logger.verbose('LogID     : %s' % sct_log_id1)
    logger.verbose('            %s' % sct_log_id2)
    logger.info('LogID b64 : %s' % sct.log_id_b64)
    logger.verbose('Timestamp : %s (%s)' % (sct.timestamp, sct.timestamp_hex))
    logger.verbose(
        'Extensions: %d (%s)' % (sct.extensions_len, sct.extensions_len_hex))
    logger.verbose('Algorithms: %s/%s (hash/sign)' % (sct.signature_alg_hash_hex, sct.signature_algorithm_signature))

    show_signature_verbose(sct.signature)
    prefix = 'Sign. b64 : '
    logger.info(prefix + text_with_newlines(sct.signature_b64, line_length=16 * 3,
                                            newline='\n' + ' ' * len(prefix)))

    logger.verbose('--')  # visual gap between sct infos and verification result

    log = verification.log
    if log is None:
        logger.info('Log not found\n')
    else:
        logger.info('Log found : %s' % log.description)
        logger.verbose('Operator  : %s' % log.operated_by['name'])
        logger.info('Chrome    : %s' % log.scts_accepted_by_chrome)

    if verification.verified:
        logger.info('Result    : Verified OK')
        logger.verbose('Result    : Verified OK')
    else:
        logger.info('Result    : Verification Failure')
        logger.verbose('Result    : Verification Failure')

    logger.info('```\n')


def scrape_and_verify_scts(hostname, verification_tasks, ctlogs):
    logger.info('# %s\n' % hostname)

    res = do_handshake(hostname,443,
                       scts_tls=(verify_scts_by_tls in verification_tasks),
                       scts_ocsp=(verify_scts_by_ocsp in verification_tasks))
    if res.ee_cert_der:
        logger.debug('got certificate\n')
        if res.ee_cert.is_ev_cert:
            logger.info('* EV cert')
            logger.verbose('EV cert     : True')
        else:
            logger.info('* no EV cert')
            logger.verbose('EV cert     : False')
        if res.ee_cert.is_letsencrypt_cert:
            logger.info("* issued by Let's Encrypt\n")
            logger.verbose("issued by Let's Encrypt: True")
        else:
            logger.info("* not issued by Let's Encrypt\n")
            logger.verbose("issued by Let's Encrypt: False")

    if res.err:
        logger.warning(res.err)
    else:
        for verification_task in verification_tasks:
            logger.info('## %s\n' % verification_task.__name__)
            logger.verbose('## %s\n' % verification_task.__name__)
            verifications = verification_task(res, ctlogs)
            if verifications:
                for verification in verifications:
                    show_verification(verification)
            elif res.ee_cert_der is not None:
                logger.info('no SCTs\n')


def main():
    init_logger()
    parser = create_parser()
    args = parser.parse_args()
    setup_logging(args.loglevel)
    logger.debug(args)

    # set ctlogs, type: [<ctzzy.ctlog.Log>, ...]
    all_dict = args.fetch_ctlogs()  # call download_log_list() to populate the list
    set_operator_names(all_dict)
    ctlogs = Logs([all_dict])
    if args.log_list_filename:
        logs_dict = read_log_list(args.log_list_filename)
        set_operator_names(logs_dict)
        ctlogs = Logs(logs_dict['logs'])
    if os.path.isfile(Path(args.df)):
        with open(args.df,'r') as f:
            host = f.readline()
            while host:
                scrape_and_verify_scts(host, args.verification_tasks, ctlogs)
                host = f.readline()
        f.close()
    else:
        print("Please enter the correct file!")


if __name__ == '__main__':
    # when calling `verify-scts` directly from source as pointed out in the
    # README.md (section Devel-Commands) the c-code part needs to be compiled,
    # else the import of the c-module `ctzzy.tls.handshake_openssl` would fail.
    import ctzzy.tls.openssl_build
    ctzzy.tls.openssl_build.compile()

    main()