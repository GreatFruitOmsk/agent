import configparser
import os
import requests
import datetime
import pytz
import pkg_resources
import platform
import socket
import netifaces
import json

from agent import iptables_helper
from agent import journal_helper
from agent import rpi_helper
from agent import security_helper
from agent.executor import Locker
from agent.rpi_helper import Confinement, detect_confinement, detect_installation
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from math import floor
from pathlib import Path
from sys import exit
import pwd
import glob

try:
    __version__ = pkg_resources.get_distribution('wott-agent')
except pkg_resources.DistributionNotFound:
    __version__ = (Path(__file__).parents[1] / 'VERSION').read_text().strip()


WOTT_ENDPOINT = os.getenv('WOTT_ENDPOINT', 'https://api.wott.io')
MTLS_ENDPOINT = WOTT_ENDPOINT.replace('api', 'mtls')
DASH_ENDPOINT = WOTT_ENDPOINT.replace('api', 'dash')
DASH_DEV_PORT = 8000
WOTT_DEV_PORT = 8001
MTLS_DEV_PORT = 8002
CONFINEMENT = detect_confinement()

# Conditional handling for if we're running
# inside a Snap.
if CONFINEMENT == Confinement.SNAP:
    CONFIG_PATH = CERT_PATH = os.getenv('SNAP_DATA')
else:
    CERT_PATH = os.getenv('CERT_PATH', '/opt/wott/certs')
    CONFIG_PATH = os.getenv('CONFIG_PATH', '/opt/wott')

if not os.path.isdir(CONFIG_PATH):
    os.makedirs(CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o711)
Locker.LOCKDIR = CONFIG_PATH

# This needs to be adjusted once we have
# changed the certificate life span from 7 days.
RENEWAL_THRESHOLD = 3

CLIENT_CERT_PATH = os.path.join(CERT_PATH, 'client.crt')
CLIENT_KEY_PATH = os.path.join(CERT_PATH, 'client.key')
CA_CERT_PATH = os.path.join(CERT_PATH, 'ca.crt')
COMBINED_PEM_PATH = os.path.join(CERT_PATH, 'combined.pem')
INI_PATH = os.path.join(CONFIG_PATH, 'config.ini')
CREDENTIALS_PATH = os.path.join(CONFIG_PATH, 'credentials')


def is_bootstrapping():
    # Create path if it doesn't exist
    if not os.path.isdir(CERT_PATH):
        os.makedirs(CERT_PATH)
    os.chmod(CERT_PATH, 0o711)

    client_cert = Path(CLIENT_CERT_PATH)

    if not client_cert.is_file():
        print('No certificate found on disk.')
        return True

    # Make sure there is no empty cert on disk
    if os.path.getsize(CLIENT_CERT_PATH) == 0:
        print('Certificate found but it is broken')
        return True

    return False


def can_read_cert():
    if not os.access(CLIENT_CERT_PATH, os.R_OK):
        print('Permission denied when trying to read the certificate file.')
        exit(1)

    if not os.access(CLIENT_KEY_PATH, os.R_OK):
        print('Permission denied when trying to read the key file.')
        exit(1)


def get_primary_ip():
    try:
        primary_interface = netifaces.gateways()['default'][netifaces.AF_INET][1]
        addrs = netifaces.ifaddresses(primary_interface)
        return addrs[netifaces.AF_INET][0]['addr']
    except (OSError, KeyError):
        return None


def get_certificate_expiration_date():
    """
    Returns the expiration date of the certificate.
    """

    can_read_cert()

    with open(CLIENT_CERT_PATH, 'r') as f:
        cert = x509.load_pem_x509_certificate(
            f.read().encode(), default_backend()
        )

    return cert.not_valid_after.replace(tzinfo=pytz.utc)


def time_for_certificate_renewal():
    """ Check if it's time for certificate renewal """
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=RENEWAL_THRESHOLD) > get_certificate_expiration_date()


def is_certificate_expired():
    return datetime.datetime.now(datetime.timezone.utc) > get_certificate_expiration_date()


def generate_device_id(debug=False):
    """
    Device ID is generated remotely.
    """
    device_id_request = requests.get(
        '{}/v0.2/generate-id'.format(WOTT_ENDPOINT)
    ).json()

    if debug:
        print("[RECEIVED] Generate Device ID: {}".format(device_id_request))

    return device_id_request['device_id']


