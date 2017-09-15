#!/usr/bin/python
# -*- coding: utf-8 -*-

# Authors:
#   Thomas Woerner <twoerner@redhat.com>
#
# Based on ipa-client-install code
#
# Copyright (C) 2017  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

ANSIBLE_METADATA = {
    'metadata_version': '1.0',
    'supported_by': 'community',
    'status': ['preview'],
}

DOCUMENTATION = '''
---
module: ipajoin
short description: Join a machine to an IPA realm and get a keytab for the host service principal
description:
  Join a machine to an IPA realm and get a keytab for the host service principal
options:
  servers:
    description: The FQDN of the IPA servers to connect to.
    required: true
  domain:
    description: The primary DNS domain of an existing IPA deployment.
    required: true
  realm:
    description: The Kerberos realm of an existing IPA deployment.
    required: true
  hostname:
    description: The hostname of the machine to join (FQDN).
    required: true
  kdc:
    description: The name or address of the host running the KDC.
    required: true
  basedn:
    description: The basedn of the IPA server (of the form dc=example,dc=com).
    required: true
  principal:
    description: The authorized kerberos principal used to join the IPA realm.
    required: false
  password:
    description: The password to use if not using Kerberos to authenticate.
    required: false
  keytab:
    description: The path to a backed-up host keytab from previous enrollment.
    required: false
  ca_cert_file:
    description: A CA certificate to use. Do not acquire the IPA CA certificate via automated means.
    required: false
  force_join:
    description: Force enrolling the host even if host entry exists.
    required: false
  kinit_attempts:
    description: Repeat the request for host Kerberos ticket X times.
    required: false
  debug:
    description: Enable debug mode.
    required: false
author:
    - Thomas Woerner
'''

EXAMPLES = '''
# Join IPA to get the keytab
- name: Join IPA in force mode with maximum 5 kinit attempts
  ipajoin:
    servers: ["server1.example.com","server2.example.com"]
    domain: example.com
    realm: EXAMPLE.COM
    kdc: server1.example.com
    basedn: dc=example,dc=com
    hostname: client1.example.com
    principal: admin
    password: MySecretPassword
    force_join: yes
    kinit_attempts: 5

# Join IPA to get the keytab using ipadiscovery return values
- name: Join IPA
  ipajoin:
    servers: "{{ ipadiscovery.servers }}"
    domain: "{{ ipadiscovery.domain }}"
    realm: "{{ ipadiscovery.realm }}"
    kdc: "{{ ipadiscovery.kdc }}"
    basedn: "{{ ipadiscovery.basedn }}"
    hostname: "{{ ipadiscovery.hostname }}"
    principal: admin
    password: MySecretPassword
'''

RETURN = '''
'''

class Object(object):
    pass
options = Object()

import os
import sys
import gssapi
import tempfile
import inspect

from ansible.module_utils.basic import AnsibleModule
from ipapython.version import NUM_VERSION, VERSION
if NUM_VERSION < 40400:
    raise Exception, "freeipa version '%s' is too old" % VERSION
from ipalib import errors
from ipapython.dn import DN
from ipaplatform.paths import paths
try:
    from ipalib.install import sysrestore
except ImportError:
    from ipapython import sysrestore
try:
    from ipalib.install.kinit import kinit_keytab, kinit_password
except ImportError:
    from ipapython.ipautil import kinit_keytab, kinit_password
try:
    from ipaclient.install.client import configure_krb5_conf, get_ca_certs, SECURE_PATH
except ImportError:
    # Create temporary copy of ipa-client-install script (as
    # ipa_client_install.py) to be able to import the script easily and also
    # to remove the global finally clause in which the generated ccache file
    # gets removed. The ccache file will be needed in the next step.
    # This is done in a temporary directory that gets removed right after
    # ipa_client_install has been imported.
    import shutil
    temp_dir = tempfile.mkdtemp(dir="/tmp")
    sys.path.append(temp_dir)
    temp_file = "%s/ipa_client_install.py" % temp_dir

    with open("/usr/sbin/ipa-client-install", "r") as f_in:
        with open(temp_file, "w") as f_out:
            for line in f_in:
                if line.startswith("finally:"):
                    break
                f_out.write(line)
    import ipa_client_install

    shutil.rmtree(temp_dir, ignore_errors=True)
    sys.path.remove(temp_dir)

    argspec = inspect.getargspec(ipa_client_install.configure_krb5_conf)
    if argspec.keywords is None:
        def configure_krb5_conf(
                cli_realm, cli_domain, cli_server, cli_kdc, dnsok,
                filename, client_domain, client_hostname, force,
                configure_sssd):
            global options
            options.force = force
            options.sssd = configure_sssd
            return ipa_client_install.configure_krb5_conf(
                cli_realm, cli_domain, cli_server, cli_kdc, dnsok, options,
                filename, client_domain, client_hostname)
    else:
        configure_krb5_conf = ipa_client_install.configure_krb5_conf
    if NUM_VERSION < 40100:
        get_ca_cert = ipa_client_install.get_ca_cert
    else:
        get_ca_certs = ipa_client_install.get_ca_certs
    SECURE_PATH = ("/bin:/sbin:/usr/kerberos/bin:/usr/kerberos/sbin:/usr/bin:/usr/sbin")
from ipapython.ipautil import realm_to_suffix, run


import logging
logger = logging.getLogger("ipa-client-install")

