#!/usr/bin/python3
# System integration tests of ubuntu-network-generate. NM and networkd are
# started on the generated configuration, using emulated ethernets (veth) and
# Wifi (mac80211-hwsim). These need to be run in a VM and do change the system
# configuration.

import os
import sys
import re
import time
import subprocess
import tempfile
import unittest

exe_generate = os.environ.get('UBUNTU_NETWORK_GENERATE',
                              '/usr/lib/ubuntu-network/generate')

for program in ['wpa_supplicant', 'hostapd', 'dnsmasq']:
    if subprocess.call(['which', program], stdout=subprocess.PIPE) != 0:
        sys.stderr.write('%s is required for this test suite, but not available. Skipping\n' % program)
        sys.exit(0)


class NetworkTestBase(unittest.TestCase):
    '''Common functionality for network test cases

    setUp() creates two test wlan devices, one for a simulated access point
    (self.dev_w_ap), the other for a simulated client device
    (self.dev_w_client), and two test ethernet devices (self.dev_e_{ap,client}
    and self.dev_e2_{ap,client}.

    Each test should call self.setup_ap() or self.setup_eth() with the desired
    configuration.
    '''
    @classmethod
    def setUpClass(klass):
        # ensure we have this so that iw works
        subprocess.check_call(['modprobe', 'cfg80211'])

        # ensure NM can manage our fake eths
        os.makedirs('/run/udev/rules.d', exist_ok=True)
        with open('/run/udev/rules.d/99-nm-veth-test.rules', 'w') as f:
            f.write('ENV{ID_NET_DRIVER}=="veth", ENV{INTERFACE}=="eth42|eth43", ENV{NM_UNMANAGED}="0"\n')
        subprocess.check_call(['udevadm', 'control', '--reload'])

        # set regulatory domain "EU", so that we can use 80211.a 5 GHz channels
        out = subprocess.check_output(['iw', 'reg', 'get'], universal_newlines=True)
        m = re.match('^country (\S+):', out)
        assert m
        klass.orig_country = m.group(1)
        subprocess.check_call(['iw', 'reg', 'set', 'EU'])

    @classmethod
    def tearDownClass(klass):
        subprocess.check_call(['iw', 'reg', 'set', klass.orig_country])
        subprocess.call(['systemctl', 'stop', 'NetworkManager'])
        os.remove('/run/udev/rules.d/99-nm-veth-test.rules')

    @classmethod
    def create_devices(klass):
        '''Create Access Point and Client devices with mac80211_hwsim and veth'''

        if os.path.exists('/sys/module/mac80211_hwsim'):
            raise SystemError('mac80211_hwsim module already loaded')
        if os.path.exists('/sys/class/net/eth42'):
            raise SystemError('eth42 interface already exists')

        # create virtual ethernet devs
        subprocess.check_call(['ip', 'link', 'add', 'name', 'eth42', 'type',
                               'veth', 'peer', 'name', 'veth42'])
        klass.dev_e_ap = 'veth42'
        klass.dev_e_client = 'eth42'
        subprocess.check_call(['ip', 'link', 'add', 'name', 'eth43', 'type',
                               'veth', 'peer', 'name', 'veth43'])
        klass.dev_e2_ap = 'veth43'
        klass.dev_e2_client = 'eth43'

        # create virtual wlan devs
        before_wlan = set([c for c in os.listdir('/sys/class/net') if c.startswith('wlan')])
        subprocess.check_call(['modprobe', 'mac80211_hwsim'])
        # wait 5 seconds for fake devices to appear
        timeout = 50
        while timeout > 0:
            after_wlan = set([c for c in os.listdir('/sys/class/net') if c.startswith('wlan')])
            if len(after_wlan) - len(before_wlan) >= 2:
                break
            timeout -= 1
            time.sleep(0.1)
        else:
            raise SystemError('timed out waiting for fake devices to appear')

        devs = list(after_wlan - before_wlan)
        klass.dev_w_ap = devs[0]
        klass.dev_w_client = devs[1]

    @classmethod
    def shutdown_devices(klass):
        '''Remove test wlan devices'''

        subprocess.check_call(['rmmod', 'mac80211_hwsim'])
        subprocess.check_call(['ip', 'link', 'del', 'dev', klass.dev_e_ap])
        subprocess.check_call(['ip', 'link', 'del', 'dev', klass.dev_e2_ap])
        subprocess.call(['ip', 'link', 'del', 'dev', 'mybr'],
                        stderr=subprocess.PIPE)
        klass.dev_w_ap = None
        klass.dev_w_client = None
        klass.dev_e_ap = None
        klass.dev_e_client = None
        klass.dev_e2_ap = None
        klass.dev_e2_client = None

    def setUp(self):
        '''Create test devices and workdir'''

        self.create_devices()
        self.addCleanup(self.shutdown_devices)
        self.workdir_obj = tempfile.TemporaryDirectory()
        self.workdir = self.workdir_obj.name
        self.config = os.path.join(self.workdir, 'config')

        # create static entropy file to avoid draining/blocking on /dev/random
        self.entropy_file = os.path.join(self.workdir, 'entropy')
        with open(self.entropy_file, 'wb') as f:
            f.write(b'012345678901234567890')

    def setup_ap(self, hostapd_conf, ipv6_mode):
        '''Set up simulated access point

        On self.dev_w_ap, run hostapd with given configuration. Setup dnsmasq
        according to ipv6_mode, see start_dnsmasq().

        This is torn down automatically at the end of the test.
        '''
        # give our AP an IP
        subprocess.check_call(['ip', 'a', 'flush', 'dev', self.dev_w_ap])
        if ipv6_mode is not None:
            subprocess.check_call(['ip', 'a', 'add', '2600::1/64', 'dev', self.dev_w_ap])
        else:
            subprocess.check_call(['ip', 'a', 'add', '192.168.5.1/24', 'dev', self.dev_w_ap])

        self.start_hostapd(hostapd_conf)
        self.start_dnsmasq(ipv6_mode, self.dev_w_ap)

    def setup_eth(self, ipv6_mode, start_dnsmasq=True):
        '''Set up simulated ethernet router

        On self.dev_e_ap, run dnsmasq according to ipv6_mode, see
        start_dnsmasq().

        This is torn down automatically at the end of the test.
        '''
        # give our router an IP
        subprocess.check_call(['ip', 'a', 'flush', 'dev', self.dev_e_ap])
        if ipv6_mode is not None:
            subprocess.check_call(['ip', 'a', 'add', '2600::1/64', 'dev', self.dev_e_ap])
            subprocess.check_call(['ip', 'a', 'add', '2601::1/64', 'dev', self.dev_e2_ap])
        else:
            subprocess.check_call(['ip', 'a', 'add', '192.168.5.1/24', 'dev', self.dev_e_ap])
            subprocess.check_call(['ip', 'a', 'add', '192.168.6.1/24', 'dev', self.dev_e2_ap])
        subprocess.check_call(['ip', 'link', 'set', self.dev_e_ap, 'up'])
        subprocess.check_call(['ip', 'link', 'set', self.dev_e2_ap, 'up'])
        # we don't really want to up the client iface already, but veth doesn't
        # work otherwise (no link detected)
        subprocess.check_call(['ip', 'link', 'set', self.dev_e_client, 'up'])

        if start_dnsmasq:
            self.start_dnsmasq(ipv6_mode, self.dev_e_ap)

    def start_wpasupp(self, conf):
        '''Start wpa_supplicant on client interface'''

        w_conf = os.path.join(self.workdir, 'wpasupplicant.conf')
        with open(w_conf, 'w') as f:
            f.write('ctrl_interface=%s\nnetwork={\n%s\n}\n' % (self.workdir, conf))
        log = os.path.join(self.workdir, 'wpasupp.log')
        p = subprocess.Popen(['wpa_supplicant', '-Dwext', '-i', self.dev_w_client,
                              '-e', self.entropy_file, '-c', w_conf, '-f', log],
                             stderr=subprocess.PIPE)
        self.addCleanup(p.wait)
        self.addCleanup(p.terminate)
        # TODO: why does this sometimes take so long?
        self.poll_text(log, 'CTRL-EVENT-CONNECTED', timeout=200)

    #
    # Internal implementation details
    #

    @classmethod
    def poll_text(klass, logpath, string, timeout=50):
        '''Poll log file for a given string with a timeout.

        Timeout is given in deciseconds.
        '''
        log = ''
        while timeout > 0:
            if os.path.exists(logpath):
                break
            timeout -= 1
            time.sleep(0.1)
        assert timeout > 0, 'Timed out waiting for file %s to appear' % logpath

        with open(logpath) as f:
            while timeout > 0:
                line = f.readline()
                if line:
                    log += line
                    if string in line:
                        break
                    continue
                timeout -= 1
                time.sleep(0.1)

        assert timeout > 0, 'Timed out waiting for "%s":\n------------\n%s\n-------\n' % (string, log)

    def start_hostapd(self, conf):
        hostapd_conf = os.path.join(self.workdir, 'hostapd.conf')
        with open(hostapd_conf, 'w') as f:
            f.write('interface=%s\ndriver=nl80211\n' % self.dev_w_ap)
            f.write(conf)

        log = os.path.join(self.workdir, 'hostapd.log')
        p = subprocess.Popen(['hostapd', '-e', self.entropy_file, '-f', log, hostapd_conf],
                             stdout=subprocess.PIPE)
        self.addCleanup(p.wait)
        self.addCleanup(p.terminate)
        self.poll_text(log, '' + self.dev_w_ap + ': AP-ENABLED')

    def start_dnsmasq(self, ipv6_mode, iface):
        '''Start dnsmasq.

        If ipv6_mode is None, IPv4 is set up with DHCP. If it is not None, it
        must be a valid dnsmasq mode, i. e. a combination of "ra-only",
        "slaac", "ra-stateless", and "ra-names". See dnsmasq(8).
        '''
        if ipv6_mode is None:
            if iface == self.dev_e2_ap:
                dhcp_range = '192.168.6.10,192.168.6.200'
            else:
                dhcp_range = '192.168.5.10,192.168.5.200'
        else:
            if iface == self.dev_e2_ap:
                dhcp_range = '2601::10,2601::20'
            else:
                dhcp_range = '2600::10,2600::20'
            if ipv6_mode:
                dhcp_range += ',' + ipv6_mode

        self.dnsmasq_log = os.path.join(self.workdir, 'dnsmasq-%s.log' % iface)
        lease_file = os.path.join(self.workdir, 'dnsmasq-%s.leases' % iface)

        p = subprocess.Popen(['dnsmasq', '--keep-in-foreground', '--log-queries',
                              '--log-facility=' + self.dnsmasq_log,
                              '--conf-file=/dev/null',
                              '--dhcp-leasefile=' + lease_file,
                              '--bind-interfaces',
                              '--interface=' + iface,
                              '--except-interface=lo',
                              '--enable-ra',
                              '--dhcp-range=' + dhcp_range])
        self.addCleanup(p.wait)
        self.addCleanup(p.terminate)

        if ipv6_mode is not None:
            self.poll_text(self.dnsmasq_log, 'IPv6 router advertisement enabled')
        else:
            self.poll_text(self.dnsmasq_log, 'DHCP, IP range')

    def assert_iface_up(self, iface, expected_ip_a=None, unexpected_ip_a=None):
        '''Assert that client interface is up'''

        out = subprocess.check_output(['ip', 'a', 'show', 'dev', iface],
                                      universal_newlines=True)
        self.assertIn('state UP', out)
        if expected_ip_a:
            for r in expected_ip_a:
                self.assertRegex(out, r)
        if unexpected_ip_a:
            for r in unexpected_ip_a:
                self.assertNotRegex(out, r)

        if iface == self.dev_w_client:
            out = subprocess.check_output(['iw', 'dev', iface, 'link'],
                                          universal_newlines=True)
            # self.assertIn('Connected to ' + self.mac_w_ap, out)
            self.assertIn('SSID: fake net', out)

    def generate_and_settle(self):
        '''Generate config, launch and settle NM and networkd'''

        subprocess.check_call([exe_generate, self.config])
        subprocess.check_call(['systemctl', 'restart', 'NetworkManager'])
        subprocess.check_call(['systemctl', 'restart', 'systemd-networkd'])
        # wait until networkd is done
        for timeout in range(50):
            out = subprocess.check_output(['networkctl'], stderr=subprocess.PIPE)
            if b'pending' not in out and b'configuring' not in out and b'n/a' not in out:
                break
            time.sleep(0.1)
        else:
            self.fail('timed out waiting for networkd to settle down')