def get_device_id(debug=False, dev=False):
    """
    Returns the WoTT Device ID (i.e. fqdn) by reading the first subject from
    the certificate on disk.
    """

    can_read_cert()

    with open(CLIENT_CERT_PATH, 'r') as f:
        cert = x509.load_pem_x509_certificate(
            f.read().encode(), default_backend()
        )

    return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def generate_cert(device_id):
    private_key = ec.generate_private_key(
        ec.SECP256R1(), default_backend()
    )
    builder = x509.CertificateSigningRequestBuilder()

    builder = builder.subject_name(x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, u'{}'.format(device_id)),
        x509.NameAttribute(NameOID.COUNTRY_NAME, u'UK'),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u'London'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, u'Web of Trusted Things, Ltd'),
    ]))

    builder = builder.add_extension(
        x509.SubjectAlternativeName(
            [x509.DNSName(u'{}'.format(device_id))]
        ),
        critical=False
    )

    csr = builder.sign(private_key, hashes.SHA256(), default_backend())

    serialized_private_key = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    serialized_csr = csr.public_bytes(serialization.Encoding.PEM)

    return {
        'csr': serialized_csr.decode(),
        'key': serialized_private_key.decode()
    }


def get_ca_cert(debug=False):
    ca = requests.get('{}/v0.2/ca-bundle'.format(WOTT_ENDPOINT))

    if debug:
        print("[RECEIVED] Get CA Cert: {}".format(ca.status_code))
        print("[RECEIVED] Get CA Cert: {}".format(ca.content))

    if not ca.ok:
        print('Failed to get CA...')
        print(ca.status_code)
        print(ca.content)
        return

    return ca.json()['ca_bundle']


def get_claim_token(debug=False, dev=False):
    setup_endpoints(dev, debug)
    can_read_cert()
    try:
        response = requests.get('{}/v0.2/claimed'.format(MTLS_ENDPOINT), cert=(CLIENT_CERT_PATH, CLIENT_KEY_PATH),
                                headers={'SSL-CLIENT-SUBJECT-DN': 'CN=' + get_device_id(),
                                         'SSL-CLIENT-VERIFY': 'SUCCESS'} if dev else {})
    except requests.exceptions.ConnectionError:
        print('Did not manage to get claim info from the server.')
        exit(2)
    if debug:
        print("[RECEIVED] Get Device Claim Info: {}".format(response))

    if response.ok:
        claim_info = response.json()
        if claim_info['claimed']:
            print('The device is already claimed.')
            exit(1)
        return claim_info['claim_token']
    else:
        print('Did not manage to get claim info from the server.')
        exit(2)


def get_fallback_token():
    config = configparser.ConfigParser()
    config.read(INI_PATH)
    return config['DEFAULT'].get('fallback_token', None)


def get_claim_url(debug=False, dev=False):
    return '{WOTT_ENDPOINT}/claim-device?device_id={device_id}&claim_token={claim_token}'.format(
        WOTT_ENDPOINT=DASH_ENDPOINT,
        device_id=get_device_id(),
        claim_token=get_claim_token(debug, dev)
    )


def get_uptime():
    """
    Returns the uptime in seconds.
    """

    with open('/proc/uptime', 'r') as f:
        uptime_seconds = float(f.readline().split()[0])

    return uptime_seconds


def get_open_ports(debug=False, dev=False):
    connections, ports = security_helper.netstat_scan()
    return ports


