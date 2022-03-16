import textwrap
from typing import Dict, Text, List, Optional, Any, Union
from unittest.mock import Mock

import pytest
from _pytest.monkeypatch import MonkeyPatch
from aioresponses import aioresponses

from rasa.core.agent import Agent
from rasa.core.policies.policy import PolicyPrediction
from rasa.core.processor import MessageProcessor
from rasa.core.tracker_store import InMemoryTrackerStore
from rasa.core.lock_store import InMemoryLockStore
from rasa.core.actions import action
from rasa.core.actions.action import ActionExecutionRejection
from rasa.shared.constants import REQUIRED_SLOTS_KEY, IGNORED_INTENTS
from rasa.shared.core.constants import ACTION_LISTEN_NAME, REQUESTED_SLOT
from rasa.core.actions.forms import FormAction
from rasa.core.channels import CollectingOutputChannel
from rasa.shared.core.domain import Domain
from rasa.shared.core.events import (
    ActiveLoop,
    SlotSet,
    UserUttered,
    ActionExecuted,
    BotUttered,
    Restarted,
    Event,
    ActionExecutionRejected,
    DefinePrevUserUtteredFeaturization,
)
from rasa.core.nlg import TemplatedNaturalLanguageGenerator
from rasa.shared.core.trackers import DialogueStateTracker
from rasa.utils.endpoints import EndpointConfig


async def test_activate():
    tracker = DialogueStateTracker.from_events(sender_id="bla", evts=[])
    form_name = "my form"
    action = FormAction(form_name, None)
    slot_name = "num_people"
    domain = f"""
forms:
  {form_name}:
    {REQUIRED_SLOTS_KEY}:
        {slot_name}:
        - type: from_entity
          entity: number
responses:
    utter_ask_num_people:
    - text: "How many people?"
"""
    domain = Domain.from_yaml(domain)

    events = await action.run(
        CollectingOutputChannel(),
        TemplatedNaturalLanguageGenerator(domain.responses),
        tracker,
        domain,
    )
    assert events[:-1] == [ActiveLoop(form_name), SlotSet(REQUESTED_SLOT, slot_name)]
    assert isinstance(events[-1], BotUttered)


async def test_activate_with_prefilled_slot():
    slot_name = "num_people"
    slot_value = 5

    tracker = DialogueStateTracker.from_events(
        sender_id="bla", evts=[SlotSet(slot_name, slot_value)]
    )
    form_name = "my form"
    action = FormAction(form_name, None)

    next_slot_to_request = "next slot to request"
    domain = f"""
    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
            {slot_name}:
            - type: from_entity
              entity: {slot_name}
            {next_slot_to_request}:
            - type: from_text
    slots:
      {slot_name}:
        type: unfeaturized
    """
    domain = Domain.from_yaml(domain)
    events = await action.run(
        CollectingOutputChannel(),
        TemplatedNaturalLanguageGenerator(domain.responses),
        tracker,
        domain,
    )
    assert events == [
        ActiveLoop(form_name),
        SlotSet(slot_name, slot_value),
        SlotSet(REQUESTED_SLOT, next_slot_to_request),
    ]


async def test_switch_forms_with_same_slot(empty_agent: Agent):
    """Tests switching of forms, where the first slot is the same in both forms.

    Tests the fix for issue 7710"""

    # Define two forms in the domain, with same first slot
    slot_a = "my_slot_a"

    form_1 = "my_form_1"
    utter_ask_form_1 = f"Please provide the value for {slot_a} of form 1"

    form_2 = "my_form_2"
    utter_ask_form_2 = f"Please provide the value for {slot_a} of form 2"

    domain = f"""
version: "2.0"
nlu:
- intent: order_status
  examples: |
    - check status of my order
    - when are my shoes coming in
- intent: return
  examples: |
    - start a return
    - I don't want my shoes anymore
forms:
  {form_1}:
    {REQUIRED_SLOTS_KEY}:
        {slot_a}:
        - type: from_entity
          entity: number
  {form_2}:
    {REQUIRED_SLOTS_KEY}:
        {slot_a}:
        - type: from_entity
          entity: number
responses:
    utter_ask_{form_1}_{slot_a}:
    - text: {utter_ask_form_1}
    utter_ask_{form_2}_{slot_a}:
    - text: {utter_ask_form_2}
"""

    domain = Domain.from_yaml(domain)

    # Driving it like rasa/core/processor
    processor = MessageProcessor(
        empty_agent.interpreter,
        empty_agent.policy_ensemble,
        domain,
        InMemoryTrackerStore(domain),
        InMemoryLockStore(),
        TemplatedNaturalLanguageGenerator(domain.responses),
    )

    # activate the first form
    tracker = DialogueStateTracker.from_events(
        "some-sender",
        evts=[
            ActionExecuted(ACTION_LISTEN_NAME),
            UserUttered("order status", {"name": "form_1", "confidence": 1.0}),
            DefinePrevUserUtteredFeaturization(False),
        ],
    )
    # rasa/core/processor.predict_next_action
    prediction = PolicyPrediction([], "some_policy")
    action_1 = FormAction(form_1, None)

    await processor._run_action(
        action_1,
        tracker,
        CollectingOutputChannel(),
        TemplatedNaturalLanguageGenerator(domain.responses),
        prediction,
    )

    events_expected = [
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered("order status", {"name": "form_1", "confidence": 1.0}),
        DefinePrevUserUtteredFeaturization(False),
        ActionExecuted(form_1),
        ActiveLoop(form_1),
        SlotSet(REQUESTED_SLOT, slot_a),
        BotUttered(
            text=utter_ask_form_1,
            metadata={"utter_action": f"utter_ask_{form_1}_{slot_a}"},
        ),
    ]
    assert tracker.applied_events() == events_expected

    next_events = [
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered("return my shoes", {"name": "form_2", "confidence": 1.0}),
        DefinePrevUserUtteredFeaturization(False),
    ]
    tracker.update_with_events(
        next_events, domain,
    )
    events_expected.extend(next_events)

    # form_1 is still active, and bot will first validate if the user utterance
    #  provides valid data for the requested slot, which is rejected
    await processor._run_action(
        action_1,
        tracker,
        CollectingOutputChannel(),
        TemplatedNaturalLanguageGenerator(domain.responses),
        prediction,
    )
    events_expected.extend([ActionExecutionRejected(action_name=form_1)])
    assert tracker.applied_events() == events_expected

    # Next, bot predicts form_2
    action_2 = FormAction(form_2, None)
    await processor._run_action(
        action_2,
        tracker,
        CollectingOutputChannel(),
        TemplatedNaturalLanguageGenerator(domain.responses),
        prediction,
    )
    events_expected.extend(
        [
            ActionExecuted(form_2),
            ActiveLoop(form_2),
            SlotSet(REQUESTED_SLOT, slot_a),
            BotUttered(
                text=utter_ask_form_2,
                metadata={"utter_action": f"utter_ask_{form_2}_{slot_a}"},
            ),
        ]
    )
    assert tracker.applied_events() == events_expected


