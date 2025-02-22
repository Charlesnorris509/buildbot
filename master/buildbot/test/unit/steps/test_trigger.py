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
from __future__ import annotations

from unittest.mock import Mock

from twisted.internet import defer
from twisted.internet import reactor
from twisted.python import failure
from twisted.trial import unittest
from zope.interface import implementer

from buildbot import config
from buildbot import interfaces
from buildbot.process import properties
from buildbot.process.results import CANCELLED
from buildbot.process.results import EXCEPTION
from buildbot.process.results import FAILURE
from buildbot.process.results import SUCCESS
from buildbot.steps import trigger
from buildbot.test import fakedb
from buildbot.test.reactor import TestReactorMixin
from buildbot.test.steps import TestBuildStepMixin
from buildbot.test.util.interfaces import InterfaceTests


@implementer(interfaces.ITriggerableScheduler)
class FakeTriggerable:
    triggered_with = None
    result = SUCCESS
    bsid = 1
    brids: dict[int, int] = {}
    exception = False
    never_finish = False

    def __init__(self, name):
        self.name = name

    def trigger(
        self,
        waited_for,
        sourcestamps=None,
        set_props=None,
        parent_buildid=None,
        parent_relationship=None,
    ):
        self.triggered_with = (waited_for, sourcestamps, set_props.properties)
        idsDeferred = defer.Deferred()
        idsDeferred.callback((self.bsid, self.brids))
        resultsDeferred = defer.Deferred()
        if not self.never_finish:
            if self.exception:
                reactor.callLater(0, resultsDeferred.errback, RuntimeError('oh noes'))
            else:
                reactor.callLater(0, resultsDeferred.callback, (self.result, self.brids))
        return (idsDeferred, resultsDeferred)


class TriggerableInterfaceTest(unittest.TestCase, InterfaceTests):
    def test_interface(self):
        self.assertInterfacesImplemented(FakeTriggerable)


class FakeSourceStamp:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def asDict(self, includePatch=True):
        return self.__dict__.copy()


class FakeSchedulerManager:
    pass


# Magic numbers that relate brid to other build settings
def BRID_TO_BSID(brid):
    return brid + 2000


def BRID_TO_BID(brid):
    return brid + 3000


def BRID_TO_BUILD_NUMBER(brid):
    return brid + 4000


