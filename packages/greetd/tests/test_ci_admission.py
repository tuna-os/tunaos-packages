"""Synthetic fresh-API admission negatives; no run/binary qualification."""
import base64
import copy
import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ci_admission', Path(__file__).with_name('ci_admission.py'))
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)
builder_spec = importlib.util.spec_from_file_location('build_native_candidate', Path(__file__).with_name('build_native_candidate.py'))
builder = importlib.util.module_from_spec(builder_spec)
builder_spec.loader.exec_module(builder)


class SyntheticAdmission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.head = 'a' * 40
        self.tree = 'b' * 40
        self.environment = {
            'GITHUB_SHA': self.head, 'GITHUB_RUN_ID': '11', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_REF': 'refs/heads/owned', 'GITHUB_REPOSITORY': admission.REPO,
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_JOB': 'greetd-retirement',
            'GITHUB_WORKFLOW_REF': admission.REPO + '/' + admission.WORKFLOW + '@refs/heads/owned',
            'GITHUB_WORKSPACE': str(self.root)}
        self.run = {'id': 11, 'run_attempt': 1, 'head_sha': self.head, 'head_branch': 'owned',
                    'path': admission.WORKFLOW, 'event': 'workflow_dispatch', 'status': 'in_progress',
                    'conclusion': None, 'repository': {'full_name': admission.REPO},
                    'head_repository': {'full_name': admission.REPO}}
        self.jobs = {'total_count': 2, 'jobs': [
            {'id': 12, 'name': 'greetd-retirement-x86_64', 'run_id': 11, 'run_attempt': 1,
             'head_sha': self.head, 'status': 'in_progress', 'conclusion': None},
            {'id': 13, 'name': 'validate', 'run_id': 11, 'run_attempt': 1,
             'head_sha': self.head, 'status': 'completed', 'conclusion': 'success'}]}
        self.blobs = {}
        for name in admission.SOURCES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = b'explicitly synthetic executing source\n'
            path.write_bytes(data)
            self.blobs[name] = {'type': 'file', 'path': name, 'encoding': 'base64', 'size': len(data),
                                'sha': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest(),
                                'content': base64.b64encode(data).decode()}

    def api(self, path):
        if path == "actions/runs/11":
            return copy.deepcopy(self.run)
        if path == "actions/runs/11/attempts/1/jobs?per_page=100":
            return copy.deepcopy(self.jobs)
        if path == "git/commits/" + self.head:
            return {"sha": self.head, "tree": {"sha": self.tree}}
        for name in admission.SOURCES:
            if path == 'contents/' + name + '?ref=' + self.head:
                return copy.deepcopy(self.blobs[name])
        raise AssertionError('unexpected synthetic API path')

    def git(self, root, *args):
        if args == ("rev-parse", "HEAD"):
            return self.head
        if args == ("rev-parse", "HEAD^{tree}"):
            return self.tree
        if args == ("status", "--porcelain", "--untracked-files=all"):
            return ""
        raise AssertionError('unexpected synthetic Git command')

    def invoke(self):
        with patch.dict(os.environ, self.environment, clear=True), patch.object(admission, 'api', self.api), \
                patch.object(admission, 'git', self.git), patch.object(admission.platform, 'machine', return_value='x86_64'):
            return admission.current(self.root, 'x86_64')

    def test_exact_synthetic_tuple_is_candidate_only(self):
        result = self.invoke()
        self.assertIs(result['candidate_only'], True)
        self.assertIs(result['release_qualified'], False)
        self.assertIs(result['publication'], False)
        self.assertEqual(result['source_commit'], self.head)

    def test_current_run_queued_but_actual_job_active_is_admitted(self):
        self.run['status'] = 'queued'
        self.assertEqual(self.invoke()['job_id'], 12)

    def test_run_source_event_attempt_repository_and_terminal_drift(self):
        mutations = [('head_sha', 'c' * 40), ('head_branch', 'foreign'), ('event', 'push'),
                     ('run_attempt', 2), ('run_attempt', True), ('id', True),
                     ('status', 'completed'), ('conclusion', 'failure'), ('path', 'foreign.yml'),
                     ('repository', {'full_name': 'foreign/repo'}),
                     ('head_repository', {'full_name': 'foreign/repo'})]
        original = copy.deepcopy(self.run)
        for key, value in mutations:
            self.run = copy.deepcopy(original)
            self.run[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.invoke()

    def test_env_ref_head_job_event_and_attempt_drift(self):
        original = self.environment.copy()
        for key, value in [('GITHUB_JOB', 'foreign'), ('GITHUB_EVENT_NAME', 'pull_request'),
                           ('GITHUB_SHA', 'c' * 40), ('GITHUB_RUN_ATTEMPT', '0'),
                           ('GITHUB_REF', 'refs/tags/owned'), ('GITHUB_WORKFLOW_REF', 'foreign')]:
            self.environment = original.copy()
            self.environment[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.invoke()

    def test_missing_duplicate_foreign_stopped_job_and_bad_prerequisite(self):
        original = copy.deepcopy(self.jobs)
        variants = []
        changed = copy.deepcopy(original)
        changed["total_count"] = 3
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed["jobs"][0]["name"] = "foreign"
        variants.append(changed)
        for key, value in [('status', 'queued'), ('conclusion', 'failure'), ('head_sha', 'c' * 40), ('run_attempt', True)]:
            changed = copy.deepcopy(original)
            changed["jobs"][0][key] = value
            variants.append(changed)
        changed = copy.deepcopy(original)
        changed["jobs"][1]["conclusion"] = "failure"
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed["jobs"].append(copy.deepcopy(changed["jobs"][0]))
        changed["total_count"] = 3
        variants.append(changed)
        for index, changed in enumerate(variants):
            self.jobs = changed
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.invoke()

    def test_remote_blob_byte_hash_encoding_or_path_drift(self):
        name = admission.SOURCES[0]
        original = copy.deepcopy(self.blobs[name])
        for key, value in [('content', base64.b64encode(b'foreign').decode()), ('sha', 'c' * 40),
                           ('encoding', 'foreign'), ('path', 'foreign'), ('size', 0), ('type', 'symlink')]:
            self.blobs[name] = copy.deepcopy(original)
            self.blobs[name][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.invoke()

    def test_local_source_symlink_refused(self):
        name = admission.SOURCES[0]
        path = self.root / name
        data = path.read_bytes()
        path.unlink()
        foreign = self.root / "foreign"
        foreign.write_bytes(data)
        path.symlink_to(foreign)
        with self.assertRaises(ValueError):
            self.invoke()

    def test_actual_architecture_must_match_matrix(self):
        with patch.object(admission.platform, 'machine', return_value='aarch64'):
            with self.assertRaises(ValueError):
                admission.current(self.root, "x86_64")

    def test_admission_keeps_token_but_child_environment_drops_it(self):
        def admit(root, architecture):
            self.assertEqual(os.environ.get('GH_TOKEN'), 'read-only-token')
            self.assertEqual((root, architecture), (self.root, 'x86_64'))
            return {'source_commit': self.head}

        with patch.dict(os.environ, {'GH_TOKEN': 'read-only-token'}, clear=True), \
                patch.object(builder.ci_admission, 'current', side_effect=admit):
            receipt, child_env = builder.admitted_child_environment(self.root, 'x86_64')

        self.assertEqual(receipt, {'source_commit': self.head})
        self.assertNotIn('GH_TOKEN', child_env)


if __name__ == "__main__":
    unittest.main()