async def test_activate_and_immediate_deactivate():
    slot_name = "num_people"
    slot_value = 5

    tracker = DialogueStateTracker.from_events(
        sender_id="bla",
        evts=[
            ActionExecuted(ACTION_LISTEN_NAME),
            UserUttered(
                "haha",
                {"name": "greet"},
                entities=[{"entity": slot_name, "value": slot_value}],
            ),
        ],
    )
    form_name = "my form"
    action = FormAction(form_name, None)
    domain = f"""
    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
            {slot_name}:
            - type: from_entity
              entity: {slot_name}
    slots:
      {slot_name}:
        type: unfeaturized
    """
    domain = Domain.from_yaml(domain)
    events = await action.run(
        CollectingOutputChannel(),
        TemplatedNaturalLanguageGenerator(domain.responses),
        tracker,
        domain,
    )
    assert events == [
        ActiveLoop(form_name),
        SlotSet(slot_name, slot_value),
        SlotSet(REQUESTED_SLOT, None),
        ActiveLoop(None),
    ]


async def test_set_slot_and_deactivate():
    form_name = "my form"
    slot_name = "num_people"
    slot_value = "dasdasdfasdf"
    events = [
        ActiveLoop(form_name),
        SlotSet(REQUESTED_SLOT, slot_name),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(slot_value),
    ]
    tracker = DialogueStateTracker.from_events(sender_id="bla", evts=events)

    domain = f"""
    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
            {slot_name}:
            - type: from_text
    slots:
      {slot_name}:
        type: text
        influence_conversation: false
    """
    domain = Domain.from_yaml(domain)

    action = FormAction(form_name, None)
    events = await action.run(
        CollectingOutputChannel(),
        TemplatedNaturalLanguageGenerator(domain.responses),
        tracker,
        domain,
    )
    assert events == [
        SlotSet(slot_name, slot_value),
        SlotSet(REQUESTED_SLOT, None),
        ActiveLoop(None),
    ]


async def test_action_rejection():
    form_name = "my form"
    slot_to_fill = "some slot"
    tracker = DialogueStateTracker.from_events(
        sender_id="bla",
        evts=[
            ActiveLoop(form_name),
            SlotSet(REQUESTED_SLOT, slot_to_fill),
            ActionExecuted(ACTION_LISTEN_NAME),
            UserUttered("haha", {"name": "greet"}),
        ],
    )
    form_name = "my form"
    action = FormAction(form_name, None)
    domain = f"""
    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
            {slot_to_fill}:
            - type: from_entity
              entity: some_entity
    slots:
      {slot_to_fill}:
        type: unfeaturized
    """
    domain = Domain.from_yaml(domain)

    with pytest.raises(ActionExecutionRejection):
        await action.run(
            CollectingOutputChannel(),
            TemplatedNaturalLanguageGenerator(domain.responses),
            tracker,
            domain,
        )


