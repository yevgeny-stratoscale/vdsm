#
# Copyright 2015 Hat, Inc.
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
from __future__ import absolute_import
from collections import defaultdict
import logging

from vdsm.network.ipwrapper import IPRoute2Error
from vdsm.network.ipwrapper import routeGet, Route, routeShowGateways
from vdsm.network.ipwrapper import route6_show_gateways
from vdsm.network.netlink import route as nl_route


def getRouteDeviceTo(destinationIP):
    """Return the name of the device leading to destinationIP or the empty
       string if none is found"""
    try:
        route = routeGet([destinationIP])[0]
    except (IPRoute2Error, IndexError):
        logging.exception('Could not route to %s', destinationIP)
        return ''

    try:
        return Route.fromText(route).device
    except ValueError:
        logging.exception('Could not parse route %s', route)
        return ''


def getDefaultGateway():
    output = routeShowGateways('main')
    return Route.fromText(output[0]) if output else None


def ipv6_default_gateway():
    output = route6_show_gateways('main')
    return Route.fromText(output[0]) if output else None


def is_default_route(gateway):
    if not gateway:
        return False

    dg = getDefaultGateway()
    return (gateway == dg.via) if dg else False


def is_ipv6_default_route(gateway):
    if not gateway:
        return False

    dg = ipv6_default_gateway()
    return (gateway == dg.via) if dg else False


def get_gateway(routes_by_dev, dev, family=4, table=nl_route._RT_TABLE_UNSPEC):
    """
    Return the default gateway for a device and an address family
    :param routes_by_dev: dictionary from device names to a list of routes.
    :type routes_by_dev: dict[str]->list[dict[str]->str]
    """
    routes = routes_by_dev[dev]

    # VDSM's source routing thread creates a separate table (with an ID derived
    # currently from an IPv4 address) for each device so we have to look for
    # the gateway in all tables (RT_TABLE_UNSPEC), not just the 'main' one.
    gateways = [r for r in routes if r['destination'] == 'none' and
                (r.get('table') == table or
                 table == nl_route._RT_TABLE_UNSPEC) and
                r['scope'] == 'global' and
                r['family'] == ('inet6' if family == 6 else 'inet')
                ]
    if not gateways:
        return '::' if family == 6 else ''
    elif len(gateways) == 1:
        return gateways[0]['gateway']
    else:
        unique_gateways = frozenset(route['gateway'] for route in gateways)
        if len(unique_gateways) == 1:
            gateway, = unique_gateways
            logging.debug('The gateway %s is duplicated for the device %s',
                          gateway, dev)
            return gateway
        else:
            # We could pick the first gateway or the one with the lowest metric
            # but, in general, there are also routing rules in the game so we
            # should probably ask the kernel somehow.
            logging.error('Multiple IPv%s gateways for the device %s in table '
                          '%s: %r', family, dev, table, gateways)
            return '::' if family == 6 else ''


def get_routes():
    """Returns all the routes data dictionaries"""
    routes = defaultdict(list)
    for route in nl_route.iter_routes():
        oif = route.get('oif')
        if oif is not None:
            routes[oif].append(route)
    return routes
