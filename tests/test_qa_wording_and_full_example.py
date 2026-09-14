"""Q&A wording and complete-demo regressions with scripted model outputs."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pm_app.db import Store, validate_row
from pm_app.full_sample import load_full_sample
from pm_app.answer_policy import wording_issues
from pm_app.service import Service, format_context, make_chunks, recall_windows
from pm_app.ollama import OllamaClient, OllamaError
from test_answer_recovery import ScriptedClient, verified, EMPTY
from test_app import FakeClient, row

ROOT = Path(__file__).resolve().parents[1]


def claim(text, quote, label='M1', uncertainties=None):
    return {'status':'supported','claims':[{'text':text,'evidence':[{'id':label,'quote':quote}]}],
            'uncertainties':uncertainties or []}


class WordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'memory.sqlite3')
        self.store.create_project('one','One')
        self.original='Баннер пришлю завтра до 12:00.'
        self.store.import_rows([row(text=self.original)], 'one')
        self.allowed={m['label']:m for m in self.store.current('one')}
        self.good='Клиент обещал прислать баннер «завтра до 12:00» относительно даты исходного сообщения; календарная дата неизвестна.'

    def tearDown(self): self.tmp.cleanup()

    def issues(self,text): return wording_issues(claim(text,self.original)['claims'],self.allowed)

    def test_unanchored_tomorrow_is_detected(self):
        self.assertIn('relative_deadline_needs_source_anchor',self.issues('Клиент обещал баннер завтра до 12:00.')[0]['reasons'])

    def test_future_promise_not_guarantee(self):
        self.assertIn('promise_is_not_a_guaranteed_event',self.issues('Баннер будет передан завтра до 12:00.')[0]['reasons'])

    def test_attributed_anchored_promise_passes(self):
        self.assertEqual(self.issues(self.good),[])

    def test_unknown_calendar_date_is_not_created(self):
        self.assertIn('calendar_date_not_supported_by_cited_sources',self.issues('Клиент обещал баннер 09.09.2026.')[0]['reasons'])

    def test_negative_completion_without_proof_rejected(self):
        self.assertIn('missing_confirmation_is_not_negative_status',self.issues('Баннер не передан.')[0]['reasons'])

    def test_absence_of_confirmation_is_not_negative_completion(self):
        self.assertEqual(self.issues('В загруженных материалах нет подтверждения передачи баннера.'),[])

    def test_observed_negative_status_is_not_blocked(self):
        raw=row(text='Баннер не передан.')
        self.store.import_rows([raw],'one',allow_revisions=True)
        allowed={m['label']:m for m in self.store.current('one')}
        self.assertEqual(wording_issues(claim('Баннер не передан.',raw['text'],'M2')['claims'],allowed),[])

    def test_wording_repair_runs_before_verification(self):
        c=ScriptedClient([('answer',claim('Баннер будет передан завтра до 12:00.',self.original)),
            ('answer',claim(self.good,self.original)),('verify',verified('supported'))])
        r=Service(self.store,ROOT,c).ask('one','Когда баннер?')
        self.assertTrue(r['diagnostics']['wording_repair_used'])
        self.assertEqual(r['claims'][0]['text'],self.good)
        self.assertEqual(r['status'],'supported')
        self.assertEqual([s['phase'] for s in r['diagnostics']['steps']],['draft','wording_repair','verify_1'])

    def test_failed_repair_never_publishes_old_guarantee(self):
        bad=claim('Баннер будет передан завтра до 12:00.',self.original)
        c=ScriptedClient([('answer',bad),('answer',bad)])
        r=Service(self.store,ROOT,c).ask('one','Когда баннер?')
        self.assertEqual(r['status'],'not_verified')
        self.assertEqual(r['claims'],[])
        self.assertTrue(r['evidence_candidates'])
        self.assertNotIn('Баннер будет',r['message'])

    def test_repair_not_found_is_not_treated_as_no_information(self):
        c=ScriptedClient([('answer',claim('Баннер будет передан завтра.',self.original)),('answer',EMPTY)])
        r=Service(self.store,ROOT,c).ask('one','Когда баннер?')
        self.assertEqual(r['status'],'not_verified')
        self.assertTrue(r['evidence_candidates'])

    def test_repair_fake_quote_rejected(self):
        c=ScriptedClient([('answer',claim('Баннер будет передан завтра.',self.original)),
            ('answer',claim(self.good,'Invented evidence'))])
        with self.assertRaises(OllamaError): Service(self.store,ROOT,c).ask('one','Когда баннер?')
        self.assertEqual(self.store.answers('one'),[])

    def test_uncertainty_is_verified_and_dropped(self):
        c=ScriptedClient([('answer',claim(self.good,self.original,uncertainties=['Клиент уже пропустил дедлайн.'])),
            ('verify',verified('supported')),('verify',verified('unsupported'))])
        r=Service(self.store,ROOT,c).ask('one','Когда баннер?')
        self.assertEqual(r['uncertainties'],[])
        self.assertEqual(r['dropped_uncertainties'],1)
        self.assertEqual(r['claims'][0]['text'],self.good)
        self.assertIn('"kind":"uncertainty"',c.calls[-1]['user'])

    def test_supported_uncertainty_kept(self):
        u='Неизвестна календарная дата исходного сообщения.'
        c=ScriptedClient([('answer',claim(self.good,self.original,uncertainties=[u])),
            ('verify',verified('supported')),('verify',verified('supported'))])
        r=Service(self.store,ROOT,c).ask('one','Когда баннер?')
        self.assertEqual(r['uncertainties'],[u])

    def test_missing_uncertainty_verdict_fails_closed(self):
        c=ScriptedClient([('answer',claim(self.good,self.original,uncertainties=['Неизвестна дата.'])),
            ('verify',verified('supported')),('verify',{'checks':[]})])
        with self.assertRaises(OllamaError): Service(self.store,ROOT,c).ask('one','Когда баннер?')
        self.assertEqual(self.store.answers('one'),[])

    def test_known_source_date_can_anchor_tomorrow_across_year(self):
        original='Передам завтра.'
        raw={**row(text=original),'occurred_at':'2026-12-31T23:30:00+05:00'}
        self.store.import_rows([raw],'one',allow_revisions=True)
        allowed={m['label']:m for m in self.store.current('one')}
        safe=claim('Клиент обещал передать 01.01.2027.',original,'M2')
        self.assertEqual(wording_issues(safe['claims'],allowed),[])

    def test_single_project_without_topics_does_not_get_fake_topic(self):
        c=ScriptedClient([('answer',claim(self.good,self.original)),('verify',verified('supported'))])
        Service(self.store,ROOT,c).ask('one','When?')
        self.assertNotIn('TOPIC=',c.calls[0]['user'])

class TopicClient(FakeClient):
    def chat(self,system,user,schema,max_tokens=2048):
        if 'checks' in schema['properties']: return super().chat(system,user,schema,max_tokens)
        if 'message_ids' in schema['properties']: return super().chat(system,user,schema,max_tokens)
        self.chat_calls.append((system,user,schema))
        lines=[json.loads(line) for line in user.splitlines() if line.startswith('["M')]
        label,speaker,date,clock,offset,text=lines[0]
        return claim('A statement in the selected topic.',text[:600],label)


@unittest.skipUnless((ROOT / "samples" / "full_xpage" / "messages.jsonl").exists(), "case-provided full-sample fixture is not included in the public repository")
class FullExampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'memory.sqlite3')
        self.client=TopicClient();self.service=Service(self.store,ROOT,self.client)
        self.rows,self.manifest=load_full_sample(ROOT)

    def tearDown(self): self.tmp.cleanup()

    def test_all_102_messages_imported_once(self):
        r=self.service.full_demo_import();self.assertEqual(r['added'],102)
        self.assertEqual(self.service.full_demo_import()['unchanged'],102)
        self.assertEqual(len(self.store.current('xpage-full-example')),102)

    def test_three_independent_topics_and_13_sources(self):
        self.assertEqual(len({r['source_id'] for r in self.rows}),13)
        self.assertEqual({t['id']:t['message_count'] for t in self.manifest['topics']},
            {'urbankey':37,'retailflow':31,'industrypulse':34})
        self.assertEqual({r['source_locator']['table'] for r in self.rows},set(range(1,14)))

    def test_short_confirmations_not_lost(self):
        self.assertTrue(any(r['text']=='Да.' for r in self.rows))
        self.assertTrue(any(r['text']=='Ок, спасибо.' for r in self.rows))

    def test_stress_is_one_ordered_source_with_original_speakers(self):
        stress=[r for r in self.rows if r['source_locator']['table']==13]
        self.assertEqual(len(stress),9)
        self.assertEqual(len({r['source_id'] for r in stress}),1)
        self.assertTrue(all(' / ' in r['speaker'] for r in stress))
        self.assertEqual([r['source_locator']['row'] for r in stress],list(range(2,11)))

    def test_email_body_and_headers_retained(self):
        emails=[r for r in self.rows if r['source_type']=='email']
        self.assertEqual(len(emails),3)
        self.assertTrue(all(len(r['email_headers'])==3 for r in emails))

    def test_no_fabricated_source_dates(self):
        self.assertTrue(all(r['occurred_at'] is None and r['timezone'] is None for r in self.rows))
        transcript=[r for r in self.rows if r['source_type']=='meeting_transcript']
        self.assertTrue(all('offset_text' in r and 'clock_time' not in r for r in transcript))

    def test_template_and_requirements_not_imported_as_facts(self):
        self.assertFalse(any('1fee297a' in r['message_id'] for r in self.rows))
        self.assertTrue(all(r['source_locator']['file'].endswith(self.manifest['original_document']) for r in self.rows))

    def test_original_three_projects_unchanged(self):
        self.service.demo_import()
        before={p:self.store.current(p) for p in ('urbankey','retailflow','industrypulse')}
        self.service.full_demo_import()
        for p in before:self.assertEqual(before[p],self.store.current(p))

    def test_all_scope_split_reads_each_message_and_saves_one_answer(self):
        self.service.full_demo_import()
        result=self.service.ask('xpage-full-example','What is recorded in each topic?')
        self.assertEqual(result['scope']['message_count'],102)
        self.assertEqual(len(result['groups']),3)
        self.assertEqual(sum(g['answer']['scope']['message_count'] for g in result['groups']),102)
        self.assertEqual(len(self.store.answers('xpage-full-example')),1)
        self.assertEqual(len(self.client.chat_calls),6)
        for system,user,schema in self.client.chat_calls:
            if 'TOPIC="UrbanKey"' in user:
                self.assertNotIn('TOPIC="RetailFlow"',user)
                self.assertNotIn('courier',user)
            self.assertLess(len(system)+len(user)+len(json.dumps(schema,ensure_ascii=False)),11500)

    def test_topic_filter_does_not_send_other_topic(self):
        self.service.full_demo_import()
        r=self.service.ask('xpage-full-example','Topic question',topic_id='retailflow')
        self.assertEqual(r['scope']['message_count'],31)
        self.assertEqual(r['scope']['topic_id'],'retailflow')
        for _,user,_ in self.client.chat_calls:
            self.assertNotIn('MES',user)
            self.assertNotIn('DataBridge',user)

    def test_source_and_topic_filters_intersect(self):
        self.service.full_demo_import()
        r=self.service.ask('xpage-full-example','Question',source_id='xpage-mixed-stress',topic_id='retailflow')
        self.assertEqual(r['scope']['message_count'],3)
        self.assertTrue(all(s['raw']['topic_id']=='retailflow' for s in r['sources']))

    def test_unknown_topic_errors_without_calling_model(self):
        self.service.full_demo_import()
        with self.assertRaises(ValueError): self.service.ask('xpage-full-example','Question',topic_id='other')
        self.assertEqual(self.client.chat_calls,[])

    def test_failed_topic_is_not_saved_as_complete(self):
        self.service.full_demo_import()
        with patch.object(self.client,'chat',side_effect=OllamaError('unavailable')):
            with self.assertRaises(OllamaError):self.service.ask('xpage-full-example','Question')
        self.assertEqual(self.store.answers('xpage-full-example'),[])

    def test_search_and_recall_windows_do_not_mix_topics(self):
        self.service.full_demo_import();msgs=self.store.current('xpage-full-example')
        for block in recall_windows(msgs):self.assertEqual(len({m['raw']['topic_id'] for m in block}),1)
        mapping={m['id']:m for m in msgs}
        for chunk in make_chunks(msgs):
            self.assertEqual(len({mapping[n]['raw']['topic_id'] for n in chunk['revision_ids']}),1)

    def test_search_topic_filter_limits_hits(self):
        self.service.full_demo_import()
        result=self.service.search('xpage-full-example','status',topic_id='retailflow')
        self.assertTrue(result['hits'])
        self.assertTrue(all(s['raw']['topic_id']=='retailflow' for h in result['hits'] for s in h['sources']))

    def test_all_topic_diagnostics_have_no_source_content(self):
        self.service.full_demo_import();r=self.service.ask('xpage-full-example','PRIVATE_QUESTION')
        text=json.dumps(r['diagnostics'],ensure_ascii=False)
        for secret in ('PRIVATE_QUESTION','Баннер','RetailFlow','UrbanKey','IndustryPulse'):self.assertNotIn(secret,text)

    def test_malformed_topic_metadata_rejected(self):
        for extra in ({'topic_id':[]},{'topic_id':'a','topic_name':[]},{'topic_name':'a'}, {'topic_id':'../a','topic_name':'a'}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):validate_row({**row(),**extra})

    def test_corrupt_fixture_rejected_before_database_write(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)/'samples/full_xpage';folder.mkdir(parents=True)
            (folder/'manifest.json').write_text(json.dumps(self.manifest))
            (folder/'messages.jsonl').write_text('{}\n')
            with self.assertRaisesRegex(ValueError,'checksum'):load_full_sample(Path(t))

    def test_complete_scope_fits_without_context_setting_change(self):
        self.service.full_demo_import()
        for topic in self.manifest['topics']:
            msgs=[m for m in self.store.current('xpage-full-example') if m['raw']['topic_id']==topic['id']]
            self.assertLess(len(format_context(msgs)),6400)


if __name__=='__main__': unittest.main()