@pytest.mark.parametrize(
    "validate_return_events, expected_events",
    [
        # Validate function returns SlotSet events for every slot to fill
        (
            [
                {"event": "slot", "name": "num_people", "value": "so_clean"},
                {"event": "slot", "name": "num_tables", "value": 5},
            ],
            [
                SlotSet("num_people", "so_clean"),
                SlotSet("num_tables", 5),
                SlotSet(REQUESTED_SLOT, None),
                ActiveLoop(None),
            ],
        ),
        # Validate function returns extra Slot Event
        (
            [
                {"event": "slot", "name": "num_people", "value": "so_clean"},
                {"event": "slot", "name": "some_other_slot", "value": 2},
            ],
            [
                SlotSet("num_people", "so_clean"),
                SlotSet("some_other_slot", 2),
                SlotSet("num_tables", 5),
                SlotSet(REQUESTED_SLOT, None),
                ActiveLoop(None),
            ],
        ),
        # Validate function only validates one of the candidates
        (
            [{"event": "slot", "name": "num_people", "value": "so_clean"}],
            [
                SlotSet("num_people", "so_clean"),
                SlotSet("num_tables", 5),
                SlotSet(REQUESTED_SLOT, None),
                ActiveLoop(None),
            ],
        ),
        # Validate function says slot is invalid
        (
            [{"event": "slot", "name": "num_people", "value": None}],
            [
                SlotSet("num_people", None),
                SlotSet("num_tables", 5),
                SlotSet(REQUESTED_SLOT, "num_people"),
            ],
        ),
        # Validate function decides to request a slot which is not part of the default
        # slot mapping
        (
            [{"event": "slot", "name": "requested_slot", "value": "is_outside"}],
            [
                SlotSet(REQUESTED_SLOT, "is_outside"),
                SlotSet("num_tables", 5),
                SlotSet("num_people", "hi"),
            ],
        ),
        # Validate function decides that no more slots should be requested
        (
            [
                {"event": "slot", "name": "num_people", "value": None},
                {"event": "slot", "name": REQUESTED_SLOT, "value": None},
            ],
            [
                SlotSet("num_people", None),
                SlotSet(REQUESTED_SLOT, None),
                SlotSet("num_tables", 5),
                SlotSet(REQUESTED_SLOT, None),
                ActiveLoop(None),
            ],
        ),
        # Validate function deactivates loop
        (
            [
                {"event": "slot", "name": "num_people", "value": None},
                {"event": "active_loop", "name": None},
            ],
            [
                SlotSet("num_people", None),
                ActiveLoop(None),
                SlotSet("num_tables", 5),
                SlotSet(REQUESTED_SLOT, None),
                ActiveLoop(None),
            ],
        ),
        # User rejected manually
        (
            [{"event": "action_execution_rejected", "name": "my form"}],
            [
                ActionExecutionRejected("my form"),
                SlotSet("num_tables", 5),
                SlotSet("num_people", "hi"),
            ],
        ),
    ],
)
async def test_validate_slots(
    validate_return_events: List[Dict], expected_events: List[Event]
):
    form_name = "my form"
    slot_name = "num_people"
    slot_value = "hi"
    events = [
        ActiveLoop(form_name),
        SlotSet(REQUESTED_SLOT, slot_name),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(slot_value, entities=[{"entity": "num_tables", "value": 5}]),
    ]
    tracker = DialogueStateTracker.from_events(sender_id="bla", evts=events)

    domain = f"""
    slots:
      {slot_name}:
        type: any
      num_tables:
        type: any
    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
            {slot_name}:
            - type: from_text
            num_tables:
            - type: from_entity
              entity: num_tables
    actions:
    - validate_{form_name}
    """
    domain = Domain.from_yaml(domain)
    action_server_url = "http:/my-action-server:5055/webhook"

    with aioresponses() as mocked:
        mocked.post(action_server_url, payload={"events": validate_return_events})

        action_server = EndpointConfig(action_server_url)
        action = FormAction(form_name, action_server)

        events = await action.run(
            CollectingOutputChannel(),
            TemplatedNaturalLanguageGenerator(domain.responses),
            tracker,
            domain,
        )
        assert events == expected_events


async def test_request_correct_slots_after_unhappy_path_with_custom_required_slots():
    form_name = "some_form"
    slot_name_1 = "slot_1"
    slot_name_2 = "slot_2"

    domain = f"""
        slots:
          {slot_name_1}:
            type: any
          {slot_name_2}:
            type: any
        forms:
          {form_name}:
            {REQUIRED_SLOTS_KEY}:
                {slot_name_1}:
                - type: from_intent
                  intent: some_intent
                  value: some_value
                {slot_name_2}:
                - type: from_intent
                  intent: some_intent
                  value: some_value
        actions:
        - validate_{form_name}
        """
    domain = Domain.from_yaml(domain)

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            ActiveLoop(form_name),
            SlotSet(REQUESTED_SLOT, "slot_2"),
            ActionExecuted(ACTION_LISTEN_NAME),
            UserUttered("hello", intent={"name": "greet", "confidence": 1.0},),
            ActionExecutionRejected(form_name),
            ActionExecuted("utter_greet"),
        ],
    )

    action_server_url = "http://my-action-server:5055/webhook"

    # Custom form validation action changes the order of the requested slots
    validate_return_events = [
        {"event": "slot", "name": REQUESTED_SLOT, "value": slot_name_2},
    ]

    # The form should ask the same slot again when coming back after unhappy path
    expected_events = [SlotSet(REQUESTED_SLOT, slot_name_2)]

    with aioresponses() as mocked:
        mocked.post(action_server_url, payload={"events": validate_return_events})

        action_server = EndpointConfig(action_server_url)
        action = FormAction(form_name, action_server)

        events = await action.run(
            CollectingOutputChannel(),
            TemplatedNaturalLanguageGenerator(domain.responses),
            tracker,
            domain,
        )
        assert events == expected_events