class Networkd(NetworkTestBase):
    def test_eth_and_bridge(self):
        self.setup_eth(None)
        self.start_dnsmasq(None, self.dev_e2_ap)
        with open(self.config, 'w') as f:
            f.write('''network:
  ethernets:
    %(ec)s:
      dhcp4: yes
    %(e2c)s: {}
  bridges:
    mybr:
      interfaces: [%(e2c)s]
      dhcp4: yes''' %
                    {'ec': self.dev_e_client, 'e2c': self.dev_e2_client})
        self.generate_and_settle()
        self.assert_iface_up(self.dev_e_client,
                             ['inet 192.168.5.[0-9]+/24'],
                             ['master'])
        self.assert_iface_up(self.dev_e2_client,
                             ['master mybr'],
                             ['inet '])
        self.assert_iface_up('mybr',
                             ['inet 192.168.6.[0-9]+/24'])
        lines = subprocess.check_output(['bridge', 'link', 'show', 'mybr'],
                                        universal_newlines=True).splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(self.dev_e2_client, lines[0])

        # ensure that they do not get managed by NM
        out = subprocess.check_output(['nmcli', 'dev'], universal_newlines=True)
        for i in [self.dev_e_client, self.dev_e2_client, 'mybr']:
            self.assertRegex(out, '%s\s+(ethernet|bridge)\s+unmanaged' % i)


unittest.main(testRunner=unittest.TextTestRunner(
        stream=sys.stdout, verbosity=2))