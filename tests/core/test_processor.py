import asyncio
import datetime
import freezegun
import pytest
import time
import uuid
import json
from _pytest.monkeypatch import MonkeyPatch
from _pytest.logging import LogCaptureFixture
from aioresponses import aioresponses
from typing import Optional, Text, List, Callable, Type, Any, Tuple
from unittest.mock import patch, Mock

from rasa.core.policies.rule_policy import RulePolicy
from rasa.core.actions.action import (
    ActionBotResponse,
    ActionListen,
    ActionExecutionRejection,
    ActionUnlikelyIntent,
)
import rasa.core.policies.policy
from rasa.core.nlg import NaturalLanguageGenerator, TemplatedNaturalLanguageGenerator
from rasa.core.policies.policy import PolicyPrediction
import tests.utilities

from rasa.core import jobs
from rasa.core.agent import Agent
from rasa.core.channels.channel import (
    CollectingOutputChannel,
    UserMessage,
    OutputChannel,
)
from rasa.shared.core.domain import SessionConfig, Domain, KEY_ACTIONS
from rasa.shared.core.events import (
    ActionExecuted,
    BotUttered,
    ReminderCancelled,
    ReminderScheduled,
    Restarted,
    UserUttered,
    SessionStarted,
    Event,
    SlotSet,
    DefinePrevUserUtteredFeaturization,
    ActionExecutionRejected,
    LoopInterrupted,
)
from rasa.core.interpreter import RasaNLUHttpInterpreter
from rasa.shared.nlu.interpreter import NaturalLanguageInterpreter, RegexInterpreter
from rasa.core.policies import SimplePolicyEnsemble, PolicyEnsemble
from rasa.core.policies.ted_policy import TEDPolicy
from rasa.core.policies.memoization import MemoizationPolicy
from rasa.core.processor import MessageProcessor
from rasa.shared.core.slots import Slot
from rasa.core.tracker_store import InMemoryTrackerStore
from rasa.core.lock_store import InMemoryLockStore
from rasa.shared.core.trackers import DialogueStateTracker
from rasa.shared.core.generator import TrackerWithCachedStates
from rasa.shared.nlu.constants import INTENT_NAME_KEY
from rasa.utils.endpoints import EndpointConfig
from rasa.shared.core.constants import (
    ACTION_RESTART_NAME,
    ACTION_UNLIKELY_INTENT_NAME,
    DEFAULT_INTENTS,
    ACTION_LISTEN_NAME,
    ACTION_SESSION_START_NAME,
    EXTERNAL_MESSAGE_PREFIX,
    IS_EXTERNAL,
    SESSION_START_METADATA_SLOT,
    RULE_SNIPPET_ACTION_NAME,
)

import logging

logger = logging.getLogger(__name__)


