"""只验证评估机制，不调用付费模型。"""
import argparse
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('runner', Path(__file__).with_name('run_evals.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_cases_cover_eight_skills_and_negatives(self):
        cases = runner.load_cases()
        self.assertEqual({c['skill'] for c in cases if c['skill']}, set(runner.SKILLS))
        self.assertEqual(sum(c['skill'] is None for c in cases), 2)

    def test_native_discovery_and_same_base_prompt(self):
        args = argparse.Namespace(provider='test', model='test', thinking='medium')
        baseline = runner.build_command(args, None, '任务')
        current = runner.build_command(args, runner.ROOT / 'skills', '任务')
        self.assertNotIn('--skill', baseline)
        self.assertEqual(current.count('--skill'), 8)
        for command in (baseline, current):
            self.assertEqual(command[command.index('--system-prompt')+1], runner.BASE)
            self.assertEqual(command[command.index('--tools')+1], 'read,grep,find,ls')
            self.assertNotIn('--no-tools', command)

    def test_dry_run_never_calls_model(self):
        with patch.object(runner.subprocess, 'run') as call, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main([]), 0)
            self.assertIn('"calls": 60', output.getvalue())
            call.assert_not_called()

    def test_error_event_and_usage_preserved(self):
        raw = '{"type":"message_end","message":{"role":"assistant","stopReason":"error","errorMessage":"failed","usage":{"input":10,"output":4,"reasoning":2,"totalTokens":14},"content":[{"type":"toolCall","name":"read","arguments":{"path":"src/a.ts"}}]}}'
        result = runner.parse_events(raw)
        self.assertEqual(result['model_errors'], ['failed'])
        self.assertEqual(result['tokens']['totalTokens'], 14)
        self.assertEqual(result['tokens']['reasoning'], 2)
        self.assertEqual(result['tool_calls'][0]['arguments']['path'], 'src/a.ts')

    def test_empty_success_exit_is_invalid(self):
        args = argparse.Namespace(provider='test', model='test', thinking='medium', timeout=1)
        fake = argparse.Namespace(stdout='', stderr='', returncode=0)
        with patch.object(runner.subprocess, 'run', return_value=fake):
            result = runner.execute(args, runner.load_cases()[0], None)
        self.assertEqual(result['status'], 'invalid')
        self.assertTrue(result['fixture_unchanged'])

    def test_timeout_is_invalid(self):
        args = argparse.Namespace(provider='test', model='test', thinking='medium', timeout=1)
        with patch.object(runner.subprocess, 'run', side_effect=runner.subprocess.TimeoutExpired('pi', 1)):
            result = runner.execute(args, runner.load_cases()[0], None)
        self.assertEqual(result['status'], 'invalid')
        self.assertEqual(result['error'], 'timeout')

    def test_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cases.json'
            path.write_text('[{"id":"x","files":{"../escape":"x"},"rubric":["x"]}]')
            with self.assertRaises(ValueError):
                runner.load_cases(path)


if __name__ == '__main__':
    unittest.main()
