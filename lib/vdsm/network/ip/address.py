# Copyright 2016-2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

import logging
import socket
import struct

from vdsm.common import cache
from vdsm.network import errors as ne
from vdsm.network import ipwrapper
from vdsm.network import sysctl
from vdsm.network.errors import ConfigNetworkError
# TODO: vdsm.network.netinfo.addresses should move to this module.
from vdsm.network.netinfo import addresses


class IPv4(object):
    def __init__(self, address=None, netmask=None, gateway=None,
                 defaultRoute=None, bootproto=None):
        if address:
            if not netmask:
                raise ConfigNetworkError(ne.ERR_BAD_ADDR, 'Must specify '
                                         'netmask to configure ip for '
                                         'network.')
            IPv4.validateAddress(address)
            IPv4.validateNetmask(netmask)
            if gateway:
                IPv4.validateGateway(gateway)
        else:
            if netmask or gateway:
                raise ConfigNetworkError(ne.ERR_BAD_ADDR, 'Specified netmask '
                                         'or gateway but not ip address.')
        if address and bootproto == 'dhcp':
            raise ConfigNetworkError(
                ne.ERR_BAD_ADDR, 'Mixing of static and dynamic IPv4 '
                'configuration is currently not supported.')
        self.address = address
        self.netmask = netmask
        self.gateway = gateway
        self.defaultRoute = defaultRoute
        self.bootproto = bootproto

    def __bool__(self):
        return bool(self.address or self.bootproto)

    def __nonzero__(self):  # TODO: drop when py2 is no longer needed
        return self.__bool__()

    def __repr__(self):
        return 'IPv4(%s, %s, %s, %s, %s)' % (self.address, self.netmask,
                                             self.gateway, self.defaultRoute,
                                             self.bootproto)

    @staticmethod
    def validateAddress(address):
        try:
            socket.inet_pton(socket.AF_INET, address)
        except socket.error:
            raise ConfigNetworkError(ne.ERR_BAD_ADDR, '%r is not a valid IPv4 '
                                     'address.' % address)

    @staticmethod
    def validateNetmask(netmask):
        try:
            IPv4.validateAddress(netmask)
        except ConfigNetworkError as cne:
            cne.message = '%r is not a valid IPv4 netmask.' % netmask
            raise
        num = struct.unpack('>I', socket.inet_aton(netmask))[0]
        if num & (num - 1) != (num << 1) & 0xffffffff:
            raise ConfigNetworkError(ne.ERR_BAD_ADDR, '%r is not a valid IPv4 '
                                     'netmask.' % netmask)

    @staticmethod
    def validateGateway(gateway):
        '''Validates the gateway form.'''
        try:
            IPv4.validateAddress(gateway)
        except ConfigNetworkError as cne:
            cne.message = '%r is not a valid IPv4 gateway' % gateway
            raise