@pytest.mark.parametrize(
    "custom_events",
    [
        # Custom action returned no events
        [],
        # Custom action returned events but no `SlotSet` events
        [BotUttered("some text").as_dict()],
        # Custom action returned only `SlotSet` event for `required_slot`
        [SlotSet(REQUESTED_SLOT, "some value").as_dict()],
    ],
)
async def test_no_slots_extracted_with_custom_slot_mappings(custom_events: List[Event]):
    form_name = "my form"
    events = [
        ActiveLoop(form_name),
        SlotSet(REQUESTED_SLOT, "num_tables"),
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered("off topic"),
    ]
    tracker = DialogueStateTracker.from_events(sender_id="bla", evts=events)

    domain = f"""
    slots:
      num_tables:
        type: any
    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
            num_tables:
            - type: from_entity
              entity: num_tables
    actions:
    - validate_{form_name}
    """
    domain = Domain.from_yaml(domain)
    action_server_url = "http:/my-action-server:5055/webhook"

    with aioresponses() as mocked:
        mocked.post(action_server_url, payload={"events": custom_events})

        action_server = EndpointConfig(action_server_url)
        action = FormAction(form_name, action_server)

        with pytest.raises(ActionExecutionRejection):
            await action.run(
                CollectingOutputChannel(),
                TemplatedNaturalLanguageGenerator(domain.responses),
                tracker,
                domain,
            )


async def test_validate_slots_on_activation_with_other_action_after_user_utterance():
    form_name = "my form"
    slot_name = "num_people"
    slot_value = "hi"
    events = [
        ActionExecuted(ACTION_LISTEN_NAME),
        UserUttered(slot_value, entities=[{"entity": "num_tables", "value": 5}]),
        ActionExecuted("action_in_between"),
    ]
    tracker = DialogueStateTracker.from_events(sender_id="bla", evts=events)

    domain = f"""
    slots:
      {slot_name}:
        type: unfeaturized
    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
            {slot_name}:
            - type: from_text
    actions:
    - validate_{form_name}
    """
    domain = Domain.from_yaml(domain)
    action_server_url = "http:/my-action-server:5055/webhook"

    expected_slot_value = "✅"
    with aioresponses() as mocked:
        mocked.post(
            action_server_url,
            payload={
                "events": [
                    {"event": "slot", "name": slot_name, "value": expected_slot_value}
                ]
            },
        )

        action_server = EndpointConfig(action_server_url)
        action = FormAction(form_name, action_server)

        events = await action.run(
            CollectingOutputChannel(),
            TemplatedNaturalLanguageGenerator(domain.responses),
            tracker,
            domain,
        )

    assert events == [
        ActiveLoop(form_name),
        SlotSet(slot_name, expected_slot_value),
        SlotSet(REQUESTED_SLOT, None),
        ActiveLoop(None),
    ]


@pytest.mark.parametrize(
    "utterance_name", ["utter_ask_my_form_num_people", "utter_ask_num_people"],
)
def test_name_of_utterance(utterance_name: Text):
    form_name = "my_form"
    slot_name = "num_people"

    domain = f"""
    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
            {slot_name}:
            - type: from_text
    responses:
        {utterance_name}:
        - text: "How many people?"
    """
    domain = Domain.from_yaml(domain)

    action = FormAction(form_name, None)

    assert action._name_of_utterance(domain, slot_name) == utterance_name


def test_temporary_tracker():
    extra_slot = "some_slot"
    sender_id = "test"
    domain = Domain.from_yaml(
        f"""
        version: "2.0"
        slots:
          {extra_slot}:
            type: unfeaturized
        """
    )

    previous_events = [ActionExecuted(ACTION_LISTEN_NAME)]
    old_tracker = DialogueStateTracker.from_events(
        sender_id, previous_events, slots=domain.slots
    )
    new_events = [Restarted()]
    form_action = FormAction("some name", None)
    temp_tracker = form_action._temporary_tracker(old_tracker, new_events, domain)

    assert extra_slot in temp_tracker.slots.keys()
    assert list(temp_tracker.events) == [
        *previous_events,
        SlotSet(REQUESTED_SLOT),
        ActionExecuted(form_action.name()),
        *new_events,
    ]


def test_extract_requested_slot_default():
    """Test default extraction of a slot value from entity with the same name."""
    form_name = "some_form"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {
            "forms": {
                form_name: {
                    REQUIRED_SLOTS_KEY: {
                        "some_slot": [
                            {
                                "type": "from_entity",
                                "entity": "some_slot",
                                "value": "some_value",
                            }
                        ]
                    }
                }
            }
        }
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            ActiveLoop("some form"),
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "bla", entities=[{"entity": "some_slot", "value": "some_value"}]
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_requested_slot(tracker, domain, "some_slot")
    assert slot_values == {"some_slot": "some_value"}


