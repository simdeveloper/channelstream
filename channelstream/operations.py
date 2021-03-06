import logging
import uuid

import channelstream

from datetime import datetime

from channelstream.user import User
from channelstream.connection import Connection
from channelstream.channel import Channel

log = logging.getLogger(__name__)


def connect(username=None,
            fresh_user_state=None,
            state_public_keys=None,
            update_user_state=None,
            conn_id=None,
            channels=None,
            channel_configs=None):
    """

    :param username:
    :param fresh_user_state:
    :param state_public_keys:
    :param update_user_state:
    :param conn_id:
    :param channels:
    :param channel_configs:
    :return:
    """
    with channelstream.lock:
        if username not in channelstream.USERS:
            user = User(username)
            user.state_from_dict(fresh_user_state)
            channelstream.USERS[username] = user
        else:
            user = channelstream.USERS[username]
        if state_public_keys is not None:
            user.state_public_keys = state_public_keys

        user.state_from_dict(update_user_state)
        connection = Connection(username, conn_id)
        if connection.id not in channelstream.CONNECTIONS:
            channelstream.CONNECTIONS[connection.id] = connection
        user.add_connection(connection)
        for channel_name in channels:
            # user gets assigned to a channel
            if channel_name not in channelstream.CHANNELS:
                channel = Channel(channel_name,
                                  channel_configs=channel_configs)
                channelstream.CHANNELS[channel_name] = channel
            channelstream.CHANNELS[channel_name].add_connection(connection)
        log.info('connecting %s with uuid %s' % (username, connection.id))
        return connection, user


def subscribe(connection=None,
              channels=None,
              channel_configs=None):
    """

    :param connection:
    :param channels:
    :param channel_configs:
    :return:
    """
    user = channelstream.USERS.get(connection.username)
    subscribed_to = []
    with channelstream.lock:
        if user:
            for channel_name in channels:
                if channel_name not in channelstream.CHANNELS:
                    channel = Channel(channel_name,
                                      channel_configs=channel_configs)
                    channelstream.CHANNELS[channel_name] = channel
                is_found = channelstream.CHANNELS[
                    channel_name].add_connection(
                    connection)
                if is_found:
                    subscribed_to.append(channel_name)
    return subscribed_to


def unsubscribe(connection=None,
                unsubscribe_channels=None):
    """

    :param connection:
    :param unsubscribe_channels:
    :return:
    """
    user = channelstream.USERS.get(connection.username)
    unsubscribed_from = []
    with channelstream.lock:
        if user:
            for channel_name in unsubscribe_channels:
                if channel_name in channelstream.CHANNELS:
                    is_found = channelstream.CHANNELS[
                        channel_name].remove_connection(connection)
                    if is_found:
                        unsubscribed_from.append(channel_name)
    return unsubscribed_from


def change_user_state(user_inst=None, user_state=None):
    """

    :param user_inst:
    :param user_state:
    :return:
    """
    changed = user_inst.state_from_dict(user_state)
    # mark active
    user_inst.last_active = datetime.utcnow()
    if changed:
        channels = user_inst.get_channels()
        for channel in [c for c in channels if c.notify_state]:
            channel.send_user_state(user_inst, changed)
    return changed


def disconnect(conn_id):
    """

    :param conn_id:
    :return:
    """
    conn = channelstream.CONNECTIONS.get(conn_id)
    if conn is not None:
        conn.mark_for_gc()
        return True
    return False


def set_channel_config(channel_configs):
    """

    :param channel_configs:
    :return:
    """
    with channelstream.lock:
        for channel_name, config in channel_configs.items():
            if not channelstream.CHANNELS.get(channel_name):
                channel = Channel(channel_name,
                                  channel_configs=channel_configs)
                channelstream.CHANNELS[channel_name] = channel
            else:
                channel = channelstream.CHANNELS[channel_name]
                channel.reconfigure_from_dict(channel_configs)


def pass_message(msg, stats):
    """

    :param msg:
    :param stats:
    :return:
    """
    if msg.get('timestamp'):
        # if present lets use timestamp provided in the message
        if '.' in msg['timestamp']:
            timestmp = datetime.strptime(msg['timestamp'],
                                         '%Y-%m-%dT%H:%M:%S.%f')
        else:
            timestmp = datetime.strptime(msg['timestamp'],
                                         '%Y-%m-%dT%H:%M:%S')
    else:
        timestmp = datetime.utcnow()
    message = {'uuid': str(uuid.uuid4()).replace('-', ''),
               'user': msg.get('user'),
               'message': msg['message'],
               'type': 'message',
               'timestamp': timestmp}
    pm_users = msg.get('pm_users', [])
    total_sent = 0
    stats['total_unique_messages'] += 1
    exclude_users = msg.get('exclude_users') or []
    if msg.get('channel'):
        channel_inst = channelstream.CHANNELS.get(msg['channel'])
        if channel_inst:
            total_sent += channel_inst.add_message(message,
                                                   pm_users=pm_users,
                                                   exclude_users=exclude_users)
    elif pm_users:
        # if pm then iterate over all users and notify about new message!
        for username in pm_users:
            user_inst = channelstream.USERS.get(username)
            if user_inst:
                total_sent += user_inst.add_message(message)
    stats['total_messages'] += total_sent
