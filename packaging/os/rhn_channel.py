#!/usr/bin/python

# (c) Vincent Van de Kussen
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

DOCUMENTATION = '''
---
module: rhn_channel
short_description: Adds or removes Red Hat software channels
description:
    - Adds or removes Red Hat software channels
version_added: "1.1"
author: "Vincent Van der Kussen (@vincentvdk)"
notes:
    - this module fetches the system id from RHN matching on the profile name
    - Invalid channel errors are usually due to typos, such as using a channel
    - from one architecture against a system registered with another.
requirements:
    - none
options:
    name:
        description:
            - name of the software child or base channel
        required: true
        default: null
        aliases: ['channel']
    sysname:
        description:
            - name of the system as it is known in RHN/Satellite
        required: true
        default: null
    state:
        description:
            - whether the channel should be present or not
        required: false
        default: present
        choices: ['present', 'absent']
    url:
        description:
            - The full url to the RHN/Satellite api
        required: true
    user:
        description:
            - RHN/Satellite user
        required: true
    password:
        description:
            - "the user's password"
        required: true
'''

EXAMPLES = '''
- rhn_channel: name=rhel-x86_64-server-v2vwin-6 sysname=server01 url=https://rhn.redhat.com/rpc/api user=rhnuser password=guessme
'''

import xmlrpclib
import socket
import errno
from operator import itemgetter
import re

from ansible.module_utils.basic import AnsibleModule


class InvalidChannel(KeyError):
    """Raised when a given channel exists but isn't valid for the given system

    Example cases:
        - different architectures
        - using child from base1 when system is subscribed to base2
    """
    def __init__(self, channelname, *args):
        super(InvalidChannel, self).__init__(*args)
        self.channelname = channelname

    def __str__(self):
        return 'Channel %s is not valid for this system' % self.channelname

# ------------------------------------------------------- #

def get_systemid(client, session, sysname):
    """
    Returns the Spacewalk ID matchin the system name

    Note: This is an exact match on the system name according to Spacewalk.

    TODO: This can result in multiple system entries, the module doesn't catch this exception.
    """
    systems = client.system.listUserSystems(session)
    for system in systems:
        if system['name'] == sysname:
            idres = system['id']
            idd = int(idres)
            return idd
    raise KeyError('System not found')

# ------------------------------------------------------- #

def get_channel(client, session, label):
    """
    Returns Spacewalk channel details
    """
    if label is None:
        raise KeyError('Channel not found')
    try:
        channel = client.channel.software.getDetails(session, label)
        return channel
    except xmlrpclib.Fault:
        e = sys.exc_info()[1]
        # xmlrpclib.Fault: <Fault -210: 'redstone.xmlrpc.XmlRpcFault: No such channel: foo'>
        if e.faultCode == -210:
            raise KeyError('Channel not found')
        raise

# ------------------------------------------------------- #

def set_base_channel(client, session, channelname, sys_id, check_mode):
    """
    Changes a system's base channel if needed

    Raises:
        InvalidChannel: The given channel is invalid for this system
    """
    # get channels for system
    base_channels, current_base = _base_channels(client, session, sys_id)

    if current_base == channelname:
        return dict(changed=False, msg="Channel %s already set" % channelname, channel_type='base')

    if channelname not in base_channels:
        raise InvalidChannel(channelname)

    if not check_mode:
        client.system.setBaseChannel(session, sys_id, channelname)

    return dict(changed=True, msg="Channel %s set" % channelname, channel_type='base')

# ------------------------------------------------------- #

def add_child_channel(client, session, channelname, sys_id, check_mode):
    """
    Add a child channel to a given system, noop otherwise

    Raises:
        InvalidChannel: The given channel is invalid for this system
    """
    channels = _subscribed_childs(client, session, sys_id)
    if channelname in channels:
        return dict(changed=False, msg="Channel %s already set" % channelname, channel_type='child')

    if channelname not in _subscribable_childs(client, session, sys_id):
        raise InvalidChannel(channelname)

    channels.append(channelname)
    if not check_mode:
        client.system.setChildChannels(session, sys_id, channels)

    return dict(changed=True, msg="Channel %s set" % channelname, channel_type='child')

# ------------------------------------------------------- #

