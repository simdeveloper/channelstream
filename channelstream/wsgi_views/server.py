import logging
from datetime import datetime
import gevent
import six
from gevent.queue import Queue, Empty
from pyramid.view import view_config, view_defaults
from pyramid.httpexceptions import HTTPUnauthorized
from pyramid.security import forget, NO_PERMISSION_REQUIRED

import channelstream

from channelstream import operations
from ..ext_json import json

log = logging.getLogger(__name__)


def get_connection_channels(connection):
    found_channels = []
    for channel in six.itervalues(channelstream.CHANNELS):
        user_conns = channel.connections.get(connection.username) or []
        if connection in user_conns:
            found_channels.append(channel.name)
    return sorted(found_channels)


@view_defaults(route_name='action', renderer='json', permission='access')
class ServerViews(object):
    def __init__(self, request):
        self.request = request
        self.request.handle_cors()

    def _get_channel_info(self, req_channels=None, include_history=True,
                          include_connections=False, include_users=False,
                          exclude_channels=None):
        """
        Gets channel information for req_channels or all channels
        if req_channels is not present
        :param: include_history (bool) will include message history
                for the channel
        :param: include_connections (bool) will include connection list
                for users
        :param: include_users (bool) will include user list for the channel
        :param: exclude_channels (bool) will exclude specific channels
                from info list (handy to exclude global broadcast)
        """

        if not exclude_channels:
            exclude_channels = []
        start_time = datetime.utcnow()

        json_data = {"channels": {},
                     "users": []}

        users_to_list = set()

        # select everything for empty list
        if not req_channels:
            channel_instances = six.itervalues(channelstream.CHANNELS)
        else:
            channel_instances = [channelstream.CHANNELS[c]
                                 for c in req_channels]

        for channel_inst in channel_instances:
            if channel_inst.name in exclude_channels:
                continue

            channel_info = channel_inst.get_info(
                include_history=include_history,
                include_users=include_users
            )
            json_data["channels"][channel_inst.name] = channel_info
            users_to_list.update(channel_info['users'])

        for username in users_to_list:
            json_data['users'].append(
                {'user': username,
                 'state': channelstream.USERS[username].state})
        log.info('info time: %s' % (datetime.utcnow() - start_time))
        return json_data

    def get_common_info(self, channels, info_config):
        include_history = info_config.get('include_history', True)
        include_users = info_config.get('include_users', True)
        exclude_channels = info_config.get('exclude_channels', [])
        include_connections = info_config.get('include_connections', False)
        channels_info = self._get_channel_info(
            channels, include_history=include_history,
            include_connections=include_connections,
            include_users=include_users, exclude_channels=exclude_channels)
        return channels_info

    @view_config(match_param='action=connect')
    def connect(self):
        """
        return the id of connected users - will be secured with password string
        for webapp to internally call the server - we combine conn string
        with user id, and we tell which channels the user is allowed to
        subscribe to
        """
        json_body = self.request.json_body
        username = json_body.get('username')
        fresh_user_state = json_body.get('fresh_user_state', {})
        update_user_state = json_body.get('user_state', {})
        channel_configs = json_body.get('channel_configs', {})
        state_public_keys = json_body.get('state_public_keys', None)
        conn_id = json_body.get('conn_id')
        channels = sorted(json_body.get('channels') or [])
        if username is None:
            self.request.response.status = 400
            return {'error': "No username specified"}
        connection, user = operations.connect(
            username=username,
            fresh_user_state=fresh_user_state,
            state_public_keys=state_public_keys,
            update_user_state=update_user_state,
            conn_id=conn_id,
            channels=channels,
            channel_configs=channel_configs)

        # get info config for channel information
        info_config = json_body.get('info') or {}
        channels_info = self.get_common_info(channels,
                                             info_config)
        return {'conn_id': connection.id, 'state': user.state,
                'channels': channels,
                'channels_info': channels_info}

    @view_config(match_param='action=subscribe')
    def subscribe(self, *args):
        """ call this to subscribe specific connection to new channels """
        json_body = self.request.json_body
        conn_id = json_body.get('conn_id', self.request.GET.get('conn_id'))
        connection = channelstream.CONNECTIONS.get(conn_id)
        channels = json_body.get('channels')
        channel_configs = json_body.get('channel_configs', {})
        if not connection:
            self.request.response.status = 403
            return {'error': "Unknown connection"}
        if not channels:
            self.request.response.status = 400
            return {'error': "No channels specified"}
        subscribed_to = operations.subscribe(
            connection=connection,
            channels=channels,
            channel_configs=channel_configs)

        # get info config for channel information
        current_channels = get_connection_channels(connection)
        info_config = json_body.get('info') or {}
        if current_channels:
            channels_info = self.get_common_info(current_channels, info_config)
        else:
            channels_info = {}
        return {"channels": current_channels,
                "channels_info": channels_info,
                "subscribed_to": sorted(subscribed_to)}

    @view_config(match_param='action=unsubscribe')
    def unsubscribe(self, *args):
        """ call this to unsubscribe specific connection from channels """
        json_body = self.request.json_body
        conn_id = json_body.get('conn_id', self.request.GET.get('conn_id'))
        connection = channelstream.CONNECTIONS.get(conn_id)
        unsubscribe_channels = sorted(json_body.get('channels') or [])
        if not connection:
            self.request.response.status = 403
            return {'error': "Unknown connection"}
        if not unsubscribe_channels:
            self.request.response.status = 400
            return {'error': "No channels specified"}
        unsubscribed_from = operations.unsubscribe(
            connection=connection,
            unsubscribe_channels=unsubscribe_channels)

        # get info config for channel information
        current_channels = get_connection_channels(connection)
        info_config = json_body.get('info') or {}
        if current_channels:
            channels_info = self.get_common_info(current_channels, info_config)
        else:
            channels_info = {}
        return {"channels": current_channels,
                "channels_info": channels_info,
                "unsubscribed_from": sorted(unsubscribed_from)}

    @view_config(match_param='action=listen', permission=NO_PERMISSION_REQUIRED)
    def listen(self):
        config = self.request.registry.settings
        self.conn_id = self.request.params.get('conn_id')
        connection = channelstream.CONNECTIONS.get(self.conn_id)
        if not connection:
            raise HTTPUnauthorized()
        # mark the conn active
        connection.last_active = datetime.utcnow()
        # attach a queue to connection
        connection.queue = Queue()

        def yield_response():
            # for chrome issues
            # yield ' ' * 1024
            # wait for this to wake up
            messages = []
            # block for first message - wake up after a while
            try:
                messages.extend(connection.queue.get(
                    timeout=config['wake_connections_after']))
            except Empty:
                pass
            # get more messages if enqueued takes up total 0.25s
            while True:
                try:
                    messages.extend(connection.queue.get(timeout=0.25))
                except Empty:
                    break
            cb = self.request.params.get('callback')
            if cb:
                resp = cb + '(' + json.dumps(messages) + ')'
            else:
                resp = json.dumps(messages)
            if six.PY2:
                yield resp
            else:
                yield resp.encode('utf8')

        self.request.response.app_iter = yield_response()
        return self.request.response

    @view_config(match_param='action=user_state')
    def user_state(self):
        """ set the status of specific user """
        json_body = self.request.json_body
        username = json_body.get('user')
        user_state = json_body.get('user_state')
        state_public_keys = json_body.get('state_public_keys', None)
        if not username:
            self.request.response.status = 400
            return {'error': "No username specified"}

        user_inst = channelstream.USERS.get(username)
        if not user_inst:
            self.request.response.status = 404
            return {'error': "User not found"}
        if state_public_keys is not None:
            user_inst.state_public_keys = state_public_keys
        changed = operations.change_user_state(
            user_inst=user_inst, user_state=user_state)
        return {
            'user_state': user_inst.state,
            'changed_state': changed,
            'public_keys': user_inst.state_public_keys
        }

    @view_config(match_param='action=message')
    def message(self):
        msg_list = self.request.json_body
        for msg in msg_list:
            if not msg.get('channel') and not msg.get('pm_users', []):
                continue
            gevent.spawn(operations.pass_message, msg, channelstream.stats)
        return True

    @view_config(match_param='action=disconnect',
                 permission=NO_PERMISSION_REQUIRED)
    def disconnect(self):
        json_body = self.request.json_body
        if json_body:
            conn_id = json_body.get('conn_id')
        else:
            conn_id = self.request.params.get('conn_id')

        return operations.disconnect(conn_id=conn_id)

    @view_config(match_param='action=channel_config')
    def channel_config(self):
        """ call this to reconfigure channels """
        json_body = self.request.json_body
        if not json_body:
            self.request.response.status = 400
            return {'error': "No channels specified"}

        operations.set_channel_config(channel_configs=json_body)
        channels_info = self._get_channel_info(json_body.keys(),
                                               include_history=False,
                                               include_users=False)
        return channels_info

    @view_config(route_name='admin',
                 renderer='templates/admin.jinja2', permission='access')
    def admin(self):
        return {}

    @view_config(route_name='admin_json',
                 renderer='json', permission='access')
    def admin_json(self):
        uptime = datetime.utcnow() - channelstream.stats['started_on']
        uptime = str(uptime).split('.')[0]
        remembered_user_count = len(
            [user for user in six.iteritems(channelstream.USERS)])
        active_users = [user for user in six.itervalues(channelstream.USERS)
                        if user.connections]
        unique_user_count = len(active_users)
        total_connections = sum([len(user.connections)
                                 for user in active_users])
        channels_info = self.get_common_info([], {
            'include_history': True,
            'include_users': True,
            'exclude_channels': [],
            'include_connections': True
        })
        return {
            "remembered_user_count": remembered_user_count,
            "unique_user_count": unique_user_count,
            "total_connections": total_connections,
            "total_channels": len(channelstream.CHANNELS.keys()),
            "total_messages": channelstream.stats['total_messages'],
            "total_unique_messages": channelstream.stats[
                'total_unique_messages'],
            "channels": channels_info['channels'],
            "users": [user.get_info(include_connections=True)
                      for user in active_users],
            "uptime": uptime
        }

    @view_config(match_param='action=info')
    def info(self):
        if not self.request.body:
            req_channels = channelstream.CHANNELS.keys()
            info_config = {
                'include_history': True,
                'include_users': True,
                'exclude_channels': [],
                'include_connections': True
            }
        else:
            json_body = self.request.json_body
            # get info config for channel information
            info_config = json_body.get('info') or {}
            req_channels = info_config.get('channels', None)
            info_config['include_connections'] = info_config.get(
                'include_connections', True)
        channels_info = self.get_common_info(req_channels, info_config)
        return channels_info


@view_config(
    context='channelstream.wsgi_views.wsgi_security:RequestBasicChallenge')
def admin_challenge(request):
    response = HTTPUnauthorized()
    response.headers.update(forget(request))
    return response
