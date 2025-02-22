# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

import json
from unittest import mock

from twisted.internet import defer
from twisted.internet import reactor
from twisted.trial import unittest
from twisted.web import client
from twisted.web.http_headers import Headers
from twisted.web.iweb import IBodyProducer
from zope.interface import implementer

from buildbot.data import connector as dataconnector
from buildbot.mq import connector as mqconnector
from buildbot.reporters import telegram
from buildbot.test import fakedb
from buildbot.test.fake import fakemaster
from buildbot.test.fake import httpclientservice as fakehttpclientservice
from buildbot.test.util import dirs
from buildbot.test.util import www
from buildbot.util import bytes2unicode
from buildbot.util import unicode2bytes
from buildbot.www import auth
from buildbot.www import authz
from buildbot.www import service as wwwservice


@implementer(IBodyProducer)
class BytesProducer:
    def __init__(self, body):
        self.body = body
        self.length = len(body)

    def resumeProducing(self) -> None:
        raise NotImplementedError()

    def startProducing(self, consumer):
        consumer.write(self.body)
        return defer.succeed(None)

    def pauseProducing(self):
        pass

    def stopProducing(self):
        pass


class TelegramBot(www.RequiresWwwMixin, dirs.DirsMixin, unittest.TestCase):
    master = None

    _commands = [
        {'command': command, 'description': doc}
        for command, doc in telegram.TelegramContact.get_commands()
    ]

    @defer.inlineCallbacks
    def setup_http_service(self, bot_token):
        base_url = "https://api.telegram.org/bot" + bot_token
        self.http = yield fakehttpclientservice.HTTPClientService.getService(
            self.master, self, base_url
        )

    def expect_telegram_requests(self, bot_token):
        self.http.expect(
            "post", "/getMe", content_json={'ok': 1, 'result': {'username': 'testbot'}}
        )
        self.http.expect(
            "post", "/setMyCommands", json={'commands': self._commands}, content_json={'ok': 1}
        )
        if bot_token == 'poll':
            self.http.expect("post", "/deleteWebhook", content_json={'ok': 1})
        else:
            self.http.expect(
                "post",
                "/setWebhook",
                json={'url': bytes2unicode(self.bot_url)},
                content_json={'ok': 1},
            )

    @defer.inlineCallbacks
    def setUp(self):
        self.setUpDirs('basedir')
        master = yield fakemaster.make_master(
            self, wantDb=True, sqlite_memory=False, wantRealReactor=True, auto_shutdown=False
        )

        master.data = dataconnector.DataConnector()
        yield master.data.setServiceParent(master)

        master.config.mq = {"type": 'simple'}
        master.mq = mqconnector.MQConnector()
        yield master.mq.setServiceParent(master)
        yield master.mq.setup()
        yield master.mq.startService()

        master.config.www = {
            "port": 'tcp:0:interface=127.0.0.1',
            "debug": True,
            "auth": auth.NoAuth(),
            "authz": authz.Authz(),
            "avatar_methods": [],
            "logfileName": 'http.log',
        }
        master.www = wwwservice.WWWService()
        yield master.www.setServiceParent(master)
        yield master.www.startService()
        yield master.www.reconfigServiceWithBuildbotConfig(master.config)
        session = mock.Mock()
        session.uid = "0"
        master.www.site.sessionFactory = mock.Mock(return_value=session)

        # now that we have a port, construct the real URL and insert it into
        # the config.  The second reconfig isn't really required, but doesn't
        # hurt.
        self.url = f'http://127.0.0.1:{master.www.getPortnum()}/'
        self.url = unicode2bytes(self.url)
        master.config.buildbotURL = self.url
        yield master.www.reconfigServiceWithBuildbotConfig(master.config)

        self.master = master

        self.agent = client.Agent(reactor)

        self.bot_url = self.url + b"telegram12345:secret"
        yield self.setup_http_service('12345:secret')
        self.expect_telegram_requests('12345:secret')

        # create a telegram bot service
        tb = master.config.services['TelegramBot'] = telegram.TelegramBot(
            bot_token='12345:secret', useWebhook=True, chat_ids=[-123456], notify_events=['worker']
        )
        yield tb.setServiceParent(self.master)

        yield tb.startService()

        self.sent_messages = []

        def send_message(chat, message, **kwargs):
            self.sent_messages.append((chat, message))

        tb.bot.send_message = send_message

        @defer.inlineCallbacks
        def cleanup():
            if self.master:
                yield self.master.www.stopService()
                yield self.master.mq.stopService()
                yield self.master.test_shutdown()

        self.addCleanup(cleanup)

    @defer.inlineCallbacks
    def testWebhook(self):
        payload = unicode2bytes(
            json.dumps({
                "update_id": 12345,
                "message": {
                    "message_id": 123,
                    "from": {
                        "id": 123456789,
                        "first_name": "Alice",
                    },
                    "chat": {"id": -12345678, "title": "Wonderlands", "type": "group"},
                    "date": 1566688888,
                    "text": "/getid",
                },
            })
        )

        pg = yield self.agent.request(
            b'POST',
            self.bot_url,
            Headers({'Content-Type': ['application/json']}),
            BytesProducer(payload),
        )
        self.assertEqual(
            pg.code, 202, f"did not get 202 response for '{bytes2unicode(self.bot_url)}'"
        )
        self.assertIn('123456789', self.sent_messages[0][1])
        self.assertIn('-12345678', self.sent_messages[1][1])

    @defer.inlineCallbacks
    def testReconfig(self):
        # initial config and reconfig will issue requests twice
        self.expect_telegram_requests('12345:secret')

        tb = self.master.config.services['TelegramBot']
        yield tb.reconfigService(
            bot_token='12345:secret', useWebhook=True, chat_ids=[-123456], notify_events=['problem']
        )

    @defer.inlineCallbacks
    def testLoadState(self):
        tboid = yield self.master.db.state.getObjectId(
            'testbot', 'buildbot.reporters.telegram.TelegramWebhookBot'
        )
        yield self.master.db.insert_test_data([
            fakedb.ObjectState(
                objectid=tboid,
                name='notify_events',
                value_json='[[123456789, ["started", "finished"]]]',
            ),
            fakedb.ObjectState(
                objectid=tboid, name='missing_workers', value_json='[[123456789, [12]]]'
            ),
        ])

        tb = self.master.config.services['TelegramBot']
        yield tb.bot.loadState()
        c = tb.bot.getContact({'id': 123456789}, {'id': 123456789})
        self.assertEqual(c.channel.notify_events, {'started', 'finished'})
        self.assertEqual(c.channel.missing_workers, {12})

    @defer.inlineCallbacks
    def testSaveState(self):
        tb = self.master.config.services['TelegramBot']
        tboid = yield self.master.db.state.getObjectId(
            'testbot', 'buildbot.reporters.telegram.TelegramWebhookBot'
        )

        notify_events = yield self.master.db.state.getState(tboid, 'notify_events', ())
        missing_workers = yield self.master.db.state.getState(tboid, 'missing_workers', ())
        self.assertNotIn([99, ['cancelled']], notify_events)
        self.assertNotIn([99, [13]], missing_workers)

        tb.bot.getChannel(98)  # this channel should not be saved
        c = tb.bot.getChannel(99)
        self.assertIn(98, tb.bot.channels)
        self.assertIn(99, tb.bot.channels)

        c.notify_events = {'cancelled'}
        c.missing_workers = {13}
        yield tb.bot.saveNotifyEvents()
        yield tb.bot.saveMissingWorkers()

        notify_events = yield self.master.db.state.getState(tboid, 'notify_events', ())
        missing_workers = yield self.master.db.state.getState(tboid, 'missing_workers', ())
        self.assertNotIn(98, (c for c, _ in notify_events))
        self.assertIn([99, ['cancelled']], notify_events)
        self.assertIn([99, [13]], missing_workers)

    @defer.inlineCallbacks
    def testMissingWorker(self):
        yield self.master.db.insert_test_data([fakedb.Worker(id=1, name='local1')])

        tb = self.master.config.services['TelegramBot']
        channel = tb.bot.getChannel(-123456)
        self.assertEqual(channel.notify_events, {'worker'})

        yield self.master.data.updates.workerMissing(
            workerid=1,
            masterid=self.master.masterid,
            last_connection='long time ago',
            notify=['admin@worker.org'],
        )
        self.assertEqual(
            self.sent_messages[0][1],
            "Worker `local1` is missing. It was seen last on long time ago.",
        )

        yield self.master.data.updates.workerConnected(
            workerid=1,
            masterid=self.master.masterid,
            workerinfo={},
        )
        self.assertEqual(self.sent_messages[1][1], "Worker `local1` is back online.")