def remove_child_channel(client, session, channelname, sys_id, check_mode):
    """
    Removes a child channel from a system, noop otherwise

    Raises:
        InvalidChannel: The given channel is invalid for this system
    """
    # channel in subscribable is valid for this system and not subscribed.
    # thats a successful absence check
    if channelname in _subscribable_childs(client, session, sys_id):
        return dict(changed=False, msg="Channel %s already removed" % channelname, channel_type='child')

    # channel not in subscribable suggests it is subscribed
    # however it could be an invalid channel for this system, raise if so
    channels = _subscribed_childs(client, session, sys_id)
    try:
        channels.remove(channelname)
    except ValueError:
        raise InvalidChannel(channelname)

    if not check_mode:
        client.system.setChildChannels(session, sys_id, channels)

    return dict(changed=True, msg="Channel %s removed" % channelname, channel_type='child')

# ------------------------------------------------------- #

def _subscribed_childs(client, session, sys_id):
    """
    List currently subscribed child channels for the system

    Returns: [subscribed_channels]
    """
    channels = client.system.listSubscribedChildChannels(session, sys_id)
    channels = [c['label'] for c in channels]
    return channels

# ------------------------------------------------------- #

def _subscribable_childs(client, session, sys_id):
    """
    List available child channels fro the system

    Returns: [available_channels]
    """
    channels = client.system.listSubscribableChildChannels(session, sys_id)
    channels = [c['label'] for c in channels]
    return channels


# ------------------------------------------------------- #

def _base_channels(client, session, sys_id):
    """List valid base channels for the given system and the currently in-use base

    Returns: ([channels], current_base_channel)
    """
    basechans = client.system.listSubscribableBaseChannels(session, sys_id)
    chans = []
    current_base = None
    for chan in basechans:
        chans.append(chan['label'])
        if chan['current_base']:
            current_base = chan['label']

    return chans, current_base

# ------------------------------------------------------- #


def main():

    module = AnsibleModule(
        argument_spec = dict(
            state = dict(default='present', choices=['present', 'absent']),
            name = dict(required=False, aliases=['channel']),
            sysname = dict(required=True),
            url = dict(required=True),
            user = dict(required=True),
            password = dict(required=True, aliases=['pwd']),
        ),
        supports_check_mode=True,
    )

    state = module.params['state']
    channelname = module.params['name']
    systname = module.params['sysname']
    saturl = module.params['url']
    user = module.params['user']
    password = module.params['password']

    #initialize connection
    client = xmlrpclib.Server(saturl, verbose=0)
    try:
        session = client.auth.login(user, password)
    except xmlrpclib.ProtocolError:
        e = sys.exc_info()[1]
        module.fail_json(msg='Failed to connect <%s> (invalid or incorrect url?): %s' % (saturl, e))
    except xmlrpclib.Fault:
        # <Fault 2950: 'redstone.xmlrpc.XmlRpcFault: Either the password or
        # username is incorrect.'>
        e = sys.exc_info()[1]
        module.fail_json(msg='Unable to login <%s>: %s' % (saturl, e.faultString))
    except socket.error:
        e = sys.exc_info()[1]
        if e.errno == errno.ETIMEDOUT:
            module.fail_json(msg='Timeout error while connecting to <%s>' % (saturl,))
        module.fail_json(msg='Failed to connect <%s>: %s' % (saturl, str(e)))

    # get systemid
    try:
        sys_id = get_systemid(client, session, systname)
    except KeyError:
        e = sys.exc_info()[1]
        module.fail_json(msg=e.message)

    # check channel existence
    try:
        channel = get_channel(client, session, channelname)
    except KeyError:
        e = sys.exc_info()[1]
        module.fail_json(msg=e.message)

    try:
        ret = dict()
        # is this a base_channel? (will be '' if so)
        is_base = not channel['parent_channel_label']
        if state == 'present':
            if not is_base:
                ret = add_child_channel(client, session, channelname, sys_id, module.check_mode)
            else:
                ret = set_base_channel(client, session, channelname, sys_id, module.check_mode)
        elif state == 'absent' and not is_base:
            try:
                ret = remove_child_channel(client, session, channelname, sys_id, module.check_mode)
            except InvalidChannel:
                # TODO: strict_mode? make this into an error. likely user tried to
                # remove a channel that isn't valid for this box. incorrect architecture
                # or the ilke
                ret = dict(changed=False, msg="No such channel %s available to unsubscribe" % channelname)
        else:
            # present/absent doesn't apply to base channels
            module.fail_json(msg='Cannot remove a base channel, only set a new one')
    except InvalidChannel:
        e = sys.exc_info()[1]
        module.fail_json(msg=str(e))

    # python 2.4 for RHEL5 compat
    # if we have an exception, we may not get here, too bad.
    client.auth.logout(session)
    module.exit_json(**ret)

    # python 2.6+
    #else:
    #    module.exit_json(**ret)
    #finally:
    #    client.auth.logout(session)


if __name__ == '__main__':
    main()