@pytest.mark.parametrize(
    "slot_mapping, expected_value",
    [
        (
            {"type": "from_entity", "entity": "some_slot", "intent": "greet"},
            "some_value",
        ),
        (
            {"type": "from_intent", "intent": "greet", "value": "other_value"},
            "other_value",
        ),
        ({"type": "from_text"}, "bla"),
        ({"type": "from_text", "intent": "greet"}, "bla"),
        ({"type": "from_text", "not_intent": "other"}, "bla"),
    ],
)
def test_extract_requested_slot_when_mapping_applies(
    slot_mapping: Dict, expected_value: Text
):
    form_name = "some_form"
    entity_name = "some_slot"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {"forms": {form_name: {REQUIRED_SLOTS_KEY: {entity_name: [slot_mapping]}}}}
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            ActiveLoop(form_name),
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "bla",
                intent={"name": "greet", "confidence": 1.0},
                entities=[{"entity": entity_name, "value": "some_value"}],
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_requested_slot(tracker, domain, "some_slot")
    # check that the value was extracted for correct intent
    assert slot_values == {"some_slot": expected_value}


@pytest.mark.parametrize(
    "entities, expected_slot_values",
    [
        # Two entities were extracted for `ListSlot`
        (
            [
                {"entity": "topping", "value": "mushrooms"},
                {"entity": "topping", "value": "kebab"},
            ],
            ["mushrooms", "kebab"],
        ),
        # Only one entity was extracted for `ListSlot`
        ([{"entity": "topping", "value": "kebab"},], ["kebab"],),
    ],
)
def test_extract_requested_slot_with_list_slot(
    entities: List[Dict[Text, Any]], expected_slot_values: List[Text]
):
    form_name = "some_form"
    slot_name = "toppings"
    form = FormAction(form_name, None)

    domain = Domain.from_yaml(
        textwrap.dedent(
            f"""
    version: "2.0"

    slots:
      {slot_name}:
        type: list
        influence_conversation: false

    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
          {slot_name}:
          - type: from_entity
            entity: topping
    """
        )
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            ActiveLoop(form_name),
            SlotSet(REQUESTED_SLOT, slot_name),
            UserUttered(
                "bla", intent={"name": "greet", "confidence": 1.0}, entities=entities,
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
        slots=domain.slots,
    )

    slot_values = form.extract_requested_slot(tracker, domain, slot_name)

    assert slot_values[slot_name] == expected_slot_values


@pytest.mark.parametrize(
    "slot_mapping",
    [
        {"type": "from_entity", "entity": "some_slot", "intent": "some_intent"},
        {"type": "from_intent", "intent": "some_intent", "value": "some_value"},
        {"type": "from_intent", "intent": "greeted", "value": "some_value"},
        {"type": "from_text", "intent": "other"},
        {"type": "from_text", "not_intent": "greet"},
        {"type": "from_trigger_intent", "intent": "greet", "value": "value"},
    ],
)
def test_extract_requested_slot_mapping_does_not_apply(slot_mapping: Dict):
    form_name = "some_form"
    entity_name = "some_slot"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {"forms": {form_name: {REQUIRED_SLOTS_KEY: {entity_name: [slot_mapping]}}}}
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "bla",
                intent={"name": "greet", "confidence": 1.0},
                entities=[{"entity": entity_name, "value": "some_value"}],
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_requested_slot(tracker, domain, "some_slot")
    # check that the value was not extracted for incorrect intent
    assert slot_values == {}


@pytest.mark.parametrize(
    "trigger_slot_mapping, expected_value",
    [
        ({"type": "from_trigger_intent", "intent": "greet", "value": "ten"}, "ten"),
        (
            {
                "type": "from_trigger_intent",
                "intent": ["bye", "greet"],
                "value": "tada",
            },
            "tada",
        ),
    ],
)
async def test_trigger_slot_mapping_applies(
    trigger_slot_mapping: Dict, expected_value: Text
):
    form_name = "some_form"
    entity_name = "some_slot"
    slot_filled_by_trigger_mapping = "other_slot"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {
            "forms": {
                form_name: {
                    REQUIRED_SLOTS_KEY: {
                        entity_name: [
                            {
                                "type": "from_entity",
                                "entity": entity_name,
                                "intent": "some_intent",
                            }
                        ],
                        slot_filled_by_trigger_mapping: [trigger_slot_mapping],
                    }
                }
            }
        }
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "bla",
                intent={"name": "greet", "confidence": 1.0},
                entities=[{"entity": entity_name, "value": "some_value"}],
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_other_slots(tracker, domain)
    assert slot_values == {slot_filled_by_trigger_mapping: expected_value}


@pytest.mark.parametrize(
    "trigger_slot_mapping",
    [
        ({"type": "from_trigger_intent", "intent": "bye", "value": "ten"}),
        ({"type": "from_trigger_intent", "not_intent": ["greet"], "value": "tada"}),
    ],
)
async def test_trigger_slot_mapping_does_not_apply(trigger_slot_mapping: Dict):
    form_name = "some_form"
    entity_name = "some_slot"
    slot_filled_by_trigger_mapping = "other_slot"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {
            "forms": {
                form_name: {
                    REQUIRED_SLOTS_KEY: {
                        entity_name: [
                            {
                                "type": "from_entity",
                                "entity": entity_name,
                                "intent": "some_intent",
                            }
                        ],
                        slot_filled_by_trigger_mapping: [trigger_slot_mapping],
                    }
                }
            }
        }
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "bla",
                intent={"name": "greet", "confidence": 1.0},
                entities=[{"entity": entity_name, "value": "some_value"}],
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_other_slots(tracker, domain)
    assert slot_values == {}


@pytest.mark.parametrize(
    "mapping_not_intent, mapping_intent, mapping_role, "
    "mapping_group, entities, intent, expected_slot_values",
    [
        (
            "some_intent",
            None,
            None,
            None,
            [{"entity": "some_entity", "value": "some_value"}],
            "some_intent",
            {},
        ),
        (
            None,
            "some_intent",
            None,
            None,
            [{"entity": "some_entity", "value": "some_value"}],
            "some_intent",
            {"some_slot": "some_value"},
        ),
        (
            "some_intent",
            None,
            None,
            None,
            [{"entity": "some_entity", "value": "some_value"}],
            "some_other_intent",
            {"some_slot": "some_value"},
        ),
        (
            None,
            None,
            "some_role",
            None,
            [{"entity": "some_entity", "value": "some_value"}],
            "some_intent",
            {},
        ),
        (
            None,
            None,
            "some_role",
            None,
            [{"entity": "some_entity", "value": "some_value", "role": "some_role"}],
            "some_intent",
            {"some_slot": "some_value"},
        ),
        (
            None,
            None,
            None,
            "some_group",
            [{"entity": "some_entity", "value": "some_value"}],
            "some_intent",
            {},
        ),
        (
            None,
            None,
            None,
            "some_group",
            [{"entity": "some_entity", "value": "some_value", "group": "some_group"}],
            "some_intent",
            {"some_slot": "some_value"},
        ),
        (
            None,
            None,
            "some_role",
            "some_group",
            [
                {
                    "entity": "some_entity",
                    "value": "some_value",
                    "group": "some_group",
                    "role": "some_role",
                }
            ],
            "some_intent",
            {"some_slot": "some_value"},
        ),
        (
            None,
            None,
            "some_role",
            "some_group",
            [{"entity": "some_entity", "value": "some_value", "role": "some_role"}],
            "some_intent",
            {},
        ),
        (
            None,
            None,
            None,
            None,
            [
                {
                    "entity": "some_entity",
                    "value": "some_value",
                    "group": "some_group",
                    "role": "some_role",
                }
            ],
            "some_intent",
            # nothing should be extracted, because entity contain role and group
            # but mapping expects them to be None
            {},
        ),
    ],
)
def test_extract_requested_slot_from_entity(
    mapping_not_intent: Optional[Text],
    mapping_intent: Optional[Text],
    mapping_role: Optional[Text],
    mapping_group: Optional[Text],
    entities: List[Dict[Text, Any]],
    intent: Text,
    expected_slot_values: Dict[Text, Text],
):
    """Test extraction of a slot value from entity with the different restrictions."""

    form_name = "some form"
    form = FormAction(form_name, None)

    mapping = form.from_entity(
        entity="some_entity",
        role=mapping_role,
        group=mapping_group,
        intent=mapping_intent,
        not_intent=mapping_not_intent,
    )
    domain = Domain.from_dict(
        {"forms": {form_name: {REQUIRED_SLOTS_KEY: {"some_slot": [mapping]}}}}
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            ActiveLoop(form_name),
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "bla", intent={"name": intent, "confidence": 1.0}, entities=entities
            ),
        ],
    )

    slot_values = form.extract_requested_slot(tracker, domain, "some_slot")
    assert slot_values == expected_slot_values