def send_ping(debug=False, dev=False):
    can_read_cert()

    ping = requests.get(
        '{}/v0.2/ping'.format(MTLS_ENDPOINT),
        cert=(CLIENT_CERT_PATH, CLIENT_KEY_PATH),
        headers={
            'SSL-CLIENT-SUBJECT-DN': 'CN=' + get_device_id(),
            'SSL-CLIENT-VERIFY': 'SUCCESS'
        } if dev else {}
    )
    if debug:
        print("[RECEIVED] GET Ping: {}".format(ping.status_code))
        print("[RECEIVED] GET Ping: {}".format(ping.content))
    if not ping.ok:
        print('Ping failed.')
        return

    connections, ports = security_helper.netstat_scan()
    payload = {
        'device_operating_system_version': platform.release(),
        'fqdn': socket.getfqdn(),
        'ipv4_address': get_primary_ip(),
        'uptime': get_uptime(),
        'agent_version': str(__version__),
        'confinement': CONFINEMENT.name,
        'installation': detect_installation().name
    }

    # Things we can't do within a Snap or Docker
    if CONFINEMENT not in (Confinement.SNAP, Confinement.DOCKER, Confinement.BALENA):
        payload.update({
            'processes': security_helper.process_scan(),
            'logins': journal_helper.logins_last_hour(),
            'default_password': security_helper.check_for_default_passwords(CONFIG_PATH)
        })

    # Things we cannot do in Docker
    if CONFINEMENT not in (Confinement.DOCKER, Confinement.BALENA):
        blocklist = ping.json()
        iptables_helper.block(blocklist, debug)

        payload.update({
            'selinux_status': security_helper.selinux_status(),
            'app_armor_enabled': security_helper.is_app_armor_enabled(),
            'firewall_rules': iptables_helper.dump(),
            'scan_info': ports,
            'netstat': connections
        })

    rpi_metadata = rpi_helper.detect_raspberry_pi()
    if rpi_metadata['is_raspberry_pi']:
        payload.update({
            'device_manufacturer': 'Raspberry Pi',
            'device_model': rpi_metadata['hardware_model'],
        })

    if debug:
        print("[GATHER] POST Ping: {}".format(payload))

    ping = requests.post(
        '{}/v0.2/ping'.format(MTLS_ENDPOINT),
        cert=(CLIENT_CERT_PATH, CLIENT_KEY_PATH),
        json=payload,
        headers={
            'SSL-CLIENT-SUBJECT-DN': 'CN=' + get_device_id(),
            'SSL-CLIENT-VERIFY': 'SUCCESS'
        } if dev else {}
    )

    if debug:
        print("[RECEIVED] POST Ping: {}".format(ping.status_code))
        print("[RECEIVED] POST Ping: {}".format(ping.content))

    if not ping.ok:
        print('Ping failed.')
        return


def say_hello(debug=False, dev=False):
    hello = requests.get(
        '{}/v0.2/hello'.format(MTLS_ENDPOINT),
        cert=(CLIENT_CERT_PATH, CLIENT_KEY_PATH),
        headers={
            'SSL-CLIENT-SUBJECT-DN': 'CN=' + get_device_id(),
            'SSL-CLIENT-VERIFY': 'SUCCESS'
        } if dev else {}
    )
    if not hello.ok:
        print('Hello failed.')
    return hello.json()


def sign_cert(csr, device_id, debug=False):
    """
    This is the function for the initial certificate generation.
    This is only valid for the first time. Future renewals require the
    existing certificate to renew.
    """

    payload = {
        'csr': csr,
        'device_id': device_id,
        'device_architecture': platform.machine(),
        'device_operating_system': platform.system(),
        'device_operating_system_version': platform.release(),
        'fqdn': socket.getfqdn(),
        'ipv4_address': get_primary_ip(),
    }

    crt_req = requests.post(
        '{}/v0.2/sign-csr'.format(WOTT_ENDPOINT),
        json=payload
    )

    if not crt_req.ok:
        print('Failed to submit CSR...')
        if debug:
            print("[RECEIVED] Sign Cert: {}".format(crt_req.status_code))
            print("[RECEIVED] Sign Cert: {}".format(crt_req.content))
        return

    res = crt_req.json()
    return {
        'crt': res['certificate'],
        'claim_token': res['claim_token'],
        'fallback_token': res['fallback_token'],
        'claimed': False
    }


def renew_cert(csr, device_id, debug=False):
    """
    This is the renewal function. We need to use the existing certificate to
    verify ourselves in order to get a renewed certificate
    """

    print('Attempting to renew certificate...')
    can_read_cert()

    payload = {
        'csr': csr,
        'device_id': device_id,
        'device_architecture': platform.machine(),
        'device_operating_system': platform.system(),
        'device_operating_system_version': platform.release(),
        'fqdn': socket.getfqdn(),
        'ipv4_address': get_primary_ip()
    }

    crt_req = requests.post(
        '{}/v0.2/sign-csr'.format(MTLS_ENDPOINT),
        cert=(CLIENT_CERT_PATH, CLIENT_KEY_PATH),
        json=payload
    )

    if not crt_req.ok:
        print('Failed to submit CSR...')
        if debug:
            print("[RECEIVED] Renew Cert: {}".format(crt_req.status_code))
            print("[RECEIVED] Renew Cert: {}".format(crt_req.content))
        return

    res = crt_req.json()
    return {
        'crt': res['certificate'],
        'claim_token': res['claim_token'],
        'fallback_token': res['fallback_token'],
        'claimed': res['claimed'],
    }


