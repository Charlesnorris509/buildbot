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


from twisted.internet import defer
from twisted.trial import unittest

from buildbot import config
from buildbot.schedulers import timed
from buildbot.test.reactor import TestReactorMixin
from buildbot.test.util import scheduler


class TestException(Exception):
    pass


class Periodic(scheduler.SchedulerMixin, TestReactorMixin, unittest.TestCase):
    OBJECTID = 23
    SCHEDULERID = 3

    @defer.inlineCallbacks
    def setUp(self):
        self.setup_test_reactor()
        yield self.setUpScheduler()

    @defer.inlineCallbacks
    def makeScheduler(self, firstBuildDuration=0, firstBuildError=False, exp_branch=None, **kwargs):
        self.sched = sched = timed.Periodic(**kwargs)

        yield self.attachScheduler(self.sched, self.OBJECTID, self.SCHEDULERID)

        # keep track of builds in self.events
        self.events = []

        def addBuildsetForSourceStampsWithDefaults(
            reason, sourcestamps, waited_for=False, properties=None, builderNames=None, **kw
        ):
            self.assertIn('Periodic scheduler named', reason)
            # TODO: check branch
            isFirst = not self.events
            if self.reactor.seconds() == 0 and firstBuildError:
                raise TestException()
            self.events.append(f'B@{int(self.reactor.seconds())}')
            if isFirst and firstBuildDuration:
                d = defer.Deferred()
                self.reactor.callLater(firstBuildDuration, d.callback, None)
                return d
            return defer.succeed(None)

        sched.addBuildsetForSourceStampsWithDefaults = addBuildsetForSourceStampsWithDefaults

        # handle state locally
        self.state = {}

        def getState(k, default):
            return defer.succeed(self.state.get(k, default))

        sched.getState = getState

        def setState(k, v):
            self.state[k] = v
            return defer.succeed(None)

        sched.setState = setState

        return sched

    # tests

    def test_constructor_invalid(self):
        with self.assertRaises(config.ConfigErrors):
            timed.Periodic(name='test', builderNames=['test'], periodicBuildTimer=-2)

    @defer.inlineCallbacks
    def test_constructor_no_reason(self):
        sched = yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=10)
        yield sched.configureService()
        self.assertEqual(sched.reason, "The Periodic scheduler named 'test' triggered this build")

    @defer.inlineCallbacks
    def test_constructor_reason(self):
        sched = yield self.makeScheduler(
            name='test', builderNames=['test'], periodicBuildTimer=10, reason="periodic"
        )
        yield sched.configureService()
        self.assertEqual(sched.reason, "periodic")

    @defer.inlineCallbacks
    def test_iterations_simple(self):
        sched = yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=13)
        yield self.master.startService()

        sched.activate()
        self.reactor.advance(0)  # let it trigger the first build
        while self.reactor.seconds() < 30:
            self.reactor.advance(1)
        self.assertEqual(self.events, ['B@0', 'B@13', 'B@26'])
        self.assertEqual(self.state.get('last_build'), 26)

        yield sched.deactivate()

    @defer.inlineCallbacks
    def test_iterations_simple_branch(self):
        yield self.makeScheduler(
            exp_branch='newfeature',
            name='test',
            builderNames=['test'],
            periodicBuildTimer=13,
            branch='newfeature',
        )

        yield self.master.startService()

        self.reactor.advance(0)  # let it trigger the first build
        while self.reactor.seconds() < 30:
            self.reactor.advance(1)
        self.assertEqual(self.events, ['B@0', 'B@13', 'B@26'])
        self.assertEqual(self.state.get('last_build'), 26)

    @defer.inlineCallbacks
    def test_iterations_long(self):
        yield self.makeScheduler(
            name='test', builderNames=['test'], periodicBuildTimer=10, firstBuildDuration=15
        )  # takes a while to start a build

        yield self.master.startService()
        self.reactor.advance(0)  # let it trigger the first (longer) build
        while self.reactor.seconds() < 40:
            self.reactor.advance(1)
        self.assertEqual(self.events, ['B@0', 'B@15', 'B@25', 'B@35'])
        self.assertEqual(self.state.get('last_build'), 35)

    @defer.inlineCallbacks
    def test_start_build_error(self):
        yield self.makeScheduler(
            name='test', builderNames=['test'], periodicBuildTimer=10, firstBuildError=True
        )  # error during first build start

        yield self.master.startService()
        self.reactor.advance(0)  # let it trigger the first (error) build
        while self.reactor.seconds() < 40:
            self.reactor.advance(1)
        self.assertEqual(self.events, ['B@10', 'B@20', 'B@30', 'B@40'])
        self.assertEqual(self.state.get('last_build'), 40)
        self.assertEqual(1, len(self.flushLoggedErrors(TestException)))

    @defer.inlineCallbacks
    def test_iterations_stop_while_starting_build(self):
        sched = yield self.makeScheduler(
            name='test', builderNames=['test'], periodicBuildTimer=13, firstBuildDuration=6
        )  # takes a while to start a build

        yield self.master.startService()
        self.reactor.advance(0)  # let it trigger the first (longer) build
        self.reactor.advance(3)  # get partway into that build

        d = sched.deactivate()  # begin stopping the service
        d.addCallback(lambda _: self.events.append(f'STOP@{int(self.reactor.seconds())}'))

        # run the clock out
        while self.reactor.seconds() < 40:
            self.reactor.advance(1)

        # note that the deactivate completes after the first build completes, and no
        # subsequent builds occur
        self.assertEqual(self.events, ['B@0', 'STOP@6'])
        self.assertEqual(self.state.get('last_build'), 0)

        yield d

    @defer.inlineCallbacks
    def test_iterations_with_initial_state(self):
        yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=13)
        # so next build should start in 6s
        self.state['last_build'] = self.reactor.seconds() - 7

        yield self.master.startService()
        self.reactor.advance(0)  # let it trigger the first build
        while self.reactor.seconds() < 30:
            self.reactor.advance(1)
        self.assertEqual(self.events, ['B@6', 'B@19'])
        self.assertEqual(self.state.get('last_build'), 19)

    @defer.inlineCallbacks
    def test_getNextBuildTime_None(self):
        sched = yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=13)
        yield sched.configureService()
        # given None, build right away
        t = yield sched.getNextBuildTime(None)
        self.assertEqual(t, 0)

    @defer.inlineCallbacks
    def test_getNextBuildTime_given(self):
        sched = yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=13)
        yield sched.configureService()
        # given a time, add the periodicBuildTimer to it
        t = yield sched.getNextBuildTime(20)
        self.assertEqual(t, 33)

    @defer.inlineCallbacks
    def test_enabled_callback(self):
        sched = yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=13)
        yield self.master.startService()
        expectedValue = not sched.enabled
        yield sched._enabledCallback(None, {'enabled': not sched.enabled})
        self.assertEqual(sched.enabled, expectedValue)
        expectedValue = not sched.enabled
        yield sched._enabledCallback(None, {'enabled': not sched.enabled})
        self.assertEqual(sched.enabled, expectedValue)

        yield sched.deactivate()

    @defer.inlineCallbacks
    def test_disabled_activate(self):
        sched = yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=13)
        yield sched._enabledCallback(None, {'enabled': not sched.enabled})
        self.assertEqual(sched.enabled, False)
        r = yield sched.activate()
        self.assertEqual(r, None)

    @defer.inlineCallbacks
    def test_disabled_deactivate(self):
        sched = yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=13)
        yield self.master.startService()
        yield sched._enabledCallback(None, {'enabled': not sched.enabled})
        self.assertEqual(sched.enabled, False)
        yield sched.deactivate()

    @defer.inlineCallbacks
    def test_disabled_start_build(self):
        sched = yield self.makeScheduler(name='test', builderNames=['test'], periodicBuildTimer=13)
        yield sched._enabledCallback(None, {'enabled': not sched.enabled})
        self.assertEqual(sched.enabled, False)
        r = yield sched.startBuild()
        self.assertEqual(r, None)