@pytest.mark.parametrize(
    "some_other_slot_mapping, some_slot_mapping, entities, "
    "intent, expected_slot_values",
    [
        (
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_entity",
                    "role": "some_role",
                }
            ],
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [
                {
                    "entity": "some_entity",
                    "value": "some_value",
                    "role": "some_other_role",
                }
            ],
            "some_intent",
            {},
        ),
        (
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_entity",
                    "role": "some_role",
                }
            ],
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [{"entity": "some_entity", "value": "some_value", "role": "some_role"}],
            "some_intent",
            {"some_other_slot": "some_value"},
        ),
        (
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_entity",
                    "group": "some_group",
                }
            ],
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [
                {
                    "entity": "some_entity",
                    "value": "some_value",
                    "group": "some_other_group",
                }
            ],
            "some_intent",
            {},
        ),
        (
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_entity",
                    "group": "some_group",
                }
            ],
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [{"entity": "some_entity", "value": "some_value", "group": "some_group"}],
            "some_intent",
            {"some_other_slot": "some_value"},
        ),
        (
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_entity",
                    "group": "some_group",
                    "role": "some_role",
                }
            ],
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [
                {
                    "entity": "some_entity",
                    "value": "some_value",
                    "role": "some_role",
                    "group": "some_group",
                }
            ],
            "some_intent",
            {"some_other_slot": "some_value"},
        ),
        (
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_other_entity",
                }
            ],
            [{"entity": "some_entity", "value": "some_value"}],
            "some_intent",
            # other slot should be extracted because slot mapping is unique
            {"some_other_slot": "some_value"},
        ),
        (
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_entity",
                    "role": "some_role",
                }
            ],
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_other_entity",
                }
            ],
            [{"entity": "some_entity", "value": "some_value", "role": "some_role"}],
            "some_intent",
            # other slot should be extracted because slot mapping is unique
            {"some_other_slot": "some_value"},
        ),
        (
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [
                {
                    "type": "from_entity",
                    "intent": "some_intent",
                    "entity": "some_other_entity",
                }
            ],
            [{"entity": "some_entity", "value": "some_value", "role": "some_role"}],
            "some_intent",
            # other slot should not be extracted
            # because even though slot mapping is unique it doesn't contain the role
            {},
        ),
        (
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [{"type": "from_entity", "intent": "some_intent", "entity": "some_entity"}],
            [{"entity": "some_entity", "value": "some_value"}],
            "some_intent",
            # other slot should not be extracted because slot mapping is not unique
            {},
        ),
    ],
)
def test_extract_other_slots_with_entity(
    some_other_slot_mapping: List[Dict[Text, Any]],
    some_slot_mapping: List[Dict[Text, Any]],
    entities: List[Dict[Text, Any]],
    intent: Text,
    expected_slot_values: Dict[Text, Text],
):
    """Test extraction of other not requested slots values from entities."""

    form_name = "some_form"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {
            "forms": {
                form_name: {
                    REQUIRED_SLOTS_KEY: {
                        "some_other_slot": some_other_slot_mapping,
                        "some_slot": some_slot_mapping,
                    }
                }
            }
        }
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "bla", intent={"name": intent, "confidence": 1.0}, entities=entities
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_other_slots(tracker, domain)
    # check that the value was extracted for non requested slot
    assert slot_values == expected_slot_values