def main():
    module = AnsibleModule(
        argument_spec = dict(
            servers=dict(required=True, type='list'),
            domain=dict(required=True),
            realm=dict(required=True),
            hostname=dict(required=True),
            kdc=dict(required=True),
            basedn=dict(required=True),            
            principal=dict(required=False),
            password=dict(required=False, no_log=True),
            keytab=dict(required=False),
            ca_cert_file=dict(required=False),
            force_join=dict(required=False, type='bool'),
            kinit_attempts=dict(required=False, type='int'),
            debug=dict(required=False, type='bool'),
        ),
        required_one_of = (['principal', 'keytab'],
                           ['password', 'keytab']),
        supports_check_mode = True,
    )

    module._ansible_debug = True
    servers = module.params.get('servers')
    domain = module.params.get('domain')
    realm = module.params.get('realm')
    hostname = module.params.get('hostname')
    basedn = module.params.get('basedn')
    kdc = module.params.get('kdc')
    force_join = module.params.get('force_join')
    principal = module.params.get('principal')
    password = module.params.get('password')
    keytab = module.params.get('keytab')
    ca_cert_file = module.params.get('ca_cert_file')
    kinit_attempts = module.params.get('kinit_attempts')
    debug = module.params.get('debug')

    client_domain = hostname[hostname.find(".")+1:]
    nolog = tuple()
    env = {'PATH': SECURE_PATH}
    fstore = sysrestore.FileStore(paths.IPA_CLIENT_SYSRESTORE)
    host_principal = 'host/%s@%s' % (hostname, realm)
    sssd = True

    options.ca_cert_file = ca_cert_file
    options.unattended = True
    options.principal = principal
    options.force = False
    options.password = password

    ccache_dir = None
    try:
        (krb_fd, krb_name) = tempfile.mkstemp()
        os.close(krb_fd)
        configure_krb5_conf(
            cli_realm=realm,
            cli_domain=domain,
            cli_server=servers,
            cli_kdc=kdc,
            dnsok=False,
            filename=krb_name,
            client_domain=client_domain,
            client_hostname=hostname,
            configure_sssd=sssd,
            force=False)
        env['KRB5_CONFIG'] = krb_name
        ccache_dir = tempfile.mkdtemp(prefix='krbcc')
        ccache_name = os.path.join(ccache_dir, 'ccache')
        join_args = [paths.SBIN_IPA_JOIN,
                     "-s", servers[0],
                     "-b", str(realm_to_suffix(realm)),
                     "-h", hostname]
        if debug:
            join_args.append("-d")
            env['XMLRPC_TRACE_CURL'] = 'yes'
        if force_join:
            join_args.append("-f")
        if principal:
            if principal.find('@') == -1:
                principal = '%s@%s' % (principal, realm)
            try:
                kinit_password(principal, password, ccache_name,
                               config=krb_name)
            except RuntimeError as e:
                module.fail_json(
                    msg="Kerberos authentication failed: {}".format(e))
        elif keytab:
            join_args.append("-f")
            if os.path.exists(keytab):
                try:
                    kinit_keytab(host_principal,
                                 keytab,
                                 ccache_name,
                                 config=krb_name,
                                 attempts=kinit_attempts)
                except gssapi.exceptions.GSSError as e:
                    module.fail_json(
                        msg="Kerberos authentication failed: {}".format(e))
            else:
                module.fail_json(
                    msg="Keytab file could not be found: {}".format(keytab))

        elif password:
            join_args.append("-w")
            join_args.append(password)
            nolog = (password,)

        env['KRB5CCNAME'] = os.environ['KRB5CCNAME'] = ccache_name
        # Get the CA certificate
        try:
            os.environ['KRB5_CONFIG'] = env['KRB5_CONFIG']
            if NUM_VERSION < 40100:
                get_ca_cert(fstore, options, servers[0], basedn)
            else:
                get_ca_certs(fstore, options, servers[0], basedn, realm)
            del os.environ['KRB5_CONFIG']
        except errors.FileError as e:
            module.fail_json(msg='%s' % e)
        except Exception as e:
            module.fail_json(msg="Cannot obtain CA certificate\n%s" % e)

        # Now join the domain
        result = run(
            join_args, raiseonerr=False, env=env, nolog=nolog,
            capture_error=True)
        stderr = result.error_output

        if result.returncode != 0:
            module.fail_json(msg="Joining realm failed: %s" % stderr)
        else:
            module.log("Enrolled in IPA realm %s" % realm)

        start = stderr.find('Certificate subject base is: ')
        if start >= 0:
            start = start + 29
            subject_base = stderr[start:]
            subject_base = subject_base.strip()
            subject_base = DN(subject_base)

        if principal:
            run(["kdestroy"], raiseonerr=False, env=env)

        # Obtain the TGT. We do it with the temporary krb5.conf, so that
        # only the KDC we're installing under is contacted.
        # Other KDCs might not have replicated the principal yet.
        # Once we have the TGT, it's usable on any server.
        try:
            kinit_keytab(host_principal, paths.KRB5_KEYTAB,
                         paths.IPA_DNS_CCACHE,
                         config=krb_name,
                         attempts=kinit_attempts)
            env['KRB5CCNAME'] = os.environ['KRB5CCNAME'] = paths.IPA_DNS_CCACHE
        except gssapi.exceptions.GSSError as e:
            # failure to get ticket makes it impossible to login and bind
            # from sssd to LDAP, abort installation and rollback changes
            module.fail_json(msg="Failed to obtain host TGT: %s" % e)

    finally:
        try:
            os.remove(krb_name)
        except OSError:
            module.fail_json(msg="Could not remove %s" % krb_name)
        if ccache_dir is not None:
            try:
                os.rmdir(ccache_dir)
            except OSError:
                pass
        if os.path.exists(krb_name + ".ipabkp"):
            try:
                os.remove(krb_name + ".ipabkp")
            except OSError:
                module.fail_json(msg="Could not remove %s.ipabkp" % krb_name)

    module.exit_json(changed=True)

if __name__ == '__main__':
    main()