def renew_expired_cert(csr, device_id, debug=False):
    """
    This is the renewal function. We need to use the existing certificate to
    verify ourselves in order to get a renewed certificate
    """

    print('Attempting to renew expired certificate...')
    can_read_cert()

    payload = {
        'csr': csr,
        'device_id': device_id,
        'device_architecture': platform.machine(),
        'device_operating_system': platform.system(),
        'device_operating_system_version': platform.release(),
        'fqdn': socket.getfqdn(),
        'ipv4_address': get_primary_ip(),
        'fallback_token': get_fallback_token()
    }

    crt_req = requests.post(
        '{}/v0.2/sign-expired-csr'.format(WOTT_ENDPOINT),
        json=payload
    )

    if not crt_req.ok:
        print('Failed to submit CSR...')
        if debug:
            print("[RECEIVED] Renew expired Cert: {}".format(crt_req.status_code))
            print("[RECEIVED] Renew expired Cert: {}".format(crt_req.content))
        return

    res = crt_req.json()
    return {
        'crt': res['certificate'],
        'claim_token': res['claim_token'],
        'fallback_token': res['fallback_token'],
        'claimed': res['claimed'],
    }


def setup_endpoints(dev, debug):
    if dev:
        global WOTT_ENDPOINT, MTLS_ENDPOINT, DASH_ENDPOINT
        endpoint = os.getenv('WOTT_ENDPOINT', 'http://localhost')
        DASH_ENDPOINT = endpoint + ':' + str(DASH_DEV_PORT)
        WOTT_ENDPOINT = endpoint + ':' + str(WOTT_DEV_PORT) + '/api'
        MTLS_ENDPOINT = endpoint + ':' + str(MTLS_DEV_PORT) + '/api'
    if debug:
        print("DASH_ENDPOINT: {}\nWOTT_ENDPOINT: {}\nMTLS_ENDPOINT: {}".format(
              DASH_ENDPOINT, WOTT_ENDPOINT, MTLS_ENDPOINT
              ))


def fetch_credentials(debug, dev):

    def clear_credentials(path):
        files = glob.glob(os.path.join(path, '**/*.json'), recursive=True)
        for file in files:
            os.remove(os.path.join(path, file))
            if debug:
                print("remove...{}".format(file))

    with Locker('credentials'):
        setup_endpoints(dev, debug)
        print('Fetching credentials...')
        can_read_cert()

        credentials_req = requests.get(
            '{}/v0.2/creds'.format(MTLS_ENDPOINT),
            cert=(CLIENT_CERT_PATH, CLIENT_KEY_PATH),
            headers={
                'SSL-CLIENT-SUBJECT-DN': 'CN=' + get_device_id(),
                'SSL-CLIENT-VERIFY': 'SUCCESS'
            } if dev else {}
        )
        if not credentials_req.ok:
            print('Fetching failed.')
            if debug:
                print("[RECEIVED] Fetch credentials: code {}".format(credentials_req.status_code))
                print("[RECEIVED] Fetch credentials: {}".format(credentials_req.content))
            return
        credentials = credentials_req.json()

        print('Credentials retreived.')

        if not os.path.exists(CREDENTIALS_PATH):
            os.mkdir(CREDENTIALS_PATH, 0o711)

        if not os.path.isdir(CREDENTIALS_PATH):
            print("There is file named as our credentials dir({}), that's strange...".format(CREDENTIALS_PATH))
            exit(1)

        clear_credentials(CREDENTIALS_PATH)

        # group received credentials, by linux_user, name
        credentials_grouped = {}
        for cred in credentials:
            name = cred['name']
            owner = cred['linux_user'] if 'linux_user' in cred else ''
            if owner not in credentials_grouped:
                credentials_grouped[owner] = {}
            if name not in credentials_grouped[owner]:
                credentials_grouped[owner][name] = {}
            credentials_grouped[owner][name][cred['key']] = cred['value']

        root_pw = pwd.getpwnam("root")

        for owner in credentials_grouped:

            pw = root_pw  # if no owner, use 'root'
            if owner:
                try:
                    pw = pwd.getpwnam(owner)
                except KeyError:
                    print("Warning. There are credentials with wrong owner ({}). Skipped.".format(owner))
                    continue

            uid = pw.pw_uid
            gid = pw.pw_gid

            owner_path = CREDENTIALS_PATH if not owner else os.path.join(CREDENTIALS_PATH, owner)

            if owner and not os.path.isdir(owner_path):
                if os.path.exists(owner_path):
                    print("There is a file with name of system user in credentials directory ({}).".format(owner_path))
                    exit(1)
                os.mkdir(owner_path, 0o700)
            os.chown(owner_path, uid, gid)  # update ownership if user existence in system changed

            for name in credentials_grouped[owner]:
                credential_file_path = os.path.join(owner_path, "{}.json".format(name))
                file_credentials = {}

                for cred in credentials_grouped[owner][name]:
                    file_credentials[cred] = credentials_grouped[owner][name][cred]

                if debug:
                    print('Store credentials to {} \n '.format(credential_file_path))

                with open(credential_file_path, 'w') as outfile:
                    json.dump(file_credentials, outfile)

                os.chmod(credential_file_path, 0o400)
                os.chown(credential_file_path, uid, gid)