class IPv6(object):
    def __init__(self, address=None, gateway=None, defaultRoute=None,
                 ipv6autoconf=None, dhcpv6=None):
        if address:
            IPv6.validateAddress(address)
            if gateway:
                IPv6.validateGateway(gateway)
        elif gateway:
            raise ConfigNetworkError(ne.ERR_BAD_ADDR, 'Specified gateway but '
                                     'not ip address.')
        if address and (ipv6autoconf or dhcpv6):
            raise ConfigNetworkError(
                ne.ERR_BAD_ADDR, 'Mixing of static and dynamic IPv6 '
                'configuration is currently not supported.')
        self.address = address
        self.gateway = gateway
        self.defaultRoute = defaultRoute
        self.ipv6autoconf = ipv6autoconf
        self.dhcpv6 = dhcpv6

    def __bool__(self):
        return bool(self.address or self.ipv6autoconf or self.dhcpv6)

    def __nonzero__(self):  # TODO: drop when py2 is no longer needed
        return self.__bool__()

    def __repr__(self):
        return 'IPv6(%s, %s, %s, %s, %s)' % (
            self.address, self.gateway, self.defaultRoute, self.ipv6autoconf,
            self.dhcpv6)

    @staticmethod
    def validateAddress(address):
        addr = address.split('/', 1)
        try:
            socket.inet_pton(socket.AF_INET6, addr[0])
        except socket.error:
            raise ConfigNetworkError(ne.ERR_BAD_ADDR, '%r is not a valid IPv6 '
                                     'address.' % address)
        if len(addr) == 2:
            IPv6.validatePrefixlen(addr[1])

    @staticmethod
    def validatePrefixlen(prefixlen):
        try:
            prefixlen = int(prefixlen)
            if prefixlen < 0 or prefixlen > 127:
                raise ConfigNetworkError(ne.ERR_BAD_ADDR, '%r is not valid '
                                         'IPv6 prefixlen.' % prefixlen)
        except ValueError:
            raise ConfigNetworkError(ne.ERR_BAD_ADDR, '%r is not valid '
                                     'IPv6 prefixlen.' % prefixlen)

    @staticmethod
    def validateGateway(gateway):
        try:
            IPv6.validateAddress(gateway)
        except ConfigNetworkError as cne:
            cne.message = '%r is not a valid IPv6 gateway.'
            raise


def addrs_info(dev, ipaddrs=None, ipv4_gateway=None):
    return addresses.getIpInfo(dev, ipaddrs, ipv4_gateway)


def enable_ipv6_local_auto(dev):
    sysctl.enable_ipv6_local_auto(dev)


def disable_ipv6_local_auto(dev):
    sysctl.disable_ipv6_local_auto(dev)


def disable_ipv6(iface):
    if ipv6_supported():
        sysctl.disable_ipv6(iface)


def add(iface, ipv4, ipv6):
    if ipv4:
        _add_ipv4_address(iface, ipv4)
    if ipv6:
        sysctl.enable_ipv6(iface)
        flush(iface, family=6)
        _add_ipv6_address(iface, ipv6)
    elif ipv6_supported():
        sysctl.disable_ipv6(iface)


def _add_ipv4_address(iface, ipv4):
    if ipv4.address:
        ipwrapper.addrAdd(iface, ipv4.address, ipv4.netmask)
        if ipv4.gateway and ipv4.defaultRoute:
            set_default_route(ipv4.gateway, family=4)


def _add_ipv6_address(iface, ipv6):
    if ipv6.address:
        ipv6addr, ipv6netmask = ipv6.address.split('/')
        ipwrapper.addrAdd(iface, ipv6addr, ipv6netmask, family=6)
        if ipv6.gateway and ipv6.defaultRoute:
            set_default_route(ipv6.gateway, family=6, dev=iface)
    if ipv6.ipv6autoconf is not None:
        with open('/proc/sys/net/ipv6/conf/%s/autoconf' % iface,
                  'w') as ipv6_autoconf:
            ipv6_autoconf.write('1' if ipv6.ipv6autoconf else '0')


def set_default_route(gateway, family, dev=None):
    try:
        ipwrapper.routeAdd(['default', 'via', gateway], family=family, dev=dev)
    except ipwrapper.IPRoute2Error:  # there already is a default route
        logging.warning(
            'Existing default route will be removed so a new one can be set.')
        ipwrapper.routeDel(['default'], family=family)
        ipwrapper.routeAdd(['default', 'via', gateway], family=family, dev=dev)


def flush(iface, family='both'):
    ipwrapper.addrFlush(iface, family)


@cache.memoized
def ipv6_supported():
    """
    Check if IPv6 is disabled by kernel arguments (or even compiled out).
    """
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    except socket.error:
        return False
    return True


def prefix2netmask(prefix):
    if not 0 <= prefix <= 32:
        raise ValueError('%s is not a valid prefix value. It must be between '
                         '0 and 32' % prefix)
    return socket.inet_ntoa(
        struct.pack("!I", int('1' * prefix + '0' * (32 - prefix), 2)))
