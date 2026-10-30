import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import io
from lxml import etree
from adoc_dita.converter import ROOT, convert_files, convert_text, schema, PARSER
from adoc_dita.repository import (compare, compare_pull_request, git,
                                  prepare_repository, pull_request_location,
                                  pull_request_metadata, refs)
from adoc_dita.report import file_diff, zip_report


class ConversionTests(unittest.TestCase):
    def test_three_types_and_explicit_selection(self):
        unknown = convert_text('= A title\n\nA paragraph.\n')
        self.assertEqual(unknown['status'], 'error')
        self.assertIn('Select Concept', unknown['diagnostics'][0]['message'])
        inferred = convert_text('= A title\n\nA paragraph.\n', filename='con-example.adoc')
        self.assertEqual(inferred['status'], 'ok', inferred)
        self.assertEqual(inferred['topic_type'], 'concept')

    def test_user_concept_and_ldap_task_structures(self):
        results = convert_files(ROOT / 'examples', ['con-role-based-access-control-in-rhdh.adoc', 'proc-share-a-secret-with-ldap.adoc'], attribute_files=['attributes.adoc'])
        for result in results:
            self.assertEqual(result['status'], 'ok', result['diagnostics'])
        concept = etree.fromstring(results[0]['xml'].encode(), PARSER())
        self.assertEqual(concept.tag, 'concept')
        self.assertIsNotNone(concept.find('shortdesc'))
        self.assertEqual(len(concept.findall('.//ol/li/ul/li')), 3)
        task = etree.fromstring(results[1]['xml'].encode(), PARSER())
        self.assertEqual(task.tag, 'task')
        self.assertEqual(len(task.findall('taskbody/steps/step')), 4)
        self.assertIsNotNone(task.find('.//step/info/dl'))
        self.assertIsNotNone(task.find('.//step/info/note[@type="warning"]'))
        self.assertIsNotNone(task.find('taskbody/result/ul'))
        self.assertIn('--from-file=./ldap_certs.pem', task.find('.//codeblock').text)
        link = task.find('.//prereq/ul/li/xref')
        self.assertEqual(link.get('scope'), 'external')
        self.assertTrue(link.get('href').startswith('https://'))

    def test_reference_matches_provided_vocabulary(self):
        sample = etree.parse(str(ROOT / 'examples/provided-reference.xml'), PARSER())
        self.assertTrue(schema('reference').validate(sample))
        result = convert_files(ROOT, ['examples/reference.adoc'])[0]
        self.assertEqual(result['status'], 'ok', result['diagnostics'])
        document = etree.fromstring(result['xml'].encode(), PARSER())
        for path in ['shortdesc', 'refbody/section', './/dl/dlentry/dt', './/dd', './/table/tgroup', './/codeblock', './/codeph', './/i']:
            self.assertTrue(document.findall(path), path)
        self.assertEqual(document.get('id'), sample.getroot().get('id'))
        code = document.find('.//codeblock').text
        self.assertEqual(json.loads(code)[0]['name'], 'role:default/guests')
        self.assertNotIn('<p>+</p>', result['xml'])

    def test_task_contains_steps_and_prerequisites(self):
        result = convert_files(ROOT, ['examples/procedure.adoc'])[0]
        self.assertEqual(result['status'], 'ok', result['diagnostics'])
        document = etree.fromstring(result['xml'].encode(), PARSER())
        self.assertEqual(document.tag, 'task')
        self.assertEqual(len(document.findall('.//steps/step')), 2)
        self.assertIsNotNone(document.find('.//prereq'))
        self.assertEqual(document.find('.//codeblock').text, 'npm install example-plugin')

    def test_stable_paste_ids_and_escaping(self):
        source = '= Stable\n\nA & B, <input>, *bold* and `code`.\n'
        one, two = convert_text(source, kind="concept"), convert_text(source, kind="concept")
        self.assertEqual(one['status'], 'ok')
        self.assertEqual(one['xml'], two['xml'])
        self.assertIn('&amp;', one['xml'])
        changed = convert_text(source.replace('A & B', 'B & C'), kind='concept')
        self.assertEqual(etree.fromstring(one['xml'].encode(), PARSER()).get('id'), etree.fromstring(changed['xml'].encode(), PARSER()).get('id'))

    def test_literal_placeholders_are_not_documentation_attributes(self):
        source = '''\
:_mod-docs-content-type: REFERENCE

= Configuration variables

Use `backstage-{cr-name}` in the configuration. Store ${TOKEN} in your secrets.

[%header,cols=1*]
|===
|Value
|`{secret-name}`
|===

[source,yaml]
----
name: {bucket_name}
password: {postgresql_admin_password}
----
'''
        result = convert_text(source)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['missing_attributes'], [])
        self.assertIn('backstage-{cr-name}', result['xml'])
        self.assertIn('${TOKEN}', result['xml'])
        self.assertIn('{secret-name}', result['xml'])
        self.assertIn('name: {bucket_name}', result['xml'])
        prose = convert_text('= Missing\n\nUse {product-name}.\n', kind='concept')
        self.assertEqual(prose['status'], 'error')
        self.assertEqual(prose['missing_attributes'], ['product-name'])
        self.assertEqual(sum(d['message'] == 'Unresolved documentation attribute: product-name'
                             for d in prose['diagnostics']), 1)

    def test_invalid_or_incomplete_input_is_not_success(self):
        for source in ['', 'No title\n', '= Broken\n\n{missing}\n', '= Broken\n\n++++\n<notdita/>\n++++\n']:
            result = convert_text(source, kind="concept")
            self.assertEqual(result['status'], 'error', result)
            self.assertIsNone(result['xml'])

    def test_local_and_remote_include_confinement(self):
        for include in ['/etc/passwd', '../outside.adoc', 'https://example.com/a.adoc']:
            result = convert_text('= Unsafe\n\ninclude::' + include + '[]\n', kind='concept')
            self.assertEqual(result['status'], 'error', result)

    def test_paste_with_local_attributes_file(self):
        with tempfile.TemporaryDirectory(prefix='attributes with spaces ') as folder:
            root = Path(folder)
            attributes = root / 'attributes.adoc'
            attributes.write_text(':_mod-docs-content-type: SNIPPET\n:product: File product\ninclude::extra.adoc[]\n')
            (root / 'extra.adoc').write_text('ifdef::enabled[]\n:config-file: app-config.yaml\nendif::[]\n')
            source = ':_mod-docs-content-type: CONCEPT\n\n[id="configuration_{context}"]\n= Configuration\n\n{product} uses `{config-file}`.\n'
            # A pasted filename may collide with a real file: no reads or writes to it.
            (root / 'document.adoc').write_text('Leave this file unchanged.\n')
            original = {p.name: p.read_bytes() for p in root.iterdir()}
            standalone = convert_text(source, attribute_file=str(attributes), attributes={'enabled': ''})
            self.assertEqual(standalone['status'], 'ok', standalone)
            self.assertEqual(standalone['missing_attributes'], [])
            self.assertIn('id="configuration"', standalone['xml'])
            self.assertEqual(standalone['standalone_context'], 'base-id')
            overrides = {'enabled': '', 'context': 'guide', 'product': 'Override product'}
            first = convert_text(source, attribute_file=str(attributes), attributes=overrides)
            self.assertEqual(first['status'], 'ok', first['diagnostics'])
            self.assertIn('id="configuration_guide"', first['xml'])
            self.assertIn('Override product uses <codeph>app-config.yaml</codeph>', first['xml'])
            self.assertEqual(first['dependencies'], ['attributes.adoc', 'extra.adoc'])
            self.assertEqual(first, convert_text(source, attribute_file=str(attributes), attributes=overrides))
            self.assertEqual(original, {p.name: p.read_bytes() for p in root.iterdir()})
            attributes.write_text(':product: Updated on disk\n')
            updated = convert_text('= Product\n\n{product}\n', attribute_file=str(attributes), kind='concept')
            self.assertIn('Updated on disk', updated['xml'])

    def test_standalone_context_is_only_removed_from_ids_and_xrefs(self):
        source = ':_mod-docs-content-type: CONCEPT\n\n[id="topic_{context}"]\n= Topic\n\nSee xref:other_{context}[Other]. The selected context is {context}.\n'
        missing = convert_text(source)
        self.assertEqual(missing['status'], 'error')
        self.assertEqual(missing['missing_attributes'], ['context'])
        self.assertTrue(any('Possible invalid reference: #other' in d['message'] for d in missing['diagnostics']))
        explicit = convert_text(source, attributes={'context': 'admin'})
        self.assertIn('id="topic_admin"', explicit['xml'])
        self.assertIn('href="#other_admin"', explicit['xml'])
        link_source = ':_mod-docs-content-type: CONCEPT\n\n[id="topic_{context}"]\n= Topic\n\nSee {book-url}#chapter_{context}[Chapter].\n'
        standalone = convert_text(link_source, attributes={'book-url': 'https://example.com/book'})
        self.assertEqual(standalone['status'], 'ok', standalone)
        self.assertIn('href="https://example.com/book#chapter"', standalone['xml'])

    def test_supplied_topic_vocabulary_table_note_links_and_code(self):
        source = '''\
:_mod-docs-content-type: REFERENCE

[id="plugins_{context}"]
= Plugins

[role="_abstract"]
Supported plugins and their required variables.

[IMPORTANT]
====
Preview features can change.
====

[%header,cols=4*]
|===
|*Name* |*Plugin* |*Version* |*Path and required variables*
|Example |`https://npmjs.com/package/example[example-plugin]` |1.0.0
|`./plugins/example`

`EXAMPLE_TOKEN`
|===

[source,yaml]
----
plugins:
  - example
----
'''
        result = convert_text(source)
        self.assertEqual(result['status'], 'ok', result)
        document = etree.fromstring(result['xml'].encode(), PARSER())
        self.assertEqual(document.get('id'), 'plugins')
        self.assertIsNotNone(document.find('.//note[@type="important"]'))
        table = document.find('.//table/tgroup')
        self.assertEqual(table.get('cols'), '4')
        self.assertEqual(len(table.findall('colspec')), 4)
        self.assertEqual(len(table.findall('thead/row/entry')), 4)
        link = document.find('.//codeph/xref')
        self.assertEqual(link.get('scope'), 'external')
        self.assertEqual(link.get('format'), 'html')
        self.assertEqual(document.find('.//codeblock').get('outputclass'), 'language-yaml')

    def test_task_preserves_non_list_content_around_procedure_steps(self):
        source = '''\
:_mod-docs-content-type: PROCEDURE

[id="configure_{context}"]
= Configure the service

[role="_abstract"]
Configure the service safely.

.Procedure

Read the settings before starting.

. Open the configuration.
. Save the configuration.
+
The following table describes the result.

[cols="2",options="header"]
|===
| Setting | Result
| enabled | The service starts.
|===
'''
        result = convert_text(source)
        self.assertEqual(result['status'], 'review', result)
        document = etree.fromstring(result['xml'].encode(), PARSER())
        self.assertEqual(document.findtext('.//context/p'), 'Read the settings before starting.')
        self.assertEqual(document.findtext('.//steps/step[2]/cmd').strip(), 'Save the configuration.')
        self.assertIsNotNone(document.find('.//steps/step[2]/info/table'))
        self.assertFalse(any('skipping' in d['message'] for d in result['diagnostics']))
        self.assertTrue(any('Preserved 2 non-list procedure element' in d['message']
                            for d in result['diagnostics']))

    def test_local_attributes_path_and_include_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for path, message in [('relative.adoc', 'absolute path'), (str(root / 'missing.adoc'), 'not found'), (str(root / 'file.txt'), 'ending in .adoc')]:
                with self.assertRaisesRegex(ValueError, message):
                    convert_text('= Test\n\nText.\n', attribute_file=path, kind='concept')
            attrs = root / 'attributes.adoc'
            for target in ['../outside.adoc', 'https://example.com/attributes.adoc']:
                attrs.write_text('include::' + target + '[]\n')
                result = convert_text('= Test\n\nText.\n', attribute_file=str(attrs), kind='concept')
                self.assertEqual(result['status'], 'error', result)
                self.assertIsNone(result['xml'])

    def test_native_includes_tags_lines_and_conditions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'modules').mkdir(); (root/'snippets').mkdir()
            (root/'modules/main.adoc').write_text('= Includes\n\nifdef::enabled[]\ninclude::../snippets/test.adoc[tag=selected]\nendif::[]\n')
            (root/'snippets/test.adoc').write_text('Excluded.\n// tag::selected[]\nIncluded *text*.\n// end::selected[]\n')
            result = convert_files(root, ['modules/main.adoc'], attributes={'enabled': ''}, kind='concept')[0]
            self.assertEqual(result['status'], 'ok', result['diagnostics'])
            self.assertIn('Included <b>text</b>', result['xml']); self.assertNotIn('Excluded.', result['xml'])
            self.assertEqual(result['dependencies'], ['snippets/test.adoc'])
            (root/'modules/main.adoc').write_text('= Lines\n\ninclude::../snippets/test.adoc[lines=3]\n')
            result = convert_files(root, ['modules/main.adoc'], kind='concept')[0]
            self.assertEqual(result['status'], 'ok', result['diagnostics'])
            self.assertIn('Included <b>text</b>', result['xml'])
            (root/'modules/main.adoc').write_text('= Root relative\n\ninclude::snippets/test.adoc[tag=selected]\n')
            result = convert_files(root, ['modules/main.adoc'], kind='concept')[0]
            self.assertEqual(result['status'], 'ok', result['diagnostics'])
            self.assertIn('Included <b>text</b>', result['xml'])

    def test_code_whitespace_and_xml_characters(self):
        code = 'if (a < b && b > 0) {\n  return "hello";\n}'
        result = convert_text('= Code\n\n[source,javascript]\n----\n' + code + '\n----\n', kind='concept')
        self.assertEqual(result['status'], 'ok', result)
        node = etree.fromstring(result['xml'].encode(), PARSER()).find('.//codeblock')
        self.assertEqual(node.text, code)

    def test_continuation_artifact_warns_and_preserves(self):
        result = convert_text('= Plus\n\nA paragraph.\n\n+\n\nAnother paragraph.\n', kind='concept')
        self.assertEqual(result['status'], 'review', result)
        self.assertIn('<p>+</p>', result['xml'])

    def test_xref_xml_suffix(self):
        result = convert_text('= Links\n\nSee xref:other.adoc[Other topic].\n', kind='concept')
        self.assertIn('href="other.xml"', result['xml'])


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Test Writer')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        git(self.repo, 'config', 'commit.gpgsign', 'false')
        self.write('artifacts/attributes.adoc', ':product: One\n')
        self.write('assemblies/assembly-guide.adoc', '= Assembly guide\n\ninclude::../modules/changed.adoc[]\n')
        self.write('modules/con-generated.template.adoc', '= Template\n\n{generated-value}\n')
        self.write('modules/changed.adoc', '= Changed\n\nOriginal text.\n')
        self.write('modules/dependent.adoc', '= Dependent\n\n{product}\n\ninclude::../snippets/shared.adoc[]\n')
        self.write('modules/deleted.adoc', '= Deleted\n\n' + '\n'.join('Remove this old procedure step '+str(i) for i in range(20)) + '\n')
        self.write('modules/rename.adoc', '[id="persistent"]\n= Renamed\n\nSame content.\n')
        self.write('modules/untouched.adoc', '= Untouched\n\nSame content.\n')
        self.write('snippets/shared.adoc', 'Snippet one.\n')
        self.commit('baseline'); git(self.repo, 'tag', 'v1')
        self.write('modules/changed.adoc', '= Changed\n\nUpdated text.\n')
        self.write('artifacts/attributes.adoc', ':product: Two\n')
        self.write('assemblies/assembly-guide.adoc', '= Assembly guide\n\nA changed guide introduction.\n\ninclude::../modules/changed.adoc[]\n')
        self.write('modules/con-generated.template.adoc', '= Template\n\n{generated-value} changed\n')
        self.write('snippets/shared.adoc', 'Snippet two.\n')
        (self.repo/'modules/deleted.adoc').unlink()
        (self.repo/'modules/rename.adoc').rename(self.repo/'modules/renamed.adoc')
        self.write('modules/added.adoc', '= Added\n\n' + '\n'.join('Brand new reference entry '+str(i) for i in range(20)) + '\n')
        self.commit('target'); git(self.repo, 'tag', 'v2')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, text):
        p = self.repo/path; p.parent.mkdir(parents=True, exist_ok=True)
        if path.startswith('modules/'):
            text = ':_mod-docs-content-type: CONCEPT\n\n' + text
        p.write_text(text)

    def commit(self, name):
        git(self.repo, 'add', '.'); git(self.repo, 'commit', '-m', name)

    def test_release_workflow_and_deterministic_bundle(self):
        before = git(self.repo, 'status', '--porcelain')
        report = compare(self.repo, 'v1', 'v2', patterns=['modules/*.adoc'])
        self.assertEqual(report['summary']['errors'], 0, report)
        by_change = {item['change']: item for item in report['files']}
        for change in ['added', 'deleted', 'renamed', 'modified', 'dependency']:
            self.assertIn(change, by_change)
        dependency = by_change['dependency']
        self.assertIn('Two', dependency['after']['xml'])
        self.assertIn('Snippet two.', dependency['after']['xml'])
        self.assertEqual(dependency['source_diff']['patch'], '')
        self.assertIn('snippets/shared.adoc', dependency['affected_dependencies'])
        self.assertNotIn('modules/untouched.adoc', [i['after_path'] for i in report['files']])
        self.assertEqual(by_change['renamed']['before']['xml'], by_change['renamed']['after']['xml'])
        self.assertEqual(before, git(self.repo, 'status', '--porcelain'))
        repeat = compare(self.repo, 'v1', 'v2', patterns=['modules/*.adoc'])
        self.assertEqual(report, repeat)
        self.assertEqual(zip_report(report), zip_report(repeat))
        with zipfile.ZipFile(io.BytesIO(zip_report(report))) as archive:
            self.assertIn('after/modules/added.xml', archive.namelist())
            self.assertIn('before/modules/deleted.xml', archive.namelist())
            self.assertIn('report.html', archive.namelist())
            self.assertIn('diffs/README.txt', archive.namelist())
            added_xml_diff = archive.read('diffs/modules/added.adoc.xml.diff').decode()
            self.assertIn('--- /dev/null', added_xml_diff)
            self.assertIn('+++ modules/added.xml', added_xml_diff)
            self.assertIn('<concept', added_xml_diff)
            self.assertTrue(all(archive.getinfo(name).file_size > 0 for name in archive.namelist()))

    def test_invalid_target_has_no_fake_deletion_diff(self):
        self.write('modules/changed.adoc', '= Broken\n\n{undefined}\n')
        self.commit('invalid')
        report = compare(self.repo, 'v2', 'HEAD', patterns=['modules/*.adoc'])
        item = next(i for i in report['files'] if i['after_path']=='modules/changed.adoc')
        self.assertEqual(item['after']['status'], 'error')
        self.assertTrue(item['xml_diff']['unavailable'])
        self.assertIsNone(item['xml_changed'])
        with zipfile.ZipFile(io.BytesIO(zip_report(report))) as archive:
            names = archive.namelist()
            self.assertNotIn('diffs/modules/changed.adoc.xml.diff', names)
            unavailable = archive.read('diffs/modules/changed.adoc.xml.unavailable.txt').decode()
            self.assertIn('Target conversion: error', unavailable)
            self.assertIn('No XML deletion or addition is implied.', unavailable)

    def test_empty_comparison_and_bad_refs(self):
        report = compare(self.repo, 'v1', 'v1')
        self.assertEqual(report['summary']['files'], 0)
        with self.assertRaises(ValueError):
            compare(self.repo, '--help', 'v2')
        self.assertIn('v1', refs(self.repo))

    def test_comparison_converts_only_changed_and_dependency_affected_topics(self):
        for number in range(15):
            self.write(f'modules/stable-{number}.adoc', f'= Stable {number}\n\nUnchanged.\n')
        self.commit('many stable topics')
        git(self.repo, 'tag', 'many-stable')
        self.write('modules/changed.adoc', '= Changed\n\nOnly this topic changed again.\n')
        self.commit('one topic changed')
        progress = []
        report = compare(self.repo, 'many-stable', 'HEAD', progress=progress.append)
        self.assertEqual(report['summary']['files'], 1)
        self.assertTrue(any(message == 'Converting 1 baseline topics and checking dependencies…'
                            for message in progress), progress)
        self.assertTrue(any(message == 'Converting 1 target topics and checking dependencies…'
                            for message in progress), progress)

    def test_dependency_mapping_resolves_include_path_attributes(self):
        self.write('artifacts/attributes.adoc', ':product: Two\n:snippet-dir: snippets\n')
        self.write('modules/dependent.adoc', '= Dependent\n\ninclude::{snippet-dir}/shared.adoc[]\n')
        self.commit('attribute include baseline')
        git(self.repo, 'tag', 'attribute-include-base')
        self.write('snippets/shared.adoc', 'Changed through an attribute-expanded include.\n')
        self.commit('attribute include target')
        progress = []
        report = compare(self.repo, 'attribute-include-base', 'HEAD', progress=progress.append)
        self.assertEqual(report['summary']['files'], 1, report)
        self.assertEqual(report['files'][0]['change'], 'dependency')
        self.assertIn('snippets/shared.adoc', report['files'][0]['affected_dependencies'])
        self.assertTrue(any(message == 'Converting 1 target topics and checking dependencies…'
                            for message in progress), progress)

    def test_pull_request_url_metadata_and_exact_snapshot_routing(self):
        self.assertEqual(pull_request_location('https://github.com/example/docs/pull/42'),
                         ('example', 'docs', 42))
        self.assertEqual(pull_request_location('https://github.com/example/docs/pull/42/files?diff=split#top'),
                         ('example', 'docs', 42))
        for invalid in ['http://github.com/example/docs/pull/42',
                        'https://github.com/example/docs/pulls/42',
                        'https://github.com/example/docs/pull/42/unrelated',
                        'https://github.com/example/docs/pull/0',
                        'https://example.com/example/docs/pull/42']:
            with self.assertRaises(ValueError):
                pull_request_location(invalid)

        payload = {'title': 'Update docs', 'state': 'open',
                   'base': {'ref': 'main', 'sha': 'a' * 40,
                            'repo': {'full_name': 'example/docs'}},
                   'head': {'label': 'writer:update', 'sha': 'b' * 40}}
        comparison = {'merge_base_commit': {'sha': 'c' * 40}}

        class Response:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(self.value).encode()

        with patch('adoc_dita.repository.urlopen', side_effect=[Response(payload), Response(comparison)]):
            metadata = pull_request_metadata('https://github.com/example/docs/pull/42')
        self.assertEqual(metadata['target']['ref'], 'refs/pull/42/head')
        self.assertEqual(metadata['repository'], 'https://github.com/example/docs')
        self.assertEqual(metadata['base']['commit'], 'c' * 40)
        self.assertEqual(metadata['base']['tip'], 'a' * 40)
        self.assertEqual(metadata['provider'], 'github')

        converted = {'base': {'ref': 'main', 'commit': 'c' * 40},
                     'target': {'ref': 'refs/pull/42/head', 'commit': 'b' * 40},
                     'settings': {}, 'summary': {'files': 1, 'errors': 0,
                                                'review': 0, 'xml_changed': 1},
                     'files': [], 'changes': []}
        with patch('adoc_dita.repository.pull_request_metadata', return_value=metadata), \
                patch('adoc_dita.repository.compare', return_value=converted) as routed:
            report = compare_pull_request(metadata['url'], patterns=['modules/*.adoc'])
        routed.assert_called_once()
        self.assertEqual(routed.call_args.args[:3],
                         (metadata['repository'], 'c' * 40, 'b' * 40))
        self.assertEqual(report['target']['ref'], 'PR #42')
        self.assertEqual(report['settings']['workflow'], 'pull_request')

    def test_gitlab_merge_request_url_metadata_and_exact_snapshot_routing(self):
        location = pull_request_location(
            'https://gitlab.com/docs/guides/platform/-/merge_requests/7/diffs')
        self.assertEqual(location['provider'], 'gitlab')
        self.assertEqual(location['project_path'], 'docs/guides/platform')
        self.assertEqual(location['number'], 7)
        self.assertEqual(location['head_ref'], 'refs/merge-requests/7/head')
        self.assertEqual(
            pull_request_location('https://gitlab.cee.redhat.com/group/project/-/merge_requests/3')['host'],
            'gitlab.cee.redhat.com')
        for invalid in ['http://gitlab.com/group/project/-/merge_requests/7',
                        'https://gitlab.com/group/project/merge_requests/7',
                        'https://gitlab.com/group/project/-/merge_requests/0',
                        'https://gitlab.com/group/project/-/issues/7']:
            with self.assertRaises(ValueError):
                pull_request_location(invalid)

        payload = {
            'title': 'Update GitLab docs',
            'state': 'opened',
            'target_branch': 'main',
            'source_branch': 'writer/update',
            'sha': 'b' * 40,
            'web_url': 'https://gitlab.com/docs/guides/platform/-/merge_requests/7',
            'references': {'full': 'docs/guides/platform!7'},
            'diff_refs': {'base_sha': 'c' * 40, 'head_sha': 'b' * 40, 'start_sha': 'a' * 40},
        }
        branch = {'commit': {'id': 'd' * 40}}

        class Response:
            def __init__(self, value):
                self.value = value

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return json.dumps(self.value).encode()

        with patch('adoc_dita.repository.urlopen', side_effect=[Response(payload), Response(branch)]):
            metadata = pull_request_metadata(
                'https://gitlab.com/docs/guides/platform/-/merge_requests/7')
        self.assertEqual(metadata['provider'], 'gitlab')
        self.assertEqual(metadata['target']['ref'], 'refs/merge-requests/7/head')
        self.assertEqual(metadata['repository'], 'https://gitlab.com/docs/guides/platform')
        self.assertEqual(metadata['base']['commit'], 'c' * 40)
        self.assertEqual(metadata['base']['tip'], 'd' * 40)
        self.assertEqual(metadata['base']['ref'], 'main')

        converted = {'base': {'ref': 'main', 'commit': 'c' * 40},
                     'target': {'ref': 'refs/merge-requests/7/head', 'commit': 'b' * 40},
                     'settings': {}, 'summary': {'files': 1, 'errors': 0,
                                                'review': 0, 'xml_changed': 1},
                     'files': [], 'changes': []}
        with patch('adoc_dita.repository.pull_request_metadata', return_value=metadata), \
                patch('adoc_dita.repository.compare', return_value=converted) as routed:
            report = compare_pull_request(metadata['url'], patterns=['modules/*.adoc'])
        routed.assert_called_once()
        self.assertEqual(routed.call_args.args[:3],
                         (metadata['repository'], 'c' * 40, 'b' * 40))
        self.assertEqual(report['target']['ref'], 'PR #7')
        self.assertEqual(report['settings']['workflow'], 'pull_request')

    def test_remote_url_accepts_github_and_gitlab(self):
        from adoc_dita.repository import remote_url
        self.assertEqual(remote_url('https://github.com/owner/docs'),
                         'https://github.com/owner/docs.git')
        self.assertEqual(remote_url('https://gitlab.com/group/sub/project.git'),
                         'https://gitlab.com/group/sub/project.git')
        self.assertEqual(remote_url('https://gitlab.cee.redhat.com/ccs/docs'),
                         'https://gitlab.cee.redhat.com/ccs/docs.git')
        with self.assertRaises(ValueError):
            remote_url('https://github.com/owner/group/docs')
        with self.assertRaises(ValueError):
            remote_url('https://gitlab.com/group/project/-/tree/main')
        with self.assertRaises(ValueError):
            remote_url('https://bitbucket.org/owner/docs')

    def test_automatic_scope_lists_but_does_not_convert_assemblies(self):
        report = compare(self.repo, 'v1', 'v2')
        converted = {item['after_path'] or item['before_path'] for item in report['files']}
        self.assertNotIn('assemblies/assembly-guide.adoc', converted)
        self.assertNotIn('modules/con-generated.template.adoc', converted)
        assembly = next(change for change in report['changes'] if change['after'] == 'assemblies/assembly-guide.adoc')
        self.assertEqual(assembly['conversion'], 'dependency-only or no document title')

    def test_uploaded_attributes_apply_to_both_snapshots(self):
        uploaded = ':product: Uploaded product\n'
        report = compare(self.repo, 'v1', 'v2', patterns=['modules/dependent.adoc'],
                         attribute_text=uploaded, attribute_filename='attributes.adoc')
        item = report['files'][0]
        self.assertIn('Uploaded product', item['before']['xml'])
        self.assertIn('Uploaded product', item['after']['xml'])
        self.assertNotIn('>One<', item['before']['xml'])
        self.assertNotIn('>Two<', item['after']['xml'])
        self.assertEqual(report['settings']['uploaded_attribute_file'], 'attributes.adoc')
        self.assertEqual(report['settings']['uploaded_attribute_sha256'], hashlib.sha256(uploaded.encode()).hexdigest())
        self.assertNotIn(uploaded, json.dumps(report))
        self.assertNotIn('.adoc-dita-uploaded-attributes-', json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'cannot include other files'):
            compare(self.repo, 'v1', 'v2', patterns=['modules/dependent.adoc'],
                    attribute_text='include::other.adoc[]\n', attribute_filename='attributes.adoc')

    def test_remote_cache_recovers_an_interrupted_git_fetch(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            url = 'https://github.com/example/documentation'
            normalized_url = url + '.git'
            repository = cache / hashlib.sha256(normalized_url.encode()).hexdigest()[:24]
            repository.mkdir()
            (repository / 'HEAD').write_text('ref: refs/heads/main\n')
            stale = repository / 'shallow.lock'
            stale.write_text('interrupted fetch')
            base_sha, target_sha = 'a' * 40, 'b' * 40
            fetched = set()
            fetch_count = 0

            def fake_git(repo, *args, **_kwargs):
                nonlocal fetch_count
                if args[0] == 'ls-remote':
                    return (f'{base_sha}\trefs/heads/base\n'
                            f'{target_sha}\trefs/heads/target\n').encode()
                if args[0] == 'fetch':
                    self.assertFalse(stale.exists())
                    fetched.add(args[-1])
                    fetch_count += 1
                    return b''
                if args[0] == 'cat-file':
                    sha = args[-1].removesuffix('^{commit}')
                    if sha not in fetched:
                        raise ValueError('missing object')
                    return b''
                if args[0] == 'rev-parse':
                    return (args[-1].removesuffix('^{commit}') + '\n').encode()
                self.fail(args)

            with patch('adoc_dita.repository.git', side_effect=fake_git):
                resolved = prepare_repository(url, 'base', 'target', cache=cache)
            self.assertEqual(resolved, (repository, base_sha, target_sha))
            self.assertFalse(stale.exists())
            self.assertEqual(fetch_count, 2)
            with patch('adoc_dita.repository.git', side_effect=fake_git):
                self.assertEqual(prepare_repository(url, 'base', 'target', cache=cache), resolved)
            self.assertEqual(fetch_count, 2)

    def test_removed_title_is_an_error_not_a_deletion(self):
        self.write('modules/changed.adoc', 'The document title was removed.\n')
        self.commit('removed title')
        report = compare(self.repo, 'v2', 'HEAD', patterns=['modules/*.adoc'])
        item = next(i for i in report['files'] if i['after_path']=='modules/changed.adoc')
        self.assertEqual(item['change'], 'modified')
        self.assertEqual(item['after']['status'], 'error')
        self.assertTrue(item['xml_diff']['unavailable'])

    def test_source_and_xml_ranges(self):
        diff = file_diff('one\ntwo\n', 'one\nnew\ntwo\n', 'a.adoc', 'b.adoc')
        hunk = diff['hunks'][0]
        self.assertEqual(hunk['before']['count'], 0)
        self.assertIsNone(hunk['before']['start'])
        self.assertEqual(hunk['after']['start'], 2)
        self.assertEqual(hunk['after']['end'], 2)


if __name__ == '__main__':
    unittest.main()
