import unittest
from agentclient.enterprise_desktop import convert


class DesktopV2Tests(unittest.TestCase):
    def item(self,item_type,**fields):
        return {'type':'event_msg','timestamp':'2026-09-25T12:00:00Z',
            'payload':{'type':'item_completed','thread_id':'session','turn_id':'turn',
                'item':{'id':'item','type':item_type,**fields}}}

    def test_actual_shape_commentary_mcp_patch_and_errors(self):
        for item in (self.item('AgentMessage',content='Visible progress',phase='commentary'),
                     self.item('McpToolCall',tool='save_file',arguments={'path':'/tmp/My Data/file.csv'},result={'status':'saved'}),
                     self.item('FileChange',changes=[{'path':'guide.md','diff':'+ CSV'}],status='failed',stderr='denied')):
            result=convert(item,'session','maple','agent-one')
            self.assertEqual(result['external_id'],'item')
            self.assertNotEqual(result['disposition'],'unsupported')
        self.assertIsNone(convert(self.item('Reasoning',raw_content='HIDDEN_CANARY'),'session','maple','agent-one'))
        self.assertEqual(convert(self.item('NewThing',content='not parsed'),'session','maple','agent-one')['disposition'],'unsupported')

    def test_protocol_inputs_retain_original_arguments_and_call_id(self):
        record={'type':'response_item','payload':{'type':'custom_tool_call','id':'native-id',
            'call_id':'call-id','name':'exec_command','input':'cat AGENTS.md'}}
        value=convert(record,'session','maple','agent-one',turn='turn',order=128)
        self.assertEqual(value['blocks'][0]['value'],'cat AGENTS.md')
        self.assertEqual(value['event']['call_id'],'call-id')
        self.assertEqual(value['event']['order'],128)

    def test_protocol_search_retains_inputs_results_and_redacts_credentials(self):
        examples = [
            {'type':'web_search_call','action':{'type':'search','query':'synthetic docs'},'status':'completed'},
            {'type':'tool_search_call','call_id':'lookup-1','arguments':{'query':'synthetic tools','api_key':'secret'},'status':'completed'},
            {'type':'tool_search_output','call_id':'lookup-1','tools':[{'name':'example.lookup'}],'status':'completed'},
        ]
        for payload in examples:
            with self.subTest(kind=payload['type']):
                value=convert({'type':'response_item','payload':payload},'session','maple','agent-one',turn='turn',order=12)
                self.assertEqual(value['event']['kind'],'PostToolUse')
                self.assertIn(value['disposition'],{'accepted','redacted'})
                self.assertNotIn('"api_key": "secret"',__import__('json').dumps(value))
                if payload.get('call_id'):self.assertEqual(value['event']['call_id'],'lookup-1')
        self.assertEqual(value['blocks'][1]['value']['tools'],[{'name':'example.lookup'}])

    def test_protocol_subagent_report_keeps_visible_speaker_and_excludes_encrypted_parts(self):
        payload={'type':'agent_message','id':'report-1','author':'/root/checker','recipient':'/root',
            'content':[{'type':'input_text','text':'Synthetic verification passed.'},
                       {'type':'encrypted_content','data':'HIDDEN_CANARY'}]}
        value=convert({'type':'response_item','payload':payload},'session','maple','agent-one',turn='turn',order=42)
        self.assertEqual(value['event']['kind'],'AssistantMessage')
        self.assertEqual(value['actor'],'/root/checker')
        self.assertEqual(value['event']['channel'],'collaboration')
        self.assertEqual(value['blocks'][0]['value'],'Synthetic verification passed.')
        self.assertNotIn('HIDDEN_CANARY',str(value))
        self.assertEqual(value['event']['parent_id'],'/root')

    def test_historical_visible_host_shapes_are_structured_evidence(self):
        examples = [
            (self.item('ContextCompaction'), 'PostCompact'),
            (self.item('Plan', text='First inspect the synthetic file, then verify it.'), 'AssistantMessage'),
            (self.item('WebSearch', query='synthetic index format', action='search'), 'PostToolUse'),
            (self.item('Extension', kind='search', query='synthetic docs',
                       results=[{'title':'Example','text':'Synthetic finding'}]), 'PostToolUse'),
            (self.item('DynamicToolCall', tool='example.lookup', arguments={'key':'x'},
                       content_items=[{'type':'text','text':'Synthetic finding'}], success=True), 'PostToolUse'),
            (self.item('SubAgentActivity', kind='completed', agent_path='/root/worker',
                       agent_thread_id='synthetic-child'), 'PostToolUse'),
        ]
        for record, expected in examples:
            with self.subTest(expected=expected, kind=record['payload']['item']['type']):
                result = convert(record,'session','maple','agent-one')
                self.assertEqual(result['event']['kind'],expected)
                self.assertNotEqual(result['disposition'],'unsupported')
        # A path to an image is not its pixels; historical image-dependent turns
        # must retain an explicit gap rather than silently claim full coverage.
        image = convert(self.item('ImageView', path='/synthetic/screenshot.png'),
                        'session','maple','agent-one')
        self.assertEqual(image['disposition'],'unsupported')