@pytest.mark.parametrize(
    "entities, expected_slot_values",
    [
        # Two entities were extracted for `ListSlot`
        (
            [
                {"entity": "topping", "value": "mushrooms"},
                {"entity": "topping", "value": "kebab"},
            ],
            ["mushrooms", "kebab"],
        ),
        # Only one entity was extracted for `ListSlot`
        ([{"entity": "topping", "value": "kebab"},], ["kebab"],),
    ],
)
def test_extract_other_list_slot_from_entity(
    entities: List[Dict[Text, Any]], expected_slot_values: List[Text]
):
    form_name = "some_form"
    slot_name = "toppings"
    domain = Domain.from_yaml(
        textwrap.dedent(
            f"""
    version: "2.0"

    slots:
      {slot_name}:
        type: list
        influence_conversation: false

    forms:
      {form_name}:
        {REQUIRED_SLOTS_KEY}:
          {slot_name}:
          - type: from_entity
            entity: topping
    """
        )
    )

    form = FormAction(form_name, None)

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            SlotSet(REQUESTED_SLOT, "some slot"),
            UserUttered(
                "bla", intent={"name": "greet", "confidence": 1.0}, entities=entities
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
        slots=domain.slots,
    )

    slots = form.extract_other_slots(tracker, domain)
    assert slots[slot_name] == expected_slot_values


@pytest.mark.parametrize(
    "domain_dict, expected_action",
    [
        (
            {
                "actions": ["action_ask_my_form_sun", "action_ask_sun"],
                "responses": {"utter_ask_my_form_sun": [{"text": "ask"}]},
            },
            "action_ask_my_form_sun",
        ),
        (
            {
                "actions": ["action_ask_sun"],
                "responses": {"utter_ask_my_form_sun": [{"text": "ask"}]},
            },
            "utter_ask_my_form_sun",
        ),
        (
            {
                "actions": ["action_ask_sun"],
                "responses": {"utter_ask_sun": [{"text": "hi"}]},
            },
            "action_ask_sun",
        ),
        (
            {
                "actions": ["action_ask_my_form_sun"],
                "responses": {"utter_ask_my_form_sun": [{"text": "hi"}]},
            },
            "action_ask_my_form_sun",
        ),
    ],
)
async def test_ask_for_slot(
    domain_dict: Dict,
    expected_action: Text,
    monkeypatch: MonkeyPatch,
    default_nlg: TemplatedNaturalLanguageGenerator,
):
    slot_name = "sun"

    action_from_name = Mock(return_value=action.ActionListen())
    endpoint_config = Mock()
    monkeypatch.setattr(
        action, action.action_for_name_or_text.__name__, action_from_name
    )

    form = FormAction("my_form", endpoint_config)
    domain = Domain.from_dict(domain_dict)
    await form._ask_for_slot(
        domain,
        default_nlg,
        CollectingOutputChannel(),
        slot_name,
        DialogueStateTracker.from_events("dasd", []),
    )

    action_from_name.assert_called_once_with(expected_action, domain, endpoint_config)


async def test_ask_for_slot_if_not_utter_ask(
    monkeypatch: MonkeyPatch, default_nlg: TemplatedNaturalLanguageGenerator
):
    action_from_name = Mock(return_value=action.ActionListen())
    endpoint_config = Mock()
    monkeypatch.setattr(
        action, action.action_for_name_or_text.__name__, action_from_name
    )

    form = FormAction("my_form", endpoint_config)
    events = await form._ask_for_slot(
        Domain.empty(),
        default_nlg,
        CollectingOutputChannel(),
        "some slot",
        DialogueStateTracker.from_events("dasd", []),
    )

    assert not events
    action_from_name.assert_not_called()