def write_metadata(data, rewrite_file):
    metadata_path = Path(CONFIG_PATH) / 'metadata.json'
    if rewrite_file or not metadata_path.is_file():
        with metadata_path.open('w') as metadata_file:
            json.dump(data, metadata_file)
    metadata_path.chmod(0o644)


def run(ping=True, debug=False, dev=False):
    with Locker('ping'):
        setup_endpoints(dev, debug)
        bootstrapping = is_bootstrapping()

        if bootstrapping:
            device_id = generate_device_id(debug=debug)
            print('Got WoTT ID: {}'.format(device_id))
            write_metadata({'device_id': device_id}, rewrite_file=True)
        else:
            device_id = get_device_id()
            write_metadata({'device_id': device_id}, rewrite_file=False)
            if not time_for_certificate_renewal() and not is_certificate_expired():
                if ping:
                    send_ping(debug=debug, dev=dev)
                    time_to_cert_expires = get_certificate_expiration_date() - datetime.datetime.now(datetime.timezone.utc)
                    print("Certificate expires in {} days and {} hours. No need for renewal. Renewal threshold is set to {} days.".format(
                        time_to_cert_expires.days,
                        floor(time_to_cert_expires.seconds / 60 / 60),
                        RENEWAL_THRESHOLD,
                    ))
                    exit(0)
                else:
                    return
            print('My WoTT ID is: {}'.format(device_id))

        print('Generating certificate...')
        gen_key = generate_cert(device_id)

        ca = get_ca_cert(debug=debug)
        if not ca:
            print('Unable to retrieve CA cert. Exiting.')
            exit(1)

        print('Submitting CSR...')

        if bootstrapping:
            crt = sign_cert(gen_key['csr'], device_id, debug=debug)
        elif is_certificate_expired():
            crt = renew_expired_cert(gen_key['csr'], device_id, debug=debug)
        else:
            crt = renew_cert(gen_key['csr'], device_id, debug=debug)

        if not crt:
            print('Unable to sign CSR. Exiting.')
            exit(1)

        print('Got Claim Token: {}'.format(crt['claim_token']))
        print('Claim your device: {WOTT_ENDPOINT}/claim-device?device_id={device_id}&claim_token={claim_token}'.format(
            WOTT_ENDPOINT=DASH_ENDPOINT,
            device_id=device_id,
            claim_token=crt['claim_token']
        )
        )
        print('Writing certificate and key to disk...')
        with open(CLIENT_CERT_PATH, 'w') as f:
            f.write(crt['crt'])
        os.chmod(CLIENT_CERT_PATH, 0o644)

        with open(CA_CERT_PATH, 'w') as f:
            f.write(ca)
        os.chmod(CA_CERT_PATH, 0o644)

        with open(CLIENT_KEY_PATH, 'w') as f:
            f.write(gen_key['key'])
        os.chmod(CLIENT_KEY_PATH, 0o600)

        with open(COMBINED_PEM_PATH, 'w') as f:
            f.write(gen_key['key'])
            f.write(crt['crt'])
        os.chmod(COMBINED_PEM_PATH, 0o600)

        print("Writing config...")
        config = configparser.ConfigParser()
        config['DEFAULT'] = {'fallback_token': crt['fallback_token']}
        with open(INI_PATH, 'w') as configfile:
            config.write(configfile)
        os.chmod(INI_PATH, 0o600)

        send_ping(debug=debug, dev=dev)
