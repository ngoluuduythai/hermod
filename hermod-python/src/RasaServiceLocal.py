""" RASA Service - local"""
import json
from MqttService import MqttService
from rasa.core.agent import Agent
from rasa.core.tracker_store import InMemoryTrackerStore
from rasa.core.events import SlotSet
#from rasa.core.channels.channel import UserMessage
# NaturalLanguageInterpreter,
from rasa.core.interpreter import RegexInterpreter, RasaNLUInterpreter
from rasa.core.utils import EndpointConfig

#from rasa.exceptions import ModelNotFound
from rasa.model import (
    # get_model_subdirectories,
    # get_latest_model,
    # unpack_model,
    get_model,
)


class RasaServiceLocal(MqttService):
    """ Load RASA model and tracker directly and use to handle intent and routing messages"""
    def __init__(
            self,

            config,
            loop
    ):
        """constructor"""
        super(
            RasaServiceLocal,
            self).__init__(config, loop)
        self.config = config
        self.subscribe_to = 'hermod/+/rasa/get_domain,hermod/+/rasa/set_slots' \
        + ',hermod/+/dialog/ended,hermod/+/dialog/init,hermod/+/nlu/externalparse,' \
        + 'hermod/+/nlu/parse,hermod/+/intent,hermod/+/intent,hermod/+/dialog/started'
        model_path = get_model(
            config['services']['RasaServiceLocal'].get('model_path'))
        endpoint = EndpointConfig(
            config['services']['RasaServiceLocal'].get('rasa_actions_url'))
        domain = 'domain.yml'
        self.tracker_store = InMemoryTrackerStore(domain)
        regex_interpreter = RegexInterpreter()
        self.text_interpreter = RasaNLUInterpreter(model_path + '/nlu')
        self.agent = Agent.load(
            model_path,
            action_endpoint=endpoint,
            tracker_store=self.tracker_store,
            interpreter=regex_interpreter)

    async def connect_hook(self):
        """mqtt connected callback"""
        # SUBSCRIBE
        for sub in self.subscribe_to.split(","):
            await self.client.subscribe(sub)
        await self.client.publish('hermod/rasa/ready', json.dumps({}))

    async def on_message(self, message):
        """handle mqtt message"""
        topic = "{}".format(message.topic)
        parts = topic.split("/")
        site = parts[1]
        payload_string = str(message.payload, encoding='utf-8')
        payload = {}
        text = ''
        try:
            payload = json.loads(payload_string)
        except json.JSONDecodeError:
            pass
        if topic == 'hermod/' + site + '/rasa/set_slots':
            if payload:
                await self.set_slots(site, payload)

        elif topic == 'hermod/' + site + '/nlu/parse':
            if payload:
                await self.client.publish('hermod/' + site \
                + '/display/startwaiting', json.dumps({}))
                text = payload.get('query')
                await self.nlu_parse_request(site, text, payload)
                await self.client.publish('hermod/' + site + '/display/stopwaiting', json.dumps({}))

        elif topic == 'hermod/' + site + '/nlu/externalparse':
            if payload:
                text = payload.get('query')
                await self.nlu_external_parse_request(site, text, payload)

        elif topic == 'hermod/' + site + '/intent':

            if payload:
                await self.client.publish('hermod/' + site \
                + '/display/startwaiting', json.dumps({}))
                await self.handle_intent(site, payload)
                await self.client.publish('hermod/' + \
                site + '/display/stopwaiting', json.dumps({}))

        elif topic == 'hermod/' + site + '/tts/finished':
            await self.client.unsubscribe('hermod/' + site + '/tts/finished')
            await self.finish(site, payload)

        elif topic == 'hermod/' + site + '/dialog/started':
            await self.reset_tracker(site)

        elif topic == 'hermod/' + site + '/ ':
            # save dialog init data to slots for custom actions
            tracker = self.tracker_store.get_or_create_tracker(site)
            tracker.update(SlotSet("hermod_client", json.dumps(payload)))
            self.tracker_store.save(tracker)

        elif topic == 'hermod/' + site + '/rasa/get_domain':
            await self.send_domain(site)

        elif topic == 'hermod/' + site + '/core/ended':
            await self.send_story(site, payload)

    async def send_story(self, site, payload):
        """send conversation history for a site"""
        # text = payload.get('text', '')
        tracker = self.tracker_store.get_or_create_tracker(site)
        response = tracker.export_stories()
        await self.client.publish('hermod/' + site + \
        '/rasa/story', json.dumps({'id': payload.get('id', ''), 'story': response}))

    async def send_domain(self, site):
        """send domain for a site"""
        await self.client.publish('hermod/' + site + \
        '/rasa/domain', json.dumps(self.agent.domain.as_dict()))

    async def reset_tracker(self, site):
        """reset conversation history for a site"""
        pass
        # self.log('RESSET tracker '+site)
        # tracker = self.tracker_store.get_or_create_tracker(site)
        # tracker._reset()

    async def handle_intent(self, site, payload):
        """handle intent message"""
        await self.client.publish('hermod/' + site + '/core/started', json.dumps(payload))
        if payload:
            intent_name = payload.get('intent', {}).get('name', '')
            entities_json = {}
            entities = payload.get('entities', [])
            for entity in entities:
                entities_json[entity.get('entity')] = entity.get('value')
            intent_json = "/" + intent_name + json.dumps(entities_json)
            messages = []
            responses = await self.agent.handle_text(intent_json, sender_id=site, \
            output_channel=None)
            for response in responses:
                messages.append(response.get("text"))
            if messages:
                message = '. '.join(messages)
                await self.client.subscribe('hermod/' + site + '/tts/finished')
                await self.client.publish('hermod/' + site + '/tts/say', json.dumps({
                    "text": message,
                    "id": payload.get('id', '')
                }))
            else:
                await self.finish(site, payload)
        else:
            await self.finish(site, payload)

    async def set_slots(self, site, payload):
        """set tracker slots"""
        tracker = self.tracker_store.get_or_create_tracker(site)
        if payload:
            for slot in payload.get('slots', []):
                tracker.update(SlotSet(slot.get('slot'), slot.get('value')))
            self.tracker_store.save(tracker)
            await self.client.publish('hermod/' + site + '/dialog/slots', \
            json.dumps(tracker.current_slot_values()))

    async def send_slots(self, site):
        """send a message with current tracker slots for site"""
        tracker = self.tracker_store.get_or_create_tracker(site)
        slots = tracker.current_slot_values()
        await self.client.publish('hermod/' + site + '/dialog/slots', json.dumps(slots))

    async def finish(self, site, payload):
        """ finish intent callback """
        tracker = self.tracker_store.get_or_create_tracker(site)
        slots = tracker.current_slot_values()
        if slots.get('hermod_force_continue', False) == 'true':
            tracker.update(SlotSet("hermod_force_continue", None))
            tracker.update(SlotSet("hermod_force_end", None))
            self.tracker_store.save(tracker)
            await self.client.publish('hermod/' + site + '/dialog/continue', json.dumps({
                "id": payload.get("id", "")
            }))
        elif slots.get('hermod_force_end', False) == 'true':
            tracker.update(SlotSet("hermod_force_continue", None))
            tracker.update(SlotSet("hermod_force_end", None))
            self.tracker_store.save(tracker)
            await self.client.publish('hermod/' + site + '/dialog/end', json.dumps({
                "id": payload.get("id", "")
            }))
        else:
            if self.config.get('keep_listening') == "true":
                await self.client.publish('hermod/' + site + '/dialog/continue', json.dumps({
                    "id": payload.get("id", "")
                }))
            else:
                await self.client.publish('hermod/' + site + '/dialog/end', json.dumps({
                    "id": payload.get("id", "")
                }))
        await self.send_slots(site)
        await self.client.publish('hermod/' + site + '/core/ended', json.dumps(payload))

    async def nlu_parse_request(self, site, text, payload):
        """ parse text into NLU json and send as message"""
        response = await self.text_interpreter.parse(text)
        response['id'] = payload.get('id', '')
        await self.client.publish('hermod/' + site + '/nlu/intent', json.dumps(response))

    async def nlu_external_parse_request(self, site, text, payload):
        """ parse text into NLU json and send as message without invoking hermod flow"""
        response = await self.text_interpreter.parse(text)
        response['id'] = payload.get('id', '')
        await self.client.publish('hermod/' + site + '/nlu/externalintent', json.dumps(response))