class TestTrigger(TestBuildStepMixin, TestReactorMixin, unittest.TestCase):
    def setUp(self):
        self.setup_test_reactor()
        return self.setup_test_build_step()

    @defer.inlineCallbacks
    def setup_step(self, step, sourcestampsInBuild=None, gotRevisionsInBuild=None, *args, **kwargs):
        sourcestamps = sourcestampsInBuild or []
        got_revisions = gotRevisionsInBuild or {}

        yield super().setup_step(step, *args, **kwargs)

        # This step reaches deeply into a number of parts of Buildbot.  That
        # should be fixed!

        # set up a buildmaster that knows about two fake schedulers, a and b
        m = self.master
        self.build.builder.botmaster = m.botmaster
        self.build.conn = object()
        m.config.buildbotURL = "baseurl/"
        m.scheduler_manager = FakeSchedulerManager()

        self.scheduler_a = a = FakeTriggerable(name='a')
        self.scheduler_b = b = FakeTriggerable(name='b')
        self.scheduler_c = c = FakeTriggerable(name='c')
        m.scheduler_manager.namedServices = {"a": a, "b": b, "c": c}

        a.brids = {77: 11}
        b.brids = {78: 22}
        c.brids = {79: 33, 80: 44}

        def make_fake_br(brid, builderid):
            return fakedb.BuildRequest(id=brid, buildsetid=BRID_TO_BSID(brid), builderid=builderid)

        def make_fake_build(brid, builderid):
            return fakedb.Build(
                buildrequestid=brid,
                id=BRID_TO_BID(brid),
                number=BRID_TO_BUILD_NUMBER(brid),
                masterid=9,
                workerid=13,
                builderid=builderid,
            )

        yield m.db.insert_test_data([
            fakedb.Builder(id=77, name='A'),
            fakedb.Builder(id=78, name='B'),
            fakedb.Builder(id=79, name='C1'),
            fakedb.Builder(id=80, name='C2'),
            fakedb.Master(id=9),
            fakedb.Buildset(id=2022),
            fakedb.Buildset(id=2011),
            fakedb.Buildset(id=2033),
            fakedb.Worker(id=13, name="some:worker"),
            make_fake_br(11, 77),
            make_fake_br(22, 78),
            fakedb.BuildRequest(id=33, buildsetid=2033, builderid=79),
            fakedb.BuildRequest(id=44, buildsetid=2033, builderid=80),
            make_fake_build(11, builderid=77),
            make_fake_build(22, builderid=78),
            make_fake_build(33, builderid=79),
            # builderid is 79 on purpose, changed, from the one of the buildrequest
            # to test the case of the virtual
            make_fake_build(44, builderid=79),
        ])

        def getAllSourceStamps():
            return sourcestamps

        self.build.getAllSourceStamps = getAllSourceStamps

        def getAllGotRevisions():
            return got_revisions

        self.get_nth_step(0).getAllGotRevisions = getAllGotRevisions

        self.exp_add_sourcestamp = None
        self.exp_a_trigger = None
        self.exp_b_trigger = None
        self.exp_c_trigger = None
        self.exp_added_urls = []

    @defer.inlineCallbacks
    def run_step(self, results_dict=None):
        if results_dict is None:
            results_dict = {}
        if self.get_nth_step(0).waitForFinish:
            for i in [11, 22, 33, 44]:
                yield self.master.db.builds.finishBuild(
                    BRID_TO_BID(i), results_dict.get(i, SUCCESS)
                )
        d = super().run_step()
        # the build doesn't finish until after a callLater, so this has the
        # effect of checking whether the deferred has been fired already;
        if self.get_nth_step(0).waitForFinish:
            self.assertFalse(d.called)
        else:
            self.assertTrue(d.called)

        yield d
        self.assertEqual(self.scheduler_a.triggered_with, self.exp_a_trigger)
        self.assertEqual(self.scheduler_b.triggered_with, self.exp_b_trigger)

        # check the URLs
        stepUrls = self.master.data.updates.stepUrls
        if stepUrls:
            got_added_urls = stepUrls[next(iter(stepUrls))]
        else:
            got_added_urls = []
        self.assertEqual(sorted(got_added_urls), sorted(self.exp_added_urls))

        if self.exp_add_sourcestamp:
            self.assertEqual(self.addSourceStamp_kwargs, self.exp_add_sourcestamp)

        # pause run_step's completion until after any other callLater's are done
        d = defer.Deferred()
        reactor.callLater(0, d.callback, None)
        yield d

    def expectTriggeredWith(self, a=None, b=None, c=None, d=None):
        self.exp_a_trigger = a
        if a is not None:
            self.expectTriggeredLinks('a_br')
        self.exp_b_trigger = b
        if b is not None:
            self.expectTriggeredLinks('b_br')
        self.exp_c_trigger = c
        if c is not None:
            self.expectTriggeredLinks('c_br')

    def expectAddedSourceStamp(self, **kwargs):
        self.exp_add_sourcestamp = kwargs

    def expectTriggeredLinks(self, *args):
        if 'a_br' in args:
            self.exp_added_urls.append(('a #11', 'baseurl/#/buildrequests/11'))
        if 'b_br' in args:
            self.exp_added_urls.append(('b #22', 'baseurl/#/buildrequests/22'))
        if 'c_br' in args:
            self.exp_added_urls.append(('c #33', 'baseurl/#/buildrequests/33'))
            self.exp_added_urls.append(('c #44', 'baseurl/#/buildrequests/44'))
        if 'a' in args:
            self.exp_added_urls.append(('success: A #4011', 'baseurl/#/builders/77/builds/4011'))
        if 'b' in args:
            self.exp_added_urls.append(('success: B #4022', 'baseurl/#/builders/78/builds/4022'))
        if 'afailed' in args:
            self.exp_added_urls.append(('failure: A #4011', 'baseurl/#/builders/77/builds/4011'))
        if 'c' in args:
            self.exp_added_urls.append(('success: C1 #4033', 'baseurl/#/builders/79/builds/4033'))
            self.exp_added_urls.append(('success: C1 #4044', 'baseurl/#/builders/79/builds/4044'))

    # tests
    def test_no_schedulerNames(self):
        with self.assertRaises(config.ConfigErrors):
            trigger.Trigger()

    def test_unimportantSchedulerNames_not_in_schedulerNames(self):
        with self.assertRaises(config.ConfigErrors):
            trigger.Trigger(schedulerNames=['a'], unimportantSchedulerNames=['b'])

    def test_unimportantSchedulerNames_not_in_schedulerNames_but_rendered(self):
        # should not raise
        trigger.Trigger(
            schedulerNames=[properties.Interpolate('a')], unimportantSchedulerNames=['b']
        )

    def test_sourceStamp_and_updateSourceStamp(self):
        with self.assertRaises(config.ConfigErrors):
            trigger.Trigger(schedulerNames=['c'], sourceStamp={"x": 1}, updateSourceStamp=True)

    def test_sourceStamps_and_updateSourceStamp(self):
        with self.assertRaises(config.ConfigErrors):
            trigger.Trigger(
                schedulerNames=['c'], sourceStamps=[{"x": 1}, {"x": 2}], updateSourceStamp=True
            )

    def test_updateSourceStamp_and_alwaysUseLatest(self):
        with self.assertRaises(config.ConfigErrors):
            trigger.Trigger(schedulerNames=['c'], updateSourceStamp=True, alwaysUseLatest=True)

    def test_sourceStamp_and_alwaysUseLatest(self):
        with self.assertRaises(config.ConfigErrors):
            trigger.Trigger(schedulerNames=['c'], sourceStamp={"x": 1}, alwaysUseLatest=True)

    def test_sourceStamps_and_alwaysUseLatest(self):
        with self.assertRaises(config.ConfigErrors):
            trigger.Trigger(
                schedulerNames=['c'], sourceStamps=[{"x": 1}, {"x": 2}], alwaysUseLatest=True
            )

    @defer.inlineCallbacks
    def test_simple(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a'], sourceStamps={}))
        self.expect_outcome(result=SUCCESS, state_string='triggered a')
        self.expectTriggeredWith(a=(False, [], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_simple_failure(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a']))
        self.scheduler_a.result = FAILURE
        # not waitForFinish, so trigger step succeeds even though the build
        # didn't fail
        self.expect_outcome(result=SUCCESS, state_string='triggered a')
        self.expectTriggeredWith(a=(False, [], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_simple_exception(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a']))
        self.scheduler_a.exception = True
        self.expect_outcome(result=SUCCESS, state_string='triggered a')
        self.expectTriggeredWith(a=(False, [], {}))
        yield self.run_step()

        self.assertEqual(len(self.flushLoggedErrors(RuntimeError)), 1)

    @defer.inlineCallbacks
    def test_bogus_scheduler(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a', 'x']))
        # bogus scheduler is an exception, not a failure (don't blame the patch)
        self.expect_outcome(result=EXCEPTION)
        self.expectTriggeredWith(a=None)  # a is not triggered!
        yield self.run_step()
        self.flushLoggedErrors(ValueError)

    @defer.inlineCallbacks
    def test_updateSourceStamp(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a'], updateSourceStamp=True),
            sourcestampsInBuild=[FakeSourceStamp(codebase='', repository='x', revision=11111)],
            gotRevisionsInBuild={'': 23456},
        )
        self.expect_outcome(result=SUCCESS, state_string='triggered a')
        self.expectTriggeredWith(
            a=(False, [{'codebase': '', 'repository': 'x', 'revision': 23456}], {})
        )
        yield self.run_step()

    @defer.inlineCallbacks
    def test_updateSourceStamp_no_got_revision(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a'], updateSourceStamp=True),
            sourcestampsInBuild=[FakeSourceStamp(codebase='', repository='x', revision=11111)],
        )
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(
            a=(
                False,
                # uses old revision
                [{'codebase': '', 'repository': 'x', 'revision': 11111}],
                {},
            )
        )
        yield self.run_step()

    @defer.inlineCallbacks
    def test_not_updateSourceStamp(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a'], updateSourceStamp=False),
            sourcestampsInBuild=[FakeSourceStamp(codebase='', repository='x', revision=11111)],
            gotRevisionsInBuild={'': 23456},
        )
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(
            a=(False, [{'codebase': '', 'repository': 'x', 'revision': 11111}], {})
        )
        yield self.run_step()

    @defer.inlineCallbacks
    def test_updateSourceStamp_multiple_repositories(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a'], updateSourceStamp=True),
            sourcestampsInBuild=[
                FakeSourceStamp(codebase='cb1', revision='12345'),
                FakeSourceStamp(codebase='cb2', revision='12345'),
            ],
            gotRevisionsInBuild={'cb1': 23456, 'cb2': 34567},
        )
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(
            a=(
                False,
                [{'codebase': 'cb1', 'revision': 23456}, {'codebase': 'cb2', 'revision': 34567}],
                {},
            )
        )
        yield self.run_step()

    @defer.inlineCallbacks
    def test_updateSourceStamp_prop_false(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a'], updateSourceStamp=properties.Property('usess')),
            sourcestampsInBuild=[FakeSourceStamp(codebase='', repository='x', revision=11111)],
            gotRevisionsInBuild={'': 23456},
        )
        self.build.setProperty('usess', False, 'me')
        self.expect_outcome(result=SUCCESS)
        # didn't use got_revision
        self.expectTriggeredWith(
            a=(False, [{'codebase': '', 'repository': 'x', 'revision': 11111}], {})
        )
        yield self.run_step()

    @defer.inlineCallbacks
    def test_updateSourceStamp_prop_true(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a'], updateSourceStamp=properties.Property('usess')),
            sourcestampsInBuild=[FakeSourceStamp(codebase='', repository='x', revision=11111)],
            gotRevisionsInBuild={'': 23456},
        )
        self.build.setProperty('usess', True, 'me')
        self.expect_outcome(result=SUCCESS)
        # didn't use got_revision
        self.expectTriggeredWith(
            a=(False, [{'codebase': '', 'repository': 'x', 'revision': 23456}], {})
        )
        yield self.run_step()

    @defer.inlineCallbacks
    def test_alwaysUseLatest(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['b'], alwaysUseLatest=True),
            sourcestampsInBuild=[FakeSourceStamp(codebase='', repository='x', revision=11111)],
        )
        self.expect_outcome(result=SUCCESS)
        # Do not pass setid
        self.expectTriggeredWith(b=(False, [], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_alwaysUseLatest_prop_false(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['b'], alwaysUseLatest=properties.Property('aul')),
            sourcestampsInBuild=[FakeSourceStamp(codebase='', repository='x', revision=11111)],
        )
        self.build.setProperty('aul', False, 'me')
        self.expect_outcome(result=SUCCESS)
        # didn't use latest
        self.expectTriggeredWith(
            b=(False, [{'codebase': '', 'repository': 'x', 'revision': 11111}], {})
        )
        yield self.run_step()

    @defer.inlineCallbacks
    def test_alwaysUseLatest_prop_true(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['b'], alwaysUseLatest=properties.Property('aul')),
            sourcestampsInBuild=[FakeSourceStamp(codebase='', repository='x', revision=11111)],
        )
        self.build.setProperty('aul', True, 'me')
        self.expect_outcome(result=SUCCESS)
        # didn't use latest
        self.expectTriggeredWith(b=(False, [], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_sourceStamp(self):
        ss = {"revision": 9876, "branch": 'dev'}
        yield self.setup_step(trigger.Trigger(schedulerNames=['b'], sourceStamp=ss))
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(b=(False, [ss], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_set_of_sourceStamps(self):
        ss1 = {"codebase": 'cb1', "repository": 'r1', "revision": 9876, "branch": 'dev'}
        ss2 = {"codebase": 'cb2', "repository": 'r2', "revision": 5432, "branch": 'dev'}
        yield self.setup_step(trigger.Trigger(schedulerNames=['b'], sourceStamps=[ss1, ss2]))
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(b=(False, [ss1, ss2], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_set_of_sourceStamps_override_build(self):
        ss1 = {"codebase": 'cb1', "repository": 'r1', "revision": 9876, "branch": 'dev'}
        ss2 = {"codebase": 'cb2', "repository": 'r2', "revision": 5432, "branch": 'dev'}
        ss3 = FakeSourceStamp(codebase='cb3', repository='r3', revision=1234, branch='dev')
        ss4 = FakeSourceStamp(codebase='cb4', repository='r4', revision=2345, branch='dev')
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['b'], sourceStamps=[ss1, ss2]),
            sourcestampsInBuild=[ss3, ss4],
        )
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(b=(False, [ss1, ss2], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_sourceStamp_prop(self):
        ss = {"revision": properties.Property('rev'), "branch": 'dev'}
        yield self.setup_step(trigger.Trigger(schedulerNames=['b'], sourceStamp=ss))
        self.build.setProperty('rev', 602, 'me')
        expected_ss = {"revision": 602, "branch": 'dev'}
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(b=(False, [expected_ss], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_waitForFinish(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a', 'b'], waitForFinish=True))
        self.expect_outcome(result=SUCCESS, state_string='triggered a, b')
        self.expectTriggeredWith(a=(True, [], {}), b=(True, [], {}))
        self.expectTriggeredLinks('a', 'b')
        yield self.run_step()

    @defer.inlineCallbacks
    def test_waitForFinish_failure(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a'], waitForFinish=True))
        self.scheduler_a.result = FAILURE
        self.expect_outcome(result=FAILURE)
        self.expectTriggeredWith(a=(True, [], {}))
        self.expectTriggeredLinks('afailed')
        yield self.run_step(results_dict={11: FAILURE})

    @defer.inlineCallbacks
    def test_waitForFinish_split_failure(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a', 'b'], waitForFinish=True))
        self.scheduler_a.result = FAILURE
        self.scheduler_b.result = SUCCESS
        self.expect_outcome(result=FAILURE, state_string='triggered a, b')
        self.expectTriggeredWith(a=(True, [], {}), b=(True, [], {}))
        self.expectTriggeredLinks('afailed', 'b')
        yield self.run_step(results_dict={11: FAILURE})

    @defer.inlineCallbacks
    def test_waitForFinish_exception(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a', 'b'], waitForFinish=True))
        self.get_nth_step(0).addCompleteLog = Mock()
        self.scheduler_b.exception = True
        self.expect_outcome(result=EXCEPTION, state_string='triggered a, b')
        self.expectTriggeredWith(a=(True, [], {}), b=(True, [], {}))
        self.expectTriggeredLinks('a')  # b doesn't return a brid
        yield self.run_step()
        self.assertEqual(len(self.get_nth_step(0).addCompleteLog.call_args_list), 1)

    @defer.inlineCallbacks
    def test_virtual_builder(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['c'], waitForFinish=True))
        self.expect_outcome(result=SUCCESS, state_string='triggered c')
        self.expectTriggeredWith(c=(True, [], {}))
        self.expectTriggeredLinks('c')
        yield self.run_step()

    @defer.inlineCallbacks
    def test_set_properties(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a'], set_properties={"x": 1, "y": 2})
        )
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(a=(False, [], {"x": (1, 'Trigger'), "y": (2, 'Trigger')}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_set_properties_prop(self):
        yield self.setup_step(
            trigger.Trigger(
                schedulerNames=['a'], set_properties={"x": properties.Property('X'), "y": 2}
            )
        )
        self.build.setProperty('X', 'xxx', 'here')
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(a=(False, [], {"x": ('xxx', 'Trigger'), "y": (2, 'Trigger')}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_copy_properties(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a'], copy_properties=['a', 'b']))
        self.build.setProperty('a', 'A', 'AA')
        self.build.setProperty('b', 'B', 'BB')
        self.build.setProperty('c', 'C', 'CC')
        self.expect_outcome(result=SUCCESS)
        self.expectTriggeredWith(a=(False, [], {"a": ('A', 'Trigger'), "b": ('B', 'Trigger')}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_waitForFinish_interrupt(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a'], waitForFinish=True))

        self.expect_outcome(result=CANCELLED, state_string='interrupted')
        self.expectTriggeredWith(a=(True, [], {}))
        d = self.run_step()

        # interrupt before the callLater representing the Triggerable
        # schedulers completes
        self.get_nth_step(0).interrupt(failure.Failure(RuntimeError('oh noes')))

        yield d

    @defer.inlineCallbacks
    def test_waitForFinish_interrupt_no_connection(self):
        yield self.setup_step(trigger.Trigger(schedulerNames=['a'], waitForFinish=True))

        self.expect_outcome(result=CANCELLED, state_string='interrupted')
        self.expectTriggeredWith(a=(True, [], {}))
        self.scheduler_a.never_finish = True
        d = self.run_step()

        # interrupt before the callLater representing the Triggerable
        # schedulers completes
        self.build.conn = None
        self.get_nth_step(0).interrupt(failure.Failure(RuntimeError('oh noes')))

        yield d

    @defer.inlineCallbacks
    def test_getSchedulersAndProperties_back_comp(self):
        class DynamicTrigger(trigger.Trigger):
            def getSchedulersAndProperties(self):
                return [("a", {}, False), ("b", {}, True)]

        yield self.setup_step(DynamicTrigger(schedulerNames=['a', 'b']))
        self.scheduler_a.result = SUCCESS
        self.scheduler_b.result = FAILURE
        self.expect_outcome(result=SUCCESS, state_string='triggered a, b')
        self.expectTriggeredWith(a=(False, [], {}), b=(False, [], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_unimportantSchedulerNames(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a', 'b'], unimportantSchedulerNames=['b'])
        )
        self.scheduler_a.result = SUCCESS
        self.scheduler_b.result = FAILURE
        self.expect_outcome(result=SUCCESS, state_string='triggered a, b')
        self.expectTriggeredWith(a=(False, [], {}), b=(False, [], {}))
        yield self.run_step()

    @defer.inlineCallbacks
    def test_unimportantSchedulerNames_with_more_brids_for_bsid(self):
        yield self.setup_step(
            trigger.Trigger(schedulerNames=['a', 'c'], unimportantSchedulerNames=['c'])
        )
        self.scheduler_a.result = SUCCESS
        self.scheduler_c.result = FAILURE
        self.expect_outcome(result=SUCCESS, state_string='triggered a, c')
        self.expectTriggeredWith(a=(False, [], {}), c=(False, [], {}))
        yield self.run_step()