@pytest.mark.parametrize(
    "ignored_intents, slot_not_intent",
    [
        # for entity_type -> from_entity
        (
            # `ignored_intents` as a string and slot's not_intent as an empty list.
            "greet",
            [],
        ),
        (
            # `ignored_intents` as an empty list and slot's not_intent has a value.
            [],
            ["greet"],
        ),
        (
            # `ignored_intents` as a list of 2 values and slot's not_intent has one
            # value different than the ones in `ignored_intents`.
            ["chitchat", "greet"],
            ["inform"],
        ),
        (
            # `ignored_intents` as a list of 2 values and slot's not_intent has one
            # value that is included also in `ignored_intents`.
            ["chitchat", "greet"],
            ["chitchat"],
        ),
    ],
)
def test_ignored_intents_with_slot_type_from_entity(
    ignored_intents: Union[Text, List[Text]], slot_not_intent: Union[Text, List[Text]],
):
    form_name = "some_form"
    entity_name = "some_slot"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {
            "forms": {
                form_name: {
                    IGNORED_INTENTS: ignored_intents,
                    REQUIRED_SLOTS_KEY: {
                        entity_name: [
                            {
                                "type": "from_entity",
                                "entity": entity_name,
                                "not_intent": slot_not_intent,
                            }
                        ],
                    },
                }
            }
        }
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "hello",
                intent={"name": "greet", "confidence": 1.0},
                entities=[{"entity": entity_name, "value": "some_value"}],
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_other_slots(tracker, domain)
    assert slot_values == {}


@pytest.mark.parametrize(
    "ignored_intents, slot_not_intent",
    [
        # same examples for entity_type -> from_text
        (
            # `ignored_intents` as a string and slot's not_intent as an empty list.
            "greet",
            [],
        ),
        (
            # `ignored_intents` as an empty list and slot's not_intent has a value.
            [],
            ["greet"],
        ),
        (
            # `ignored_intents` as a list of 2 values and slot's not_intent has one
            # value different than the ones in `ignored_intents`.
            ["chitchat", "greet"],
            ["inform"],
        ),
        (
            # `ignored_intents` as a list of 2 values and slot's not_intent has one
            # value that is included also in `ignored_intents`.
            ["chitchat", "greet"],
            ["chitchat"],
        ),
    ],
)
def test_ignored_intents_with_slot_type_from_text(
    ignored_intents: Union[Text, List[Text]], slot_not_intent: Union[Text, List[Text]],
):
    form_name = "some_form"
    entity_name = "some_slot"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {
            "forms": {
                form_name: {
                    IGNORED_INTENTS: ignored_intents,
                    REQUIRED_SLOTS_KEY: {
                        entity_name: [
                            {
                                "type": "from_text",
                                "intent": "some_intent",
                                "not_intent": slot_not_intent,
                            }
                        ],
                    },
                }
            }
        }
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "hello",
                intent={"name": "greet", "confidence": 1.0},
                entities=[{"entity": entity_name, "value": "some_value"}],
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_other_slots(tracker, domain)
    assert slot_values == {}


@pytest.mark.parametrize(
    "ignored_intents, slot_not_intent, entity_type",
    [
        # same examples for entity_type -> from_intent
        (
            # `ignored_intents` as a string and slot's not_intent as an empty list.
            "greet",
            [],
            "from_intent",
        ),
        (
            # `ignored_intents` as an empty list and slot's not_intent has a value.
            [],
            ["greet"],
            "from_intent",
        ),
        (
            # `ignored_intents` as a list of 2 values and slot's not_intent has one
            # value different than the ones in `ignored_intents`.
            ["chitchat", "greet"],
            ["inform"],
            "from_intent",
        ),
        (
            # `ignored_intents` as a list of 2 values and slot's not_intent has one
            # value that is included also in `ignored_intents`.
            ["chitchat", "greet"],
            ["chitchat"],
            "from_intent",
        ),
        # same examples for entity_type -> from_trigger_intent
        (
            # `ignored_intents` as a string and slot's not_intent as an empty list.
            "greet",
            [],
            "from_trigger_intent",
        ),
        (
            # `ignored_intents` as an empty list and slot's not_intent has a value.
            [],
            ["greet"],
            "from_trigger_intent",
        ),
        (
            # `ignored_intents` as a list of 2 values and slot's not_intent has one
            # value different than the ones in `ignored_intents`.
            ["chitchat", "greet"],
            ["inform"],
            "from_trigger_intent",
        ),
        (
            # `ignored_intents` as a list of 2 values and slot's not_intent has one
            # value that is included also in `ignored_intents`.
            ["chitchat", "greet"],
            ["chitchat"],
            "from_trigger_intent",
        ),
    ],
)
def test_ignored_intents_with_other_type_of_slots(
    ignored_intents: Union[Text, List[Text]],
    slot_not_intent: Union[Text, List[Text]],
    entity_type: Text,
):
    form_name = "some_form"
    entity_name = "some_slot"
    form = FormAction(form_name, None)

    domain = Domain.from_dict(
        {
            "forms": {
                form_name: {
                    IGNORED_INTENTS: ignored_intents,
                    REQUIRED_SLOTS_KEY: {
                        entity_name: [
                            {
                                "type": entity_type,
                                "value": "affirm",
                                "intent": "true",
                                "not_intent": slot_not_intent,
                            }
                        ],
                    },
                }
            }
        }
    )

    tracker = DialogueStateTracker.from_events(
        "default",
        [
            SlotSet(REQUESTED_SLOT, "some_slot"),
            UserUttered(
                "hello",
                intent={"name": "greet", "confidence": 1.0},
                entities=[{"entity": entity_name, "value": "some_value"}],
            ),
            ActionExecuted(ACTION_LISTEN_NAME),
        ],
    )

    slot_values = form.extract_other_slots(tracker, domain)
    assert slot_values == {}
