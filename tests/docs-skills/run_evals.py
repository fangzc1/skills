"""只读仓库评估：默认展示计划；--run 才调用模型，产物与分组键分开保存。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
CASE_FILE = Path(__file__).with_name('cases.json')
SKILLS = ['docs-' + name for name in (
    'agents', 'api', 'architecture', 'checks', 'contributing', 'security', 'test', 'ui-design')]
BASE = '你是中文编程助手。请根据当前工作目录的仓库证据完成用户任务。本次只读，最终答复给出文档草案或核对报告，不写文件，不执行命令。'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_cases(path=CASE_FILE):
    cases = json.loads(path.read_text())
    ids = set()
    for case in cases:
        if case['id'] in ids or not case['rubric']:
            raise ValueError('场景重复或缺少预定义判据')
        ids.add(case['id'])
        for name in case['files']:
            p = Path(name)
            if p.is_absolute() or '..' in p.parts:
                raise ValueError('fixture路径越界')
    return cases


def parse_events(raw):
    outputs, calls, usage, errors = [], [], [], []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') != 'message_end':
            continue
        msg = event.get('message', {})
        if msg.get('role') != 'assistant':
            continue
        if msg.get('stopReason') in ('error', 'aborted'):
            errors.append(msg.get('errorMessage') or msg['stopReason'])
        if msg.get('usage'):
            usage.append(msg['usage'])
        for part in msg.get('content', []):
            if part.get('type') == 'text':
                outputs.append(part.get('text', ''))
            elif part.get('type') == 'toolCall':
                calls.append({'name': part.get('name'), 'arguments': part.get('arguments', {})})
    # 各字段独立保留；不把reasoning再加到output，避免供应商统计口径重叠。
    totals = {key: sum(u.get(key, 0) for u in usage)
              for key in ('input', 'output', 'reasoning', 'cacheRead', 'cacheWrite', 'totalTokens')}
    return {'output': '\n'.join(outputs), 'tool_calls': calls, 'usage': usage,
            'tokens': totals, 'model_errors': errors}


def build_command(args, skill_root, prompt):
    cmd = ['pi', '--print', '--mode', 'json', '--no-session', '--no-extensions',
           '--no-mcp', '--no-skills', '--no-context-files', '--no-prompt-templates',
           '--no-themes', '--tools', 'read,grep,find,ls', '--provider', args.provider,
           '--model', args.model, '--thinking', args.thinking,
           '--system-prompt', BASE]
    if skill_root:
        # 使用原生description发现与read加载机制，而非强制注入全文。
        for name in SKILLS:
            cmd += ['--skill', str(skill_root / name / 'SKILL.md')]
    return cmd + [prompt]


def tree_hashes(root):
    return {str(p.relative_to(root)): digest(p.read_bytes())
            for p in root.rglob('*') if p.is_file()}


def execute(args, case, skill_root):
    with tempfile.TemporaryDirectory(prefix='docs-eval-') as directory:
        work = Path(directory)
        for name, content in case['files'].items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        before = tree_hashes(work)
        started = time.monotonic()
        try:
            proc = subprocess.run(build_command(args, skill_root, case['prompt']),
                                  cwd=work, capture_output=True, text=True, timeout=args.timeout)
            raw, stderr, code = proc.stdout, proc.stderr, proc.returncode
            error = None
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or b''
            stderr = exc.stderr or b''
            raw = raw.decode(errors='replace') if isinstance(raw, bytes) else raw
            stderr = stderr.decode(errors='replace') if isinstance(stderr, bytes) else stderr
            code, error = None, 'timeout'
        result = parse_events(raw)
        result.update(exit_code=code, error=error, seconds=round(time.monotonic()-started, 3),
                      stderr=stderr, raw_events=raw, fixture_unchanged=before == tree_hashes(work))
        result['status'] = ('completed' if code == 0 and result['output'].strip()
                            and not result['model_errors'] and result['fixture_unchanged'] else 'invalid')
        return result


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='显式授权实际模型调用')
    parser.add_argument('--output', type=Path, help='新的结果目录，拒绝覆盖')
    parser.add_argument('--case', action='append', dest='selected')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--max-calls', type=int, default=60)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--timeout', type=int, default=150)
    parser.add_argument('--provider', default=os.getenv('PI_PROVIDER'))
    parser.add_argument('--model', default=os.getenv('PI_MODEL'))
    parser.add_argument('--thinking', default=os.getenv('PI_REASONING_LEVEL', 'medium'))
    parser.add_argument('--old-skills', type=Path, help='经确认的修改前skills快照，不从HEAD推断')
    args = parser.parse_args(argv)
    cases = load_cases()
    if args.selected:
        unknown = set(args.selected) - {c['id'] for c in cases}
        if unknown:
            parser.error(f'未知场景：{sorted(unknown)}')
        cases = [c for c in cases if c['id'] in args.selected]
    if args.repeats < 1 or args.timeout < 1:
        parser.error('重复次数和超时必须大于0')
    variants = {'without_skill': None, 'current': ROOT / 'skills'}
    if args.old_skills:
        variants['old'] = args.old_skills.resolve()
    snapshots = {}
    for variant, root in variants.items():
        if root:
            snapshots[variant] = {name: (root / name / 'SKILL.md').read_text() for name in SKILLS}
    jobs = [(case, variant, repeat) for case in cases for variant in variants
            for repeat in range(args.repeats)]
    random.Random(args.seed).shuffle(jobs)
    plan = {'cases': [c['id'] for c in cases], 'variants': list(variants),
            'calls': len(jobs), 'repeats': args.repeats, 'seed': args.seed,
            'provider': args.provider, 'model': args.model, 'thinking': args.thinking}
    print(json.dumps(plan, ensure_ascii=False))
    if not args.run:
        return 0
    if len(jobs) > args.max_calls:
        parser.error('调用数超过--max-calls；请显式调整预算')
    if not args.output or not args.provider or not args.model:
        parser.error('--run要求--output、provider和model')
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    blind = destination / 'blind'
    private = destination / 'private'
    blind.mkdir()
    private.mkdir()
    # 调用前固化判据与技能快照，使后续修改可检测，不把答案标准放进模型工作目录。
    save_json(private / 'manifest.json', {'plan': plan, 'cases': cases, 'skills': snapshots,
               'runner_sha256': digest(Path(__file__).read_bytes()),
               'pi_version': subprocess.run(['pi', '--version'], capture_output=True, text=True).stdout.strip()})
    mapping, invalid = [], 0
    for index, (case, variant, repeat) in enumerate(jobs):
        label = f'sample-{index+1:03d}'
        result = execute(args, case, variants[variant])
        invalid += result['status'] != 'completed'
        save_json(private / f'{label}.json', result)
        # 分组与成本只存在private中；产物可能自述技能，盲评并非绝对不可识别。
        save_json(blind / f'{label}.json', {'case': case['id'], 'prompt': case['prompt'],
                  'rubric': case['rubric'], 'output': result['output'],
                  'review': [{'criterion': r, 'passed': None, 'evidence': ''} for r in case['rubric']]})
        mapping.append({'sample': label, 'case': case['id'], 'variant': variant,
                        'repeat': repeat, 'status': result['status']})
        save_json(private / 'mapping.json', mapping)
        print(f'{label}: {result["status"]}', flush=True)
    return 1 if invalid else 0


if __name__ == '__main__':
    raise SystemExit(main())