async def test_message_processor(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    await default_processor.handle_message(
        UserMessage('/greet{"name":"Core"}', default_channel)
    )
    assert default_channel.latest_output() == {
        "recipient_id": "default",
        "text": "hey there Core!",
    }


async def test_message_id_logging(default_processor: MessageProcessor):
    message = UserMessage("If Meg was an egg would she still have a leg?")
    tracker = DialogueStateTracker("1", [])
    await default_processor._handle_message_with_tracker(message, tracker)
    logged_event = tracker.events[-1]

    assert logged_event.message_id == message.message_id
    assert logged_event.message_id is not None


async def test_parsing(default_processor: MessageProcessor):
    message = UserMessage('/greet{"name": "boy"}')
    parsed = await default_processor.parse_message(message)
    assert parsed["intent"][INTENT_NAME_KEY] == "greet"
    assert parsed["entities"][0]["entity"] == "name"


async def test_check_for_unseen_feature(default_processor: MessageProcessor):
    message = UserMessage('/dislike{"test_entity": "RASA"}')
    parsed = await default_processor.parse_message(message)
    with pytest.warns(UserWarning) as record:
        default_processor._check_for_unseen_features(parsed)
    assert len(record) == 2

    assert (
        record[0].message.args[0].startswith("Interpreter parsed an intent 'dislike'")
    )
    assert (
        record[1]
        .message.args[0]
        .startswith("Interpreter parsed an entity 'test_entity'")
    )


@pytest.mark.parametrize("default_intent", DEFAULT_INTENTS)
async def test_default_intent_recognized(
    default_processor: MessageProcessor, default_intent: Text
):
    message = UserMessage(default_intent)
    parsed = await default_processor.parse_message(message)
    with pytest.warns(None) as record:
        default_processor._check_for_unseen_features(parsed)
    assert len(record) == 0


async def test_http_parsing():
    message = UserMessage("lunch?")

    endpoint = EndpointConfig("https://interpreter.com")
    with aioresponses() as mocked:
        mocked.post("https://interpreter.com/model/parse", repeat=True, status=200)

        inter = RasaNLUHttpInterpreter(endpoint_config=endpoint)
        try:
            await MessageProcessor(inter, None, None, None, None, None).parse_message(
                message
            )
        except KeyError:
            pass  # logger looks for intent and entities, so we except

        r = tests.utilities.latest_request(
            mocked, "POST", "https://interpreter.com/model/parse"
        )

        assert r


async def mocked_parse(self, text, message_id=None, tracker=None, metadata=None):
    """Mock parsing a text message and augment it with the slot
    value from the tracker's state."""

    return {
        "intent": {INTENT_NAME_KEY: "", "confidence": 0.0},
        "entities": [],
        "text": text,
        "requested_language": tracker.get_slot("requested_language"),
    }


async def test_parsing_with_tracker():
    tracker = DialogueStateTracker.from_dict("1", [], [Slot("requested_language")])

    # we'll expect this value 'en' to be part of the result from the interpreter
    tracker._set_slot("requested_language", "en")

    endpoint = EndpointConfig("https://interpreter.com")
    with aioresponses() as mocked:
        mocked.post("https://interpreter.com/parse", repeat=True, status=200)

        # mock the parse function with the one defined for this test
        with patch.object(RasaNLUHttpInterpreter, "parse", mocked_parse):
            interpreter = RasaNLUHttpInterpreter(endpoint_config=endpoint)
            agent = Agent(None, None, interpreter)
            result = await agent.parse_message_using_nlu_interpreter("lunch?", tracker)

            assert result["requested_language"] == "en"


async def test_reminder_scheduled(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = uuid.uuid4().hex

    reminder = ReminderScheduled("remind", datetime.datetime.now())
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    tracker.update(UserUttered("test"))
    tracker.update(ActionExecuted("action_schedule_reminder"))
    tracker.update(reminder)

    default_processor.tracker_store.save(tracker)

    await default_processor.handle_reminder(reminder, sender_id, default_channel)

    # retrieve the updated tracker
    t = default_processor.tracker_store.retrieve(sender_id)

    assert t.events[1] == UserUttered("test")
    assert t.events[2] == ActionExecuted("action_schedule_reminder")
    assert isinstance(t.events[3], ReminderScheduled)
    assert t.events[4] == UserUttered(
        f"{EXTERNAL_MESSAGE_PREFIX}remind",
        intent={INTENT_NAME_KEY: "remind", IS_EXTERNAL: True},
    )


async def test_reminder_lock(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    caplog: LogCaptureFixture,
):
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        sender_id = uuid.uuid4().hex

        reminder = ReminderScheduled("remind", datetime.datetime.now())
        tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

        tracker.update(UserUttered("test"))
        tracker.update(ActionExecuted("action_schedule_reminder"))
        tracker.update(reminder)

        default_processor.tracker_store.save(tracker)

        await default_processor.handle_reminder(reminder, sender_id, default_channel)

        assert f"Deleted lock for conversation '{sender_id}'." in caplog.text


async def test_trigger_external_latest_input_channel(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = uuid.uuid4().hex
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)
    input_channel = "test_input_channel_external"

    tracker.update(UserUttered("test1"))
    tracker.update(UserUttered("test2", input_channel=input_channel))

    await default_processor.trigger_external_user_uttered(
        "test3", None, tracker, default_channel
    )

    tracker = default_processor.tracker_store.retrieve(sender_id)

    assert tracker.get_latest_input_channel() == input_channel


async def test_reminder_aborted(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = uuid.uuid4().hex

    reminder = ReminderScheduled(
        "utter_greet", datetime.datetime.now(), kill_on_user_message=True
    )
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    tracker.update(reminder)
    tracker.update(UserUttered("test"))  # cancels the reminder

    default_processor.tracker_store.save(tracker)
    await default_processor.handle_reminder(reminder, sender_id, default_channel)

    # retrieve the updated tracker
    t = default_processor.tracker_store.retrieve(sender_id)
    assert len(t.events) == 3  # nothing should have been executed


async def wait_until_all_jobs_were_executed(
    timeout_after_seconds: Optional[float] = None,
) -> None:
    total_seconds = 0.0
    while len((await jobs.scheduler()).get_jobs()) > 0 and (
        not timeout_after_seconds or total_seconds < timeout_after_seconds
    ):
        await asyncio.sleep(0.1)
        total_seconds += 0.1

    if total_seconds >= timeout_after_seconds:
        jobs.kill_scheduler()
        raise TimeoutError


async def test_reminder_cancelled_multi_user(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    trackers = []
    for sender_id in sender_ids:
        tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

        tracker.update(UserUttered("test"))
        tracker.update(ActionExecuted("action_reminder_reminder"))
        tracker.update(
            ReminderScheduled(
                "greet", datetime.datetime.now(), kill_on_user_message=True
            )
        )
        trackers.append(tracker)

    # cancel all reminders (one) for the first user
    trackers[0].update(ReminderCancelled())

    for tracker in trackers:
        default_processor.tracker_store.save(tracker)
        await default_processor._schedule_reminders(
            tracker.events, tracker, default_channel
        )
    # check that the jobs were added
    assert len((await jobs.scheduler()).get_jobs()) == 2

    for tracker in trackers:
        await default_processor._cancel_reminders(tracker.events, tracker)
    # check that only one job was removed
    assert len((await jobs.scheduler()).get_jobs()) == 1

    # execute the jobs
    await wait_until_all_jobs_were_executed(timeout_after_seconds=5.0)

    tracker_0 = default_processor.tracker_store.retrieve(sender_ids[0])
    # there should be no utter_greet action
    assert (
        UserUttered(
            f"{EXTERNAL_MESSAGE_PREFIX}greet",
            intent={INTENT_NAME_KEY: "greet", IS_EXTERNAL: True},
        )
        not in tracker_0.events
    )

    tracker_1 = default_processor.tracker_store.retrieve(sender_ids[1])
    # there should be utter_greet action
    assert (
        UserUttered(
            f"{EXTERNAL_MESSAGE_PREFIX}greet",
            intent={INTENT_NAME_KEY: "greet", IS_EXTERNAL: True},
        )
        in tracker_1.events
    )


async def test_reminder_cancelled_cancels_job_with_name(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = "][]][xy,,=+2f'[:/;>]  <0d]A[e_,02"

    reminder = ReminderScheduled(
        intent="greet", trigger_date_time=datetime.datetime.now()
    )
    job_name = reminder.scheduled_job_name(sender_id)
    reminder_cancelled = ReminderCancelled()

    assert reminder_cancelled.cancels_job_with_name(job_name, sender_id)
    assert not reminder_cancelled.cancels_job_with_name(job_name.upper(), sender_id)


async def test_reminder_cancelled_cancels_job_with_name_special_name(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = "][]][xy,,=+2f'[:/;  >]<0d]A[e_,02"
    name = "wkjbgr,34(,*&%^^&*(OP#LKMN V#NF# # #R"

    reminder = ReminderScheduled(
        intent="greet", trigger_date_time=datetime.datetime.now(), name=name
    )
    job_name = reminder.scheduled_job_name(sender_id)
    reminder_cancelled = ReminderCancelled(name)

    assert reminder_cancelled.cancels_job_with_name(job_name, sender_id)
    assert not reminder_cancelled.cancels_job_with_name(job_name.upper(), sender_id)


async def cancel_reminder_and_check(
    tracker: DialogueStateTracker,
    default_processor: MessageProcessor,
    reminder_canceled_event: ReminderCancelled,
    num_jobs_before: int,
    num_jobs_after: int,
) -> None:
    # cancel the sixth reminder
    tracker.update(reminder_canceled_event)

    # check that the jobs were added
    assert len((await jobs.scheduler()).get_jobs()) == num_jobs_before

    await default_processor._cancel_reminders(tracker.events, tracker)

    # check that only one job was removed
    assert len((await jobs.scheduler()).get_jobs()) == num_jobs_after


async def test_reminder_cancelled_by_name(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    tracker_with_six_scheduled_reminders: DialogueStateTracker,
):
    tracker = tracker_with_six_scheduled_reminders
    await default_processor._schedule_reminders(
        tracker.events, tracker, default_channel
    )

    # cancel the sixth reminder
    await cancel_reminder_and_check(
        tracker, default_processor, ReminderCancelled("special"), 6, 5
    )


async def test_reminder_cancelled_by_entities(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    tracker_with_six_scheduled_reminders: DialogueStateTracker,
):
    tracker = tracker_with_six_scheduled_reminders
    await default_processor._schedule_reminders(
        tracker.events, tracker, default_channel
    )

    # cancel the fourth reminder
    await cancel_reminder_and_check(
        tracker,
        default_processor,
        ReminderCancelled(entities=[{"entity": "name", "value": "Bruce Wayne"}]),
        6,
        5,
    )


async def test_reminder_cancelled_by_intent(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    tracker_with_six_scheduled_reminders: DialogueStateTracker,
):
    tracker = tracker_with_six_scheduled_reminders
    await default_processor._schedule_reminders(
        tracker.events, tracker, default_channel
    )

    # cancel the third, fifth, and sixth reminder
    await cancel_reminder_and_check(
        tracker, default_processor, ReminderCancelled(intent="default"), 6, 3
    )


async def test_reminder_cancelled_all(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    tracker_with_six_scheduled_reminders: DialogueStateTracker,
):
    tracker = tracker_with_six_scheduled_reminders
    await default_processor._schedule_reminders(
        tracker.events, tracker, default_channel
    )

    # cancel all reminders
    await cancel_reminder_and_check(
        tracker, default_processor, ReminderCancelled(), 6, 0
    )


async def test_reminder_restart(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = uuid.uuid4().hex

    reminder = ReminderScheduled(
        "utter_greet", datetime.datetime.now(), kill_on_user_message=False
    )
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    tracker.update(reminder)
    tracker.update(Restarted())  # cancels the reminder
    tracker.update(UserUttered("test"))

    default_processor.tracker_store.save(tracker)
    await default_processor.handle_reminder(reminder, sender_id, default_channel)

    # retrieve the updated tracker
    t = default_processor.tracker_store.retrieve(sender_id)
    assert len(t.events) == 4  # nothing should have been executed


@pytest.mark.parametrize(
    "event_to_apply,session_expiration_time_in_minutes,has_expired",
    [
        # last user event is way in the past
        (UserUttered(timestamp=1), 60, True),
        # user event are very recent
        (UserUttered("hello", timestamp=time.time()), 120, False),
        # there is user event
        (ActionExecuted(ACTION_LISTEN_NAME, timestamp=time.time()), 60, False),
        # Old event, but sessions are disabled
        (UserUttered("hello", timestamp=1), 0, False),
        # there is no event
        (None, 1, False),
    ],
)
async def test_has_session_expired(
    event_to_apply: Optional[Event],
    session_expiration_time_in_minutes: float,
    has_expired: bool,
    default_processor: MessageProcessor,
):
    sender_id = uuid.uuid4().hex

    default_processor.domain.session_config = SessionConfig(
        session_expiration_time_in_minutes, True
    )
    # create new tracker without events
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)
    tracker.events.clear()

    # apply desired event
    if event_to_apply:
        tracker.update(event_to_apply)

    # noinspection PyProtectedMember
    assert default_processor._has_session_expired(tracker) == has_expired


# noinspection PyProtectedMember


async def test_update_tracker_session(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    monkeypatch: MonkeyPatch,
):
    sender_id = uuid.uuid4().hex
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    # patch `_has_session_expired()` so the `_update_tracker_session()` call actually
    # does something
    monkeypatch.setattr(default_processor, "_has_session_expired", lambda _: True)

    await default_processor._update_tracker_session(tracker, default_channel)

    # the save is not called in _update_tracker_session()
    default_processor._save_tracker(tracker)

    # inspect tracker and make sure all events are present
    tracker = default_processor.tracker_store.retrieve(sender_id)

    assert list(tracker.events) == [
        ActionExecuted(ACTION_LISTEN_NAME),
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        ActionExecuted(ACTION_LISTEN_NAME),
    ]


async def test_update_tracker_session_with_metadata(
    default_processor: MessageProcessor, monkeypatch: MonkeyPatch,
):
    sender_id = uuid.uuid4().hex
    metadata = {"metadataTestKey": "metadataTestValue"}
    message = UserMessage(
        text="hi",
        output_channel=CollectingOutputChannel(),
        sender_id=sender_id,
        metadata=metadata,
    )
    await default_processor.handle_message(message)

    tracker = default_processor.tracker_store.retrieve(sender_id)
    events = list(tracker.events)

    assert events[0] == SlotSet(SESSION_START_METADATA_SLOT, metadata)
    assert tracker.slots[SESSION_START_METADATA_SLOT].value == metadata

    assert events[1] == ActionExecuted(ACTION_SESSION_START_NAME)
    assert events[2] == SessionStarted()
    assert events[2].metadata == metadata
    assert events[3] == SlotSet(SESSION_START_METADATA_SLOT, metadata)
    assert events[4] == ActionExecuted(ACTION_LISTEN_NAME)
    assert isinstance(events[5], UserUttered)


@freezegun.freeze_time("2020-02-01")
async def test_custom_action_session_start_with_metadata(
    default_processor: MessageProcessor,
):
    domain = Domain.from_dict({KEY_ACTIONS: [ACTION_SESSION_START_NAME]})
    default_processor.domain = domain
    action_server_url = "http://some-url"
    default_processor.action_endpoint = EndpointConfig(action_server_url)

    sender_id = uuid.uuid4().hex
    metadata = {"metadataTestKey": "metadataTestValue"}
    message = UserMessage(
        text="hi",
        output_channel=CollectingOutputChannel(),
        sender_id=sender_id,
        metadata=metadata,
    )

    with aioresponses() as mocked:
        mocked.post(action_server_url, payload={"events": []})
        await default_processor.handle_message(message)

    last_request = tests.utilities.latest_request(mocked, "post", action_server_url)
    tracker_for_custom_action = tests.utilities.json_of_latest_request(last_request)[
        "tracker"
    ]

    assert tracker_for_custom_action["events"] == [
        {
            "event": "slot",
            "timestamp": 1580515200.0,
            "name": SESSION_START_METADATA_SLOT,
            "value": metadata,
        }
    ]


# noinspection PyProtectedMember


async def test_update_tracker_session_with_slots(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    monkeypatch: MonkeyPatch,
):
    sender_id = uuid.uuid4().hex
    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    # apply a user uttered and five slots
    user_event = UserUttered("some utterance")
    tracker.update(user_event)

    slot_set_events = [SlotSet(f"slot key {i}", f"test value {i}") for i in range(5)]

    for event in slot_set_events:
        tracker.update(event)

    # patch `_has_session_expired()` so the `_update_tracker_session()` call actually
    # does something
    monkeypatch.setattr(default_processor, "_has_session_expired", lambda _: True)

    await default_processor._update_tracker_session(tracker, default_channel)

    # the save is not called in _update_tracker_session()
    default_processor._save_tracker(tracker)

    # inspect tracker and make sure all events are present
    tracker = default_processor.tracker_store.retrieve(sender_id)
    events = list(tracker.events)

    # the first three events should be up to the user utterance
    assert events[:2] == [ActionExecuted(ACTION_LISTEN_NAME), user_event]

    # next come the five slots
    assert events[2:7] == slot_set_events

    # the next two events are the session start sequence
    assert events[7:9] == [ActionExecuted(ACTION_SESSION_START_NAME), SessionStarted()]

    # the five slots should be reapplied
    assert events[9:14] == slot_set_events

    # finally an action listen, this should also be the last event
    assert events[14] == events[-1] == ActionExecuted(ACTION_LISTEN_NAME)


async def test_fetch_tracker_and_update_session(
    default_channel: CollectingOutputChannel, default_processor: MessageProcessor
):
    sender_id = uuid.uuid4().hex
    tracker = await default_processor.fetch_tracker_and_update_session(
        sender_id, default_channel
    )

    # ensure session start sequence is present
    assert list(tracker.events) == [
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        ActionExecuted(ACTION_LISTEN_NAME),
    ]


@pytest.mark.parametrize(
    "initial_events,expected_event_types",
    [
        # tracker is initially not empty - when it is fetched, it will just contain
        # these four events
        (
            [
                ActionExecuted(ACTION_SESSION_START_NAME),
                SessionStarted(),
                ActionExecuted(ACTION_LISTEN_NAME),
                UserUttered("/greet", {INTENT_NAME_KEY: "greet", "confidence": 1.0}),
            ],
            [ActionExecuted, SessionStarted, ActionExecuted, UserUttered],
        ),
        # tracker is initially empty, and contains the session start sequence when
        # fetched
        ([], [ActionExecuted, SessionStarted, ActionExecuted]),
    ],
)
async def test_fetch_tracker_with_initial_session(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    initial_events: List[Event],
    expected_event_types: List[Type[Event]],
):
    conversation_id = uuid.uuid4().hex

    tracker = DialogueStateTracker.from_events(conversation_id, initial_events)

    default_processor.tracker_store.save(tracker)

    tracker = await default_processor.fetch_tracker_with_initial_session(
        conversation_id, default_channel
    )

    # the events in the fetched tracker are as expected
    assert len(tracker.events) == len(expected_event_types)

    assert all(
        isinstance(tracker_event, expected_event_type)
        for tracker_event, expected_event_type in zip(
            tracker.events, expected_event_types
        )
    )


async def test_fetch_tracker_with_initial_session_does_not_update_session(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    monkeypatch: MonkeyPatch,
):
    conversation_id = uuid.uuid4().hex

    # the domain has a session expiration time of one second
    monkeypatch.setattr(
        default_processor.tracker_store.domain,
        "session_config",
        SessionConfig(carry_over_slots=True, session_expiration_time=1 / 60),
    )

    now = time.time()

    # the tracker initially contains events
    initial_events = [
        ActionExecuted(ACTION_SESSION_START_NAME, timestamp=now - 10),
        SessionStarted(timestamp=now - 9),
        ActionExecuted(ACTION_LISTEN_NAME, timestamp=now - 8),
        UserUttered(
            "/greet", {INTENT_NAME_KEY: "greet", "confidence": 1.0}, timestamp=now - 7
        ),
    ]

    tracker = DialogueStateTracker.from_events(conversation_id, initial_events)

    default_processor.tracker_store.save(tracker)

    tracker = await default_processor.fetch_tracker_with_initial_session(
        conversation_id, default_channel
    )

    # the conversation session has expired, but calling
    # `fetch_tracker_with_initial_session()` did not update it
    assert default_processor._has_session_expired(tracker)
    assert [event.as_dict() for event in tracker.events] == [
        event.as_dict() for event in initial_events
    ]


async def test_handle_message_with_session_start(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    monkeypatch: MonkeyPatch,
):
    sender_id = uuid.uuid4().hex

    entity = "name"
    slot_1 = {entity: "Core"}
    await default_processor.handle_message(
        UserMessage(f"/greet{json.dumps(slot_1)}", default_channel, sender_id)
    )

    assert default_channel.latest_output() == {
        "recipient_id": sender_id,
        "text": "hey there Core!",
    }

    # patch processor so a session start is triggered
    monkeypatch.setattr(default_processor, "_has_session_expired", lambda _: True)

    slot_2 = {entity: "post-session start hello"}
    # handle a new message
    await default_processor.handle_message(
        UserMessage(f"/greet{json.dumps(slot_2)}", default_channel, sender_id)
    )

    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    # make sure the sequence of events is as expected
    expected = [
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(
            f"/greet{json.dumps(slot_1)}",
            {INTENT_NAME_KEY: "greet", "confidence": 1.0},
            [{"entity": entity, "start": 6, "end": 22, "value": "Core"}],
        ),
        SlotSet(entity, slot_1[entity]),
        DefinePrevUserUtteredFeaturization(False),
        ActionExecuted("utter_greet"),
        BotUttered("hey there Core!", metadata={"utter_action": "utter_greet"}),
        ActionExecuted(ACTION_LISTEN_NAME),
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        # the initial SlotSet is reapplied after the SessionStarted sequence
        SlotSet(entity, slot_1[entity]),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(
            f"/greet{json.dumps(slot_2)}",
            {INTENT_NAME_KEY: "greet", "confidence": 1.0},
            [
                {
                    "entity": entity,
                    "start": 6,
                    "end": 42,
                    "value": "post-session start hello",
                }
            ],
        ),
        SlotSet(entity, slot_2[entity]),
        DefinePrevUserUtteredFeaturization(False),
        ActionExecuted("utter_greet"),
        BotUttered(
            "hey there post-session start hello!",
            metadata={"utter_action": "utter_greet"},
        ),
        ActionExecuted(ACTION_LISTEN_NAME),
    ]

    assert list(tracker.events) == expected


# noinspection PyProtectedMember
@pytest.mark.parametrize(
    "action_name, should_predict_another_action",
    [
        (ACTION_LISTEN_NAME, False),
        (ACTION_SESSION_START_NAME, False),
        ("utter_greet", True),
    ],
)
async def test_should_predict_another_action(
    default_processor: MessageProcessor,
    action_name: Text,
    should_predict_another_action: bool,
):
    assert (
        default_processor.should_predict_another_action(action_name)
        == should_predict_another_action
    )


def test_get_next_action_probabilities_passes_interpreter_to_policies(
    monkeypatch: MonkeyPatch,
):
    policy = TEDPolicy()
    test_interpreter = Mock()

    def predict_action_probabilities(
        tracker: DialogueStateTracker,
        domain: Domain,
        interpreter: NaturalLanguageInterpreter,
        **kwargs,
    ) -> PolicyPrediction:
        assert interpreter == test_interpreter
        return PolicyPrediction([1, 0], "some-policy", policy_priority=1)

    policy.predict_action_probabilities = predict_action_probabilities
    ensemble = SimplePolicyEnsemble(policies=[policy])

    domain = Domain.empty()

    processor = MessageProcessor(
        test_interpreter,
        ensemble,
        domain,
        InMemoryTrackerStore(domain),
        InMemoryLockStore(),
        Mock(),
    )

    # This should not raise
    processor._get_next_action_probabilities(
        DialogueStateTracker.from_events("lala", [ActionExecuted(ACTION_LISTEN_NAME)])
    )


async def test_action_unlikely_intent_metadata(default_processor: MessageProcessor):
    tracker = DialogueStateTracker.from_events(
        "some-sender", evts=[ActionExecuted(ACTION_LISTEN_NAME),],
    )
    domain = Domain.empty()
    metadata = {"key1": 1, "key2": "2"}

    await default_processor._run_action(
        ActionUnlikelyIntent(),
        tracker,
        CollectingOutputChannel(),
        TemplatedNaturalLanguageGenerator(domain.responses),
        PolicyPrediction([], "some policy", action_metadata=metadata),
    )

    applied_events = tracker.applied_events()
    assert applied_events == [
        ActionExecuted(ACTION_LISTEN_NAME),
        ActionExecuted(ACTION_UNLIKELY_INTENT_NAME, metadata=metadata),
    ]
    assert applied_events[1].metadata == metadata


@pytest.mark.parametrize(
    "predict_function",
    [
        lambda tracker, domain, _: PolicyPrediction([1, 0, 2, 3], "some-policy"),
        lambda tracker, domain, _=True: PolicyPrediction([1, 0], "some-policy"),
    ],
)
def test_get_next_action_probabilities_pass_policy_predictions_without_interpreter_arg(
    predict_function: Callable,
):
    policy = TEDPolicy()

    policy.predict_action_probabilities = predict_function

    ensemble = SimplePolicyEnsemble(policies=[policy])
    interpreter = Mock()
    domain = Domain.empty()

    processor = MessageProcessor(
        interpreter,
        ensemble,
        domain,
        InMemoryTrackerStore(domain),
        InMemoryLockStore(),
        Mock(),
    )

    with pytest.warns(DeprecationWarning):
        processor._get_next_action_probabilities(
            DialogueStateTracker.from_events(
                "lala", [ActionExecuted(ACTION_LISTEN_NAME)]
            )
        )


async def test_restart_triggers_session_start(
    default_channel: CollectingOutputChannel,
    default_processor: MessageProcessor,
    monkeypatch: MonkeyPatch,
):
    # The rule policy is trained and used so as to allow the default action
    # ActionRestart to be predicted
    rule_policy = RulePolicy()
    rule_policy.train([], default_processor.domain, RegexInterpreter())
    monkeypatch.setattr(
        default_processor.policy_ensemble,
        "policies",
        [rule_policy, *default_processor.policy_ensemble.policies],
    )

    sender_id = uuid.uuid4().hex

    entity = "name"
    slot_1 = {entity: "name1"}
    await default_processor.handle_message(
        UserMessage(f"/greet{json.dumps(slot_1)}", default_channel, sender_id)
    )

    assert default_channel.latest_output() == {
        "recipient_id": sender_id,
        "text": "hey there name1!",
    }

    # This restarts the chat
    await default_processor.handle_message(
        UserMessage("/restart", default_channel, sender_id)
    )

    tracker = default_processor.tracker_store.get_or_create_tracker(sender_id)

    expected = [
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(
            f"/greet{json.dumps(slot_1)}",
            {INTENT_NAME_KEY: "greet", "confidence": 1.0},
            [{"entity": entity, "start": 6, "end": 23, "value": "name1"}],
        ),
        SlotSet(entity, slot_1[entity]),
        DefinePrevUserUtteredFeaturization(use_text_for_featurization=False),
        ActionExecuted("utter_greet"),
        BotUttered("hey there name1!", metadata={"utter_action": "utter_greet"}),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered("/restart", {INTENT_NAME_KEY: "restart", "confidence": 1.0}),
        DefinePrevUserUtteredFeaturization(use_text_for_featurization=False),
        ActionExecuted(ACTION_RESTART_NAME),
        Restarted(),
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        # No previous slot is set due to restart.
        ActionExecuted(ACTION_LISTEN_NAME),
    ]
    for actual, expected in zip(tracker.events, expected):
        assert actual == expected


async def test_handle_message_if_action_manually_rejects(
    default_processor: MessageProcessor, monkeypatch: MonkeyPatch
):
    conversation_id = "test"
    message = UserMessage("/greet", sender_id=conversation_id)

    rejection_events = [
        SlotSet("my_slot", "test"),
        ActionExecutionRejected("utter_greet"),
        SlotSet("some slot", "some value"),
    ]

    async def mocked_run(self, *args: Any, **kwargs: Any) -> List[Event]:
        return rejection_events

    monkeypatch.setattr(ActionBotResponse, ActionBotResponse.run.__name__, mocked_run)
    await default_processor.handle_message(message)

    tracker = default_processor.tracker_store.retrieve(conversation_id)

    logged_events = list(tracker.events)

    assert ActionExecuted("utter_greet") not in logged_events
    assert all(event in logged_events for event in rejection_events)


def test_predict_next_action_with_deprecated_ensemble(
    default_processor: MessageProcessor, monkeypatch: MonkeyPatch
):
    expected_confidence = 2.0
    expected_action = "utter_greet"
    expected_probabilities = rasa.core.policies.policy.confidence_scores_for(
        expected_action, expected_confidence, default_processor.domain
    )
    expected_policy_name = "deprecated ensemble"

    class DeprecatedEnsemble(PolicyEnsemble):
        def probabilities_using_best_policy(
            self,
            tracker: DialogueStateTracker,
            domain: Domain,
            interpreter: NaturalLanguageInterpreter,
            **kwargs: Any,
        ) -> Tuple[List[float], Optional[Text]]:
            return expected_probabilities, expected_policy_name

    monkeypatch.setattr(default_processor, "policy_ensemble", DeprecatedEnsemble([]))

    tracker = DialogueStateTracker.from_events(
        "some sender", [ActionExecuted(ACTION_LISTEN_NAME)]
    )

    with pytest.warns(FutureWarning):
        action, prediction = default_processor.predict_next_action(tracker)

    assert action.name() == expected_action
    assert prediction == PolicyPrediction(expected_probabilities, expected_policy_name)


async def test_policy_events_are_applied_to_tracker(
    default_processor: MessageProcessor, monkeypatch: MonkeyPatch
):
    expected_action = ACTION_LISTEN_NAME
    policy_events = [LoopInterrupted(True)]
    conversation_id = "test_policy_events_are_applied_to_tracker"
    user_message = "/greet"

    expected_events = [
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(user_message, intent={"name": "greet"}),
        *policy_events,
    ]

    class ConstantEnsemble(PolicyEnsemble):
        def probabilities_using_best_policy(
            self,
            tracker: DialogueStateTracker,
            domain: Domain,
            interpreter: NaturalLanguageInterpreter,
            **kwargs: Any,
        ) -> PolicyPrediction:
            prediction = PolicyPrediction.for_action_name(
                default_processor.domain, expected_action, "some policy"
            )
            prediction.events = policy_events

            return prediction

    monkeypatch.setattr(default_processor, "policy_ensemble", ConstantEnsemble([]))

    action_received_events = False

    async def mocked_run(
        self,
        output_channel: "OutputChannel",
        nlg: "NaturalLanguageGenerator",
        tracker: "DialogueStateTracker",
        domain: "Domain",
    ) -> List[Event]:
        # The action already has access to the policy events
        nonlocal action_received_events
        action_received_events = list(tracker.events) == expected_events
        return []

    monkeypatch.setattr(ActionListen, ActionListen.run.__name__, mocked_run)

    await default_processor.handle_message(
        UserMessage(user_message, sender_id=conversation_id)
    )

    assert action_received_events

    tracker = default_processor.get_tracker(conversation_id)
    # The action was logged on the tracker as well
    expected_events.append(ActionExecuted(ACTION_LISTEN_NAME))

    for event, expected in zip(tracker.events, expected_events):
        assert event == expected


# noinspection PyTypeChecker
@pytest.mark.parametrize(
    "reject_fn",
    [
        lambda: [ActionExecutionRejected(ACTION_LISTEN_NAME)],
        lambda: (_ for _ in ()).throw(ActionExecutionRejection(ACTION_LISTEN_NAME)),
    ],
)
async def test_policy_events_not_applied_if_rejected(
    default_processor: MessageProcessor,
    monkeypatch: MonkeyPatch,
    reject_fn: Callable[[], List[Event]],
):
    expected_action = ACTION_LISTEN_NAME
    expected_events = [LoopInterrupted(True)]
    conversation_id = "test_policy_events_are_applied_to_tracker"
    user_message = "/greet"

    class ConstantEnsemble(PolicyEnsemble):
        def probabilities_using_best_policy(
            self,
            tracker: DialogueStateTracker,
            domain: Domain,
            interpreter: NaturalLanguageInterpreter,
            **kwargs: Any,
        ) -> PolicyPrediction:
            prediction = PolicyPrediction.for_action_name(
                default_processor.domain, expected_action, "some policy"
            )
            prediction.events = expected_events

            return prediction

    monkeypatch.setattr(default_processor, "policy_ensemble", ConstantEnsemble([]))

    async def mocked_run(*args: Any, **kwargs: Any) -> List[Event]:
        return reject_fn()

    monkeypatch.setattr(ActionListen, ActionListen.run.__name__, mocked_run)

    await default_processor.handle_message(
        UserMessage(user_message, sender_id=conversation_id)
    )

    tracker = default_processor.get_tracker(conversation_id)
    expected_events = [
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(user_message, intent={"name": "greet"}),
        ActionExecutionRejected(ACTION_LISTEN_NAME),
    ]
    for event, expected in zip(tracker.events, expected_events):
        assert event == expected


async def test_logging_of_end_to_end_action():
    end_to_end_action = "hi, how are you?"
    domain = Domain(
        intents=["greet"],
        entities=[],
        slots=[],
        responses={},
        action_names=[],
        forms={},
        action_texts=[end_to_end_action],
    )

    conversation_id = "test_logging_of_end_to_end_action"
    user_message = "/greet"

    class ConstantEnsemble(PolicyEnsemble):
        def __init__(self) -> None:
            super().__init__([])
            self.number_of_calls = 0

        def probabilities_using_best_policy(
            self,
            tracker: DialogueStateTracker,
            domain: Domain,
            interpreter: NaturalLanguageInterpreter,
            **kwargs: Any,
        ) -> PolicyPrediction:
            if self.number_of_calls == 0:
                prediction = PolicyPrediction.for_action_name(
                    domain, end_to_end_action, "some policy"
                )
                prediction.is_end_to_end_prediction = True
                self.number_of_calls += 1
                return prediction
            else:
                return PolicyPrediction.for_action_name(domain, ACTION_LISTEN_NAME)

    tracker_store = InMemoryTrackerStore(domain)
    lock_store = InMemoryLockStore()
    processor = MessageProcessor(
        RegexInterpreter(),
        ConstantEnsemble(),
        domain,
        tracker_store,
        lock_store,
        NaturalLanguageGenerator.create(None, domain),
    )

    await processor.handle_message(UserMessage(user_message, sender_id=conversation_id))

    tracker = tracker_store.retrieve(conversation_id)
    expected_events = [
        ActionExecuted(ACTION_SESSION_START_NAME),
        SessionStarted(),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(user_message, intent={"name": "greet"}),
        ActionExecuted(action_text=end_to_end_action),
        BotUttered("hi, how are you?", {}, {}, 123),
        ActionExecuted(ACTION_LISTEN_NAME),
    ]
    for event, expected in zip(tracker.events, expected_events):
        assert event == expected


def test_predict_next_action_with_hidden_rules():
    rule_intent = "rule_intent"
    rule_action = "rule_action"
    story_intent = "story_intent"
    story_action = "story_action"
    rule_slot = "rule_slot"
    story_slot = "story_slot"
    domain = Domain.from_yaml(
        f"""
        version: "2.0"
        intents:
        - {rule_intent}
        - {story_intent}
        actions:
        - {rule_action}
        - {story_action}
        slots:
          {rule_slot}:
            type: text
          {story_slot}:
            type: text
        """
    )

    rule = TrackerWithCachedStates.from_events(
        "rule",
        domain=domain,
        slots=domain.slots,
        evts=[
            ActionExecuted(RULE_SNIPPET_ACTION_NAME),
            ActionExecuted(ACTION_LISTEN_NAME),
            UserUttered(intent={"name": rule_intent}),
            ActionExecuted(rule_action),
            SlotSet(rule_slot, rule_slot),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
        is_rule_tracker=True,
    )
    story = TrackerWithCachedStates.from_events(
        "story",
        domain=domain,
        slots=domain.slots,
        evts=[
            ActionExecuted(ACTION_LISTEN_NAME),
            UserUttered(intent={"name": story_intent}),
            ActionExecuted(story_action),
            SlotSet(story_slot, story_slot),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )
    interpreter = RegexInterpreter()
    ensemble = SimplePolicyEnsemble(policies=[RulePolicy(), MemoizationPolicy()])
    ensemble.train([rule, story], domain, interpreter)

    tracker_store = InMemoryTrackerStore(domain)
    lock_store = InMemoryLockStore()
    processor = MessageProcessor(
        interpreter,
        ensemble,
        domain,
        tracker_store,
        lock_store,
        TemplatedNaturalLanguageGenerator(domain.templates),
    )

    tracker = DialogueStateTracker.from_events(
        "casd",
        evts=[
            ActionExecuted(ACTION_LISTEN_NAME),
            UserUttered(intent={"name": rule_intent}),
        ],
        slots=domain.slots,
    )
    action, prediction = processor.predict_next_action(tracker)
    assert action._name == rule_action
    assert prediction.hide_rule_turn

    processor._log_action_on_tracker(
        tracker, action, [SlotSet(rule_slot, rule_slot)], prediction
    )

    action, prediction = processor.predict_next_action(tracker)
    assert isinstance(action, ActionListen)
    assert prediction.hide_rule_turn

    processor._log_action_on_tracker(tracker, action, None, prediction)

    tracker.events.append(UserUttered(intent={"name": story_intent}))

    # rules are hidden correctly if memo policy predicts next actions correctly
    action, prediction = processor.predict_next_action(tracker)
    assert action._name == story_action
    assert not prediction.hide_rule_turn

    processor._log_action_on_tracker(
        tracker, action, [SlotSet(story_slot, story_slot)], prediction
    )

    action, prediction = processor.predict_next_action(tracker)
    assert isinstance(action, ActionListen)
    assert not prediction.hide_rule_turn
